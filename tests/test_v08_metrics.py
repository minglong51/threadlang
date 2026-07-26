"""v0.8 metrics tests.

Metrics are a *derived view* of the trace — a pure fold over the persisted
`TraceEvent` stream, never a separately-reported number. These guard that:

  1. Control-flow metrics are computed correctly from a known program's trace
     (steps, model calls, context vars) — deterministic, reproducible.
  2. Agent steps surface tool-call volume and tool errors.
  3. Token usage is summed from `data.usage` when present, and is `None`
     (not 0) when no event carried it — "no data" ≠ "zero".
  4. A durable run's metrics include wall-clock latency from event timestamps,
     and a resumed run counts its resumed steps.
  5. The aggregate rollup reports success rate, per-status and per-program
     breakdowns over many runs.
  6. The store migrates an older DB that predates the `events.ts` column.
  7. The dashboard renders the metric panels.

All offline — dry-run client, no network or key.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.dashboard import render_run_detail, render_run_list  # noqa: E402
from threadlang.llm import DryRunClient  # noqa: E402
from threadlang.metrics import (  # noqa: E402
    aggregate,
    compute_metrics,
    trace_span_ms,
)
from threadlang.parser import parse_program  # noqa: E402
from threadlang.runtime import run_program  # noqa: E402
from threadlang.store import RunStore, run_durable  # noqa: E402
from threadlang.trace import TraceEvent  # noqa: E402


_TWO_STEP = """
thread Pipe {
  context {
    who = "tester"
    tone = "plain"
  }
  steps {
    step a { llm "m1" { "A:" + inputs.x } }
    step b { llm "m2" { "B:" + steps.a.output } }
  }
  emit text { steps.b.output }
}
"""

_AGENT = """
thread Researcher {
  context { persona = "precise" }
  steps {
    step solve {
      agent "deepseek-chat" {
        tools [ echo, calculator ]
        max_iters 4
        "Task: " + inputs.task
      }
    }
  }
  emit text { steps.solve.output }
}
"""


def _run_trace(source: str, **inputs) -> List[TraceEvent]:
    program = parse_program(source)
    result = run_program(program, inputs=inputs, llm_client=DryRunClient())
    return result.trace


# ---- 1. control-flow metrics from a known trace ----


def test_control_flow_metrics_are_derived_from_trace() -> None:
    m = compute_metrics(_run_trace(_TWO_STEP, x="hi"), status="completed")
    assert m.context_vars == 2  # who, tone
    assert m.steps_completed == 2  # a, b
    assert m.model_calls == 2  # one complete() per step (emit text → no call)
    assert m.agent_steps == 0
    assert m.tool_calls == 0
    assert m.resumed_steps == 0
    assert m.ok is True


def test_emit_llm_counts_as_a_model_call() -> None:
    src = """
    thread E {
      context {}
      steps { step a { llm "m" { "x" } } }
      emit llm "m2" { steps.a.output }
    }
    """
    m = compute_metrics(_run_trace(src), status="completed")
    assert m.model_calls == 2  # step a + emit llm


# ---- 2. agent steps: tool volume + errors ----


def test_agent_step_metrics() -> None:
    m = compute_metrics(_run_trace(_AGENT, task="add"), status="completed")
    assert m.agent_steps == 1
    assert m.tool_calls == 1  # dry-run agent calls the first tool once
    assert m.tool_errors == 0
    assert m.agent_turns >= 2  # a tool-calling turn, then a finishing turn
    assert m.steps_completed == 1


def test_tool_errors_are_counted() -> None:
    trace = [
        TraceEvent("agent", "Agent step 's' started", {"step": "s"}),
        TraceEvent("agent", "Tool 'boom' called", {"tool": "boom", "result": "error: kaboom"}),
        TraceEvent("agent", "Agent 's' finished", {"step": "s"}),
    ]
    m = compute_metrics(trace)
    assert m.tool_calls == 1
    assert m.tool_errors == 1
    assert m.denials == 0


def test_denials_are_counted() -> None:
    trace = [
        TraceEvent("agent", "Agent step 's' started", {"step": "s"}),
        TraceEvent("denial", "Tool 'rogue' denied", {"tool": "rogue", "code": "tool-not-allowed"}),
        TraceEvent("agent", "Agent 's' finished", {"step": "s"}),
    ]
    m = compute_metrics(trace)
    assert m.denials == 1
    assert m.tool_calls == 0


# ---- 3. token usage: summed when present, None when absent ----


def test_tokens_none_when_no_usage() -> None:
    m = compute_metrics(_run_trace(_TWO_STEP, x="hi"))
    assert m.input_tokens is None
    assert m.output_tokens is None
    assert m.total_tokens is None


def test_tokens_summed_from_usage_events() -> None:
    trace = [
        TraceEvent(
            "step", "Calling LLM for step 'a'", {"usage": {"input_tokens": 10, "output_tokens": 4}}
        ),
        TraceEvent(
            "step",
            "Step 'a' produced output",
            {"step": "a", "usage": {"input_tokens": 2, "output_tokens": 1}},
        ),
    ]
    m = compute_metrics(trace)
    assert m.input_tokens == 12
    assert m.output_tokens == 5
    assert m.total_tokens == 17


# ---- 4. durable run: latency + resume ----


def test_durable_run_metrics_include_latency(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_TWO_STEP)
    durable = run_durable(program, {"x": "hi"}, store, llm_client=DryRunClient())
    m = store.run_metrics(durable.run_id)
    assert m is not None
    assert m.status == "completed"
    assert m.steps_completed == 2
    assert m.duration_ms is not None and m.duration_ms >= 0.0
    store.close()


def test_unknown_run_metrics_is_none(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    assert store.run_metrics("nope") is None
    store.close()


def test_resumed_steps_counted(tmp_path: Path) -> None:
    """A run that crashes after step a and resumes should count a as resumed."""

    class _Flaky:
        def __init__(self) -> None:
            self.fail_b = True

        def complete(self, model: str, prompt: str) -> str:
            if prompt.startswith("B:") and self.fail_b:
                self.fail_b = False
                raise RuntimeError("boom")
            return f"[{model}] {prompt}"

    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_TWO_STEP)
    client = _Flaky()
    run_id = store.create_run(program.thread_name, {"x": "hi"})
    try:
        run_durable(program, {"x": "hi"}, store, llm_client=client, run_id=run_id)
    except Exception:
        pass
    durable = run_durable(program, {"x": "hi"}, store, llm_client=client, run_id=run_id)
    assert durable.result.output  # completed on resume
    m = store.run_metrics(run_id)
    assert m is not None and m.resumed_steps >= 1
    store.close()


# ---- 5. aggregate rollup ----


def test_aggregate_rollup(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_TWO_STEP)
    for _ in range(3):
        run_durable(program, {"x": "ok"}, store, llm_client=DryRunClient())
    # one failure
    bad = store.create_run("Pipe", {"x": "bad"})
    store.mark_failed(bad, "deliberate")

    agg = store.aggregate_metrics()
    assert agg.total_runs == 4
    assert agg.by_status.get("completed") == 3
    assert agg.by_status.get("failed") == 1
    assert agg.success_rate == 0.75
    assert "Pipe" in agg.by_program
    assert agg.by_program["Pipe"]["runs"] == 4
    store.close()


def test_aggregate_empty_is_safe() -> None:
    agg = aggregate([])
    assert agg.total_runs == 0
    assert agg.success_rate is None
    assert agg.avg_duration_ms is None


# ---- 6. migration of a pre-v0.8 store ----


def test_migrates_store_without_ts_column(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    # Hand-build an events table WITHOUT the ts column, as a pre-v0.8 store had.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE runs (id TEXT PRIMARY KEY, program_name TEXT, status TEXT,
          inputs_json TEXT, source TEXT, output TEXT, error TEXT,
          created_at TEXT, updated_at TEXT);
        CREATE TABLE events (run_id TEXT, seq INTEGER, phase TEXT, message TEXT,
          data_json TEXT, PRIMARY KEY (run_id, seq));
        CREATE TABLE step_outputs (run_id TEXT, step_name TEXT, output TEXT,
          PRIMARY KEY (run_id, step_name));
        """
    )
    conn.close()

    # Opening it should add the ts column and a fresh run should work end-to-end.
    store = RunStore(str(db))
    cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(events)").fetchall()}
    assert "ts" in cols
    durable = run_durable(parse_program(_TWO_STEP), {"x": "y"}, store, llm_client=DryRunClient())
    assert store.run_metrics(durable.run_id) is not None
    store.close()


