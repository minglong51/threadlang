"""Runtime execution for ThreadLang AST programs.

Execution order:
    1. Build context from context block (deterministic).
    2. Traverse the step graph starting at the first declared step. Each step
       renders its prompt by evaluating expression terms against (context,
       inputs, prior step outputs), calls the LLM client with (model, prompt),
       and binds the response to `steps.<name>.output`. The next step is the
       current step's outgoing edge: a `route` step's chosen arm, a `then ->`
       target, or fall-through to the next declared step. Edges are forward-
       only (parser-enforced), so every step runs at most once.
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
from typing import Callable, Dict, Mapping, Optional

from .ast import (
    END_TARGET,
    AgentStep,
    ContextRef,
    EmitBlock,
    Expression,
    InputsRef,
    Program,
    RouteStep,
    Step,
    StepsRef,
    StringLiteral,
)
from .llm import DryRunClient, LLMClient, Message
from .tools import ToolRegistry, default_registry
from .trace import DenialCode, Trace, TraceEvent


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
    *,
    trace: Optional[Trace] = None,
    resume_outputs: Optional[Mapping[str, str]] = None,
    on_step_complete: Optional[Callable[[str, str], None]] = None,
) -> RuntimeResult:
    """Execute a ThreadLang program.

    `llm_client` is required if the program contains any `steps` or uses
    `emit llm`. If omitted, a `DryRunClient` is used — fine for tests, but
    callers wiring a real workflow should pass an `AnthropicClient` (or
    any object satisfying the `LLMClient` protocol).

    `tools` is the registry an `agent` step draws from; if omitted, the
    deterministic built-ins (`default_registry()`) are used.

    Durability hooks (used by `store.run_durable`, ignored otherwise; the
    runtime stays storage-agnostic):

    - `trace` — supply a Trace object to append into (e.g. a write-through
      trace that persists each event). Defaults to a fresh list.
    - `resume_outputs` — step outputs already completed in a prior attempt;
      these steps are skipped and their stored output reused.
    - `on_step_complete(name, output)` — called after each freshly-run step,
      so a caller can checkpoint it.
    """
    trace = trace if trace is not None else []
    context = _build_context(program, trace)

    client = llm_client or DryRunClient()
    registry = tools or default_registry()
    step_outputs = _run_steps(
        program, context, inputs, client, registry, trace, resume_outputs, on_step_complete
    )

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
    resume_outputs: Optional[Mapping[str, str]] = None,
    on_step_complete: Optional[Callable[[str, str], None]] = None,
) -> Dict[str, str]:
    steps = program.steps.steps
    index_by_name = {step.name: i for i, step in enumerate(steps)}
    step_outputs: Dict[str, str] = {}
    i = 0
    while i < len(steps):
        step = steps[i]
        resumed = resume_outputs is not None and step.name in resume_outputs
        if resumed:
            # This step finished in a prior attempt and was checkpointed; reuse
            # its output instead of re-running it. The skip is itself traced.
            # A resumed route step re-derives its jump from the stored label
            # below — deterministically, with no model call.
            output = resume_outputs[step.name]
            trace.append(
                TraceEvent(
                    phase="route" if isinstance(step, RouteStep) else "step",
                    message=f"Step '{step.name}' resumed from checkpoint",
                    data={"step": step.name, "output": output, "resumed": True},
                )
            )
            step_outputs[step.name] = output
        elif isinstance(step, RouteStep):
            output = _run_route_step(step, context, inputs, step_outputs, client, trace)
            step_outputs[step.name] = output
        elif isinstance(step, AgentStep):
            output = _run_agent_step(
                step, context, inputs, step_outputs, client, registry, trace
            )
            step_outputs[step.name] = output
        else:
            output = _run_llm_step(step, context, inputs, step_outputs, client, trace)
            step_outputs[step.name] = output
        if not resumed and on_step_complete is not None:
            on_step_complete(step.name, output)

        # Follow the step's outgoing edge. Arm dispatch is deterministic code:
        # the model only picked the label, the transition is decided here.
        if isinstance(step, RouteStep):
            target = _route_target(step, output)
            if target is None:
                raise RuntimeError(
                    f"route step '{step.name}' produced '{output}', which matches "
                    f"no arm, and no else -> edge is defined"
                )
            trace.append(
                TraceEvent(
                    phase="route",
                    message=f"Route step '{step.name}' chose '{output}'",
                    data={
                        "step": step.name,
                        "label": output,
                        "target": target,
                        "resumed": resumed,
                    },
                )
            )
        else:
            target = step.next_target
        if target is None:
            i += 1
        elif target == END_TARGET:
            break
        else:
            i = index_by_name[target]
    return step_outputs


def _route_target(step: RouteStep, label: str) -> Optional[str]:
    """Resolve a route step's output to its jump target: the matching arm, or
    `else_target` when no arm matches (a contract violation that exhausted its
    retry stores the raw normalized reply as the step output)."""
    for arm in step.arms:
        if arm.label == label:
            return arm.target
    return step.else_target


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


def _normalize_route_output(response: str, labels: Mapping[str, str]) -> Optional[str]:
    """Match a model reply against the arm labels: strip whitespace and
    surrounding quote/punctuation noise, compare case-insensitively, return the
    canonical label. Strict beyond that — no substring matching — so the output
    contract stays a contract."""
    cleaned = response.strip().strip("\"'`.,:;!").strip().casefold()
    return labels.get(cleaned)


def _run_route_step(
    step: RouteStep,
    context: Mapping[str, str],
    inputs: Mapping[str, str],
    step_outputs: Mapping[str, str],
    client: LLMClient,
    trace: Trace,
) -> str:
    """Run a route step's model call under its output contract: the reply must
    be exactly one of the arm labels. The contract is rendered into the prompt
    from the arms themselves. A non-matching reply is rejected (traced) and
    retried once with the violation fed back; a second miss returns the
    stripped raw reply, which the caller resolves through the `else ->` edge
    or fails loud. A client exposing `.route(model, prompt, options)` is called
    through it (the dry-run client picks the first arm deterministically);
    otherwise the plain `complete` path is used."""
    labels = [arm.label for arm in step.arms]
    by_normalized = {label.casefold(): label for label in labels}
    contract = (
        "Reply with exactly one of: "
        + ", ".join(labels)
        + ". Output only the label — no other text."
    )
    prompt = _render_expression(step.prompt, context, inputs, step_outputs)
    full_prompt = f"{prompt}\n\n{contract}"

    route_fn = getattr(client, "route", None)

    def ask(p: str, attempt: int) -> str:
        trace.append(
            TraceEvent(
                phase="route",
                message=f"Calling LLM for route step '{step.name}'",
                data={
                    "step": step.name,
                    "model": step.model,
                    "prompt": p,
                    "labels": labels,
                    "attempt": attempt,
                },
            )
        )
        try:
            if route_fn is not None:
                return route_fn(model=step.model, prompt=p, options=labels)
            return client.complete(model=step.model, prompt=p)
        except Exception as exc:
            raise RuntimeError(
                f"LLM call failed in route step '{step.name}': {type(exc).__name__}: {exc}"
            ) from exc

    response = ask(full_prompt, attempt=1)
    label = _normalize_route_output(response, by_normalized)
    if label is not None:
        return label

    trace.append(
        TraceEvent(
            phase="route",
            message=f"Route step '{step.name}' output rejected",
            data={"step": step.name, "response": response, "labels": labels, "attempt": 1},
        )
    )
    retry_prompt = (
        f"{full_prompt}\n\nYour previous reply was not one of the allowed labels. "
        + contract
    )
    response = ask(retry_prompt, attempt=2)
    label = _normalize_route_output(response, by_normalized)
    if label is not None:
        return label

    trace.append(
        TraceEvent(
            phase="route",
            message=f"Route step '{step.name}' output rejected",
            data={"step": step.name, "response": response, "labels": labels, "attempt": 2},
        )
    )
    return response.strip()


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
            else:
                code = (
                    DenialCode.TOOL_NOT_ALLOWED
                    if call.name not in allowed
                    else DenialCode.TOOL_NOT_REGISTERED
                )
                result = f"error: {code.value}: tool '{call.name}' is not available to this agent"
                trace.append(
                    TraceEvent(
                        phase="denial",
                        message=f"Tool '{call.name}' denied",
                        data={
                            "step": step.name,
                            "tool": call.name,
                            "arguments": call.arguments,
                            "code": code.value,
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
                if not term.optional:
                    raise RuntimeError(
                        f"Reference to step '{term.step_name}' before it ran "
                        f"(a step skipped by routing renders as \"\" only via "
                        f"steps.{term.step_name}.output?)"
                    )
                value = ""
            else:
                value = step_outputs[term.step_name]
            source = f"steps.{term.step_name}.output" + ("?" if term.optional else "")
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
