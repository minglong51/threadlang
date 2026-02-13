"""Runtime evaluator for ThreadLang v0.1 AST."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .ast import Concat, Emit, Expr, Program, StringLiteral, VariableRef
from .trace import TraceEvent


@dataclass(frozen=True)
class RunResult:
    output: str
    trace: List[TraceEvent]


def run_program(program: Program, inputs: Dict[str, str] | None = None) -> RunResult:
    inputs = inputs or {}
    trace: List[TraceEvent] = [
        TraceEvent("parse_ok", {"thread": program.thread_name}),
    ]

    context: Dict[str, str] = {}
    for assign in program.context:
        context[assign.key] = assign.value
        trace.append(TraceEvent("context_set", {"key": assign.key, "value": assign.value}))

    output_parts: List[str] = []
    for emit in program.emits:
        value = _eval_emit(emit, context=context, inputs=inputs)
        if emit.target == "text":
            output_parts.append(value)
        trace.append(TraceEvent("emit", {"target": emit.target, "value": value}))

    return RunResult(output="".join(output_parts), trace=trace)


def _eval_emit(emit: Emit, context: Dict[str, str], inputs: Dict[str, str]) -> str:
    return _eval_expr(emit.expression, context=context, inputs=inputs)


def _eval_expr(expr: Expr, context: Dict[str, str], inputs: Dict[str, str]) -> str:
    if isinstance(expr, StringLiteral):
        return expr.value
    if isinstance(expr, VariableRef):
        if expr.scope == "context":
            return context.get(expr.key, "")
        if expr.scope == "inputs":
            return inputs.get(expr.key, "")
        raise ValueError(f"Unsupported variable scope: {expr.scope}")
    if isinstance(expr, Concat):
        return "".join(_eval_expr(part, context=context, inputs=inputs) for part in expr.parts)
    raise ValueError(f"Unsupported expression node: {type(expr).__name__}")
