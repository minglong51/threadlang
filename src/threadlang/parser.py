"""Deterministic parser for ThreadLang v1.

Grammar (informal):

    program     = "thread" name "{" context [ steps ] emit "}"
    context     = "context" "{" { name "=" string } "}"
    steps       = "steps" "{" { step } "}"
    step        = "step" name "{" "llm" string "{" expression "}" "}"
    emit_text   = "emit" "text" "{" expression "}"
    emit_llm    = "emit" "llm" string "{" expression "}"
    expression  = term { "+" term }
    term        = string | "context." name | "inputs." name | "steps." name ".output"

`steps` is optional. `emit` is required and is either `emit text` or
`emit llm`. Whitespace is insignificant.

This stays regex-based on purpose: the grammar is small enough that a
parser-generator dependency would be a cost without a benefit. When the
grammar grows (rules block, control flow, multiple emits) the right move
is a hand-written recursive-descent parser, not a tool.
"""

from __future__ import annotations

import re
from typing import List

from .ast import (
    AgentStep,
    ContextAssignment,
    ContextBlock,
    ContextRef,
    EmitBlock,
    Expression,
    InputsRef,
    Program,
    Step,
    StepNode,
    StepsBlock,
    StepsRef,
    StringLiteral,
)

DEFAULT_MAX_ITERS = 6


class ParseError(ValueError):
    """Raised when source text does not match the supported ThreadLang syntax."""


