"""v0.10 controllability-probe tests.

What these guard:
  1. The report is a pure fold — same runs in, same report out, computed only
     from persisted run data (status, output, checkpoints, metrics).
  2. Routing-skipped steps count as absent, never as stable — a step no run
     reached reports runs=0, not a perfect mode_frequency.
  3. Failed runs are data (failure_rate), not exceptions that abort the probe.

All offline: variance across runs is manufactured with per-run scripted
clients; the dry-run path proves the zero-variance baseline.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.llm import DryRunClient  # noqa: E402
from threadlang.parser import parse_program  # noqa: E402
from threadlang.probe import ProbeRunData, probe_report  # noqa: E402
from threadlang.runtime import RuntimeError as TLRuntimeError  # noqa: E402
from threadlang.store import RunStore, run_durable  # noqa: E402

ROUTE_SOURCE = """
thread ProbeDemo {
  context {}
  steps {
    step classify {
      route "test-model" {
        "Handle: " + inputs.task
        on "math" -> solve
        on "writing" -> draft
      }
    }
    step solve {
      llm "test-model" {
        "Solve: " + inputs.task
        then -> end
      }
    }
    step draft {
      llm "test-model" { "Draft: " + inputs.task }
    }
  }
  emit text { steps.solve.output? + steps.draft.output? }
}
"""


class ScriptedClient:
    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)

    def complete(self, model: str, prompt: str) -> str:
        if not self._responses:
            raise ConnectionError("script exhausted")
        return self._responses.pop(0)


def _probe_store(tmp_path: Path, scripts: Sequence[Sequence[str]]) -> tuple:
    """Mimic the CLI probe loop: one durable run per script, failures kept as
    data, ProbeRunData assembled from the store."""
    program = parse_program(ROUTE_SOURCE)
    store = RunStore(str(tmp_path / "probe.db"))
    runs = []
    for script in scripts:
        run_id = store.create_run(program.thread_name, {"task": "x"})
        try:
            run_durable(
                program,
                {"task": "x"},
                store,
                llm_client=ScriptedClient(script),
                run_id=run_id,
            )
        except TLRuntimeError:
            pass
        record = store.get_run(run_id)
        runs.append(
            ProbeRunData(
                status=record.status,
                output=record.output,
                step_outputs=store.load_step_outputs(run_id),
                metrics=store.run_metrics(run_id),
            )
        )
    return program, runs


def test_probe_folds_mixed_runs(tmp_path: Path) -> None:
    program, runs = _probe_store(
        tmp_path,
        [
            ["math", "42"],  # → solve
            ["writing", "a poem"],  # → draft
            ["math", "42"],  # → solve, same output
            ["banana", "math", "43"],  # violation, retry matches → solve
            ["writing"],  # → draft, then the draft call dies
        ],
    )
    report = probe_report(program, runs)

    assert report.runs == 5
    assert report.completed == 4
    assert report.failed == 1
    assert report.failure_rate == 0.2
    assert report.route_violations == 1

    classify, solve, draft = report.steps
    assert classify.kind == "route"
    assert classify.runs == 5
    assert classify.label_counts == {"math": 3, "writing": 2}
    assert classify.mode_frequency == 0.6

    assert solve.kind == "llm"
    assert solve.runs == 3
    assert solve.distinct_outputs == 2  # "42" twice, "43" once
    assert solve.mode_frequency == 2 / 3
    assert solve.label_counts is None

    assert draft.runs == 1  # the failed run never checkpointed draft

    # Final outputs of completed runs: "42", "a poem", "42", "43".
    assert report.output_distinct == 3
    assert report.output_mode_frequency == 0.5


def test_probe_reports_unreached_step_as_absent_not_stable() -> None:
    program = parse_program(ROUTE_SOURCE)
    runs = [
        ProbeRunData(
            status="completed",
            output="42",
            step_outputs={"classify": "math", "solve": "42"},
            metrics=_metrics_stub(),
        )
    ]
    report = probe_report(program, runs)
    draft = report.steps[2]
    assert draft.runs == 0
    assert draft.distinct_outputs == 0
    assert draft.mode_frequency is None


def test_probe_dry_run_is_zero_variance(tmp_path: Path) -> None:
    program = parse_program(ROUTE_SOURCE)
    store = RunStore(str(tmp_path / "probe.db"))
    runs = []
    for _ in range(3):
        run_id = store.create_run(program.thread_name, {"task": "x"})
        run_durable(program, {"task": "x"}, store, llm_client=DryRunClient(), run_id=run_id)
        record = store.get_run(run_id)
        runs.append(
            ProbeRunData(
                status=record.status,
                output=record.output,
                step_outputs=store.load_step_outputs(run_id),
                metrics=store.run_metrics(run_id),
            )
        )
    report = probe_report(program, runs)
    assert report.failure_rate == 0.0
    assert report.output_distinct == 1
    assert report.output_mode_frequency == 1.0
    assert all(s.mode_frequency == 1.0 for s in report.steps if s.runs)


def test_probe_report_is_pure() -> None:
    program = parse_program(ROUTE_SOURCE)
    runs = [
        ProbeRunData(
            status="completed",
            output="42",
            step_outputs={"classify": "math", "solve": "42"},
            metrics=_metrics_stub(),
        )
    ] * 2
    assert probe_report(program, runs).to_dict() == probe_report(program, runs).to_dict()


def test_probe_report_empty_runs() -> None:
    program = parse_program(ROUTE_SOURCE)
    report = probe_report(program, [])
    assert report.runs == 0
    assert report.failure_rate is None
    assert report.output_mode_frequency is None


def _metrics_stub():
    from threadlang.metrics import compute_metrics

    return compute_metrics([], status="completed")
