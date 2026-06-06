# Phase 3 — Control Plane (v0.5)

## Where this fits

Phase 2 made a run a durable record with an id and a status. Phase 3 separates
*submitting* a run from *executing* it, and puts an HTTP API in front: a client
enqueues a run and gets an id back immediately; a pool of workers executes
runs asynchronously; the client polls for status and the full trace.

| Layer | Status |
|---|---|
| L0 DSL — authoring model | shipped (v0.1–0.2) |
| L1 Runtime — synchronous executor + trace stream | shipped (v0.1–0.2) |
| L2 Agentic core — tools + agent loop | shipped (v0.3) |
| L3 Durability — sqlite event log, resume-from-failure | shipped (v0.4) |
| **L4 Control plane — API + worker pool + run queue** | **this doc (v0.5)** |
| L5 Observability — trace-timeline dashboard | next |
| L6 Vertical-slice app — one concrete multi-agent product | planned |

## What shipped

- **The queue is the store.** A run is `enqueue`d as `pending` (program source
  + inputs persisted); the `pending` rows in the `runs` table *are* the queue.
  No separate broker, no second source of truth.
- **`claim_next_pending()`** (`store.py`) — atomically takes the oldest pending
  run and flips it to `running` inside a `BEGIN IMMEDIATE` write transaction,
  so concurrent workers never claim the same run.
- **`control.py`** — `process_one(store)` claims and runs exactly one queued
  run via `run_durable` (a failing run is recorded `failed` and the exception
  swallowed, so one bad run never kills a worker). `WorkerPool` runs N daemon
  threads each draining the queue, each with its own `RunStore` connection.
- **`server.py`** — a stdlib `http.server` JSON API: `POST /runs`,
  `GET /runs`, `GET /runs/{id}` (status + persisted trace), `GET /healthz`.
  `serve()` runs the API and the worker pool together. New console script
  `threadlang-serve`.

```
POST /runs  {"source": "...", "inputs": {...}}  -> 201 {"run_id", "status":"pending"}
GET  /runs/{id}                                 -> 200 {status, output, error, trace:[...]}
```

## Key design decisions

**1. The store is the queue (no extra broker).** The durable record already
holds everything a queue needs — submission order (`created_at`), state
(`status`), and payload (`source` + `inputs`). Adding Redis/RabbitMQ would
create a second source of truth to keep consistent with the store. A restart
recovers the queue for free: pending runs are still pending on disk. Swapping
in a real broker later is a `RunStore` implementation detail, not an API change.

**2. Crash-safety is inherited from L3, not re-built.** A worker that dies
mid-run leaves the run `running` with its completed steps checkpointed.
Re-dispatching the same id resumes it (idempotently — a completed run replays
with zero model calls). This is *why* L4 sits on L3: the control plane is only
safe because the execution under it is. The worker loop itself stays trivial.

**3. Atomic claim via `BEGIN IMMEDIATE`, one connection per worker.** sqlite
connections are not shared across threads, so each worker opens its own; the
immediate-mode write lock (plus `busy_timeout`) serializes claims so no run is
handed to two workers. Verified by a concurrent test that counts per-run model
calls and asserts each run executed exactly once.

**4. Stdlib HTTP, validate-before-enqueue.** `http.server` keeps the
zero-dependency promise (a framework is a real dependency with its own surface;
the API is small enough not to need one). `POST /runs` parses the program
before enqueuing, so a malformed program is rejected `400` synchronously rather
than failing later inside a worker.

**5. `process_one` is the testable unit.** The pool is just `process_one` in a
loop across threads. Keeping the claim-and-run logic in one synchronous,
thread-free function means the core is golden-tested without sockets or sleeps;
only the thin threaded/HTTP wrappers need a live smoke test.

## Verification

- Suite: 31/31 (10 back-compat + 13 agent + 3 durability + 5 control-plane
  golden tests). The control-plane tests cover: enqueue→claim→complete; an
  empty-queue claim returns None; a second claim never double-claims; **a real
  4-worker pool draining 8 runs executes each exactly once** (asserted by a
  per-run call counter); a failing run is recorded `failed` and the worker
  survives.
- **Live, end-to-end:** `threadlang-serve` started with 2 workers; `POST /runs`
  with a two-step program returned a `run_id` (`pending`); polling `GET
  /runs/{id}` showed `running` → `completed` with the full persisted trace;
  `GET /runs` listed it; an unknown id returned `404`; a malformed program
  returned `400` with the parse error; `GET /healthz` returned `{"ok": true}`.

## Next (L5 — observability)

The API already serves every run's status and full trace as JSON. L5 is a
read-only dashboard over exactly that: a run list, and a per-run timeline that
renders the `events` stream — context bindings, step calls, agent turns, tool
calls, tool results — as an inspectable trace. No new execution surface; it
reads what L3 persists and L4 exposes.
