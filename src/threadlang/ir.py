"""Versioned, canonical intermediate representation for ThreadLang programs.

IR v1 is additive and non-executing: it losslessly represents the v0.12 source
AST for inspection, stable serialization, and definition fingerprints. The
existing runtime remains authoritative until a differential IR interpreter is
implemented and verified.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional, Tuple, Union

from .ast import (
    AgentStep,
    ContextAssignment,
    ContextBlock,
    ContextRef,
    EmitBlock,
    ExpectRule,
    Expression,
    InputsRef,
    Program,
    RouteArm,
    RouteStep,
    Step,
    StepsBlock,
    StepsRef,
    StringLiteral,
)
from .policy import (
    MAX_AGENT_ITERS,
    MAX_IR_BYTES,
    MAX_REGEX_PATTERN_CHARS,
    MAX_STRING_CHARS,
)

if TYPE_CHECKING:
    from .llm import LLMClient
    from .runtime import RuntimeResult
    from .tools import ToolRegistry
    from .trace import Trace

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


def _term_from_ir(term: IRTerm) -> StringLiteral | ContextRef | InputsRef | StepsRef:
    if term.kind == "literal" and term.value is not None:
        return StringLiteral(value=term.value)
    if term.kind == "context_ref" and term.name is not None:
        return ContextRef(name=term.name)
    if term.kind == "input_ref" and term.name is not None:
        return InputsRef(name=term.name)
    if term.kind == "step_ref" and term.step_name is not None:
        return StepsRef(step_name=term.step_name, optional=term.optional)
    raise IRCompileError(f"invalid IR term payload for kind {term.kind!r}")


def _expression_from_ir(expression: IRExpression) -> Expression:
    return Expression(terms=[_term_from_ir(term) for term in expression.terms])


def _expectation_from_ir(expectation: IRExpectation) -> ExpectRule:
    if expectation.kind == "one_of" and expectation.values:
        return ExpectRule(kind=expectation.kind, values=expectation.values)
    if expectation.kind == "matches" and expectation.pattern is not None:
        return ExpectRule(kind=expectation.kind, pattern=expectation.pattern)
    if expectation.kind == "max_chars" and expectation.limit is not None:
        return ExpectRule(kind=expectation.kind, limit=expectation.limit)
    if expectation.kind == "nonempty":
        return ExpectRule(kind=expectation.kind)
    raise IRCompileError(f"invalid IR expectation payload for kind {expectation.kind!r}")


def _step_from_ir(step: IRStep) -> Step | AgentStep | RouteStep:
    if isinstance(step, IRLLMStep):
        return Step(
            name=step.name,
            model=step.model,
            prompt=_expression_from_ir(step.prompt),
            next_target=step.next_target,
            expect=tuple(_expectation_from_ir(rule) for rule in step.expect),
        )
    if isinstance(step, IRAgentStep):
        return AgentStep(
            name=step.name,
            model=step.model,
            prompt=_expression_from_ir(step.prompt),
            tools=step.tools,
            max_iters=step.max_iters,
            next_target=step.next_target,
        )
    if isinstance(step, IRRouteStep):
        return RouteStep(
            name=step.name,
            model=step.model,
            prompt=_expression_from_ir(step.prompt),
            arms=tuple(RouteArm(label=arm.label, target=arm.target) for arm in step.arms),
            else_target=step.else_target,
        )
    raise IRCompileError(f"unsupported IR step node: {type(step).__name__}")


def program_from_ir(workflow: WorkflowIR) -> Program:
    """Reconstruct the current runtime AST from validated Workflow IR v1.

    This compatibility bridge lets systems persist and exchange versioned IR
    while the existing interpreter remains authoritative. It is deliberately a
    total explicit conversion rather than reflective dataclass unpacking.
    """
    if workflow.ir_version != IR_VERSION:
        raise IRCompileError(f"unsupported IR version: {workflow.ir_version!r}")
    if workflow.language_version != LANGUAGE_VERSION:
        raise IRCompileError(f"unsupported language version: {workflow.language_version!r}")
    if workflow.emit.kind not in ("text", "llm"):
        raise IRCompileError(f"unsupported emit kind: {workflow.emit.kind!r}")
    if workflow.emit.kind == "text" and workflow.emit.model is not None:
        raise IRCompileError("text emit must not declare a model")
    if workflow.emit.kind == "llm" and workflow.emit.model is None:
        raise IRCompileError("llm emit requires a model")
    return Program(
        thread_name=workflow.name,
        context=ContextBlock(
            assignments=[
                ContextAssignment(name=entry.name, value=entry.value) for entry in workflow.context
            ]
        ),
        steps=StepsBlock(steps=[_step_from_ir(step) for step in workflow.steps]),
        emit=EmitBlock(
            kind=workflow.emit.kind,
            expression=_expression_from_ir(workflow.emit.expression),
            model=workflow.emit.model,
        ),
    )


def run_ir(
    workflow: WorkflowIR,
    inputs: Mapping[str, str],
    *,
    llm_client: Optional[LLMClient] = None,
    tools: Optional[ToolRegistry] = None,
    trace: Optional[Trace] = None,
    resume_outputs: Optional[Dict[str, str]] = None,
    on_step_complete: Optional[Callable[[str, str], None]] = None,
) -> RuntimeResult:
    """Execute Workflow IR v1 through the compatibility AST interpreter."""
    from .runtime import run_program

    return run_program(
        program_from_ir(workflow),
        inputs,
        llm_client=llm_client,
        tools=tools,
        trace=trace,
        resume_outputs=resume_outputs,
        on_step_complete=on_step_complete,
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


def _object(value: Any, path: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IRCompileError(f"{path} must be an object")
    actual = set(value)
    missing = keys - actual
    unexpected = actual - keys
    if missing:
        raise IRCompileError(f"{path} missing field: {sorted(missing)[0]}")
    if unexpected:
        raise IRCompileError(f"{path} has unexpected field: {sorted(unexpected)[0]}")
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise IRCompileError(f"{path} must be an array")
    return value


def _string(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise IRCompileError(f"{path} must be a string")
    return value


def _nullable_string(value: Any, path: str) -> Optional[str]:
    if value is None:
        return None
    return _string(value, path)


def _integer(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IRCompileError(f"{path} must be an integer")
    return value


def _term_from_object(value: Any, path: str) -> IRTerm:
    if not isinstance(value, dict):
        raise IRCompileError(f"{path} must be an object")
    kind = _string(value.get("kind"), f"{path}.kind")
    if kind == "literal":
        obj = _object(value, path, {"kind", "value"})
        return IRTerm(kind=kind, value=_string(obj["value"], f"{path}.value"))
    if kind in ("context_ref", "input_ref"):
        obj = _object(value, path, {"kind", "name"})
        return IRTerm(kind=kind, name=_string(obj["name"], f"{path}.name"))
    if kind == "step_ref":
        obj = _object(value, path, {"kind", "optional", "step_name"})
        optional = obj["optional"]
        if not isinstance(optional, bool):
            raise IRCompileError(f"{path}.optional must be a boolean")
        return IRTerm(
            kind=kind,
            step_name=_string(obj["step_name"], f"{path}.step_name"),
            optional=optional,
        )
    raise IRCompileError(f"{path} has unsupported term kind: {kind!r}")


def _expression_from_object(value: Any, path: str) -> IRExpression:
    obj = _object(value, path, {"terms"})
    terms = _array(obj["terms"], f"{path}.terms")
    if not terms:
        raise IRCompileError(f"{path}.terms must not be empty")
    return IRExpression(
        terms=tuple(
            _term_from_object(term, f"{path}.terms[{index}]") for index, term in enumerate(terms)
        )
    )


def _expectation_from_object(value: Any, path: str) -> IRExpectation:
    if not isinstance(value, dict):
        raise IRCompileError(f"{path} must be an object")
    kind = _string(value.get("kind"), f"{path}.kind")
    if kind == "one_of":
        obj = _object(value, path, {"kind", "values"})
        values = tuple(
            _string(item, f"{path}.values[{index}]")
            for index, item in enumerate(_array(obj["values"], f"{path}.values"))
        )
        if not values:
            raise IRCompileError(f"{path}.values must not be empty")
        return IRExpectation(kind=kind, values=values)
    if kind == "matches":
        obj = _object(value, path, {"kind", "pattern"})
        return IRExpectation(kind=kind, pattern=_string(obj["pattern"], f"{path}.pattern"))
    if kind == "max_chars":
        obj = _object(value, path, {"kind", "limit"})
        limit = _integer(obj["limit"], f"{path}.limit")
        if limit < 1:
            raise IRCompileError(f"{path}.limit must be >= 1")
        return IRExpectation(kind=kind, limit=limit)
    if kind == "nonempty":
        _object(value, path, {"kind"})
        return IRExpectation(kind=kind)
    raise IRCompileError(f"{path} has unsupported expectation kind: {kind!r}")


def _step_from_object(value: Any, path: str) -> IRStep:
    if not isinstance(value, dict):
        raise IRCompileError(f"{path} must be an object")
    kind = _string(value.get("kind"), f"{path}.kind")
    common = {"kind", "model", "name", "prompt"}
    name = _string(value.get("name"), f"{path}.name")
    model = _string(value.get("model"), f"{path}.model")
    prompt = _expression_from_object(value.get("prompt"), f"{path}.prompt")
    if kind == "llm":
        obj = _object(value, path, common | {"expect", "next_target"})
        expectations = tuple(
            _expectation_from_object(item, f"{path}.expect[{index}]")
            for index, item in enumerate(_array(obj["expect"], f"{path}.expect"))
        )
        return IRLLMStep(
            name=name,
            model=model,
            prompt=prompt,
            next_target=_nullable_string(obj["next_target"], f"{path}.next_target"),
            expect=expectations,
        )
    if kind == "agent":
        obj = _object(value, path, common | {"max_iters", "next_target", "tools"})
        tools = tuple(
            _string(item, f"{path}.tools[{index}]")
            for index, item in enumerate(_array(obj["tools"], f"{path}.tools"))
        )
        max_iters = _integer(obj["max_iters"], f"{path}.max_iters")
        if max_iters < 1:
            raise IRCompileError(f"{path}.max_iters must be >= 1")
        return IRAgentStep(
            name=name,
            model=model,
            prompt=prompt,
            tools=tools,
            max_iters=max_iters,
            next_target=_nullable_string(obj["next_target"], f"{path}.next_target"),
        )
    if kind == "route":
        obj = _object(value, path, common | {"arms", "else_target"})
        arms = []
        for index, item in enumerate(_array(obj["arms"], f"{path}.arms")):
            arm_path = f"{path}.arms[{index}]"
            arm = _object(item, arm_path, {"label", "target"})
            arms.append(
                IRRouteArm(
                    label=_string(arm["label"], f"{arm_path}.label"),
                    target=_string(arm["target"], f"{arm_path}.target"),
                )
            )
        if not arms:
            raise IRCompileError(f"{path}.arms must not be empty")
        return IRRouteStep(
            name=name,
            model=model,
            prompt=prompt,
            arms=tuple(arms),
            else_target=_nullable_string(obj["else_target"], f"{path}.else_target"),
        )
    raise IRCompileError(f"{path} has unsupported step kind: {kind!r}")


def _validate_loaded_workflow(workflow: WorkflowIR) -> None:
    program = program_from_ir(workflow)

    def identifier(value: str, path: str) -> None:
        if not value or not (value[0].isalpha() or value[0] == "_"):
            raise IRCompileError(f"{path} is not a valid identifier")
        if any(not (char.isalnum() or char == "_") for char in value[1:]):
            raise IRCompileError(f"{path} is not a valid identifier")

    def bounded(value: str, path: str) -> None:
        if len(value) > MAX_STRING_CHARS:
            raise IRCompileError(f"{path} exceeds {MAX_STRING_CHARS} characters")

    identifier(workflow.name, "workflow.name")
    context_names = [entry.name for entry in workflow.context]
    step_names = [step.name for step in workflow.steps]
    for index, entry in enumerate(workflow.context):
        identifier(entry.name, f"workflow.context[{index}].name")
        bounded(entry.value, f"workflow.context[{index}].value")
    if len(context_names) != len(set(context_names)):
        raise IRCompileError("context names must be unique")
    if len(step_names) != len(set(step_names)):
        raise IRCompileError("step names must be unique")
    if "end" in step_names:
        raise IRCompileError("step name 'end' is reserved")

    expressions = [workflow.emit.expression]
    if workflow.emit.model is not None:
        bounded(workflow.emit.model, "workflow.emit.model")
    for index, step in enumerate(workflow.steps):
        path = f"workflow.steps[{index}]"
        identifier(step.name, f"{path}.name")
        bounded(step.model, f"{path}.model")
        expressions.append(step.prompt)
        if isinstance(step, IRAgentStep):
            if step.max_iters > MAX_AGENT_ITERS:
                raise IRCompileError(f"{path}.max_iters exceeds {MAX_AGENT_ITERS}")
            if len(step.tools) != len(set(step.tools)):
                raise IRCompileError(f"{path}.tools must be unique")
            for tool_index, tool in enumerate(step.tools):
                identifier(tool, f"{path}.tools[{tool_index}]")
        elif isinstance(step, IRRouteStep):
            normalized = [arm.label.casefold() for arm in step.arms]
            if len(normalized) != len(set(normalized)):
                raise IRCompileError(f"{path}.arms labels must be unique")
            for arm_index, arm in enumerate(step.arms):
                bounded(arm.label, f"{path}.arms[{arm_index}].label")
        elif isinstance(step, IRLLMStep):
            kinds = [rule.kind for rule in step.expect if rule.kind != "matches"]
            if len(kinds) != len(set(kinds)):
                raise IRCompileError(f"{path}.expect has duplicate rule kinds")
            for rule_index, rule in enumerate(step.expect):
                rule_path = f"{path}.expect[{rule_index}]"
                if rule.kind == "matches" and rule.pattern is not None:
                    if len(rule.pattern) > MAX_REGEX_PATTERN_CHARS:
                        raise IRCompileError(
                            f"{rule_path}.pattern exceeds {MAX_REGEX_PATTERN_CHARS} characters"
                        )
                    try:
                        re.compile(rule.pattern)
                    except re.error as exc:
                        raise IRCompileError(f"{rule_path}.pattern is invalid: {exc}") from exc
                if rule.kind == "one_of":
                    normalized_values = [value.casefold() for value in rule.values]
                    if len(normalized_values) != len(set(normalized_values)):
                        raise IRCompileError(f"{rule_path}.values must be unique")
                    for value_index, value in enumerate(rule.values):
                        bounded(value, f"{rule_path}.values[{value_index}]")

    for expression_index, expression in enumerate(expressions):
        for term_index, term in enumerate(expression.terms):
            path = f"workflow.expressions[{expression_index}].terms[{term_index}]"
            if term.value is not None:
                bounded(term.value, f"{path}.value")
            if term.name is not None:
                identifier(term.name, f"{path}.name")
            if term.step_name is not None:
                identifier(term.step_name, f"{path}.step_name")

    try:
        from .parser import _validate_program, _validate_targets

        _validate_targets(program.steps.steps)
        _validate_program(program.context, program.steps, program.emit)
    except ValueError as exc:
        raise IRCompileError(f"invalid workflow semantics: {exc}") from exc


def load_ir_bytes(payload: bytes) -> WorkflowIR:
    """Load and strictly validate an untrusted Workflow IR v1 JSON document."""
    if len(payload) > MAX_IR_BYTES:
        raise IRCompileError(f"IR document exceeds {MAX_IR_BYTES} bytes")
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IRCompileError(f"invalid IR JSON: {exc}") from exc
    root = _object(
        decoded,
        "workflow",
        {"context", "emit", "ir_version", "language_version", "name", "steps"},
    )
    ir_version = _string(root["ir_version"], "workflow.ir_version")
    if ir_version != IR_VERSION:
        raise IRCompileError(f"unsupported IR version: {ir_version!r}")
    language_version = _string(root["language_version"], "workflow.language_version")
    if language_version != LANGUAGE_VERSION:
        raise IRCompileError(f"unsupported language version: {language_version!r}")
    context = []
    for index, item in enumerate(_array(root["context"], "workflow.context")):
        path = f"workflow.context[{index}]"
        entry = _object(item, path, {"name", "value"})
        context.append(
            IRContextEntry(
                name=_string(entry["name"], f"{path}.name"),
                value=_string(entry["value"], f"{path}.value"),
            )
        )
    steps = tuple(
        _step_from_object(item, f"workflow.steps[{index}]")
        for index, item in enumerate(_array(root["steps"], "workflow.steps"))
    )
    emit_obj = _object(root["emit"], "workflow.emit", {"expression", "kind", "model"})
    emit_kind = _string(emit_obj["kind"], "workflow.emit.kind")
    if emit_kind not in ("text", "llm"):
        raise IRCompileError(f"unsupported emit kind: {emit_kind!r}")
    workflow = WorkflowIR(
        name=_string(root["name"], "workflow.name"),
        context=tuple(context),
        steps=steps,
        emit=IREmit(
            kind=emit_kind,
            expression=_expression_from_object(emit_obj["expression"], "workflow.emit.expression"),
            model=_nullable_string(emit_obj["model"], "workflow.emit.model"),
        ),
        ir_version=ir_version,
        language_version=language_version,
    )
    _validate_loaded_workflow(workflow)
    return workflow


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
