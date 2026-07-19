"""Run metrics (L7) — metrics are a *derived view* of the trace, never a
separately-reported number.

ThreadLang's founding bet is that every run is a replayable, inspectable
trace. This module follows that bet to its conclusion: a metric is a **pure
function of the durable trace**, computed by walking the `TraceEvent` stream —
not something the runtime or an agent emits imperatively on the side.

Why derive instead of report? A derived metric *cannot drift* from what
actually happened: it is recomputed from the same events the dashboard renders,
so "8 tool calls" on the metrics view and the eight tool-call events on the
timeline are the same fact. It is also retroactive — add a new metric here and
every historical run gains it for free, because the source events were always
recorded. Imperative `metric.emit("tool_calls", n)` calls scattered through the
runtime would rot and lie; a fold over the trace can't.

Two kinds of metric, kept deliberately separate on `RunMetrics`:

- **Deterministic** — pure functions of control flow (how many steps ran, how
  many tools were called, did a step resume from a checkpoint). Given the same
  inputs and the same model responses, these are reproducible run to run.
- **Observational** — depend on the wall clock or the model itself (latency,
  token counts). Not reproducible; recorded for monitoring, but never mixed
  into the deterministic core so wall-clock noise can't contaminate a metric
  that is supposed to be exact.

Token usage is read from any event whose `data` carries a `usage`
`{input_tokens, output_tokens}` dict. The built-in clients do not emit usage
yet (capturing it cleanly requires evolving the `LLMClient` return shape
without breaking the worker pool's shared-client thread-safety contract); this
fold is ready for the day they do, and reports `None` until then — "no data",
distinct from a real zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .trace import TraceEvent

_ACTIVE = ("pending", "running")


@dataclass(frozen=True)
class RunMetrics:
    """A single run's metrics, derived by folding its trace.

    The first block is deterministic (control flow); the second is
    observational (wall-clock / model-dependent). `None` in an observational
    field means the underlying signal was never recorded, not that it was zero.
    """

    # ── deterministic: pure functions of control flow (reproducible) ──
    context_vars: int
    steps_completed: int
    agent_steps: int
    agent_turns: int
    model_calls: int  # complete() + agent_step() + route model invocations
    tool_calls: int
    tool_errors: int
    denials: int
    resumed_steps: int
    route_steps: int  # routing decisions taken (one per executed route step)
    route_violations: int  # route replies rejected by the output contract
    status: Optional[str]

    # ── observational: depend on wall-clock / the model (not reproducible) ──
    duration_ms: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    @property
    def total_tokens(self) -> Optional[int]:
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    def to_dict(self) -> Dict[str, object]:
        return {
            "deterministic": {
                "context_vars": self.context_vars,
                "steps_completed": self.steps_completed,
                "agent_steps": self.agent_steps,
                "agent_turns": self.agent_turns,
                "model_calls": self.model_calls,
                "tool_calls": self.tool_calls,
                "tool_errors": self.tool_errors,
                "denials": self.denials,
                "resumed_steps": self.resumed_steps,
                "route_steps": self.route_steps,
                "route_violations": self.route_violations,
                "status": self.status,
            },
            "observational": {
                "duration_ms": self.duration_ms,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "total_tokens": self.total_tokens,
            },
        }


def compute_metrics(
    trace: Sequence[TraceEvent],
    *,
    status: Optional[str] = None,
    duration_ms: Optional[float] = None,
) -> RunMetrics:
    """Fold a trace into a `RunMetrics`. Pure: same trace in → same metrics out
    (modulo the observational `status`/`duration_ms` the caller supplies, which
    the trace doesn't carry). The event shapes folded here are exactly those
    `runtime.run_program` appends, so this stays in lockstep with execution."""
    context_vars = 0
    llm_calls = 0  # complete() calls (plain `llm` steps + `emit llm`)
    agent_steps = 0
    agent_turns = 0  # agent_step() model invocations inside tool-use loops
    tool_calls = 0
    tool_errors = 0
    denials = 0
    resumed_steps = 0
    route_steps = 0
    route_violations = 0
    completed_steps: set = set()
    in_tok = 0
    out_tok = 0
    saw_usage = False

    for event in trace:
        data: Mapping[str, object] = event.data or {}
        phase, message = event.phase, event.message

        if phase == "context" and message == "Context assignment":
            context_vars += 1
        elif phase == "step":
            if message.startswith("Calling LLM for step"):
                llm_calls += 1
            elif "produced output" in message:
                completed_steps.add(data.get("step"))
            elif "resumed from checkpoint" in message:
                resumed_steps += 1
                completed_steps.add(data.get("step"))
        elif phase == "agent":
            if message.endswith("started"):
                agent_steps += 1
            elif " turn " in message:
                agent_turns += 1
            elif message.startswith("Tool '"):
                tool_calls += 1
                if str(data.get("result", "")).startswith("error:"):
                    tool_errors += 1
            elif message.endswith("finished"):
                completed_steps.add(data.get("step"))
        elif phase == "route":
            if message.startswith("Calling LLM for route step"):
                llm_calls += 1
            elif "output rejected" in message:
                route_violations += 1
            elif " chose " in message:
                route_steps += 1
                completed_steps.add(data.get("step"))
            elif "resumed from checkpoint" in message:
                resumed_steps += 1
                completed_steps.add(data.get("step"))
        elif phase == "denial":
            denials += 1
        elif phase == "emit" and message == "Calling LLM for emit":
            llm_calls += 1

        usage = data.get("usage")
        if isinstance(usage, Mapping):
            saw_usage = True
            in_tok += int(usage.get("input_tokens", 0) or 0)
            out_tok += int(usage.get("output_tokens", 0) or 0)

    return RunMetrics(
        context_vars=context_vars,
        steps_completed=len(completed_steps),
        agent_steps=agent_steps,
        agent_turns=agent_turns,
        model_calls=llm_calls + agent_turns,
        tool_calls=tool_calls,
        tool_errors=tool_errors,
        denials=denials,
        resumed_steps=resumed_steps,
        route_steps=route_steps,
        route_violations=route_violations,
        status=status,
        duration_ms=duration_ms,
        input_tokens=in_tok if saw_usage else None,
        output_tokens=out_tok if saw_usage else None,
    )


def trace_span_ms(timestamps: Sequence[Optional[str]]) -> Optional[float]:
    """Observational duration = wall-clock span between the first and last
    event timestamp. Returns None if fewer than two parseable ISO timestamps
    are present (e.g. an old run persisted before the `ts` column existed)."""
    parsed: List[datetime] = []
    for ts in timestamps:
        if not ts:
            continue
        try:
            parsed.append(datetime.fromisoformat(ts))
        except ValueError:
            continue
    if len(parsed) < 2:
        return None
    return (max(parsed) - min(parsed)).total_seconds() * 1000.0


@dataclass(frozen=True)
class AggregateMetrics:
    """Fleet-of-runs rollup: the monitoring view over many runs. Built by
    folding per-run `RunMetrics`, so it inherits their derived-not-reported
    guarantee."""

    total_runs: int
    by_status: Dict[str, int]
    success_rate: Optional[float]  # completed / (completed + failed); None if neither
    avg_duration_ms: Optional[float]
    total_model_calls: int
    total_tool_calls: int
    total_tool_errors: int
    total_denials: int
    by_program: Dict[str, Dict[str, object]]

    def to_dict(self) -> Dict[str, object]:
        return {
            "total_runs": self.total_runs,
            "by_status": self.by_status,
            "success_rate": self.success_rate,
            "avg_duration_ms": self.avg_duration_ms,
            "total_model_calls": self.total_model_calls,
            "total_tool_calls": self.total_tool_calls,
            "total_tool_errors": self.total_tool_errors,
            "total_denials": self.total_denials,
            "by_program": self.by_program,
        }


def aggregate(items: Sequence[Tuple[str, RunMetrics]]) -> AggregateMetrics:
    """Roll up `(program_name, RunMetrics)` pairs into an `AggregateMetrics`.
    Success rate counts only terminal runs (completed / failed); `pending` and
    `running` are excluded from the rate but still counted in `by_status`."""
    by_status: Dict[str, int] = {}
    durations: List[float] = []
    total_model_calls = 0
    total_tool_calls = 0
    total_tool_errors = 0
    total_denials = 0
    # program -> mutable accumulator
    progs: Dict[str, Dict[str, object]] = {}

    completed = failed = 0
    for program_name, m in items:
        status = m.status or "unknown"
        by_status[status] = by_status.get(status, 0) + 1
        if status == "completed":
            completed += 1
        elif status == "failed":
            failed += 1
        if m.duration_ms is not None:
            durations.append(m.duration_ms)
        total_model_calls += m.model_calls
        total_tool_calls += m.tool_calls
        total_tool_errors += m.tool_errors
        total_denials += m.denials

        p = progs.setdefault(
            program_name,
            {"runs": 0, "completed": 0, "failed": 0, "_durations": []},
        )
        p["runs"] = int(p["runs"]) + 1  # type: ignore[arg-type]
        if status == "completed":
            p["completed"] = int(p["completed"]) + 1  # type: ignore[arg-type]
        elif status == "failed":
            p["failed"] = int(p["failed"]) + 1  # type: ignore[arg-type]
        if m.duration_ms is not None:
            p["_durations"].append(m.duration_ms)  # type: ignore[union-attr]

    by_program: Dict[str, Dict[str, object]] = {}
    for name, p in progs.items():
        c, f = int(p["completed"]), int(p["failed"])  # type: ignore[arg-type]
        terminal = c + f
        ds: List[float] = p.pop("_durations")  # type: ignore[assignment]
        by_program[name] = {
            "runs": p["runs"],
            "completed": c,
            "failed": f,
            "success_rate": (c / terminal) if terminal else None,
            "avg_duration_ms": (sum(ds) / len(ds)) if ds else None,
        }

    terminal_total = completed + failed
    return AggregateMetrics(
        total_runs=len(items),
        by_status=by_status,
        success_rate=(completed / terminal_total) if terminal_total else None,
        avg_duration_ms=(sum(durations) / len(durations)) if durations else None,
        total_model_calls=total_model_calls,
        total_tool_calls=total_tool_calls,
        total_tool_errors=total_tool_errors,
        total_denials=total_denials,
        by_program=by_program,
    )
