"""Versioned, canonical intermediate representation for ThreadLang programs.

IR v1 is additive and non-executing: it losslessly represents the v0.12 source
AST for inspection, stable serialization, and definition fingerprints. The
existing runtime remains authoritative until a differential IR interpreter is
implemented and verified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional, Tuple, Union

from .ast import (
    AgentStep,
    ContextRef,
    EmitBlock,
    ExpectRule,
    Expression,
    InputsRef,
    Program,
    RouteStep,
    Step,
    StepsRef,
    StringLiteral,
)

IR_VERSION = "threadlang.ir/v1"
LANGUAGE_VERSION = "threadlang/v0.12"


class IRCompileError(ValueError):
    """Raised when an AST cannot be represented losslessly in the target IR."""


@dataclass(frozen=True)
class IRContextEntry:
    name: str
    value: str


@dataclass(frozen=True)
class IRTerm:
    kind: str
    value: Optional[str] = None
    name: Optional[str] = None
    step_name: Optional[str] = None
    optional: bool = False


@dataclass(frozen=True)
class IRExpression:
    terms: Tuple[IRTerm, ...]


@dataclass(frozen=True)
class IRExpectation:
    kind: str
    values: Tuple[str, ...] = ()
    pattern: Optional[str] = None
    limit: Optional[int] = None


@dataclass(frozen=True)
class IRRouteArm:
    label: str
    target: str


@dataclass(frozen=True)
class IRLLMStep:
    name: str
    model: str
    prompt: IRExpression
    next_target: Optional[str]
    expect: Tuple[IRExpectation, ...]
    kind: str = field(default="llm", init=False)


@dataclass(frozen=True)
class IRAgentStep:
    name: str
    model: str
    prompt: IRExpression
    tools: Tuple[str, ...]
    max_iters: int
    next_target: Optional[str]
    kind: str = field(default="agent", init=False)


@dataclass(frozen=True)
class IRRouteStep:
    name: str
    model: str
    prompt: IRExpression
    arms: Tuple[IRRouteArm, ...]
    else_target: Optional[str]
    kind: str = field(default="route", init=False)


IRStep = Union[IRLLMStep, IRAgentStep, IRRouteStep]


@dataclass(frozen=True)
class IREmit:
    kind: str
    expression: IRExpression
    model: Optional[str]


@dataclass(frozen=True)
class WorkflowIR:
    name: str
    context: Tuple[IRContextEntry, ...]
    steps: Tuple[IRStep, ...]
    emit: IREmit
    ir_version: str = IR_VERSION
    language_version: str = LANGUAGE_VERSION


def _compile_term(term: object) -> IRTerm:
    if isinstance(term, StringLiteral):
        return IRTerm(kind="literal", value=term.value)
    if isinstance(term, ContextRef):
        return IRTerm(kind="context_ref", name=term.name)
    if isinstance(term, InputsRef):
        return IRTerm(kind="input_ref", name=term.name)
    if isinstance(term, StepsRef):
        return IRTerm(kind="step_ref", step_name=term.step_name, optional=term.optional)
    raise IRCompileError(f"unsupported expression term: {type(term).__name__}")


def _compile_expression(expression: Expression) -> IRExpression:
    return IRExpression(terms=tuple(_compile_term(term) for term in expression.terms))


def _compile_expectation(rule: ExpectRule) -> IRExpectation:
    if rule.kind == "one_of":
        return IRExpectation(kind=rule.kind, values=tuple(rule.values))
    if rule.kind == "matches":
        return IRExpectation(kind=rule.kind, pattern=rule.pattern)
    if rule.kind == "max_chars":
        return IRExpectation(kind=rule.kind, limit=rule.limit)
    if rule.kind == "nonempty":
        return IRExpectation(kind=rule.kind)
    raise IRCompileError(f"unsupported expectation rule: {rule.kind!r}")


def _compile_step(step: object) -> IRStep:
    if isinstance(step, Step):
        return IRLLMStep(
            name=step.name,
            model=step.model,
            prompt=_compile_expression(step.prompt),
            next_target=step.next_target,
            expect=tuple(_compile_expectation(rule) for rule in step.expect),
        )
    if isinstance(step, AgentStep):
        return IRAgentStep(
            name=step.name,
            model=step.model,
            prompt=_compile_expression(step.prompt),
            tools=tuple(step.tools),
            max_iters=step.max_iters,
            next_target=step.next_target,
        )
    if isinstance(step, RouteStep):
        return IRRouteStep(
            name=step.name,
            model=step.model,
            prompt=_compile_expression(step.prompt),
            arms=tuple(IRRouteArm(label=arm.label, target=arm.target) for arm in step.arms),
            else_target=step.else_target,
        )
    raise IRCompileError(f"unsupported step node: {type(step).__name__}")


def _compile_emit(emit: EmitBlock) -> IREmit:
    if emit.kind not in ("text", "llm"):
        raise IRCompileError(f"unsupported emit kind: {emit.kind!r}")
    return IREmit(
        kind=emit.kind,
        expression=_compile_expression(emit.expression),
        model=emit.model,
    )


def compile_program(program: Program) -> WorkflowIR:
    """Compile a validated v0.12 source AST into lossless Workflow IR v1."""
    if not isinstance(program, Program):
        raise IRCompileError(f"unsupported program node: {type(program).__name__}")
    return WorkflowIR(
        name=program.thread_name,
        context=tuple(
            IRContextEntry(name=assignment.name, value=assignment.value)
            for assignment in program.context.assignments
        ),
        steps=tuple(_compile_step(step) for step in program.steps.steps),
        emit=_compile_emit(program.emit),
    )


def _term_object(term: IRTerm) -> dict[str, object]:
    if term.kind == "literal":
        return {"kind": term.kind, "value": term.value}
    if term.kind in ("context_ref", "input_ref"):
        return {"kind": term.kind, "name": term.name}
    if term.kind == "step_ref":
        return {
            "kind": term.kind,
            "optional": term.optional,
            "step_name": term.step_name,
        }
    raise IRCompileError(f"unsupported IR term kind: {term.kind!r}")


def _expression_object(expression: IRExpression) -> dict[str, object]:
    return {"terms": [_term_object(term) for term in expression.terms]}


def _expectation_object(expectation: IRExpectation) -> dict[str, object]:
    if expectation.kind == "one_of":
        return {"kind": expectation.kind, "values": list(expectation.values)}
    if expectation.kind == "matches":
        return {"kind": expectation.kind, "pattern": expectation.pattern}
    if expectation.kind == "max_chars":
        return {"kind": expectation.kind, "limit": expectation.limit}
    if expectation.kind == "nonempty":
        return {"kind": expectation.kind}
    raise IRCompileError(f"unsupported IR expectation kind: {expectation.kind!r}")


def _step_object(step: IRStep) -> dict[str, object]:
    common: dict[str, object] = {
        "kind": step.kind,
        "model": step.model,
        "name": step.name,
        "prompt": _expression_object(step.prompt),
    }
    if isinstance(step, IRLLMStep):
        common["expect"] = [_expectation_object(rule) for rule in step.expect]
        common["next_target"] = step.next_target
        return common
    if isinstance(step, IRAgentStep):
        common["max_iters"] = step.max_iters
        common["next_target"] = step.next_target
        common["tools"] = list(step.tools)
        return common
    if isinstance(step, IRRouteStep):
        common["arms"] = [{"label": arm.label, "target": arm.target} for arm in step.arms]
        common["else_target"] = step.else_target
        return common
    raise IRCompileError(f"unsupported IR step node: {type(step).__name__}")


def workflow_ir_object(workflow: WorkflowIR) -> dict[str, object]:
    """Convert Workflow IR v1 to its explicit JSON-compatible object shape."""
    if workflow.ir_version != IR_VERSION:
        raise IRCompileError(f"unsupported IR version: {workflow.ir_version!r}")
    return {
        "context": [
            {"name": assignment.name, "value": assignment.value} for assignment in workflow.context
        ],
        "emit": {
            "expression": _expression_object(workflow.emit.expression),
            "kind": workflow.emit.kind,
            "model": workflow.emit.model,
        },
        "ir_version": workflow.ir_version,
        "language_version": workflow.language_version,
        "name": workflow.name,
        "steps": [_step_object(step) for step in workflow.steps],
    }


def canonical_ir_bytes(workflow: WorkflowIR) -> bytes:
    """Return the canonical UTF-8 JSON bytes used for workflow identity."""
    return json.dumps(
        workflow_ir_object(workflow),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def workflow_fingerprint(workflow: WorkflowIR) -> str:
    """Return the SHA-256 digest of the workflow's canonical IR bytes."""
    return hashlib.sha256(canonical_ir_bytes(workflow)).hexdigest()
