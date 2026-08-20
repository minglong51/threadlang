# ThreadLang — High-Level Design

*Refreshed for v0.13.3. Source line numbers are intentionally omitted where they
would make the design document brittle.*

The supported production boundary is one POSIX process and one local SQLite
store. "Durable" below means step-boundary checkpoints and crash recovery, not
deterministic event-history replay. See [`../production.md`](../production.md).

## Purpose

ThreadLang is a small DSL for **bounded, fully-traceable LLM and agent
workflows**. A `.thread` program declares fixed context, a forward-only graph
of `llm`, tool-using `agent`, and closed-label `route` steps, optional hard
output contracts, and a final `emit`. Source is parsed to the frozen AST and
compiled to canonical Workflow IR; validated IR executes through the strict
IR→AST compatibility bridge, with the established AST interpreter remaining
authoritative. Canonical bytes and a SHA-256 definition fingerprint give
durable runs a stable, reviewable identity. Every execution phase appends a
structured `TraceEvent`. Around that core the repo provides a sqlite-backed
**durable run store** with checkpoint/resume and definition fencing
(`store.py`), a **control plane** (HTTP API + worker pool draining a pending-run
queue, `control.py`/`server.py`), a read-only **observability dashboard**
(`dashboard.py`), **metrics derived purely from the trace** (`metrics.py`), and
a first vertical-slice product, a **support-triage app**
(`apps/support_triage/`). Zero required runtime dependencies
(`pyproject.toml`); `anthropic` is an optional extra (`pyproject.toml`).

## System Context

```
 .thread source ──► threadlang CLI (cli.py) ─────────────┐
                                                            │ parse_program (parser.py)
 HTTP client ──► POST /runs (server.py) ─► RunStore     ▼
 (curl / app)     GET /runs, /metrics, /ui     (sqlite)   run_program (runtime.py)
                        ▲                        ▲  │        │ .complete / .agent_step
                        │                        │  │        ▼
 Browser ──► dashboard HTML (dashboard.py) ──────┘  │      LLMClient (llm.py)
                                                    │       ├─ DryRunClient      (echo, no network)
 WorkerPool threads (control.py) ────────────────┘       ├─ OpenAICompatClient ─► any /v1/chat/completions
   claim pending → run_durable (store.py)               │    (DeepSeek hosted, local Ollama, vLLM…)
                                                            └─ AnthropicClient ──► Anthropic SDK ─► Claude API
 support-triage CLI (apps/support_triage/app.py)              (optional extra)
   wraps the same store/serve/run_durable paths
```

The source-oriented labels show the original entry path. CLI and HTTP callers
may also submit canonical IR; both forms converge on the same Workflow IR
identity and AST compatibility runtime.

External dependencies:

- **OpenAI-compatible endpoints** (DeepSeek hosted by default, local Ollama,
  vLLM, Together…) — reached over **stdlib `urllib`**, no SDK
  (`llm.py`). Auth uses an explicit key or `THREADLANG_API_KEY`;
  `OPENAI_API_KEY` is accepted only for the official HTTPS OpenAI host. Keys
  are optional for local servers and are refused on plain HTTP except for
  loopback endpoints, whose HTTP connections bypass environment proxies.
  Provider redirects are refused, and endpoint URLs cannot carry embedded
  credentials, query parameters, or fragments. Compatible-provider response
  bodies are capped at 8 MiB, and malformed tool-call payloads fail closed
  before the tool execution boundary.
- **Anthropic SDK + Claude API** — only when `AnthropicClient` is used; gated
  behind the `anthropic` extra and `ANTHROPIC_API_KEY` (`llm.py`).
- **sqlite** (stdlib `sqlite3`) — the run store file passed via `--store`
  (`store.py`). No external database server.
- **CI**: GitHub Actions covers Python 3.11–3.13 plus Ruff, mypy, Bandit,
  packaging checks, dependency audit, and a container smoke test
  (`.github/workflows/ci.yml`).

There is no message broker (pending rows in sqlite *are* the queue,
`control.py`), no web framework (stdlib `http.server`, `server.py`), no
JS build (server-rendered HTML with inline CSS, `dashboard.py`).

## Component Map

