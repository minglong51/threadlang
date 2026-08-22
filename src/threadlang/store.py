"""Durable run store (L3) — the trace becomes an event log.

ThreadLang's founding bet is that every run is a replayable, inspectable
trace. v0.4 makes that trace *durable*: a run gets an id and a status, its
`TraceEvent` stream is persisted as it happens, and each completed step is
checkpointed. If a run crashes (an LLM call fails, the process dies), it can
resume from the last completed step instead of re-running from the top.

The design keeps the runtime storage-agnostic. This module supplies:

- `RunStore` — a thin sqlite wrapper (stdlib `sqlite3`, no dependency) holding
  four tables: `runs`, `events`, `step_outputs`, and `llm_journal` (the
  per-call model-call journal; see journal.py).
- `run_durable()` — orchestrates a persisted run: it hands the runtime a
  write-through trace (every appended event lands in `events`) and a
  step-complete hook (every step output lands in `step_outputs`), wraps the
  LLM client in a per-call journaling wrapper, then marks the run completed
  or failed. On resume it pre-loads the completed step outputs and tells the
  runtime to skip them.

Checkpoint granularity is one step. A crash *inside* a step (mid agent loop)
re-runs that whole step on resume; steps that already finished do not. Within
the re-run step, model calls completed before the crash replay from
`llm_journal` — at most the single in-flight call re-executes. That is the
right boundary — coarse enough to be simple and correct, fine
enough that a long pipeline doesn't redo completed work.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .ast import AgentStep, Program
from .ir import (
    IR_VERSION,
    canonical_ir_bytes,
    compile_program,
    load_ir_bytes,
    workflow_fingerprint,
)
from .journal import JournaledLLMClient
from .llm import DryRunClient, LLMClient
from .metrics import AggregateMetrics, RunMetrics, aggregate, compute_metrics, trace_span_ms
from .policy import DEFAULT_MAX_PENDING_RUNS, DEFAULT_MAX_RETAINED_RUNS
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
    program_sha256 TEXT,
    inputs_sha256  TEXT,
    definition_json TEXT,
    definition_sha256 TEXT,
    ir_version TEXT,
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
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS step_outputs (
    run_id    TEXT NOT NULL,
    step_name TEXT NOT NULL,
    output    TEXT NOT NULL,
    PRIMARY KEY (run_id, step_name),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS llm_journal (
    run_id              TEXT NOT NULL,
    call_seq            INTEGER NOT NULL,   -- append order within the run
    request_fingerprint TEXT NOT NULL,      -- sha256 of the canonical request JSON
    occurrence          INTEGER NOT NULL,   -- per-attempt ordinal of this fingerprint
    request_json        TEXT NOT NULL,
    response_json       TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (run_id, call_seq),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_runs_status_created ON runs(status, created_at, id);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_llm_journal_lookup
    ON llm_journal(run_id, request_fingerprint, occurrence);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _program_fingerprint(program: Program, source: Optional[str] = None) -> str:
    if source is not None:
        return hashlib.sha256(source.encode("utf-8")).hexdigest()
    return _sha256_json(asdict(program))


def _inputs_fingerprint(inputs: Dict[str, str]) -> str:
    return _sha256_json(dict(inputs))


class RunStoreCapacityError(RuntimeError):
    """Raised when the bounded pending queue cannot accept another run."""


@dataclass(frozen=True)
class RunRecord:
    id: str
    program_name: str
    status: str
    inputs: Dict[str, str]
    output: Optional[str]
    error: Optional[str]
    source: Optional[str] = None
    program_sha256: Optional[str] = None
    inputs_sha256: Optional[str] = None
    definition_json: Optional[str] = None
    definition_sha256: Optional[str] = None
    ir_version: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


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
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring an older on-disk store up to the current schema. `events.ts`
        (v0.8) is added to stores created before it existed; old rows keep a
        NULL ts and are simply excluded from latency metrics."""
        event_cols = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "ts" not in event_cols:
            self._conn.execute("ALTER TABLE events ADD COLUMN ts TEXT")
        run_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        if "program_sha256" not in run_cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN program_sha256 TEXT")
        if "inputs_sha256" not in run_cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN inputs_sha256 TEXT")
        if "definition_json" not in run_cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN definition_json TEXT")
        if "definition_sha256" not in run_cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN definition_sha256 TEXT")
        if "ir_version" not in run_cols:
            self._conn.execute("ALTER TABLE runs ADD COLUMN ir_version TEXT")
        self._conn.executescript(
            "CREATE INDEX IF NOT EXISTS idx_runs_status_created "
            "ON runs(status, created_at, id);"
            "CREATE INDEX IF NOT EXISTS idx_runs_created "
            "ON runs(created_at DESC, id DESC);"
            "CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);"
        )

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
            program_sha256=row["program_sha256"],
            inputs_sha256=row["inputs_sha256"],
            definition_json=row["definition_json"],
            definition_sha256=row["definition_sha256"],
            ir_version=row["ir_version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ----- runs -----

    def create_run(
        self,
        program_name: str,
        inputs: Dict[str, str],
        *,
        program_sha256: Optional[str] = None,
        inputs_sha256: Optional[str] = None,
        definition_json: Optional[str] = None,
        definition_sha256: Optional[str] = None,
        ir_version: Optional[str] = None,
    ) -> str:
        run_id = uuid.uuid4().hex
        now = _now()
        self._conn.execute(
            "INSERT INTO runs (id, program_name, status, inputs_json, program_sha256, "
            "inputs_sha256, definition_json, definition_sha256, ir_version, created_at, updated_at) "
            "VALUES (?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                program_name,
                json.dumps(dict(inputs)),
                program_sha256,
                inputs_sha256 or _inputs_fingerprint(inputs),
                definition_json,
                definition_sha256,
                ir_version,
                now,
                now,
            ),
        )
        return run_id

    def get_run(self, run_id: str) -> Optional[RunRecord]:
        row = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._row_to_record(row) if row is not None else None

    def list_runs(self, *, limit: Optional[int] = None, offset: int = 0) -> List[RunRecord]:
        """All runs, newest first — the basis for a run list / dashboard."""
        sql = "SELECT * FROM runs ORDER BY created_at DESC, id DESC"
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params = (limit, offset)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ----- control-plane queue: pending runs ARE the queue -----

    def enqueue_run(
        self,
        program_name: str,
        source: str,
        inputs: Dict[str, str],
        *,
        definition_json: Optional[str] = None,
        definition_sha256: Optional[str] = None,
        ir_version: Optional[str] = None,
        max_pending: int = DEFAULT_MAX_PENDING_RUNS,
        max_retained: int = DEFAULT_MAX_RETAINED_RUNS,
    ) -> str:
        """Enqueue source text for a worker to parse and execute."""
        return self._enqueue_definition(
            program_name,
            inputs,
            source=source,
            program_sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            definition_json=definition_json,
            definition_sha256=definition_sha256,
            ir_version=ir_version,
            max_pending=max_pending,
            max_retained=max_retained,
        )

    def enqueue_ir(
        self,
        program_name: str,
        definition_json: str,
        definition_sha256: str,
        ir_version: str,
        inputs: Dict[str, str],
        *,
        max_pending: int = DEFAULT_MAX_PENDING_RUNS,
        max_retained: int = DEFAULT_MAX_RETAINED_RUNS,
    ) -> str:
        """Enqueue a validated canonical IR definition without source text."""
        return self._enqueue_definition(
            program_name,
            inputs,
            definition_json=definition_json,
            definition_sha256=definition_sha256,
            ir_version=ir_version,
            program_sha256=definition_sha256,
            max_pending=max_pending,
            max_retained=max_retained,
        )

    def _enqueue_definition(
        self,
        program_name: str,
        inputs: Dict[str, str],
        *,
        program_sha256: str,
        source: Optional[str] = None,
        definition_json: Optional[str] = None,
        definition_sha256: Optional[str] = None,
        ir_version: Optional[str] = None,
        max_pending: int,
        max_retained: int,
    ) -> str:
        run_id = uuid.uuid4().hex
        now = _now()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            pending = self._conn.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE status = 'pending'"
            ).fetchone()["count"]
            if pending >= max_pending:
                raise RunStoreCapacityError(f"pending run limit reached ({max_pending})")
            self._prune_terminal_locked(max_retained)
            self._conn.execute(
                "INSERT INTO runs (id, program_name, status, inputs_json, source, "
                "program_sha256, inputs_sha256, definition_json, definition_sha256, "
                "ir_version, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    program_name,
                    json.dumps(dict(inputs)),
                    source,
                    program_sha256,
                    _inputs_fingerprint(inputs),
                    definition_json,
                    definition_sha256,
                    ir_version,
                    now,
                    now,
                ),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return run_id

    def _prune_terminal_locked(self, max_retained: int) -> int:
        if max_retained < 0:
            raise ValueError("max_retained must be >= 0")
        rows = self._conn.execute(
            "SELECT id FROM runs WHERE status IN ('completed', 'failed') "
            "ORDER BY updated_at DESC, id DESC LIMIT -1 OFFSET ?",
            (max_retained,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        for run_id in ids:
            # Explicit deletes preserve cleanup for stores created before the
            # foreign-key declarations existed.
            self._conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
            self._conn.execute("DELETE FROM step_outputs WHERE run_id = ?", (run_id,))
            self._conn.execute("DELETE FROM llm_journal WHERE run_id = ?", (run_id,))
            self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return len(ids)

    def counts_by_status(self) -> Dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS count FROM runs GROUP BY status"
        ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def bind_run_identity(
        self,
        run_id: str,
        program_sha256: str,
        inputs_sha256: str,
        *,
        definition_json: Optional[str] = None,
        definition_sha256: Optional[str] = None,
        ir_version: Optional[str] = None,
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET program_sha256 = COALESCE(program_sha256, ?), "
            "inputs_sha256 = COALESCE(inputs_sha256, ?), "
            "definition_json = COALESCE(definition_json, ?), "
            "definition_sha256 = COALESCE(definition_sha256, ?), "
            "ir_version = COALESCE(ir_version, ?) WHERE id = ?",
            (
                program_sha256,
                inputs_sha256,
                definition_json,
                definition_sha256,
                ir_version,
                run_id,
            ),
        )

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
            program_sha256=record.program_sha256,
            inputs_sha256=record.inputs_sha256,
            definition_json=record.definition_json,
            definition_sha256=record.definition_sha256,
            ir_version=record.ir_version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def requeue_orphans(self) -> int:
        """Reset runs stranded in `running` (e.g. by a process crash or
        restart) back to `pending` so a worker can re-claim them. Only runs
        with a persisted source or canonical IR definition are requeued —
        re-dispatch needs one complete workflow representation. Safe because
        resume via `run_durable` is idempotent. Returns the number of runs
        requeued."""
        cursor = self._conn.execute(
            "UPDATE runs SET status = 'pending', updated_at = ? "
            "WHERE status = 'running' "
            "AND (source IS NOT NULL OR definition_json IS NOT NULL)",
            (_now(),),
        )
        return cursor.rowcount

    def mark_running(self, run_id: str, *, expected: str) -> None:
        """Acquire execution ownership with a compare-and-swap transition.

        This prevents two CLI resume attempts from executing the same failed
        run concurrently. Queue workers already own a run after the atomic
        pending->running claim and therefore do not call this method.
        """
        cursor = self._conn.execute(
            "UPDATE runs SET status = 'running', error = NULL, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (_now(), run_id, expected),
        )
        if cursor.rowcount != 1:
            raise ValueError("run is already active or is not resumable")

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

    # ----- LLM call journal (the replay half of journal.py) -----

    def load_llm_journal_entry(
        self, run_id: str, request_fingerprint: str, occurrence: int
    ) -> Optional[str]:
        """The recorded response JSON for the `occurrence`-th call with this
        request fingerprint in `run_id`, or None — in which case the caller
        must go live and then persist via `save_llm_journal_entry`."""
        row = self._conn.execute(
            "SELECT response_json FROM llm_journal "
            "WHERE run_id = ? AND request_fingerprint = ? AND occurrence = ?",
            (run_id, request_fingerprint, occurrence),
        ).fetchone()
        return row["response_json"] if row is not None else None

    def save_llm_journal_entry(
        self,
        run_id: str,
        request_fingerprint: str,
        occurrence: int,
        request_json: str,
        response_json: str,
    ) -> None:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(call_seq), -1) + 1 AS next FROM llm_journal WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        self._conn.execute(
            "INSERT INTO llm_journal (run_id, call_seq, request_fingerprint, occurrence, "
            "request_json, response_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                row["next"],
                request_fingerprint,
                occurrence,
                request_json,
                response_json,
                _now(),
            ),
        )

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
        return compute_metrics(self.load_events(run_id), status=record.status, duration_ms=duration)

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
    source: Optional[str] = None,
    claimed: bool = False,
    journal_llm: bool = True,
) -> DurableRun:
    """Execute a program with its trace and step checkpoints persisted to
    `store`.

    Pass `run_id` of a prior **failed** run to resume it: the steps already
    checkpointed are skipped and execution continues from the first incomplete
    one. Passing the id of an active run is rejected; passing a `completed` run
    returns its stored result without re-executing. Omit `run_id` to start a
    fresh run. Queue workers set `claimed=True` only after an atomic claim.

    Model calls are journaled per call (`journal_llm=False` opts out): on
    resume, a call whose request fingerprint matches a journaled row replays
    the recorded response instead of re-calling the provider, so a crash
    re-executes at most the interrupted step's single in-flight call. A fresh
    run always calls live. Exactly-once stays out of scope; see journal.py and
    docs/production.md.
    """
    if tools is not None:
        for step in program.steps.steps:
            if isinstance(step, AgentStep):
                tools.validate_durable(list(step.tools))
    resume_outputs: Optional[Dict[str, str]] = None
    program_sha256 = _program_fingerprint(program, source)
    inputs_sha256 = _inputs_fingerprint(inputs)
    workflow = compile_program(program)
    definition_bytes = canonical_ir_bytes(workflow)
    definition_json = definition_bytes.decode("utf-8")
    definition_sha256 = workflow_fingerprint(workflow)

    if run_id is not None:
        record = store.get_run(run_id)
        if record is None:
            raise ValueError(f"unknown run_id: {run_id}")
        if (
            record.definition_sha256 is None
            and record.program_sha256 is not None
            and record.program_sha256 != program_sha256
        ):
            raise ValueError("resume refused: program source does not match the original run")
        if record.inputs_sha256 is not None and record.inputs_sha256 != inputs_sha256:
            raise ValueError("resume refused: inputs do not match the original run")
        if record.ir_version is not None and record.ir_version != IR_VERSION:
            raise ValueError(f"resume refused: unsupported stored IR version {record.ir_version!r}")
        if record.definition_json is not None:
            stored_bytes = record.definition_json.encode("utf-8")
            stored_workflow = load_ir_bytes(stored_bytes)
            stored_digest = workflow_fingerprint(stored_workflow)
            if record.definition_sha256 is None or stored_digest != record.definition_sha256:
                raise ValueError(
                    "resume refused: stored workflow definition failed integrity check"
                )
        if record.definition_sha256 is not None and record.definition_sha256 != definition_sha256:
            # Preserve the v0.12 diagnostic substring for callers/tests while
            # making clear that canonical IR identity is now authoritative.
            raise ValueError(
                "resume refused: program source does not match the original run "
                "(workflow definition mismatch)"
            )
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
        if claimed:
            if record.status != "running":
                raise ValueError("claimed run is no longer running")
        elif record.status not in ("created", "failed"):
            raise ValueError("run is already active or is not resumable")
        identity_missing = record.program_sha256 is None or record.inputs_sha256 is None
        definition_missing = (
            record.definition_json is None
            or record.definition_sha256 is None
            or record.ir_version is None
        )
        if identity_missing:
            # A fresh v0.12 CLI run is still `created`; a claimed queue run is
            # executing source read from this record. A migrated failed CLI run
            # with no persisted source cannot prove which program produced its
            # checkpoints, so fail closed instead of blessing arbitrary source.
            identity_is_provable = (
                record.status == "created"
                or claimed
                or (record.source is not None and source == record.source)
            )
            if not identity_is_provable:
                raise ValueError("resume refused: legacy run has no verifiable program identity")
        if identity_missing or definition_missing:
            store.bind_run_identity(
                run_id,
                program_sha256,
                inputs_sha256,
                definition_json=definition_json,
                definition_sha256=definition_sha256,
                ir_version=IR_VERSION,
            )
        resume_outputs = store.load_step_outputs(run_id)
        if not claimed:
            store.mark_running(run_id, expected=record.status)
    else:
        run_id = store.create_run(
            program.thread_name,
            inputs,
            program_sha256=program_sha256,
            inputs_sha256=inputs_sha256,
            definition_json=definition_json,
            definition_sha256=definition_sha256,
            ir_version=IR_VERSION,
        )
        store.mark_running(run_id, expected="created")

    trace = _WriteThroughTrace(store, run_id)

    client: LLMClient = llm_client if llm_client is not None else DryRunClient()
    if journal_llm:
        # Per-call response journal: on resume, the interrupted step's
        # completed calls replay from `llm_journal`; at most the call that was
        # in flight at crash time re-executes. A fresh run_id's journal is
        # empty, so a first attempt always calls live.
        client = JournaledLLMClient(client, store, run_id)

    def _checkpoint(step_name: str, output: str) -> None:
        store.save_step_output(run_id, step_name, output)

    try:
        result = run_program(
            program,
            inputs,
            llm_client=client,
            tools=tools,
            trace=trace,
            resume_outputs=resume_outputs,
            on_step_complete=_checkpoint,
        )
        store.mark_completed(run_id, result.output)
    except Exception as exc:
        store.mark_failed(run_id, f"{type(exc).__name__}: {exc}")
        raise

    return DurableRun(run_id=run_id, result=result)