_THREAD_RE = re.compile(
    r"\s*thread\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{(.*)\}\s*", re.DOTALL
)
_CONTEXT_RE = re.compile(r"context\s*\{([^{}]*)\}", re.DOTALL)
# A step now carries either an `llm` body or an `agent` body, and an agent body
# nests deeper than the old two-level regex could track. The steps block is
# located by keyword and carved out with a brace-balanced scan (see
# `_extract_braced`); each `step <name> { ... }` is then carved the same way and
# dispatched by body kind. This preserves declaration order across mixed step
# kinds — which a per-kind regex sweep could not.
_STEPS_HEAD_RE = re.compile(r"\bsteps\s*\{")
_STEP_HEAD_RE = re.compile(r"\bstep\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_LLM_BODY_RE = re.compile(r'\s*llm\s+"([^"]+)"\s*\{(.*)\}\s*', re.DOTALL)
_AGENT_BODY_RE = re.compile(r'\s*agent\s+"([^"]+)"\s*\{(.*)\}\s*', re.DOTALL)
_TOOLS_RE = re.compile(r"\btools\s*\[([^\]]*)\]")
_MAX_ITERS_RE = re.compile(r"\bmax_iters\s+(\d+)")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EMIT_TEXT_RE = re.compile(r"emit\s+text\s*\{([^{}]*)\}", re.DOTALL)
_EMIT_LLM_RE = re.compile(r"emit\s+llm\s+\"([^\"]+)\"\s*\{([^{}]*)\}", re.DOTALL)
_CONTEXT_ASSIGN_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"')
_STEPS_REF_RE = re.compile(r"steps\.([A-Za-z_][A-Za-z0-9_]*)\.output")


def parse_program(source: str) -> Program:
    match = _THREAD_RE.fullmatch(source)
    if not match:
        raise ParseError("Expected: thread <Name> { ... }")

    thread_name = match.group(1)
    body = match.group(2)

    context_block = _parse_context_block(body)
    steps_block = _parse_steps_block(body)
    emit_block = _parse_emit_block(body)

    return Program(
        thread_name=thread_name,
        context=context_block,
        steps=steps_block,
        emit=emit_block,
    )


def _parse_context_block(body: str) -> ContextBlock:
    match = _CONTEXT_RE.search(body)
    if not match:
        raise ParseError("Missing context block")

    assignments: List[ContextAssignment] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        assign_match = _CONTEXT_ASSIGN_RE.fullmatch(stripped)
        if not assign_match:
            raise ParseError(f"Invalid context assignment: {stripped}")
        assignments.append(
            ContextAssignment(name=assign_match.group(1), value=assign_match.group(2))
        )

    return ContextBlock(assignments=assignments)


def _extract_braced(text: str, brace_index: int) -> tuple[str, int]:
    """Given the index of an opening `{`, return (inner_text, close_index) for
    the matching `}`. Raises if the braces never balance."""
    depth = 0
    for i in range(brace_index, len(text)):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace_index + 1 : i], i
    raise ParseError("Unbalanced braces in steps block")


def _parse_steps_block(body: str) -> StepsBlock:
    head = _STEPS_HEAD_RE.search(body)
    if not head:
        return StepsBlock(steps=[])

    steps_body, _ = _extract_braced(body, head.end() - 1)

    steps: List[StepNode] = []
    seen_names: set[str] = set()
    pos = 0
    while True:
        step_head = _STEP_HEAD_RE.search(steps_body, pos)
        if not step_head:
            break
        name = step_head.group(1)
        step_body, close_index = _extract_braced(steps_body, step_head.end() - 1)
        if name in seen_names:
            raise ParseError(f"Duplicate step name: {name}")
        seen_names.add(name)
        steps.append(_parse_step_body(name, step_body))
        pos = close_index + 1

    # If the steps block exists but no step parsed, the user wrote something
    # unsupported; fail loud rather than silently dropping their code.
    if not steps and steps_body.strip():
        raise ParseError(
            "steps block present but no valid step found. "
            'Expected: step <name> { llm "<model>" { <expression> } } '
            'or step <name> { agent "<model>" { ... } }'
        )

    return StepsBlock(steps=steps)


def _join_expression_text(raw: str) -> str:
    return " ".join(line.strip() for line in raw.splitlines() if line.strip()).strip()


def _parse_step_body(name: str, step_body: str) -> StepNode:
    llm_match = _LLM_BODY_RE.fullmatch(step_body)
    if llm_match:
        model = llm_match.group(1)
        expression_text = _join_expression_text(llm_match.group(2))
        if not expression_text:
            raise ParseError(f"step '{name}': llm body must include a prompt expression")
        return Step(name=name, model=model, prompt=_parse_expression(expression_text))

    agent_match = _AGENT_BODY_RE.fullmatch(step_body)
    if agent_match:
        return _parse_agent_step(name, agent_match.group(1), agent_match.group(2))

    raise ParseError(
        f"step '{name}': body must be llm \"<model>\" {{ ... }} or agent \"<model>\" {{ ... }}"
    )


def _parse_agent_step(name: str, model: str, inner: str) -> AgentStep:
    """Parse an agent body: optional `tools [...]`, optional `max_iters N`, and
    a prompt expression (everything left over)."""
    tools: tuple[str, ...] = ()
    tools_match = _TOOLS_RE.search(inner)
    if tools_match:
        raw_names = [t.strip() for t in tools_match.group(1).split(",") if t.strip()]
        for tool_name in raw_names:
            if not _IDENT_RE.fullmatch(tool_name):
                raise ParseError(f"agent '{name}': invalid tool name: {tool_name}")
        tools = tuple(raw_names)
        inner = inner[: tools_match.start()] + inner[tools_match.end() :]

    max_iters = DEFAULT_MAX_ITERS
    iters_match = _MAX_ITERS_RE.search(inner)
    if iters_match:
        max_iters = int(iters_match.group(1))
        if max_iters < 1:
            raise ParseError(f"agent '{name}': max_iters must be >= 1")
        inner = inner[: iters_match.start()] + inner[iters_match.end() :]

    expression_text = _join_expression_text(inner)
    if not expression_text:
        raise ParseError(f"agent '{name}': missing prompt expression")
    return AgentStep(
        name=name,
        model=model,
        prompt=_parse_expression(expression_text),
        tools=tools,
        max_iters=max_iters,
    )


def _parse_emit_block(body: str) -> EmitBlock:
    llm_match = _EMIT_LLM_RE.search(body)
    text_match = _EMIT_TEXT_RE.search(body)

    if llm_match:
        model = llm_match.group(1)
        expression_text = " ".join(
            line.strip() for line in llm_match.group(2).splitlines()
        ).strip()
        if not expression_text:
            raise ParseError("emit llm block must include a prompt expression")
        return EmitBlock(
            kind="llm",
            expression=_parse_expression(expression_text),
            model=model,
        )

    if text_match:
        expression_text = " ".join(
            line.strip() for line in text_match.group(1).splitlines()
        ).strip()
        if not expression_text:
            raise ParseError("emit text block must include an expression")
        return EmitBlock(kind="text", expression=_parse_expression(expression_text))

    raise ParseError(
        'Missing emit block. Expected: emit text { ... } or emit llm "<model>" { ... }'
    )


def _parse_expression(expression_text: str) -> Expression:
    raw_terms = [term.strip() for term in expression_text.split("+")]
    if not raw_terms:
        raise ParseError("Expression is empty")

    parsed_terms = []
    for term in raw_terms:
        if re.fullmatch(r'"[^"]*"', term):
            parsed_terms.append(StringLiteral(value=term[1:-1]))
        elif re.fullmatch(r"context\.[A-Za-z_][A-Za-z0-9_]*", term):
            parsed_terms.append(ContextRef(name=term.split(".", 1)[1]))
        elif re.fullmatch(r"inputs\.[A-Za-z_][A-Za-z0-9_]*", term):
            parsed_terms.append(InputsRef(name=term.split(".", 1)[1]))
        elif (steps_match := _STEPS_REF_RE.fullmatch(term)) is not None:
            parsed_terms.append(StepsRef(step_name=steps_match.group(1)))
        else:
            raise ParseError(f"Unsupported expression term: {term}")

    return Expression(terms=parsed_terms)
