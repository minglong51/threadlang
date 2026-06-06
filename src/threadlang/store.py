"""Durable run store (L3) — the trace becomes an event log.

ThreadLang's founding bet is that every run is a replayable, inspectable
trace. v0.4 makes that trace *durable*: a run gets an id and a status, its
`TraceEvent` stream is persisted as it happens, and each completed step is
checkpointed. If a run crashes (an LLM call fails, the process dies), it can
resume from the last completed step instead of re-running from the top.

The design keeps the runtime storage-agnostic. This module supplies:

- `RunStore` — a thin sqlite wrapper (stdlib `sqlite3`, no dependency) holding
  three tables: `runs`, `events`, `step_outputs`.
- `run_durable()` — orchestrates a persisted run: it hands the runtime a
  write-through trace (every appended event lands in `events`) and a
  step-complete hook (every step output lands in `step_outputs`), then marks
  the run completed or failed. On resume it pre-loads the completed step
  outputs and tells the runtime to skip them.

Checkpoint granularity is one step. A crash *inside* a step (mid agent loop)
re-runs that whole step on resume; steps that already finished do not. That is
the right boundary for v0.4 — coarse enough to be simple and correct, fine
enough that a long pipeline doesn't redo completed work.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .ast import Program
from .llm import LLMClient
from .metrics import AggregateMetrics, RunMetrics, aggregate, compute_metrics, trace_span_ms
from .runtime import RuntimeResult, run_program
from .tools import ToolRegistry
from .trace import Trace, TraceEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    program_name TEXT NOT NULL,
    status       TEXT NOT NULL,            -- pending | running | completed | failed
    inputs_json  TEXT NOT NULL,
    source       TEXT,                     -- program text, set when enqueued via the control plane
    output       TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    phase     TEXT NOT NULL,
    message   TEXT NOT NULL,
    data_json TEXT NOT NULL,
    ts        TEXT,                     -- wall-clock when appended (observational; nullable for pre-v0.8 rows)
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS step_outputs (
    run_id    TEXT NOT NULL,
    step_name TEXT NOT NULL,
    output    TEXT NOT NULL,
    PRIMARY KEY (run_id, step_name)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RunRecord:
    id: str
    program_name: str
    status: str
    inputs: Dict[str, str]
    output: Optional[str]
    error: Optional[str]
    source: Optional[str] = None


class RunStore:
    """A sqlite-backed store for runs, their event streams, and step
    checkpoints. Writes commit immediately so a crash leaves a consistent,
    resumable record on disk."""

    def __init__(self, path: str) -> None:
        # isolation_level=None → autocommit; every write is durable at once,
        # which is the whole point of a crash-resumable store.
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        # Workers run in separate threads/processes against the same file; a
        # busy_timeout lets a claim wait for the lock instead of erroring.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older on-disk store up to the current schema. `events.ts`
        (v0.8) is added to stores created before it existed; old rows keep a
        NULL ts and are simply excluded from latency metrics."""
        cols = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "ts" not in cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN ts TEXT")

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            program_name=row["program_name"],
            status=row["status"],
            inputs=json.loads(row["inputs_json"]),
            output=row["output"],
            error=row["error"],
            source=row["source"],
        )

    # ----- runs -----

    def create_run(self, program_name: str, inputs: Dict[str, str]) -> str:
        run_id = uuid.uuid4().hex
        now = _now()
        self._conn.execute(
            "INSERT INTO runs (id, program_name, status, inputs_json, created_at, updated_at) "
            "VALUES (?, ?, 'running', ?, ?, ?)",
            (run_id, program_name, json.dumps(dict(inputs)), now, now),
        )
        return run_id

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_runs(self) -> List[RunRecord]:
        """All runs, newest first — the basis for a run list / dashboard."""
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY created_at DESC, id DESC"
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ----- control-plane queue: pending runs ARE the queue -----

    def enqueue_run(
        self, program_name: str, source: str, inputs: Dict[str, str]
    ) -> str:
        """Create a run in `pending` state without executing it. A worker
        claims and runs it later. The program `source` is stored so any worker
        can reconstruct the program."""
        run_id = uuid.uuid4().hex
        now = _now()
        self._conn.execute(
            "INSERT INTO runs (id, program_name, status, inputs_json, source, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?, ?, ?)",
            (run_id, program_name, json.dumps(dict(inputs)), source, now, now),
        )
        return run_id

    def claim_next_pending(self) -> Optional[RunRecord]:
        """Atomically take the oldest `pending` run and mark it `running`,
        returning it (with `source`). Returns None if the queue is empty. The
        `BEGIN IMMEDIATE` write-lock serializes claims across worker
        connections, so no run is ever claimed twice."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE status = 'pending' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE runs SET status = 'running', updated_at = ? WHERE id = ?",
                (_now(), row["id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        record = self._row_to_record(row)
        return RunRecord(
            id=record.id,
            program_name=record.program_name,
            status="running",
            inputs=record.inputs,
            output=record.output,
            error=record.error,
            source=record.source,
        )

    def mark_running(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE runs SET status = 'running', error = NULL, updated_at = ? WHERE id = ?",
            (_now(), run_id),
        )

    def mark_completed(self, run_id: str, output: str) -> None:
        self._conn.execute(
            "UPDATE runs SET status = 'completed', output = ?, error = NULL, updated_at = ? WHERE id = ?",
            (output, _now(), run_id),
        )

    def mark_failed(self, run_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE runs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error, _now(), run_id),
        )

    # ----- events -----

    def append_event(self, run_id: str, event: TraceEvent) -> None:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS next FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        self._conn.execute(
            "INSERT INTO events (run_id, seq, phase, message, data_json, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, row["next"], event.phase, event.message, json.dumps(event.data), _now()),
        )

    def load_events(self, run_id: str) -> Trace:
        rows = self._conn.execute(
            "SELECT phase, message, data_json FROM events WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [
            TraceEvent(phase=r["phase"], message=r["message"], data=json.loads(r["data_json"]))
            for r in rows
        ]

    # ----- step checkpoints -----

    def save_step_output(self, run_id: str, step_name: str, output: str) -> None:
        self._conn.execute(
            "INSERT INTO step_outputs (run_id, step_name, output) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id, step_name) DO UPDATE SET output = excluded.output",
            (run_id, step_name, output),
        )

    def load_step_outputs(self, run_id: str) -> Dict[str, str]:
        rows = self._conn.execute(
            "SELECT step_name, output FROM step_outputs WHERE run_id = ?", (run_id,)
        ).fetchall()
        return {r["step_name"]: r["output"] for r in rows}

    # ----- metrics (a derived view of the persisted trace) -----

    def _event_timestamps(self, run_id: str) -> List[Optional[str]]:
        rows = self._conn.execute(
            "SELECT ts FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [r["ts"] for r in rows]

    def run_metrics(self, run_id: str) -> Optional[RunMetrics]:
        """Per-run metrics, computed by folding the run's persisted trace.
        Returns None for an unknown run. Latency comes from the event
        timestamp span; control-flow metrics from the event stream itself."""
        record = self.get_run(run_id)
        if record is None:
            return None
        duration = trace_span_ms(self._event_timestamps(run_id))
        return compute_metrics(
            self.load_events(run_id), status=record.status, duration_ms=duration
        )

    def aggregate_metrics(self) -> AggregateMetrics:
        """Roll up every run's metrics into one monitoring view — success rate,
        average latency, model/tool-call volume, per-program breakdown. This is
        the dashboard's `/metrics` summary and the seed of any data-driven
        iteration on a program."""
        items: List[tuple] = []
        for record in self.list_runs():
            duration = trace_span_ms(self._event_timestamps(record.id))
            metrics = compute_metrics(
                self.load_events(record.id), status=record.status, duration_ms=duration
            )
            items.append((record.program_name, metrics))
        return aggregate(items)


class _WriteThroughTrace(List[TraceEvent]):
    """A Trace (list of TraceEvents) that also persists each appended event to
    the store. Because the runtime appends through this object, no runtime code
    needs to know storage exists — every existing `trace.append(...)` site is
    durable for free."""

    def __init__(self, store: RunStore, run_id: str) -> None:
        super().__init__()
        self._store = store
        self._run_id = run_id

    def append(self, event: TraceEvent) -> None:  # type: ignore[override]
        super().append(event)
        self._store.append_event(self._run_id, event)


@dataclass(frozen=True)
class DurableRun:
    """The result of a persisted run: the assigned `run_id` plus the usual
    `RuntimeResult`."""

    run_id: str
    result: RuntimeResult


def run_durable(
    program: Program,
    inputs: Dict[str, str],
    store: RunStore,
    *,
    llm_client: Optional[LLMClient] = None,
    tools: Optional[ToolRegistry] = None,
    run_id: Optional[str] = None,
) -> DurableRun:
    """Execute a program with its trace and step checkpoints persisted to
    `store`.

    Pass `run_id` of a prior **failed** (or still-running) run to resume it:
    the steps already checkpointed are skipped and execution continues from the
    first incomplete one. Passing the id of a `completed` run returns its stored
    result without re-executing. Omit `run_id` to start a fresh run.
    """
    resume_outputs: Optional[Dict[str, str]] = None

    if run_id is not None:
        record = store.get_run(run_id)
        if record is None:
            raise ValueError(f"unknown run_id: {run_id}")
        if record.status == "completed" and record.output is not None:
            # Already done — replay the stored result rather than re-running.
            return DurableRun(
                run_id=run_id,
                result=RuntimeResult(
                    output=record.output,
                    trace=store.load_events(run_id),
                    step_outputs=store.load_step_outputs(run_id),
                ),
            )
        resume_outputs = store.load_step_outputs(run_id)
        store.mark_running(run_id)
    else:
        run_id = store.create_run(program.thread_name, inputs)

    trace = _WriteThroughTrace(store, run_id)

    def _checkpoint(step_name: str, output: str) -> None:
        store.save_step_output(run_id, step_name, output)

    try:
        result = run_program(
            program,
            inputs,
            llm_client=llm_client,
            tools=tools,
            trace=trace,
            resume_outputs=resume_outputs,
            on_step_complete=_checkpoint,
        )
    except Exception as exc:
        store.mark_failed(run_id, f"{type(exc).__name__}: {exc}")
        raise

    store.mark_completed(run_id, result.output)
    return DurableRun(run_id=run_id, result=result)
