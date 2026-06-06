"""Control plane (L4) — a worker pool draining a durable run queue.

L3 made a run a durable record with an id and a status. L4 separates
*submitting* a run from *executing* it:

- A run is `enqueue`d into the store as `pending` (program source + inputs) —
  the `pending` rows in the `runs` table *are* the queue. No extra broker.
- A pool of workers each claim the next pending run (atomically, so no run
  runs twice), parse its source, and execute it via `run_durable`.

Because `run_durable` is crash-safe and its resume is idempotent, a worker
that dies mid-run loses nothing: the run is left `running` with its completed
steps checkpointed, and re-dispatching the same id resumes it. The durability
layer is exactly what makes the worker pool safe — that is why L4 sits on L3.

The pure function `process_one` (claim + run exactly one) is the unit the pool
is built from; it is fully testable without threads or sockets.
"""

from __future__ import annotations

import threading
from typing import Optional

from .llm import LLMClient
from .parser import parse_program
from .store import DurableRun, RunStore, run_durable
from .tools import ToolRegistry


def process_one(
    store: RunStore,
    *,
    llm_client: Optional[LLMClient] = None,
    tools: Optional[ToolRegistry] = None,
) -> Optional[DurableRun]:
    """Claim the next pending run and execute it. Returns the `DurableRun`, or
    None if the queue was empty. A run that raises is already marked `failed`
    by `run_durable` and the exception is swallowed here — one bad run must
    never take down a worker."""
    claimed = store.claim_next_pending()
    if claimed is None:
        return None
    assert claimed.source is not None, "enqueued runs always carry their source"
    program = parse_program(claimed.source)
    try:
        return run_durable(
            program,
            claimed.inputs,
            store,
            llm_client=llm_client,
            tools=tools,
            run_id=claimed.id,
        )
    except Exception:
        # run_durable has marked the run failed and persisted the error; keep
        # the worker alive to take the next run.
        return None


class WorkerPool:
    """A fixed pool of threads, each draining pending runs from the store.

    Each worker opens its *own* `RunStore` (sqlite connections are not shared
    across threads); the atomic claim in `claim_next_pending` keeps them from
    stepping on each other. `llm_client` is shared — pass a thread-safe client
    (the built-in HTTP clients are; they hold no mutable per-call state).
    """

    def __init__(
        self,
        store_path: str,
        *,
        n_workers: int = 2,
        llm_client: Optional[LLMClient] = None,
        tools: Optional[ToolRegistry] = None,
        poll_interval: float = 0.05,
    ) -> None:
        self._store_path = store_path
        self._n_workers = n_workers
        self._llm_client = llm_client
        self._tools = tools
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for i in range(self._n_workers):
            t = threading.Thread(target=self._loop, name=f"tl-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def _loop(self) -> None:
        store = RunStore(self._store_path)
        try:
            while not self._stop.is_set():
                durable = process_one(
                    store, llm_client=self._llm_client, tools=self._tools
                )
                if durable is None:
                    # Queue empty — wait briefly (interruptible) before polling.
                    self._stop.wait(self._poll_interval)
        finally:
            store.close()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads.clear()

    def drain(self, store: RunStore, *, max_runs: int = 10_000) -> int:
        """Synchronously process pending runs until the queue is empty (or
        `max_runs` is hit), in the current thread. Useful for tests and for a
        one-shot batch mode without spinning up threads. Returns how many runs
        were processed."""
        count = 0
        while count < max_runs:
            durable = process_one(store, llm_client=self._llm_client, tools=self._tools)
            if durable is None:
                break
            count += 1
        return count
