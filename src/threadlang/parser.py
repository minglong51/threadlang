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
    ContextAssignment,
    ContextBlock,
    ContextRef,
    EmitBlock,
    Expression,
    InputsRef,
    Program,
    Step,
    StepsBlock,
    StepsRef,
    StringLiteral,
)


class ParseError(ValueError):
    """Raised when source text does not match the supported ThreadLang syntax."""


_THREAD_RE = re.compile(
    r"\s*thread\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{(.*)\}\s*", re.DOTALL
)
_CONTEXT_RE = re.compile(r"context\s*\{([^{}]*)\}", re.DOTALL)
# `steps { step a { llm "m" { ... } } step b { ... } }` — body can contain
# nested braces, so use a permissive non-greedy match and rely on the per-step
# regex to actually structure it.
_STEPS_RE = re.compile(
    r"steps\s*\{((?:[^{}]|\{[^{}]*\}|\{[^{}]*\{[^{}]*\}[^{}]*\})*)\}",
    re.DOTALL,
)
_STEP_RE = re.compile(
    r"step\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{"
    r"\s*llm\s+\"([^\"]+)\"\s*\{([^{}]*)\}\s*"
    r"\}",
    re.DOTALL,
)
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


def _parse_steps_block(body: str) -> StepsBlock:
    match = _STEPS_RE.search(body)
    if not match:
        return StepsBlock(steps=[])

    steps_body = match.group(1)
    steps: List[Step] = []
    seen_names: set[str] = set()
    for step_match in _STEP_RE.finditer(steps_body):
        name = step_match.group(1)
        model = step_match.group(2)
        prompt_text = step_match.group(3)
        if name in seen_names:
            raise ParseError(f"Duplicate step name: {name}")
        seen_names.add(name)
        steps.append(Step(name=name, model=model, prompt=_parse_expression(prompt_text)))

    # If the steps block exists but no step parsed, the user wrote something
    # unsupported; fail loud rather than silently dropping their code.
    if not steps and steps_body.strip():
        raise ParseError(
            "steps block present but no valid step found. "
            'Expected: step <name> { llm "<model>" { <expression> } }'
        )

    return StepsBlock(steps=steps)


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
