# ThreadLang

A small DSL for **deterministic, fully-traceable LLM and agent workflows** —
the authoring layer of an agent platform whose bet is that every run should be
a replayable, inspectable trace. Execution is **parse → AST → runtime → emit**,
and every phase (context binding, step call, agent turn, tool call, tool
result) appends a structured `TraceEvent`. The trace is the durable record of
what happened.

As of **v0.3** a step can be an `agent`: a model that runs a tool-use loop, not
just a single prompt (see [Agentic steps](#agentic-steps-v03)). As of **v0.4** a
run can be **durable**: its trace persists to sqlite as an event log, and a run
that crashes [resumes from the last completed step](#durability-v04). As of
**v0.5** there is a [**control plane**](#control-plane-v05): an HTTP API + worker
pool that drains a durable run queue. As of **v0.6** the same server hosts a
read-only [**observability dashboard**](#observability-v06) — a run list and a
per-run trace timeline. As of **v0.7** there is a first product on the stack: a
[**support-triage app**](#vertical-slice-support-triage-v07) that classifies a
ticket, searches a knowledge base, and drafts a reply — durable, queued, and
inspectable like any other run. As of **v0.8** every run has
[**metrics**](#metrics-v08) — a deterministic-vs-observational view *derived
from* the trace (`GET /metrics`), so monitoring and data-driven iteration read
the same events the timeline does. Build plans: [`docs/design/`](docs/design/).

```thread
thread TwoStep {
  context {
    audience = "a curious 10-year-old"
  }

  steps {
    step extract {
      llm "claude-haiku-4-5-20251001" {
        "Extract the three most important claims from this text. Reply as a numbered list, no preamble. Text:\n" + inputs.text
      }
    }
    step retell {
      llm "claude-haiku-4-5-20251001" {
        "Rewrite the following claims for " + context.audience + ". Keep the meaning, change the words. Claims:\n" + steps.extract.output
      }
    }
  }

  emit text {
    steps.retell.output
  }
}
```

## Install

```bash
pip install threadlang                   # core only — includes the OpenAI-compatible
                                         #   backend (DeepSeek / Ollama / vLLM …), zero deps
pip install 'threadlang[anthropic]'      # + AnthropicClient (premium Claude calls)
```

## Run

```bash
# v0 string interpolation (no LLM)
threadlang examples/hello.thread --input name=world
# → Hello, world!

# v1 with a real Claude call (needs ANTHROPIC_API_KEY)
threadlang examples/summarize.thread --input text="The cat sat on the mat..."

# v1 dry-run — works without an API key; LLM calls are deterministic echoes
threadlang examples/two_step.thread --input text="..." --dry-run

# show structured trace events
threadlang examples/two_step.thread --input text="..." --dry-run --trace

# v0.3 agent step — runs a tool-use loop (dry-run needs no API key)
threadlang examples/agent.thread --input task="what is 21*2?" --dry-run --trace
```

## Backends

The model is the only non-deterministic part of a run; everything around it is
deterministic and traced. Pick the backend with `--backend` — the cheap/open
path is first-class, not an afterthought:

```bash
# Open / low-cost (default for agent examples): any OpenAI-compatible endpoint.
# Hosted DeepSeek — reliable native tool-calling, a few cents per run:
export THREADLANG_API_KEY=sk-...      # DeepSeek key
threadlang examples/agent.thread --input task="what is 21*2?" --backend openai

# Free local Ollama (no key, runs on your own GPU) — great for llm/complete steps:
threadlang examples/summarize.thread --input text="..." \
  --backend openai --base-url http://<host>:11434/v1
#   (set the model name in the .thread file to a pulled model, e.g. qwen2.5-coder:14b)

# Premium — Anthropic Claude:
export ANTHROPIC_API_KEY=sk-ant-...
threadlang examples/agent.thread --input task="what is 21*2?" --backend anthropic
```

| Backend | Flag | Auth | `llm` steps | `agent` tool-calls |
|---|---|---|---|---|
| Dry-run (echo) | `--dry-run` | none | deterministic stub | deterministic stub |
| OpenAI-compatible | `--backend openai` | `THREADLANG_API_KEY` (optional for local) | ✓ | ✓ when the model emits native `tool_calls` |
| Anthropic | `--backend anthropic` | `ANTHROPIC_API_KEY` | ✓ | ✓ |

`--backend openai` talks to any `/v1/chat/completions` server (DeepSeek, Ollama,
vLLM, Together, …) over stdlib HTTP — no SDK. Tool-calling rides the OpenAI
`tools`/`tool_calls` shape; DeepSeek supports it natively. Some local models
(e.g. `qwen2.5-coder:14b` via Ollama's `/v1`) describe the call as text instead
of emitting native `tool_calls` — use them for `llm`/`complete` steps, and
DeepSeek (or Claude) for `agent` steps.

## Agentic steps (v0.3)

An `agent` step is a model that can *act*. It runs a tool-use loop — model →
tool calls → observations → model — until the model returns a tool-free
answer or `max_iters` is hit:

```thread
step solve {
  agent "claude-haiku-4-5-20251001" {
    tools [ echo, calculator ]
    max_iters 4
    "Use a tool if it helps. Task: " + inputs.task
  }
}
```

- **`tools [...]`** is an allow-list. The model can only call tools named here,
  and only the `ToolRegistry` can turn a name into executable code — that pair
  is the execution boundary.
- **`max_iters`** bounds the loop (default `6`).
- Built-in deterministic tools: `echo`, `calculator` (side-effect-free, so the
  loop runs end-to-end under `--dry-run`). Register your own via
  `run_program(..., tools=my_registry)`.
- Every model turn, tool call, and tool result is a `TraceEvent` — the entire
  agent run reconstructs from the trace.

## Durability (v0.4)

A run can persist to a sqlite store: its `TraceEvent` stream becomes a durable
event log, each completed step is checkpointed, and the run carries an id +
status. If it crashes, resume it from the last completed step — no re-running
finished work.

```bash
# Persist a run; prints run_id and (on failure) the resume command
threadlang examples/two_step.thread --input text="..." --backend openai --store runs.db

# A crash prints:  run failed; resume with: --store runs.db --resume <id>
threadlang examples/two_step.thread --input text="..." --backend openai \
  --store runs.db --resume <id>          # skips completed steps, continues
```

```python
from threadlang import parse_program, run_durable, RunStore

store = RunStore("runs.db")
durable = run_durable(parse_program(src), {"text": "..."}, store)
durable.run_id              # the run's id
store.get_run(durable.run_id).status        # 'completed' | 'failed' | 'running'
store.load_events(durable.run_id)           # the persisted trace
store.list_runs()                           # all runs (for a dashboard)
```

The runtime stays storage-agnostic — `run_durable` hands it a write-through
trace and a checkpoint callback, so the same executor runs durable or
ephemeral. Checkpoints are step-level; a crash mid-step re-runs that step,
finished steps are reused. Replaying a *completed* run returns the stored
result and makes no model calls. Details:
[`docs/design/phase-2-durability.md`](docs/design/phase-2-durability.md).

## Control plane (v0.5)

Submit runs over HTTP and let a worker pool execute them. The `pending` rows in
the run store *are* the queue — no extra broker — so the queue survives a
restart, and because workers run on the durable path, a worker that crashes
mid-run is recovered by re-dispatching the same id.

```bash
# Start the API + workers (stdlib http.server; zero deps)
threadlang-serve --store runs.db --port 8765 --workers 2 --backend openai

# Enqueue a run → get an id back immediately
curl -X POST localhost:8765/runs -H 'content-type: application/json' \
  -d '{"source":"thread T { context{} steps{} emit text { inputs.x } }","inputs":{"x":"hi"}}'
# → {"run_id": "ab12…", "status": "pending"}

curl localhost:8765/runs/ab12…     # status + output + full persisted trace
curl localhost:8765/runs           # list all runs
```

| Method / path | Does |
|---|---|
| `POST /runs` | enqueue `{source, inputs}` (program validated first) → `run_id` |
| `GET /runs` | list runs (id, status, program, output) |
| `GET /runs/{id}` | one run: status, output, error, and the persisted trace |
| `GET /healthz` | liveness |

Built from `process_one` (claim + run one queued run) and a `WorkerPool` of
threads; the claim is atomic so no run executes twice. Details:
[`docs/design/phase-3-control-plane.md`](docs/design/phase-3-control-plane.md).

## Observability (v0.6)

The control-plane server also hosts a read-only dashboard over the persisted
trace — no extra process, no build step, no JavaScript. The trace has been the
durable record since v0.1; this is the layer that lets a human read it.

```bash
threadlang-serve --store runs.db --port 8765 --workers 2 --backend openai
# then open:
#   http://localhost:8765/            run list (status + output, links to each run)
#   http://localhost:8765/ui/runs/ab12…   one run: status, inputs, output, trace timeline
```

| Path | Renders |
|---|---|
| `GET /` (or `/ui`) | run list — every run, status badge, links to detail |
| `GET /ui/runs/{id}` | one run: status, inputs, output/error, and the `TraceEvent` timeline |

The detail page renders the full event stream — context bindings, step calls,
agent turns, tool calls, tool results — as a phase-colored timeline, so an agent
run reconstructs visually from exactly the data L3 persists. A run still
`pending`/`running` auto-refreshes so its timeline updates live; a settled run
is static. All model output and trace data is `html.escape`d before rendering —
untrusted text never reaches the page raw. Details:
[`docs/design/phase-4-observability.md`](docs/design/phase-4-observability.md).

## Vertical slice: support-triage (v0.7)

The first product *on* the stack — proof the layers compose into something a
user runs. Given a support ticket it runs a two-step program: an `agent` step
classifies priority and searches a knowledge base using the app's **own tools**,
then an `llm` step drafts a customer reply. It is an ordinary durable run, so it
checkpoints, enqueues over the API, and renders on the dashboard with no
app-specific support.

```bash
# one ticket, durably, in-process — deterministic, no key
support-triage run --ticket "The dashboard is down with 500s, urgent" --dry-run

# real model (DeepSeek native tool-calling, or local Ollama)
support-triage run --ticket "..." --backend openai

# or serve the API + workers + dashboard with the app's tool registry wired in
support-triage serve --store runs.db --backend openai
#   POST /runs {"source": <triage.thread>, "inputs": {"ticket": "..."}}
```

The app lives in `src/threadlang/apps/support_triage/` and adds **no core
machinery** — a program (`triage.thread`), a tool registry (`build_registry()`
extends the built-ins with `classify_priority` + `search_kb`), and a thin
entrypoint. The one core change is `serve(tools=...)`, so any app can serve its
own programs over the same API. `classify_priority` is deterministic keyword
rules (no model call — cheap, inspectable); the model is spent only on the
draft. Because the tools are pure and `DryRunClient` fires the first
allow-listed tool, the whole product runs end-to-end under `--dry-run` and is
golden-tested. Details:
[`docs/design/phase-5-vertical-slice.md`](docs/design/phase-5-vertical-slice.md).

## Metrics (v0.8)

Metrics are a **derived view of the trace, never a separately-reported number.**
Every metric is a pure fold over the persisted `TraceEvent` stream, so it cannot
drift from what the dashboard timeline shows, and adding a new metric gives it
to every historical run for free. Two kinds, kept deliberately apart:

- **Deterministic** — pure functions of control flow (steps completed, model
  calls, tool calls/errors, resumed steps). Reproducible given the same inputs
  and model responses.
- **Observational** — wall-clock latency and token counts. Not reproducible;
  recorded for monitoring but never mixed into the deterministic core. A `None`
  token count means "not recorded", distinct from a real zero.

```bash
# per-run metrics on the CLI (with --store, includes wall-clock latency)
threadlang examples/agent.thread --dry-run --input task=… --store runs.db --metrics
```

| Path | Returns |
|---|---|
| `GET /metrics` | aggregate rollup — success rate, avg latency, model/tool-call volume, per-program breakdown |
| `GET /runs/{id}/metrics` | one run's `{deterministic, observational}` metrics |

The dashboard shows the aggregate panel atop the run list and per-run metric
chips on each detail page — same numbers, same source events. `metrics.py`
holds the pure `compute_metrics(trace) -> RunMetrics` fold and `aggregate(...)`;
the store layer adds an `events.ts` column (wall-clock at append) and
`run_metrics` / `aggregate_metrics` query helpers. Token capture from the live
clients is the one piece deferred: the worker pool shares one client and
requires it hold no mutable per-call state, so usage will ride a future
return-shape change rather than client-side state — the fold already reads
`data.usage` the moment it appears.

## Language

```
context   — deterministic values available during execution     [yes]
steps     — ordered transformations: llm or agent               [yes]
  · llm   — call a model with a rendered prompt                 [yes]
  · agent — tool-use loop over an allow-listed tool registry    [v0.3]
emit text — string concatenation over expression terms          [yes]
emit llm  — call a model with a rendered prompt                 [yes]
rules     — constraints and invariants                          [planned]
```

Expression terms (joined with `+`):

- string literal: `"hello"`
- context value: `context.<name>`
- input value: `inputs.<name>`
- prior step output: `steps.<step_name>.output`

Full grammar in [`docs/grammar.ebnf`](docs/grammar.ebnf); semantics in
[`docs/spec.md`](docs/spec.md).

## What this deliberately does not have yet

Still narrow on purpose: no branching/recursion, no streaming, no system
prompts, no real type system, and tools are pure functions with no sandbox or
resource limits. Each is a real addition with its own design surface, sequenced
behind the platform layers below rather than bolted on early.

## Project shape

- Zero runtime dependencies. The OpenAI-compatible backend (DeepSeek / Ollama /
  vLLM …) ships in core over stdlib HTTP; `anthropic` is an *optional* extra; the
  `DryRunClient` lets you run any program — agent steps included — without either.
- Frozen dataclass AST nodes (`src/threadlang/ast.py`); `Step` (llm) and
  `AgentStep` are distinct node types.
- Parser (`src/threadlang/parser.py`) — regex for the flat blocks, plus a
  brace-balanced scan for steps so `llm` and `agent` bodies interleave in
  declaration order. (A hand-written recursive-descent parser is the next
  move when control flow lands.)
- Deterministic runtime (`src/threadlang/runtime.py`) returns
  `(output, trace, step_outputs)`. Every binding, step call, agent turn, tool
  call, and tool result appends a `TraceEvent`.
- LLM-client protocols (`src/threadlang/llm.py`) — `complete(model, prompt)`
  for `llm` steps, `agent_step(model, messages, tools)` for `agent` steps.
  Backends: `OpenAICompatClient` (DeepSeek / Ollama / any `/v1`), `AnthropicClient`,
  `DryRunClient`. Per-step model names are the cost-routing lever — cheap open
  model for easy steps, a strong model only where it earns it.
- Tools (`src/threadlang/tools.py`) — `ToolRegistry` allow-list +
  `FunctionTool` wrapper + deterministic built-ins.
- Durable store (`src/threadlang/store.py`) — `RunStore` (stdlib sqlite) +
  `run_durable`; the trace becomes a persisted event log with step checkpoints
  and resume-from-failure. The runtime stays storage-agnostic.
- Control plane (`src/threadlang/control.py`, `server.py`) — `process_one` +
  `WorkerPool` drain the store's `pending` runs; a stdlib `http.server` JSON API
  enqueues and reports them. The queue is the store; no extra broker.
- Observability (`src/threadlang/dashboard.py`) — pure `(record, events) -> HTML`
  render functions for a read-only run list + per-run trace timeline, served by
  the same `http.server` on `/` and `/ui/runs/{id}`. Server-rendered, inline CSS,
  zero JS; all untrusted text is `html.escape`d.
- Vertical-slice app (`src/threadlang/apps/support_triage/`) — a product *on*
  the stack: a triage program, an app-owned tool registry (`build_registry`
  extends the built-ins), and a `support-triage` entrypoint. Adds no core
  machinery; runs as an ordinary durable/queued/observable run.

## Roadmap — the platform layers

ThreadLang is the authoring + execution core of a production agent platform.
The remaining layers each keep the determinism/trace bet:

1. **Agentic core** *(v0.3, shipped)* — tools + agent tool-use loop.
2. **Durability** *(v0.4, shipped)* — sqlite run store; the trace becomes an
   event log, so runs checkpoint and resume from failure.
3. **Control plane** *(v0.5, shipped)* — an API + worker pool draining a
   durable run queue.
4. **Observability** *(v0.6, shipped)* — a read-only run list + per-run
   trace-timeline dashboard, served by the same control-plane process.
5. **Vertical-slice app** *(v0.7, shipped)* — a support-triage product on the
   stack (agent classify + KB search → llm draft), proving the layers compose
   end to end with no new core machinery.
6. **Metrics** *(v0.8, shipped)* — deterministic + observational metrics
   *derived* from the persisted trace (`metrics.py`), surfaced per-run and as an
   aggregate rollup on the dashboard and `GET /metrics`. The substrate a
   data-driven self-improvement loop reads.

## License

MIT — see [LICENSE](LICENSE).
