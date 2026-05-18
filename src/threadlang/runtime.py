"""Runtime execution for ThreadLang AST programs.

Execution order:
    1. Build context from context block (deterministic).
    2. Run each step in declaration order. Each step renders its prompt by
       evaluating expression terms against (context, inputs, prior step
       outputs), calls the LLM client with (model, prompt), and binds the
       response to `steps.<name>.output`.
    3. Evaluate the emit block:
       - emit text → string concat of expression terms.
       - emit llm  → render the prompt as above, call the LLM client one
         more time, return the response as program output.

Every phase appends structured TraceEvents so callers can inspect what
happened. The trace is the durable record of an execution; the runtime
itself is stateless across runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Optional

from .ast import (
    ContextRef,
    EmitBlock,
    Expression,
    InputsRef,
    Program,
    StepsRef,
    StringLiteral,
)
from .llm import DryRunClient, LLMClient
from .trace import Trace, TraceEvent


class RuntimeError(ValueError):
    """Raised when runtime execution fails deterministically."""


@dataclass(frozen=True)
class RuntimeResult:
    output: str
    trace: Trace
    step_outputs: Dict[str, str]


def run_program(
    program: Program,
    inputs: Mapping[str, str],
    llm_client: Optional[LLMClient] = None,
) -> RuntimeResult:
    """Execute a ThreadLang program.

    `llm_client` is required if the program contains any `steps` or uses
    `emit llm`. If omitted, a `DryRunClient` is used — fine for tests, but
    callers wiring a real workflow should pass an `AnthropicClient` (or
    any object satisfying the `LLMClient` protocol).
    """
    trace: Trace = []
    context = _build_context(program, trace)

    client = llm_client or DryRunClient()
    step_outputs = _run_steps(program, context, inputs, client, trace)

    output = _evaluate_emit(
        program.emit, context, inputs, step_outputs, client, trace
    )
    trace.append(
        TraceEvent(phase="emit", message="Output emitted", data={"output": output})
    )
    return RuntimeResult(output=output, trace=trace, step_outputs=dict(step_outputs))


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


def _run_steps(
    program: Program,
    context: Mapping[str, str],
    inputs: Mapping[str, str],
    client: LLMClient,
    trace: Trace,
) -> Dict[str, str]:
    step_outputs: Dict[str, str] = {}
    for step in program.steps.steps:
        prompt = _render_expression(step.prompt, context, inputs, step_outputs)
        trace.append(
            TraceEvent(
                phase="step",
                message=f"Calling LLM for step '{step.name}'",
                data={"step": step.name, "model": step.model, "prompt": prompt},
            )
        )
        try:
            response = client.complete(model=step.model, prompt=prompt)
        except Exception as exc:
            raise RuntimeError(
                f"LLM call failed in step '{step.name}': {type(exc).__name__}: {exc}"
            ) from exc
        step_outputs[step.name] = response
        trace.append(
            TraceEvent(
                phase="step",
                message=f"Step '{step.name}' produced output",
                data={"step": step.name, "output": response},
            )
        )
    return step_outputs


def _evaluate_emit(
    emit: EmitBlock,
    context: Mapping[str, str],
    inputs: Mapping[str, str],
    step_outputs: Mapping[str, str],
    client: LLMClient,
    trace: Trace,
) -> str:
    if emit.kind == "text":
        return _render_expression(emit.expression, context, inputs, step_outputs, trace=trace)
    if emit.kind == "llm":
        prompt = _render_expression(emit.expression, context, inputs, step_outputs)
        assert emit.model is not None, "parser invariant: emit llm must carry a model"
        trace.append(
            TraceEvent(
                phase="emit",
                message="Calling LLM for emit",
                data={"model": emit.model, "prompt": prompt},
            )
        )
        try:
            return client.complete(model=emit.model, prompt=prompt)
        except Exception as exc:
            raise RuntimeError(
                f"LLM call failed during emit: {type(exc).__name__}: {exc}"
            ) from exc
    raise RuntimeError(f"Unknown emit kind: {emit.kind}")


def _render_expression(
    expression: Expression,
    context: Mapping[str, str],
    inputs: Mapping[str, str],
    step_outputs: Mapping[str, str],
    trace: Optional[Trace] = None,
) -> str:
    pieces = []
    for term in expression.terms:
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
        elif isinstance(term, StepsRef):
            if term.step_name not in step_outputs:
                raise RuntimeError(
                    f"Reference to step '{term.step_name}' before it ran"
                )
            value = step_outputs[term.step_name]
            source = f"steps.{term.step_name}.output"
        else:
            raise RuntimeError(f"Unsupported term type: {type(term)!r}")

        pieces.append(value)
        if trace is not None:
            trace.append(
                TraceEvent(
                    phase="runtime",
                    message="Expression term evaluated",
                    data={"source": source, "value": value},
                )
            )

    return "".join(pieces)
