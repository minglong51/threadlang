"""Deterministic parser for ThreadLang v1.

Grammar (informal):

    program     = "thread" name "{" context [ steps ] emit "}"
    context     = "context" "{" { name "=" string } "}"
    steps       = "steps" "{" { step } "}"
    step        = "step" name "{" ( llm_body | agent_body | route_body ) "}"
    route_body  = "route" string "{" expression arm { arm } [ "else" "->" name ] "}"
    arm         = "on" string "->" ( name | "end" )
    emit_text   = "emit" "text" "{" expression "}"
    emit_llm    = "emit" "llm" string "{" expression "}"
    expression  = term { "+" term }
    term        = string | "context." name | "inputs." name | "steps." name ".output" [ "?" ]

Steps form a forward-only DAG: an `llm`/`agent` body may end with
`then -> <step|end>` (default: fall through to the next declared step), a
`route` body's arms are its conditional edges, and every jump target must be
declared *after* the step that jumps to it. `end` is a reserved target that
skips to emit; no step may be named `end`.

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
    END_TARGET,
    AgentStep,
    ContextAssignment,
    ContextBlock,
    ContextRef,
    EmitBlock,
    Expression,
    InputsRef,
    Program,
    RouteArm,
    RouteStep,
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
_ROUTE_BODY_RE = re.compile(r'\s*route\s+"([^"]+)"\s*\{(.*)\}\s*', re.DOTALL)
_TOOLS_RE = re.compile(r"\btools\s*\[([^\]]*)\]")
_MAX_ITERS_RE = re.compile(r"\bmax_iters\s+(\d+)")
_THEN_RE = re.compile(r"\bthen\s*->\s*([A-Za-z_][A-Za-z0-9_]*)")
_ARM_RE = re.compile(r'\bon\s+"([^"]+)"\s*->\s*([A-Za-z_][A-Za-z0-9_]*)')
_ELSE_RE = re.compile(r"\belse\s*->\s*([A-Za-z_][A-Za-z0-9_]*)")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EMIT_TEXT_RE = re.compile(r"emit\s+text\s*\{([^{}]*)\}", re.DOTALL)
_EMIT_LLM_RE = re.compile(r"emit\s+llm\s+\"([^\"]+)\"\s*\{([^{}]*)\}", re.DOTALL)
_CONTEXT_ASSIGN_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"')
_STEPS_REF_RE = re.compile(r"steps\.([A-Za-z_][A-Za-z0-9_]*)\.output(\??)")


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
        if name == END_TARGET:
            raise ParseError(
                f"'{END_TARGET}' is a reserved jump target and cannot be a step name"
            )
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
            'Expected: step <name> { llm "<model>" { <expression> } }, '
            'step <name> { agent "<model>" { ... } }, '
            'or step <name> { route "<model>" { ... } }'
        )

    _validate_targets(steps)
    return StepsBlock(steps=steps)


def _validate_targets(steps: List[StepNode]) -> None:
    """Every jump target must be a step declared *after* the jumping step, or
    the reserved `end`. Forward-only edges keep the step graph a DAG — each
    step runs at most once, which is what makes step-name checkpoints and
    resume-from-failure stay correct with routing in the language."""
    index = {step.name: i for i, step in enumerate(steps)}

    def check(origin: str, position: int, target: str, kind: str) -> None:
        if target == END_TARGET:
            return
        if target not in index:
            raise ParseError(f"step '{origin}': {kind} -> unknown step '{target}'")
        if index[target] <= position:
            raise ParseError(
                f"step '{origin}': {kind} -> '{target}' jumps backward; "
                "targets must be declared after the step that jumps to them"
            )

    for i, step in enumerate(steps):
        if isinstance(step, RouteStep):
            seen_labels: set[str] = set()
            for arm in step.arms:
                if arm.label in seen_labels:
                    raise ParseError(
                        f"step '{step.name}': duplicate route label \"{arm.label}\""
                    )
                seen_labels.add(arm.label)
                check(step.name, i, arm.target, f'on "{arm.label}"')
            if step.else_target is not None:
                check(step.name, i, step.else_target, "else")
        elif step.next_target is not None:
            check(step.name, i, step.next_target, "then")


def _join_expression_text(raw: str) -> str:
    return " ".join(line.strip() for line in raw.splitlines() if line.strip()).strip()


def _parse_then(name: str, inner: str) -> tuple[str, str | None]:
    """Extract an optional `then -> <target>` edge from a step body, returning
    (remaining_body, target)."""
    then_match = _THEN_RE.search(inner)
    if not then_match:
        return inner, None
    target = then_match.group(1)
    inner = inner[: then_match.start()] + inner[then_match.end() :]
    if _THEN_RE.search(inner):
        raise ParseError(f"step '{name}': multiple then -> edges")
    return inner, target


def _parse_step_body(name: str, step_body: str) -> StepNode:
    llm_match = _LLM_BODY_RE.fullmatch(step_body)
    if llm_match:
        model = llm_match.group(1)
        inner, next_target = _parse_then(name, llm_match.group(2))
        expression_text = _join_expression_text(inner)
        if not expression_text:
            raise ParseError(f"step '{name}': llm body must include a prompt expression")
        return Step(
            name=name,
            model=model,
            prompt=_parse_expression(expression_text),
            next_target=next_target,
        )

    agent_match = _AGENT_BODY_RE.fullmatch(step_body)
    if agent_match:
        return _parse_agent_step(name, agent_match.group(1), agent_match.group(2))

    route_match = _ROUTE_BODY_RE.fullmatch(step_body)
    if route_match:
        return _parse_route_step(name, route_match.group(1), route_match.group(2))

    raise ParseError(
        f"step '{name}': body must be llm \"<model>\" {{ ... }}, "
        f"agent \"<model>\" {{ ... }}, or route \"<model>\" {{ ... }}"
    )


def _parse_route_step(name: str, model: str, inner: str) -> RouteStep:
    """Parse a route body: a prompt expression, one or more `on "<label>" ->
    <target>` arms, and an optional `else -> <target>`."""
    arms: List[RouteArm] = []
    for arm_match in _ARM_RE.finditer(inner):
        arms.append(RouteArm(label=arm_match.group(1), target=arm_match.group(2)))
    inner = _ARM_RE.sub("", inner)
    if not arms:
        raise ParseError(
            f"route '{name}': needs at least one arm: on \"<label>\" -> <step|end>"
        )

    else_target: str | None = None
    else_match = _ELSE_RE.search(inner)
    if else_match:
        else_target = else_match.group(1)
        inner = inner[: else_match.start()] + inner[else_match.end() :]
        if _ELSE_RE.search(inner):
            raise ParseError(f"route '{name}': multiple else -> edges")

    expression_text = _join_expression_text(inner)
    if not expression_text:
        raise ParseError(f"route '{name}': missing prompt expression")
    return RouteStep(
        name=name,
        model=model,
        prompt=_parse_expression(expression_text),
        arms=tuple(arms),
        else_target=else_target,
    )


def _parse_agent_step(name: str, model: str, inner: str) -> AgentStep:
    """Parse an agent body: optional `tools [...]`, optional `max_iters N`,
    optional `then -> <target>`, and a prompt expression (everything left
    over)."""
    inner, next_target = _parse_then(name, inner)
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
        next_target=next_target,
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
            parsed_terms.append(
                StepsRef(
                    step_name=steps_match.group(1),
                    optional=steps_match.group(2) == "?",
                )
            )
        else:
            raise ParseError(f"Unsupported expression term: {term}")

    return Expression(terms=parsed_terms)
