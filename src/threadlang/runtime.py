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
    AgentStep,
    ContextRef,
    EmitBlock,
    Expression,
    InputsRef,
    Program,
    Step,
    StepsRef,
    StringLiteral,
)
from .llm import DryRunClient, LLMClient, Message
from .tools import ToolRegistry, default_registry
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
    tools: Optional[ToolRegistry] = None,
) -> RuntimeResult:
    """Execute a ThreadLang program.

    `llm_client` is required if the program contains any `steps` or uses
    `emit llm`. If omitted, a `DryRunClient` is used — fine for tests, but
    callers wiring a real workflow should pass an `AnthropicClient` (or
    any object satisfying the `LLMClient` protocol).

    `tools` is the registry an `agent` step draws from; if omitted, the
    deterministic built-ins (`default_registry()`) are used.
    """
    trace: Trace = []
    context = _build_context(program, trace)

    client = llm_client or DryRunClient()
    registry = tools or default_registry()
    step_outputs = _run_steps(program, context, inputs, client, registry, trace)

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
    registry: ToolRegistry,
    trace: Trace,
) -> Dict[str, str]:
    step_outputs: Dict[str, str] = {}
    for step in program.steps.steps:
        if isinstance(step, AgentStep):
            output = _run_agent_step(
                step, context, inputs, step_outputs, client, registry, trace
            )
        else:
            output = _run_llm_step(step, context, inputs, step_outputs, client, trace)
        step_outputs[step.name] = output
    return step_outputs


def _run_llm_step(
    step: Step,
    context: Mapping[str, str],
    inputs: Mapping[str, str],
    step_outputs: Mapping[str, str],
    client: LLMClient,
    trace: Trace,
) -> str:
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
    trace.append(
        TraceEvent(
            phase="step",
            message=f"Step '{step.name}' produced output",
            data={"step": step.name, "output": response},
        )
    )
    return response


def _run_agent_step(
    step: AgentStep,
    context: Mapping[str, str],
    inputs: Mapping[str, str],
    step_outputs: Mapping[str, str],
    client: LLMClient,
    registry: ToolRegistry,
    trace: Trace,
) -> str:
    """Run a tool-use loop: render the opening prompt, then call the model up
    to `max_iters` times, executing every tool the model asks for and feeding
    the result back, until the model returns a tool-free final answer. Every
    model turn, every tool call, and every tool result is a TraceEvent — the
    loop is fully reconstructible from the trace."""
    agent_step = getattr(client, "agent_step", None)
    if agent_step is None:
        raise RuntimeError(
            f"agent step '{step.name}' needs an agent-capable client "
            f"(got {type(client).__name__}, which only does .complete)"
        )

    for tool_name in step.tools:
        if not registry.has(tool_name):
            raise RuntimeError(
                f"agent step '{step.name}' references unknown tool: {tool_name}"
            )
    specs = registry.specs(list(step.tools))
    allowed = set(step.tools)

    prompt = _render_expression(step.prompt, context, inputs, step_outputs)
    messages: list[Message] = [{"role": "user", "content": prompt}]
    trace.append(
        TraceEvent(
            phase="agent",
            message=f"Agent step '{step.name}' started",
            data={
                "step": step.name,
                "model": step.model,
                "prompt": prompt,
                "tools": list(step.tools),
                "max_iters": step.max_iters,
            },
        )
    )

    for turn in range(step.max_iters):
        try:
            response = agent_step(model=step.model, messages=messages, tools=specs)
        except Exception as exc:
            raise RuntimeError(
                f"LLM call failed in agent step '{step.name}': {type(exc).__name__}: {exc}"
            ) from exc

        trace.append(
            TraceEvent(
                phase="agent",
                message=f"Agent '{step.name}' turn {turn}",
                data={
                    "step": step.name,
                    "turn": turn,
                    "text": response.text,
                    "tool_calls": [
                        {"name": c.name, "arguments": c.arguments}
                        for c in response.tool_calls
                    ],
                },
            )
        )

        if not response.tool_calls:
            trace.append(
                TraceEvent(
                    phase="agent",
                    message=f"Agent '{step.name}' finished",
                    data={"step": step.name, "turns": turn + 1, "output": response.text},
                )
            )
            return response.text

        messages.append(
            {"role": "assistant", "text": response.text, "tool_calls": response.tool_calls}
        )
        for call in response.tool_calls:
            if call.name in allowed and registry.has(call.name):
                try:
                    result = registry.get(call.name).run(call.arguments)
                except Exception as exc:
                    result = f"error: {type(exc).__name__}: {exc}"
            else:
                result = f"error: tool '{call.name}' is not available to this agent"
            trace.append(
                TraceEvent(
                    phase="agent",
                    message=f"Tool '{call.name}' called",
                    data={
                        "step": step.name,
                        "tool": call.name,
                        "arguments": call.arguments,
                        "result": result,
                    },
                )
            )
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result}
            )

    raise RuntimeError(
        f"agent step '{step.name}' exceeded max_iters ({step.max_iters}) without a final answer"
    )


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
