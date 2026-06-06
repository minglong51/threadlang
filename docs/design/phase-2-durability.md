# Phase 2 — Durability (v0.4)

## Where this fits

Phase 1 turned a prompt-chain DSL into something that can *act* (tool-use
loop). Phase 2 makes a run **durable**: the trace stops being an in-memory
list that vanishes when the process exits and becomes a persisted event log,
so a run gets an identity, its progress survives a crash, and it can resume
from the last completed step instead of from the top.

| Layer | Status |
|---|---|
| L0 DSL — authoring model | shipped (v0.1–0.2) |
| L1 Runtime — synchronous executor + trace stream | shipped (v0.1–0.2) |
| L2 Agentic core — tools + agent loop | shipped (v0.3) |
| **L3 Durability — sqlite event log, resume-from-failure** | **this doc (v0.4)** |
| L4 Control plane — API + worker pool + run queue | next |
| L5 Observability — trace-timeline dashboard | planned |
| L6 Vertical-slice app — one concrete multi-agent product | planned |

This is the layer that makes the founding bet — *every run is a durable,
replayable, inspectable trace* — literally true. Until now "durable" was
aspirational; the trace was real but ephemeral.

## What shipped

`store.py`:

- **`RunStore`** — a stdlib `sqlite3` wrapper (no dependency added). Three
  tables: `runs` (id, status, inputs, output, error, timestamps), `events`
  (the persisted `TraceEvent` stream, ordered by `seq` per run), and
  `step_outputs` (the per-step checkpoints). Autocommit, so every write is
  durable the instant it happens — the prerequisite for crash-resume.
- **`run_durable(program, inputs, store, run_id=None)`** — runs a program with
  its trace and step outputs persisted, marking the run `completed` or
  `failed`. Pass `run_id` of a failed run to **resume**: completed steps are
  skipped and reused, execution continues from the first incomplete one.
  Passing a `completed` run's id replays the stored result without
  re-executing.
- **`list_runs()` / `load_events()` / `load_step_outputs()`** — read APIs the
  L5 dashboard (and a CLI run list) will build on.

CLI: `--store PATH` persists a run and prints its `run_id`; on a crash it
prints the exact `--resume <id>` command. `--resume RUN_ID --store PATH`
continues a failed run.

## Key design decisions

**1. The runtime stays storage-agnostic.** `store.py` depends on `runtime.py`,
never the reverse. Persistence reaches the runtime through two narrow,
optional hooks on `run_program`:

- a **write-through `Trace`** (`_WriteThroughTrace(list)`) whose `append` also
  writes the event to sqlite — so every one of the ~dozen existing
  `trace.append(...)` sites becomes durable with *zero* changes to runtime
  logic;
- `resume_outputs` (a map of already-completed steps to skip) and
  `on_step_complete(name, output)` (the checkpoint callback).

A runtime that knows nothing about sqlite stays testable, swappable (a future
Postgres/Redis store implements the same `RunStore` surface), and honest about
its single responsibility.

**2. Checkpoint granularity is one step.** A crash *inside* a step (mid agent
loop) re-runs that whole step on resume; steps that already finished do not.
Step-level is the right v0.4 boundary: coarse enough to be simple and provably
correct, fine enough that a long pipeline never redoes completed work. Mid-step
(per-agent-turn) checkpointing is a real option, deferred until a step is
expensive enough to earn the extra state.

**3. The event log is append-only across attempts.** Resuming a run continues
the same `events` stream (seq keeps climbing) rather than truncating the failed
attempt's events. The history of *what actually happened*, including the
failure and the resume, is preserved — which is the whole point of an event
log.

**4. Resume is idempotent on a completed run.** Re-invoking a completed run id
returns the stored output and makes no model calls. This makes the durable
path safe to retry blindly (a control-plane worker can re-dispatch without
double-spending tokens).

## Verification

- Suite: 26/26 (10 back-compat + 13 agent + 3 durability golden tests).
- Durability tests (offline, scripted flaky client): a run persists its
  events/status/checkpoints; a run that crashes after step `a` resumes without
  re-running step `a` (asserted by counting model calls) and completes; a
  completed run replays from the store with zero model calls.
- **Live, end-to-end:** via the CLI against a running Ollama, a two-step
  program whose step `b` named a missing model crashed after checkpointing
  step `a` (`status=failed`, one checkpoint, resume hint printed). Fixing the
  model and `--resume <id>` reused step `a` from the checkpoint (model not
  re-called — visible as `Step 'a' resumed from checkpoint` in the trace) and
  ran only step `b`, emitting the final output and marking the run completed.

## Next (L4 — control plane)

A run is now a durable record with an id and a status. L4 puts an API in front
of it (`POST /runs` enqueues, `GET /runs/{id}` reads status + trace) and a
worker pool that drains a queue of pending runs, each worker calling
`run_durable`. Because resume is idempotent and crash-safe, a worker that dies
mid-run is recovered by re-dispatching the same run id — the durability layer
is exactly what makes the control plane safe.
