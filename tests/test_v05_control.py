"""v0.5 control-plane tests.

What these guard:
  1. The queue: an enqueued run is `pending`; a worker claims it, runs it via
     the durable path, and it ends `completed`.
  2. Atomicity under concurrency: with several workers draining the same store,
     no run is executed twice (proven by a per-run call counter).
  3. A failing run is recorded `failed` and the worker survives to take the next.

The threaded test uses a real `WorkerPool`; everything else is synchronous via
`process_one` and `drain`. No sockets, no network — a `DryRunClient` (or a
small counting client) stands in for the model.
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # type: ignore  # noqa: E402

from threadlang.control import WorkerPool, process_one  # noqa: E402
from threadlang.llm import DryRunClient  # noqa: E402
from threadlang.store import RunStore  # noqa: E402


_ONE_STEP = """
thread Q {
  context { c = "x" }
  steps { step a { llm "m" { "run:" + inputs.x } } }
  emit text { steps.a.output }
}
"""


def test_enqueue_then_process_one_completes(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "q.db"))
    run_id = store.enqueue_run("Q", _ONE_STEP, {"x": "1"})
    assert store.get_run(run_id).status == "pending"  # type: ignore[union-attr]

    durable = process_one(store, llm_client=DryRunClient())
    assert durable is not None and durable.run_id == run_id
    record = store.get_run(run_id)
    assert record is not None and record.status == "completed"
    assert "run:1" in record.output  # type: ignore[operator]
    store.close()


def test_process_one_on_empty_queue_returns_none(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "q.db"))
    assert process_one(store, llm_client=DryRunClient()) is None
    store.close()


def test_claim_is_atomic_no_double_claim(tmp_path: Path) -> None:
    # Two independent connections, one pending run: exactly one claim succeeds.
    path = str(tmp_path / "q.db")
    a = RunStore(path)
    b = RunStore(path)
    a.enqueue_run("Q", _ONE_STEP, {"x": "1"})
    first = a.claim_next_pending()
    second = b.claim_next_pending()
    assert first is not None and first.source is not None
    assert second is None, "a second claim must not see the already-claimed run"
    a.close()
    b.close()


class _CountingClient:
    """Counts how many times each distinct prompt is completed — so a run that
    executed twice would show a count of 2 for its prompt."""

    def __init__(self) -> None:
        self.counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    def complete(self, model: str, prompt: str) -> str:
        with self._lock:
            self.counts[prompt] = self.counts.get(prompt, 0) + 1
        return f"[{model}] {prompt}"


def test_concurrent_workers_execute_each_run_once(tmp_path: Path) -> None:
    path = str(tmp_path / "q.db")
    store = RunStore(path)
    n_runs = 8
    ids: List[str] = [store.enqueue_run("Q", _ONE_STEP, {"x": str(i)}) for i in range(n_runs)]

    client = _CountingClient()
    pool = WorkerPool(path, n_workers=4, llm_client=client, poll_interval=0.01)
    pool.start()
    try:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if all(store.get_run(i).status == "completed" for i in ids):  # type: ignore[union-attr]
                break
            time.sleep(0.02)
    finally:
        pool.stop()

    statuses = [store.get_run(i).status for i in ids]  # type: ignore[union-attr]
    assert statuses == ["completed"] * n_runs, statuses
    # The proof: every run's single prompt was completed exactly once.
    for i in range(n_runs):
        assert client.counts.get(f"run:{i}") == 1, (i, client.counts)
    store.close()


class _AlwaysFails:
    def complete(self, model: str, prompt: str) -> str:
        raise RuntimeError("boom")


def test_failed_run_is_recorded_and_worker_survives(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "q.db"))
    bad = store.enqueue_run("Q", _ONE_STEP, {"x": "1"})

    # process_one swallows the failure (returns None) but records it.
    assert process_one(store, llm_client=_AlwaysFails()) is None
    record = store.get_run(bad)
    assert record is not None and record.status == "failed"
    assert "boom" in (record.error or "")

    # A failed run is no longer pending, so the queue is empty and a worker
    # would simply move on.
    assert process_one(store, llm_client=DryRunClient()) is None
    store.close()
