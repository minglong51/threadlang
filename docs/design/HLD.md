# ThreadLang — High-Level Design

*Refreshed for v0.12. Source line numbers are intentionally omitted where they
would make the design document brittle.*

The supported production boundary is one POSIX process and one local SQLite
store. "Durable" below means step-boundary checkpoints and crash recovery, not
deterministic event-history replay. See [`../production.md`](../production.md).

## Purpose

ThreadLang is a small DSL for **deterministic, fully-traceable LLM and agent
workflows** — the authoring layer of an agent platform whose bet is that every
run is a replayable, inspectable trace (`README.md:5`). A `.thread` program
declares a `context` block of fixed values, an ordered `steps` block where each
step is either a single-shot `llm` call or a tool-using `agent` loop, and a
final `emit` (`text` concat or one more `llm` call). Execution is
**parse → AST → runtime → emit**, and every phase — context binding, step call,
agent turn, tool call, tool result, denial — appends a structured `TraceEvent`
(`src/threadlang/runtime.py:14`). Around that core the repo has grown the full
platform stack: a sqlite-backed **durable run store** with checkpoint/resume
(`store.py`), a **control plane** (HTTP API + worker pool draining a pending-run
queue, `control.py`/`server.py`), a read-only **observability dashboard**
(`dashboard.py`), **metrics derived purely from the trace** (`metrics.py`), and
a first vertical-slice product, a **support-triage app**
(`apps/support_triage/`). Zero required runtime dependencies
(`pyproject.toml:13`); `anthropic` is an optional extra (`pyproject.toml:16`).

## System Context

```
 .thread source ──► threadlang CLI (cli.py:26) ─────────────┐
                                                            │ parse_program (parser.py:74)
 HTTP client ──► POST /runs (server.py:115) ─► RunStore     ▼
 (curl / app)     GET /runs, /metrics, /ui     (sqlite)   run_program (runtime.py:51)
                        ▲                        ▲  │        │ .complete / .agent_step
                        │                        │  │        ▼
 Browser ──► dashboard HTML (dashboard.py) ──────┘  │      LLMClient (llm.py:28)
                                                    │       ├─ DryRunClient      (echo, no network)
 WorkerPool threads (control.py:61) ────────────────┘       ├─ OpenAICompatClient ─► any /v1/chat/completions
   claim pending → run_durable (store.py:317)               │    (DeepSeek hosted, local Ollama, vLLM…)
                                                            └─ AnthropicClient ──► Anthropic SDK ─► Claude API
 support-triage CLI (apps/support_triage/app.py:60)              (optional extra)
   wraps the same store/serve/run_durable paths
```

External dependencies:

- **OpenAI-compatible endpoints** (DeepSeek hosted by default, local Ollama,
  vLLM, Together…) — reached over **stdlib `urllib`**, no SDK
  (`llm.py:232`, base URL default `llm.py:229`). Auth via `THREADLANG_API_KEY`
  (or `OPENAI_API_KEY`), optional for local servers (`llm.py:261`).
- **Anthropic SDK + Claude API** — only when `AnthropicClient` is used; gated
  behind the `anthropic` extra and `ANTHROPIC_API_KEY` (`llm.py:137`,
  `llm.py:143`).
- **sqlite** (stdlib `sqlite3`) — the run store file passed via `--store`
  (`store.py:94`). No external database server.
- **CI**: GitHub Actions runs `pip install -e . pytest && pytest` on Python 3.12
  (`.github/workflows/ci.yml:17`).

There is no message broker (pending rows in sqlite *are* the queue,
`control.py:6`), no web framework (stdlib `http.server`, `server.py:21`), no
JS build (server-rendered HTML with inline CSS, `dashboard.py:9`).

## Component Map

