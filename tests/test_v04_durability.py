"""v0.4 durability tests.

What these guard — the claims the durability layer makes:
  1. A run is persisted: its trace becomes a queryable event log, its step
     outputs are checkpointed, and its status is tracked.
  2. Resume-from-failure works: a run that crashes after step 1 does NOT
     re-run step 1 on resume — it reuses the checkpoint and continues.
  3. A completed run replays from the store without re-executing.

All offline: a scripted client lets one specific step raise on the first
attempt and succeed on the second, so resume is exercised deterministically
without a network or an API key.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # type: ignore  # noqa: E402

from threadlang.llm import DryRunClient  # noqa: E402
from threadlang.parser import parse_program  # noqa: E402
from threadlang.store import RunStore, run_durable  # noqa: E402


_TWO_STEP = """
thread Pipe {
  context { who = "tester" }
  steps {
    step a { llm "m1" { "A:" + inputs.x } }
    step b { llm "m2" { "B:" + steps.a.output } }
  }
  emit text { steps.b.output }
}
"""


class _FlakyClient:
    """A `complete` client that fails on step `b` the first time it is asked,
    then succeeds. Lets a test crash a run mid-pipeline and then resume it."""

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.fail_on_prompt_prefix = "B:"
        self._armed = True

    def complete(self, model: str, prompt: str) -> str:
        self.calls.append(prompt)
        if self._armed and prompt.startswith(self.fail_on_prompt_prefix):
            self._armed = False
            raise RuntimeError("simulated crash in step b")
        return f"[{model}] {prompt}"


def test_durable_run_persists_events_status_and_checkpoints(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_TWO_STEP)
    durable = run_durable(program, {"x": "hello"}, store, llm_client=DryRunClient())

    record = store.get_run(durable.run_id)
    assert record is not None
    assert record.status == "completed"
    assert record.output == durable.result.output
    # Both steps checkpointed.
    checkpoints = store.load_step_outputs(durable.run_id)
    assert set(checkpoints) == {"a", "b"}
    # The trace persisted as a queryable event log.
    events = store.load_events(durable.run_id)
    assert events, "events should have been persisted"
    assert [e.message for e in events] == [e.message for e in durable.result.trace]
    store.close()


def test_completion_persistence_failure_marks_run_failed(tmp_path: Path) -> None:
    class InvalidUnicodeClient:
        def complete(self, model: str, prompt: str) -> str:
            return "\ud800"

    source = 'thread T { context {} steps { step x { llm "m" { "prompt" } } } emit text { steps.x.output } }'
    store = RunStore(str(tmp_path / "runs.db"))

    with pytest.raises(UnicodeEncodeError):
        run_durable(parse_program(source), {}, store, llm_client=InvalidUnicodeClient())

    runs = store.list_runs()
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error is not None and "UnicodeEncodeError" in runs[0].error
    store.close()


def test_resume_skips_completed_step_after_crash(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_TWO_STEP)
    client = _FlakyClient()

    # First attempt: step a succeeds and is checkpointed; step b raises.
    with pytest.raises(Exception, match="simulated crash"):
        run_durable(program, {"x": "hello"}, store, llm_client=client)

    # Find the failed run (only one exists in this fresh store).
    runs = store.list_runs()
    assert len(runs) == 1
    failed_id = runs[0].id
    record = store.get_run(failed_id)
    assert record is not None and record.status == "failed"
    assert set(store.load_step_outputs(failed_id)) == {"a"}  # only a checkpointed

    a_calls_before = [c for c in client.calls if c.startswith("A:")]
    assert len(a_calls_before) == 1

    # Resume: step a must NOT re-run (reused from checkpoint); step b succeeds now.
    durable = run_durable(program, {"x": "hello"}, store, llm_client=client, run_id=failed_id)
    assert durable.run_id == failed_id
    assert store.get_run(failed_id).status == "completed"  # type: ignore[union-attr]
    assert set(store.load_step_outputs(failed_id)) == {"a", "b"}

    # The whole point: step a's model was never called a second time.
    a_calls_after = [c for c in client.calls if c.startswith("A:")]
    assert len(a_calls_after) == 1, "step a should have been skipped on resume, not re-run"

    # And the resume was recorded in the (continued) event log.
    messages = [e.message for e in store.load_events(failed_id)]
    assert any("resumed from checkpoint" in m for m in messages)
    store.close()


def test_completed_run_replays_without_reexecuting(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_TWO_STEP)
    client = _FlakyClient()  # armed to fail on B: — must NOT be called on replay

    first = run_durable(program, {"x": "hi"}, store, llm_client=DryRunClient())
    assert store.get_run(first.run_id).status == "completed"  # type: ignore[union-attr]

    # Replaying a completed run returns the stored result and runs no model calls.
    replay = run_durable(program, {"x": "hi"}, store, llm_client=client, run_id=first.run_id)
    assert replay.result.output == first.result.output
    assert client.calls == [], "a completed run must replay from the store, not re-execute"
    store.close()


def test_resume_rejects_program_or_input_identity_drift(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_TWO_STEP)
    completed = run_durable(program, {"x": "hi"}, store, llm_client=DryRunClient())

    changed_program = parse_program(_TWO_STEP.replace('"A:"', '"CHANGED:"'))
    with pytest.raises(ValueError, match="program source does not match"):
        run_durable(
            changed_program,
            {"x": "hi"},
            store,
            llm_client=DryRunClient(),
            run_id=completed.run_id,
        )
    with pytest.raises(ValueError, match="inputs do not match"):
        run_durable(
            program,
            {"x": "different"},
            store,
            llm_client=DryRunClient(),
            run_id=completed.run_id,
        )
    store.close()


def test_resume_rejects_an_already_active_run(tmp_path: Path) -> None:
    program = parse_program(_TWO_STEP)
    store = RunStore(str(tmp_path / "runs.db"))
    run_id = store.create_run(program.thread_name, {"x": "hello"})
    store.mark_running(run_id, expected="created")

    with pytest.raises(ValueError, match="already active"):
        run_durable(program, {"x": "hello"}, store, run_id=run_id)
    store.close()


def test_legacy_failed_run_without_source_identity_cannot_resume(tmp_path: Path) -> None:
    program = parse_program(_TWO_STEP)
    store = RunStore(str(tmp_path / "runs.db"))
    run_id = store.create_run(program.thread_name, {"x": "hello"})
    store.mark_running(run_id, expected="created")
    store.save_step_output(run_id, "first", "checkpoint")
    store.mark_failed(run_id, "legacy crash")

    with pytest.raises(ValueError, match="no verifiable program identity"):
        run_durable(program, {"x": "hello"}, store, run_id=run_id, source=_TWO_STEP)
    store.close()


def test_store_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    store.close()
