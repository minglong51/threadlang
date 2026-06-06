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
pool that drains a durable run queue. Build plans:
[`docs/design/`](docs/design/).

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

## Roadmap — the platform layers

ThreadLang is the authoring + execution core of a production agent platform.
The remaining layers each keep the determinism/trace bet:

1. **Agentic core** *(v0.3, shipped)* — tools + agent tool-use loop.
2. **Durability** *(v0.4, shipped)* — sqlite run store; the trace becomes an
   event log, so runs checkpoint and resume from failure.
3. **Control plane** *(v0.5, shipped)* — an API + worker pool draining a
   durable run queue.
4. **Observability** — a read-only trace-timeline dashboard.
5. **Vertical-slice app** — one concrete multi-agent product proving it end to end.

## License

MIT — see [LICENSE](LICENSE).
