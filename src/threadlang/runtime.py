"""Runtime execution for ThreadLang AST programs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping

from .ast import ContextRef, InputsRef, Program, StringLiteral
from .trace import Trace, TraceEvent


class RuntimeError(ValueError):
    """Raised when runtime execution fails deterministically."""


@dataclass(frozen=True)
class RuntimeResult:
    output: str
    trace: Trace


def run_program(program: Program, inputs: Mapping[str, str]) -> RuntimeResult:
    trace: Trace = []
    context = _build_context(program, trace)
    output = _evaluate_emit(program, context, inputs, trace)
    trace.append(TraceEvent(phase="emit", message="Output emitted", data={"output": output}))
    return RuntimeResult(output=output, trace=trace)


def _build_context(program: Program, trace: Trace) -> Dict[str, str]:
    context: Dict[str, str] = {}
    for assignment in program.context.assignments:
        context[assignment.name] = assignment.value
        trace.append(
            TraceEvent(
                phase="context",
                message="Context assignment",
                data={"name": assignment.name, "value": assignment.value},
            )
        )
    return context


def _evaluate_emit(
    program: Program,
    context: Mapping[str, str],
    inputs: Mapping[str, str],
    trace: Trace,
) -> str:
    pieces = []
    for term in program.emit.expression.terms:
        if isinstance(term, StringLiteral):
            value = term.value
            source = "string"
        elif isinstance(term, ContextRef):
            if term.name not in context:
                raise RuntimeError(f"Unknown context value: {term.name}")
            value = context[term.name]
            source = f"context.{term.name}"
        elif isinstance(term, InputsRef):
            if term.name not in inputs:
                raise RuntimeError(f"Missing input value: {term.name}")
            value = str(inputs[term.name])
            source = f"inputs.{term.name}"
        else:
            raise RuntimeError(f"Unsupported term type: {type(term)!r}")

        pieces.append(value)
        trace.append(
            TraceEvent(
                phase="runtime",
                message="Expression term evaluated",
                data={"source": source, "value": value},
            )
        )

    return "".join(pieces)
