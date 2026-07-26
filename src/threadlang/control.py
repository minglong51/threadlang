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

import fcntl
import json
import sys
import threading
from typing import IO, Optional

from .ir import load_ir_bytes, program_from_ir
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
    if claimed.source is None and claimed.definition_json is None:
        store.mark_failed(claimed.id, "InvariantError: enqueued run has no workflow definition")
        return None
    try:
        if claimed.definition_json is not None:
            program = program_from_ir(load_ir_bytes(claimed.definition_json.encode("utf-8")))
        else:
            source = claimed.source
            if source is None:
                raise RuntimeError("enqueued run has no workflow definition")
            program = parse_program(source)
        return run_durable(
            program,
            claimed.inputs,
            store,
            llm_client=llm_client,
            tools=tools,
            run_id=claimed.id,
            source=claimed.source,
            claimed=True,
        )
    except Exception as exc:
        # Parse failures happen before run_durable can record them. Runtime
        # failures are already marked failed; updating again is harmless and
        # guarantees malformed persisted source cannot kill a worker.
        store.mark_failed(claimed.id, f"{type(exc).__name__}: {str(exc)[:1000]}")
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
        self._lock_file: Optional[IO[str]] = None

    def _acquire_store_lock(self) -> None:
        lock_file = open(f"{self._store_path}.worker.lock", "a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lock_file.close()
            raise RuntimeError("another ThreadLang worker pool owns this store") from exc
        self._lock_file = lock_file

    def _release_store_lock(self) -> None:
        if self._lock_file is not None:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None

    def start(self) -> None:
        if self._threads:
            raise RuntimeError("worker pool is already started")
        if self._n_workers < 1:
            raise ValueError("n_workers must be >= 1")
        self._acquire_store_lock()
        try:
            store = RunStore(self._store_path)
            try:
                store.requeue_orphans()
            finally:
                store.close()
        except Exception:
            self._release_store_lock()
            raise
        for i in range(self._n_workers):
            t = threading.Thread(target=self._loop, name=f"tl-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def _loop(self) -> None:
        store = RunStore(self._store_path)
        try:
            while not self._stop.is_set():
                try:
                    durable = process_one(store, llm_client=self._llm_client, tools=self._tools)
                except Exception as exc:
                    # Store/provider infrastructure faults should be observable
                    # through readiness, but a transient sqlite lock or client
                    # bug must not terminate the thread permanently.
                    print(
                        json.dumps(
                            {
                                "component": "threadlang-worker",
                                "worker": threading.current_thread().name,
                                "error_type": type(exc).__name__,
                            },
                            separators=(",", ":"),
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
                    durable = None
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
        self._release_store_lock()

    def is_healthy(self) -> bool:
        """True only while every configured worker thread is alive."""
        return (
            bool(self._threads)
            and len(self._threads) == self._n_workers
            and all(thread.is_alive() for thread in self._threads)
            and not self._stop.is_set()
        )

    def status(self) -> dict[str, object]:
        return {
            "configured": self._n_workers,
            "alive": sum(thread.is_alive() for thread in self._threads),
            "healthy": self.is_healthy(),
        }

    def drain(self, store: RunStore, *, max_runs: int = 10_000) -> int:
        """Synchronously process pending runs until the queue is empty (or
        `max_runs` is hit), in the current thread. Useful for tests and for a
        one-shot batch mode without spinning up threads. Returns how many runs
        were processed."""
        count = 0
        while count < max_runs:
            pending_before = store.counts_by_status().get("pending", 0)
            if pending_before == 0:
                break
            durable = process_one(store, llm_client=self._llm_client, tools=self._tools)
            if durable is None:
                # None can mean either an empty queue or a run that was claimed
                # and failed. The pre-claim count distinguishes those cases so
                # one bad job cannot prevent later jobs from draining.
                count += 1
                continue
            count += 1
        return count