# ---- 7. trace_span_ms edge cases ----


def test_trace_span_needs_two_timestamps() -> None:
    assert trace_span_ms([]) is None
    assert trace_span_ms(["2026-06-06T00:00:00+00:00"]) is None
    assert trace_span_ms([None, None]) is None
    span = trace_span_ms(["2026-06-06T00:00:00+00:00", "2026-06-06T00:00:01+00:00"])
    assert span is not None and abs(span - 1000.0) < 1.0


# ---- 8. dashboard renders the metric panels ----


def test_dashboard_renders_run_metrics(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    durable = run_durable(parse_program(_TWO_STEP), {"x": "hi"}, store, llm_client=DryRunClient())
    record = store.get_run(durable.run_id)
    html = render_run_detail(
        record, store.load_events(durable.run_id), store.run_metrics(durable.run_id)
    )
    assert "class='metrics'" in html
    assert "steps" in html
    # aggregate panel on the run list
    list_html = render_run_list(store.list_runs(), store.aggregate_metrics())
    assert "success" in list_html
    store.close()


def test_run_detail_metrics_optional_without_store() -> None:
    """render_run_detail still works with metrics derived from events alone."""
    from threadlang.store import RunRecord

    rec = RunRecord(
        id="x",
        program_name="P",
        status="completed",
        inputs={},
        output="o",
        error=None,
        source=None,
    )
    events = [TraceEvent("step", "Step 'a' produced output", {"step": "a"})]
    html = render_run_detail(rec, events)  # no metrics arg
    assert "class='metrics'" in html
