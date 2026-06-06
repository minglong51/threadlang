# Phase 4 — Observability (v0.6)

## Where this fits

Phase 3 made every run's status and full trace available over JSON. Phase 4
adds no execution surface at all: it renders exactly that persisted data as
HTML — a run list and a per-run *timeline* of the `TraceEvent` stream. The trace
has been the durable record since v0.1; this is the layer that lets a human
read it without curling JSON.

| Layer | Status |
|---|---|
| L0 DSL — authoring model | shipped (v0.1–0.2) |
| L1 Runtime — synchronous executor + trace stream | shipped (v0.1–0.2) |
| L2 Agentic core — tools + agent loop | shipped (v0.3) |
| L3 Durability — sqlite event log, resume-from-failure | shipped (v0.4) |
| L4 Control plane — API + worker pool + run queue | shipped (v0.5) |
| **L5 Observability — read-only trace-timeline dashboard** | **this doc (v0.6)** |
| L6 Vertical-slice app — one concrete multi-agent product | planned |

## What shipped

- **`dashboard.py`** — pure render functions, record/events in, HTML string out:
  - `render_run_list(runs)` — every run with status badge + truncated
    output/error, each linking to its detail page.
  - `render_run_detail(record, events)` — status, inputs, output (or error),
    then the `TraceEvent` timeline: a phase-colored dot per event with its
    message and pretty-printed `data`.
- **HTML routes in `server.py`** — `GET /` and `/ui` → run list;
  `GET /ui/runs/{id}` → detail (`404` for unknown id). Served alongside the
  existing JSON API on the same port via a new `_send_html` helper; the JSON
  routes are unchanged.
- A run still `pending`/`running` emits a `<meta http-equiv="refresh">` so the
  list and timeline update live as workers drive the run; a settled run does not
  refresh.

```
GET /            -> run list (HTML)
GET /ui/runs/{id} -> run detail + trace timeline (HTML)
```

## Key design decisions

**1. Server-rendered, inline CSS, zero client framework.** No build step, no
JS, no dependency — in keeping with the project's zero-dependency promise. The
dashboard is HTML strings assembled from the same `RunStore` the API uses. A
framework would be a real dependency with its own surface for a UI this small.

**2. Pure render functions, golden-testable without a server.** Rendering is
`(record, events) -> str` with no I/O, so the seven dashboard tests assert on
the HTML directly — no sockets, no sleeps. The server smoke test only has to
confirm the routes are wired, not re-test the rendering.

**3. Every interpolated value is `html.escape`d.** Model output and trace data
are untrusted text — a run could `emit` `</pre><script>…`. All values go through
`_esc` (`html.escape(..., quote=True)`); two tests assert a `<script>` payload
in both the output and the trace data renders escaped, never raw. This is the
one real risk a read-only HTML view introduces, so it is tested explicitly.

**4. Read-only, reuses L3/L4 — no new execution surface.** The dashboard adds
no write path and no way to start, mutate, or cancel a run. It is strictly a
view over what L3 persists and L4 already serves; the entire feature is render
code plus three `GET` routes. The observability layer cannot affect a run's
outcome, only display it.

**5. Live refresh by meta-refresh, gated on run state.** A 1-second
`<meta refresh>` is emitted only while a run is `pending`/`running`, so an
in-flight timeline updates as events land but a finished run is static (no
needless polling). Cheap, no websockets, no JS — the simplest thing that shows
a run progressing.

## Verification

- Suite: 38/38 (10 back-compat + 13 agent + 3 durability + 5 control-plane + 7
  dashboard golden tests). The dashboard tests cover: the run list shows each
  run and links to its detail page; the empty state; the list auto-refreshes
  while a run is in flight and stops once settled; the detail page shows status,
  output, and the timeline; a `<script>` payload in the output is escaped; a
  `<script>` payload in trace `data` is escaped; a failed run shows its error.
- **Live, end-to-end:** `threadlang-serve` started with 2 workers; `POST /runs`
  enqueued a one-step program; `GET /` rendered the run list with a `completed`
  badge linking to the detail page; `GET /ui/runs/{id}` rendered the status,
  the output (`[dry-run:m] go:demo`), and the trace timeline; the page contained
  no raw `<script>` and the step name `'s'` appeared HTML-escaped.

## Next (L6 — vertical-slice app)

Every platform layer is now in place: author (L0), execute + trace (L1), act
(L2), persist + resume (L3), submit + drain (L4), inspect (L5). L6 is one
concrete multi-agent product built on this stack end to end — submitted over the
API, executed durably by the worker pool, and watched on the dashboard — proving
the determinism/trace bet on a real workload rather than a demo program.