| Path | Responsibility |
|------|----------------|
| `src/threadlang/__init__.py` | Public API surface — re-exports the whole stack: parse/run, store/durable, control plane, server, dashboard renderers, metrics, clients, tools (`__init__.py`). |
| `src/threadlang/ast.py` | Frozen-dataclass source AST: `Program`, context, expressions (including optional branch references), `Step` + `ExpectRule`, `AgentStep`, `RouteStep`/arms, explicit next targets, and `EmitBlock`. Parser↔current-runtime contract. |
| `src/threadlang/ir.py` | Load-bearing Workflow IR v1 contract: AST compilation, strict untrusted-JSON loading, canonical JSON bytes, SHA-256 fingerprints, and compatibility execution through `program_from_ir`/`run_ir`. It deliberately does not introduce a second interpreter. |
| `src/threadlang/parser.py` | Position-aware lexer and recursive-descent parser. It consumes all source, handles strings/comments structurally, validates graph/reference availability, and raises line/column `ParseError` diagnostics. |
| `src/threadlang/runtime.py` | Deterministic control-flow interpreter. `run_program(...) -> RuntimeResult`; runs llm steps with contracts, agent tool-use loops, forward routing, and emit. Storage-agnostic durability hooks carry traces, checkpoints, and resume outputs. |
| `src/threadlang/llm.py` | Client backends behind a baseline protocol plus optional capabilities: `LLMClient.complete`, `AgentLLMClient.agent_step`, and `RouteLLMClient.route`. `DryRunClient` (deterministic echo + two-phase agent stub), `OpenAICompatClient` (stdlib HTTP), `AnthropicClient` (SDK). |
| `src/threadlang/tools.py` | The agent execution boundary: `ToolSpec`/`Tool`/`FunctionTool`, `ToolRegistry` allow-list, deterministic built-ins `echo` + `calculator` (AST-walked arithmetic, no `eval`, no `**`, `tools.py`). |
| `src/threadlang/trace.py` | `TraceEvent(phase, message, data)`, `Trace` alias, `DenialCode` enum. The durable record's unit. |
| `src/threadlang/store.py` | Durability (L3): `RunStore` (sqlite tables `runs`/`events`/`step_outputs`, WAL/autocommit), canonical definition/input binding with legacy source fencing, bounded queue/retention, CAS resume, write-through traces, step checkpoints, replay, and metrics queries. |
| `src/threadlang/control.py` | Control plane workers (L4): exclusive per-store process lock, orphan requeue, atomic claim, source-or-IR execution, per-thread stores, exception-contained worker loops, and readiness state. |
| `src/threadlang/server.py` | Authenticated stdlib JSON API + dashboard host: source-or-IR `POST /runs`, paginated run queries, metrics, liveness/readiness, Host/origin/body/input admission checks, and HTML views. `serve()` starts the exclusive worker pool and server together. |
| `src/threadlang/dashboard.py` | Observability (L5): pure `(record, events, metrics) -> HTML` renderers for the run list (with aggregate panel) and per-run trace timeline; everything `html.escape`d; meta-refresh while a run is in flight. |
| `src/threadlang/metrics.py` | Metrics (v0.8): `compute_metrics` — a pure fold over the trace into `RunMetrics` (deterministic control-flow counts vs observational latency/tokens, kept apart); `aggregate` rolls runs up per-program. |
| `src/threadlang/apps/support_triage/` | Vertical slice (v0.7): `triage.thread` (agent classify+KB-search → llm draft), app tools `classify_priority`/`search_kb` over a bundled in-process KB (`kb.py`), and the `support-triage` entrypoint (`app.py`). Adds no core machinery. |
| `docs/spec.md`, `docs/grammar.ebnf` | Language spec + EBNF grammar. |
| `docs/design/phase-*.md` | Historical per-phase build plans (agentic core → durability → control plane → observability → vertical slice → routing → probes → contracts); HLD/LLD are the live architecture contracts. |
| `examples/*.thread` | Runnable samples spanning interpolation, llm chains, agents, routing, contracts, and the release-report pipeline. |
| `tests/` | Golden, per-version, IR, durability-policy, parser-pressure, provider-security, and server-hardening suites; live provider credentials are not required. |

## Runtime / Deploy Model

Three console scripts, all defined in `pyproject.toml`:

- **`threadlang`** (`threadlang.cli:main`) — one-shot CLI. Reads a `.thread`
  file or canonical IR, picks a backend (`--backend
  dry-run|anthropic|openai`, default anthropic), runs synchronously and fails
  closed if a required real provider is unavailable,
  prints output to stdout; `--store PATH` makes the run durable/resumable and
  `--metrics`/`--trace` print derived views to stderr.
- **`threadlang-serve`** (`threadlang.server:main`) — the long-running control
  plane: one process hosting a `ThreadingHTTPServer` **and** a `WorkerPool`
  against the same sqlite file (`server.py`). Deploy is "run the process
  with a store path"; default bind `127.0.0.1:8765`, default backend dry-run
  (`server.py`). Restart-safe: the queue is `pending` rows in sqlite, and a
  process restart requeues orphaned work and resumes from `run_durable`'s step
  checkpoints. An advisory lock prevents two worker pools from owning one
  store.
- **`support-triage`** (`apps.support_triage.app:main`) — the vertical-slice
  product with two subcommands: `run --ticket ...` (one durable in-process run)
  and `serve` (the same API/workers/dashboard with the app's tool registry
  wired in via `serve(tools=...)`, `app.py`).

It is also a plain **library**: `from threadlang import parse_program,
run_program, run_durable, RunStore, ...` with any object satisfying the client
protocols. Concurrency model: threads only (workers + per-request handler
threads), each opening its own sqlite connection; claims are serialized with
`BEGIN IMMEDIATE` so no run executes twice (`store.py`). Determinism: the
model is the only non-deterministic part; `DryRunClient` makes even agent loops
reproducible end-to-end (`llm.py`).

## How It's Used

```bash
# One-shot CLI (see README.md)
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
curl -X POST localhost:8765/runs -H 'content-type: application/json' \
  -d '{"source":"thread T {...}","inputs":{"x":"hi"}}'
curl localhost:8765/runs/<id>          # status + output + persisted trace
curl localhost:8765/metrics            # aggregate rollup
open http://localhost:8765/            # run list; /ui/runs/<id> for the timeline

# Vertical-slice product
support-triage run --ticket "The dashboard is down with 500s, urgent" --dry-run
support-triage serve --store runs.db --backend openai
```

- `--input key=value` is repeatable and becomes `inputs.<key>` (`cli.py`).
- Per-step model names in the `.thread` file are the cost-routing lever — a
  cheap open model for easy steps, a strong model only where it earns it
  (`README.md`).
- Library use mirrors the tests: `run_program(program, inputs,
  llm_client=..., tools=my_registry)` or `run_durable(..., store)`; see
  `README.md` and `tests/`.
