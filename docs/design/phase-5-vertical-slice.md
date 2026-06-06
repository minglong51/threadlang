# Phase 5 — Vertical-slice app (v0.7)

## Where this fits

Phases 1–4 built the platform: author, execute+trace, act, persist+resume,
submit+drain, inspect. Phase 5 adds the first **product on it** — a concrete
multi-agent application that exercises every layer end to end, and adds no new
core machinery. It is the proof that the layers compose into something a user
runs, not just a set of capabilities.

| Layer | Status |
|---|---|
| L0 DSL — authoring model | shipped (v0.1–0.2) |
| L1 Runtime — synchronous executor + trace stream | shipped (v0.1–0.2) |
| L2 Agentic core — tools + agent loop | shipped (v0.3) |
| L3 Durability — sqlite event log, resume-from-failure | shipped (v0.4) |
| L4 Control plane — API + worker pool + run queue | shipped (v0.5) |
| L5 Observability — read-only trace-timeline dashboard | shipped (v0.6) |
| **L6 Vertical-slice app — support-triage product** | **this doc (v0.7)** |

## The product

A **support-ticket triage service**. Given a ticket, it:

1. **`investigate`** (agent step) — classifies the ticket's priority and finds
   the most relevant knowledge-base article(s), by calling the app's own tools.
2. **`draft`** (llm step) — writes a customer-facing reply grounded in those
   findings.

```
ticket ──▶ [agent: classify_priority + search_kb] ──▶ [llm: draft reply] ──▶ reply
```

It runs three ways, all on the same program:

```bash
support-triage run --ticket "..." --dry-run          # one ticket, durably, in-process
support-triage run --ticket "..." --backend openai   # real model (DeepSeek/Ollama)
support-triage serve --store runs.db --backend openai # API + workers + dashboard
```

## What shipped

`src/threadlang/apps/support_triage/` — a consumer of the library, not part of
the core:

- **`kb.py`** — a small in-process knowledge base (`Article` records).
- **`tools.py`** — the app's own tools and `build_registry()`:
  - `classify_priority(text)` — deterministic keyword rules → P0/P1/P2.
  - `search_kb(query)` — token-overlap search over the KB, tag hits weighted.
  - `build_registry()` returns `default_registry()` **extended** with both — the
    agent step references them by name, the registry is the only thing that can
    run them. Same allow-list boundary the core defines, now carrying app logic.
- **`triage.thread`** — the two-step program (agent `investigate` → llm `draft`).
- **`app.py`** — the `support-triage` entrypoint with `run` (one ticket via
  `run_durable`) and `serve` (the L4 server with this app's registry wired in).
- One plumbing change in the core: `server.serve()` now accepts a `tools`
  registry and passes it to the `WorkerPool` (which already took one), so an app
  can serve its own domain programs over the API.

## Key design decisions

**1. The app is a consumer, not a layer.** It adds a program, a tool registry,
and a thin entrypoint — zero new runtime, store, queue, or dashboard code. The
single core edit (`serve(tools=...)`) is a parameter, not a mechanism. This is
the point of the slice: if building a real product required changing the core,
the layering would be wrong.

**2. App tools are the real extension point.** The core ships `echo`/`calculator`
only to make the loop demonstrable; a product brings domain tools. `build_registry`
extends the built-ins rather than replacing them, and the agent step's
`tools [ classify_priority, search_kb ]` allow-list still gates execution — the
app gains capability without widening the boundary.

**3. Deterministic tools → the whole product is golden-testable.** Both tools
are pure functions of their arguments over an in-process KB (no network, no
key). Because `DryRunClient.agent_step` deterministically fires the first
allow-listed tool, the full pipeline runs end to end under `--dry-run` and the
trace proves a real custom tool was called — so the app has unit tests *and* an
end-to-end test with no live model. (Under dry-run the agent passes placeholder
tool arguments; a real model reads the actual ticket. The plumbing is what the
dry-run path proves.)

**4. Rule-based priority, not a model call.** Classification is deterministic
keyword rules, not an LLM step — it is cheap, inspectable, and testable, and a
model is not needed to decide P0/P1/P2. The model is spent only where it earns
it: drafting the reply. This is the per-step cost-routing the platform is built
to express.

**5. No new persistence/queue/UI.** A triage run is an ordinary durable run: it
checkpoints, resumes, enqueues over `POST /runs`, and renders on the dashboard
with no app-specific support. The product inherits crash-safety and
observability for free — which is the entire bet.

## Verification

- Suite: 44/44 (37 prior + 6 new triage tests + the shared plumbing). The
  triage tests cover: the priority rules (P0/P1/P2 + empty); KB search hits the
  right article and returns "no matching articles" on a miss; the registry
  extends the built-ins; the program parses as agent→llm with the two tools;
  an end-to-end dry-run produces a reply and the trace proves a custom tool was
  called; and the **durable + queued path** (enqueue → `WorkerPool.drain` with
  the app registry → `completed`, with the custom-tool call in the persisted
  trace).
- **Live, end-to-end:** `support-triage run --dry-run` triaged a ticket to a
  completed durable run whose trace shows `classify_priority` called. Then
  `support-triage serve` (2 workers, dry-run): `POST /runs` enqueued a triage
  ticket → `GET /runs/{id}` showed `completed` with the `classify_priority`
  call in the trace → `GET /ui/runs/{id}` rendered the run with the agent step
  and the tool call (HTML-escaped) on the timeline.

## What this slice deliberately keeps small

The KB is in-process and the tools are pure — swapping in a real datastore or a
vector search is a tool-implementation detail that does not touch the program,
runtime, store, control plane, or dashboard. The product is intentionally one
clean path (triage → reply), not a feature-complete help desk: its job is to
prove the platform composes, not to be the platform's roadmap.