| Path | Responsibility |
|------|----------------|
| `src/threadlang/__init__.py` | Public API surface — re-exports the whole stack: parse/run, store/durable, control plane, server, dashboard renderers, metrics, clients, tools (`__init__.py:28`). |
| `src/threadlang/ast.py` | Frozen-dataclass AST: `Program`, `ContextBlock`, `Step` (llm) and `AgentStep` as distinct node types, `EmitBlock`, expression terms. Parser↔runtime contract. |
| `src/threadlang/parser.py` | Position-aware lexer and recursive-descent parser. It consumes all source, handles strings/comments structurally, validates graph/reference availability, and raises line/column `ParseError` diagnostics. |
| `src/threadlang/runtime.py` | Deterministic interpreter. `run_program(...) -> RuntimeResult`; runs llm steps, agent tool-use loops (allow-list enforced, denials traced), and emit. Storage-agnostic durability hooks (`trace`, `resume_outputs`, `on_step_complete`, `runtime.py:57`). |
| `src/threadlang/llm.py` | Client backends behind two protocols: `LLMClient.complete` and `AgentLLMClient.agent_step`. `DryRunClient` (deterministic echo + two-phase agent stub), `OpenAICompatClient` (stdlib HTTP), `AnthropicClient` (SDK). |
| `src/threadlang/tools.py` | The agent execution boundary: `ToolSpec`/`Tool`/`FunctionTool`, `ToolRegistry` allow-list, deterministic built-ins `echo` + `calculator` (AST-walked arithmetic, no `eval`, no `**`, `tools.py:111`). |
| `src/threadlang/trace.py` | `TraceEvent(phase, message, data)`, `Trace` alias, `DenialCode` enum. The durable record's unit. |
| `src/threadlang/store.py` | Durability (L3): `RunStore` (sqlite tables `runs`/`events`/`step_outputs`, autocommit), `run_durable` (write-through trace + step checkpoints + resume/replay), per-run and aggregate metrics queries. |
| `src/threadlang/control.py` | Control plane workers (L4): `process_one` (atomic claim + execute one pending run) and `WorkerPool` (threads, per-thread stores, shared client). |
| `src/threadlang/server.py` | Stdlib `http.server` JSON API + dashboard host: `POST /runs`, `GET /runs[/{id}[/metrics]]`, `GET /metrics`, `GET /healthz`, `GET /` + `/ui/runs/{id}` (HTML). `serve()` starts pool + server together; `main()` is the `threadlang-serve` script. |
| `src/threadlang/dashboard.py` | Observability (L5): pure `(record, events, metrics) -> HTML` renderers for the run list (with aggregate panel) and per-run trace timeline; everything `html.escape`d; meta-refresh while a run is in flight. |
| `src/threadlang/metrics.py` | Metrics (v0.8): `compute_metrics` — a pure fold over the trace into `RunMetrics` (deterministic control-flow counts vs observational latency/tokens, kept apart); `aggregate` rolls runs up per-program. |
| `src/threadlang/apps/support_triage/` | Vertical slice (v0.7): `triage.thread` (agent classify+KB-search → llm draft), app tools `classify_priority`/`search_kb` over a bundled in-process KB (`kb.py`), and the `support-triage` entrypoint (`app.py`). Adds no core machinery. |
| `docs/spec.md`, `docs/grammar.ebnf` | Language spec + EBNF grammar. |
| `docs/design/phase-*.md` | The per-phase build plans (agentic core → durability → control plane → observability → vertical slice). |
| `examples/*.thread` | Runnable samples: `hello`, `summarize`, `two_step`, `agent`, `release_report`. |
| `tests/` | Golden + per-version suites (`test_golden_hello.py`, `test_v1_llm.py`, `test_v03_agent.py` … `test_v08_metrics.py`), all runnable without a key via `DryRunClient`. |

## Runtime / Deploy Model

Three console scripts, all defined in `pyproject.toml:25`:

- **`threadlang`** (`threadlang.cli:main`) — one-shot CLI. Reads a `.thread`
  file, picks a backend (`--backend dry-run|anthropic|openai`, default
  anthropic with soft dry-run fallback, `cli.py:98`), runs synchronously,
  prints output to stdout; `--store PATH` makes the run durable/resumable and
  `--metrics`/`--trace` print derived views to stderr.
- **`threadlang-serve`** (`threadlang.server:main`) — the long-running control
  plane: one process hosting a `ThreadingHTTPServer` **and** a `WorkerPool`
  against the same sqlite file (`server.py:181`). Deploy is "run the process
  with a store path"; default bind `127.0.0.1:8765`, default backend dry-run
  (`server.py:203`). Restart-safe: the queue is `pending` rows in sqlite, and a
  worker crash mid-run resumes via `run_durable`'s step checkpoints
  (`control.py:11`).
- **`support-triage`** (`apps.support_triage.app:main`) — the vertical-slice
  product with two subcommands: `run --ticket ...` (one durable in-process run)
  and `serve` (the same API/workers/dashboard with the app's tool registry
  wired in via `serve(tools=...)`, `app.py:83`).

It is also a plain **library**: `from threadlang import parse_program,
run_program, run_durable, RunStore, ...` with any object satisfying the client
protocols. Concurrency model: threads only (workers + per-request handler
threads), each opening its own sqlite connection; claims are serialized with
`BEGIN IMMEDIATE` so no run executes twice (`store.py:168`). Determinism: the
model is the only non-deterministic part; `DryRunClient` makes even agent loops
reproducible end-to-end (`llm.py:99`).

## How It's Used

```bash
# One-shot CLI (see README.md:62)
threadlang examples/hello.thread --input name=world
threadlang examples/two_step.thread --input text="..." --dry-run --trace
threadlang examples/agent.thread --input task="what is 21*2?" --backend openai   # DeepSeek, THREADLANG_API_KEY
threadlang examples/summarize.thread --input text="..." \
  --backend openai --base-url http://<host>:11434/v1                            # local Ollama, no key
threadlang examples/release_report.thread --input stats=... --input notes=... \
  --dry-run --store runs.db --metrics                                            # durable + metrics
threadlang <file> --store runs.db --resume <run_id>                              # resume a failed run

# Control plane + dashboard
threadlang-serve --store runs.db --port 8765 --workers 2 --backend openai
curl -X POST localhost:8765/runs -d '{"source":"thread T {...}","inputs":{"x":"hi"}}'
curl localhost:8765/runs/<id>          # status + output + persisted trace
curl localhost:8765/metrics            # aggregate rollup
open http://localhost:8765/            # run list; /ui/runs/<id> for the timeline

# Vertical-slice product
support-triage run --ticket "The dashboard is down with 500s, urgent" --dry-run
support-triage serve --store runs.db --backend openai
```

- `--input key=value` is repeatable and becomes `inputs.<key>` (`cli.py:30`).
- Per-step model names in the `.thread` file are the cost-routing lever — a
  cheap open model for easy steps, a strong model only where it earns it
  (`README.md:350`).
- Library use mirrors the tests: `run_program(program, inputs,
  llm_client=..., tools=my_registry)` or `run_durable(..., store)`; see
  `README.md:163` and `tests/`.
