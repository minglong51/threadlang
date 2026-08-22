# ThreadLang — Low-Level Design

Refreshed for v0.14.0. The supported boundary is one POSIX process and one local
SQLite store; see [`../production.md`](../production.md). Historical line
references elsewhere in this document are explanatory and not API contracts.

> **Refreshed 2026-08-22.** Covers the shipped canonical-IR execution and
> durable-binding path, the v0.12 admission, recovery, and ownership
> hardening, and per-call LLM response journaling on the durable path
> (`journal.py`). Where this disagrees with the code, the code wins.

## Module Breakdown

### `ast.py` — AST node contract

Frozen dataclasses (immutable) shared by parser and runtime:

- `ContextAssignment(name: str, value: str)` (`ast.py`);
  `ContextBlock(assignments: List[ContextAssignment])` (`ast.py`)
- Expression terms: `StringLiteral(value)`, `ContextRef(name)`,
  `InputsRef(name)`, and `StepsRef(step_name, optional=False)`; the optional
  form represents `steps.<name>.output?` on branch joins.
- `Expression(terms: List[ExpressionTerm])` (`ast.py`)
- `ExpectRule(kind, values, pattern, limit)` plus `Step(name, model, prompt,
  next_target=None, expect=())` — contracted single-shot llm step.
- `AgentStep(name, model, prompt, tools=(), max_iters=6, next_target=None)` —
  bounded tool-use loop with an optional explicit edge.
- `RouteArm(label, target)` and `RouteStep(name, model, prompt, arms,
  else_target=None)` — closed-label forward dispatch.
- `StepNode = Union[Step, AgentStep, RouteStep]`.
- `StepsBlock(steps: List[StepNode] = [])` (`ast.py`)
- `EmitBlock(kind: str, expression: Expression, model: Optional[str] = None)` —
  `kind ∈ {"text","llm"}`; `model` only for `llm` (`ast.py`)
- `Program(thread_name, context, steps, emit)` (`ast.py`)

### `parser.py` — position-aware recursive-descent parser

- `_lex(source)` emits typed tokens carrying offsets, lines, and columns. It is
  string/comment aware and enforces source/string limits before parsing.
- `_Parser.parse()` consumes exactly one `thread` declaration and rejects every
  trailing or unrecognized token rather than scanning past it.
- Dedicated context, steps, llm, agent, route, contract, emit, and expression
  productions build the frozen AST without regex carving.
- Static validation rejects duplicates, excessive `max_iters`, unavailable
  `steps.*` references, backward/unknown graph targets, and duplicate route
  labels. `ParseError` includes source position.

### `runtime.py` — interpreter

- `run_program(program, inputs, llm_client=None, tools=None, *, trace=None,
  resume_outputs=None, on_step_complete=None) -> RuntimeResult`
  (`runtime.py`). Defaults: `DryRunClient()` and `default_registry()`. The three keyword hooks are the
  durability seam used by `store.run_durable` — the runtime never sees storage
  (`runtime.py`).
- `RuntimeResult(output: str, trace: Trace, step_outputs: Dict[str, str])`
  (`runtime.py`).
- `_build_context(program, trace) -> Dict[str, str]` (`runtime.py`) — one
  `context` trace event per assignment.
- `_run_steps(...)` walks the forward-only graph from the first declaration.
  A checkpointed step is skipped, its stored output reused, and the resume is
  traced. Fresh `Step`, `AgentStep`, and `RouteStep` nodes dispatch to their
  specific executor; routing or `then ->` selects the next index, and each
  fresh output is checkpointed through `on_step_complete`.
- `_run_llm_step(...)` renders the prompt, calls `complete`, and enforces every
  `expect` rule. A violation is traced and retried once with feedback; a second
  violation fails the run. `one_of` may use the optional `route` client method.
- `_run_route_step(...)` asks for one label from the declared arm set, retries
  one invalid answer, then follows the matching arm, `else`, or fails closed.
- `_run_agent_step(...) -> str` (`runtime.py`) — the tool-use loop:
  1. Requires the client to expose `agent_step` (duck-typed via `getattr`;
     `RuntimeError` otherwise, `runtime.py`).
  2. Validates every allow-listed tool exists in the registry
     (`runtime.py`); collects `specs` and the `allowed` set.
  3. Seeds `messages = [{"role": "user", "content": prompt}]`, traces
     "Agent ... started" with tools + max_iters (`runtime.py`).
  4. Up to `max_iters` turns: call `agent_step(model, messages, tools=specs)`;
     trace the turn (text + tool_calls). Empty `tool_calls` → trace
     "finished", return `response.text` (`runtime.py`).
  5. Otherwise append the assistant message and, per call: if allowed **and**
     registered, `registry.get(name).run(arguments)` — a raising tool becomes
     an observable `"error: ..."` string, not a crash (`runtime.py`);
     else a `phase="denial"` event with `DenialCode.TOOL_NOT_ALLOWED` /
     `TOOL_NOT_REGISTERED` (`runtime.py`). Results feed back as
     `{"role": "tool", "tool_call_id": ...}` messages (`runtime.py`).
  6. Loop exhaustion raises `RuntimeError("... exceeded max_iters ...")`
     (`runtime.py`).
- `_evaluate_emit(...)` (`runtime.py`) — `text` → concat with per-term
  tracing; `llm` → render, assert model (parser invariant, `runtime.py`),
  call the client (wrapped on failure); unknown kind raises.
- `_render_expression(expression, context, inputs, step_outputs, trace=None)`
  (`runtime.py`) — resolves terms; missing context/input or a
  forward/unknown step reference raises `RuntimeError`. Per-term trace events only when `trace`
  is passed — only `emit text` passes it (`runtime.py`).
- `RuntimeError(ValueError)` (`runtime.py`) — shadows the builtin
  deliberately; re-exported by the package (`__init__.py`).

### `llm.py` — client backends

One baseline protocol plus two step-specific capabilities:

- `LLMClient.complete(model: str, prompt: str) -> str` (`llm.py`) — plain
  `llm` steps and `emit llm`.
- `AgentLLMClient.agent_step(model, messages: Sequence[Message],
  tools: Sequence[ToolSpec]) -> AgentTurn` (`llm.py`) — required by `agent`
  steps.
- `RouteLLMClient.route(model, prompt, options: Sequence[str]) -> str`
  (`llm.py`) — optional closed-label capability; the runtime falls back to
  `complete` when it is absent.

All built-in backends implement `complete` and `agent_step`; `DryRunClient`
also implements `route` directly. The CLI's lazy wrapper exposes `route` and
delegates to the selected backend's `complete` fallback when necessary.

Shapes:

- `ToolCall(id, name, arguments: Dict[str, object])` (`llm.py`)
- `AgentTurn(text, tool_calls: Sequence[ToolCall] = ())` — empty `tool_calls`
  means done (`llm.py`)
- `Message = Dict[str, object]` — the runtime-owned normalized message; roles
  `user` / `assistant` (text + tool_calls) / `tool` (tool_call_id + content)
  (`llm.py`)
- `LLMError(RuntimeError)` (`llm.py`)

Backends:

- `DryRunClient` (`llm.py`) — `complete` returns
  `f"[dry-run:{model}] {prompt}"`. `agent_step` is a deterministic two-phase
  loop: if tools exist and no tool has run yet, call the *first* tool with
  placeholder args deterministically derived from its JSON schema
  (`_placeholder_args`, `llm.py`); otherwise finalize, echoing the latest
  tool/user observation (`llm.py`). Makes agent programs golden-testable.
- `AnthropicClient(api_key=None, max_tokens=1024)` (`llm.py`) — lazy-imports
  the SDK (`LLMError` if absent, `llm.py`), reads `ANTHROPIC_API_KEY`
  (`llm.py`). `agent_step` maps normalized messages ↔ Anthropic content
  blocks (`tool_use` / `tool_result`) via `_to_anthropic_messages`
  (`llm.py`).
- `OpenAICompatClient(base_url=None, api_key=None, max_tokens=1024,
  timeout=120.0)` (`llm.py`) — any `/v1/chat/completions` server over
  stdlib `urllib` (`_post`, `llm.py`). Defaults: `THREADLANG_BASE_URL` or
  DeepSeek. An explicit key or `THREADLANG_API_KEY` applies to compatible
  endpoints; ambient `OPENAI_API_KEY` is used only when the endpoint is
  `https://api.openai.com`. Keys are optional for local servers and keyed HTTP
  is accepted only for loopback endpoints, using a proxy-disabled opener.
  Endpoint URLs containing userinfo, a query, or a fragment are rejected, and
  provider redirects are refused. HTTP/URL/JSON failures raise `LLMError`;
  upstream HTTP bodies and endpoint details are never copied into durable
  errors.
  Tool-calling rides the
  OpenAI `tools`/`tool_calls` shape (`_to_openai_messages`, `llm.py`;
  defensive `choices[0].message` extraction in `_openai_message`,
  `llm.py`). Responses over 8 MiB, invalid Unicode/text shapes, and malformed
  or non-object tool-call arguments raise `LLMError` before tool execution.
- `default_client() -> LLMClient` returns `AnthropicClient()` (`llm.py`).

Both HTTP clients hold no mutable per-call state — the thread-safety contract
the shared-client `WorkerPool` relies on (`control.py`).

### `tools.py` — the execution boundary

- `ToolSpec(name, description, parameters: Dict[str, object])` — the JSON
  schema the model sees (`tools.py`).
- `Tool` Protocol: `.spec` + `.run(args: Mapping) -> str` (`tools.py`);
  `FunctionTool(spec, _fn)` wraps a plain callable (`tools.py`).
- `ToolRegistry` (`tools.py`) — `register` (rejects duplicates), `has`,
  `get`, `specs(names)`, `names()`. An agent step references tools by name;
  only the registry turns a name into code.
- Built-ins (`default_registry()`, `tools.py`): `echo` (`tools.py`) and
  `calculator` (`tools.py`). The calculator parses with `ast.parse` and
  walks the tree (`_eval_arithmetic`, `tools.py`) — only numeric literals
  and whitelisted operators (`+ - * / // % `, unary `+/-`); `**` is excluded as
  a DoS vector (`tools.py`). Errors return `"error: ..."` strings.

### `trace.py`

- `TraceEvent(phase: str, message: str, data: Dict[str, Any] = {})`
  (`trace.py`); `Trace = List[TraceEvent]` (`trace.py`).
- `DenialCode(str, Enum)`: `TOOL_NOT_ALLOWED = "tool-not-allowed"`,
  `TOOL_NOT_REGISTERED = "tool-not-registered"` (`trace.py`).
- Phases in use: `context`, `step`, `agent`, `route`, `contract`, `denial`,
  `runtime`, `emit`
  (colors in `dashboard.py`).

### `store.py` — durable run store

- `RunStore(path)` opens a per-thread stdlib sqlite connection in WAL mode,
  enables foreign keys and a five-second busy timeout, applies the schema, and
  performs additive migrations. Writes use autocommit.
- `RunRecord` includes status/output/error plus optional source, source/input
  digests, canonical `definition_json`, definition digest, IR version, and
  timestamps.
- `create_run(...)` inserts `created`; `mark_running(expected=...)` is a
  compare-and-swap transition that fences concurrent CLI resumes.
- `enqueue_run(...)` and `enqueue_ir(...)` bind canonical definition and input identity,
  enforce the pending limit, prune terminal retention, and insert `pending`
  under `BEGIN IMMEDIATE`. `claim_next_pending()` atomically claims the oldest
  row. `requeue_orphans()` moves restart-stranded sourced/IR runs back to
  `pending`.
- Events are sequenced and timestamped; step outputs are upserted checkpoints;
  journaled model calls are appended with a per-run `call_seq` and looked up
  by `(run_id, request_fingerprint, occurrence)`. Per-run and aggregate
  metrics are folds over those persisted events.
- `run_durable(...)` compiles the current program to canonical IR and binds its
  digest with canonical inputs. The source digest remains metadata and the
  identity fence for legacy rows lacking canonical definition identity. Resume
  verifies stored IR integrity, definition/input identity, IR version, and
  eligible status before it loads checkpoints. A completed run replays without
  model calls; a fresh run moves `created→running`; any execution exception
  marks `failed`; success marks `completed`. Unless `journal_llm=False`, the
  run's LLM client is wrapped in `JournaledLLMClient` (`journal.py`) before
  `run_program` sees it, so a resumed run replays the interrupted step's
  completed model calls from `llm_journal` and re-executes at most the single
  in-flight call.

### `journal.py` — per-call LLM response journal

- `JournaledLLMClient(client, store, run_id)` (`journal.py`) — the per-run
  wrapper `run_durable` installs. It exposes `complete` unconditionally and
  `route`/`agent_step` only when the wrapped client has them (class-level
  annotations, conditionally assigned in `__init__`), so the runtime's
  `getattr` capability probes see exactly the wrapped client's surface.
- Every call is keyed by `(run_id, request_fingerprint, occurrence)`: the
  fingerprint is SHA-256 over the canonical JSON of the full request (`kind`
  + `model` + prompt / options / messages+tools; `ToolCall`-carrying messages
  and `ToolSpec`s serialize via `dataclasses.asdict`), and `occurrence` is the
  per-attempt ordinal of that fingerprint, so two identical requests in one
  run keep distinct rows (`journal.py`).
- A journal hit replays the recorded response with no provider call —
  `agent_step` payloads reconstruct an `AgentTurn` (`_agent_turn_from_json`,
  `journal.py`); a miss calls through and persists request + response JSON. A
  fresh run_id starts with an empty journal, so first attempts always call
  live; only resumed runs replay. Tool calls are not journaled and
  re-execute; exactly-once remains out of scope (`docs/production.md`).

### `control.py` — worker pool

- `process_one(store, *, llm_client=None, tools=None) -> Optional[DurableRun]`
  claims one row, loads its bound canonical IR when present (or parses legacy
  source), and calls `run_durable` with the claimed id. Every malformed or
  raising run is marked failed and contained so one job cannot kill a worker.
- `WorkerPool(store_path, *, n_workers=2, llm_client=None, tools=None,
  poll_interval=0.05)` acquires `<store>.worker.lock`, requeues orphaned
  `running` rows, then starts daemon threads with one `RunStore` each. The loop
  contains store/provider infrastructure errors; `is_healthy()`/`status()`
  expose thread liveness. `stop()` joins and releases the lock; `drain()` is
  the synchronous batch/test path and continues past failed jobs.

### `server.py` — HTTP API + dashboard host

- `_Handler(BaseHTTPRequestHandler)` runs on `ThreadingHTTPServer`; each data
  request opens and closes its own `RunStore`. JSON and HTML responses set
  no-store, nosniff, and frame-denial headers; HTML also has a restrictive CSP.
- Tokenless mode admits only loopback Host/origin traffic. A configured bearer
  token gates every data, dashboard, metrics, and submission route;
  `/healthz` and `/readyz` intentionally reveal only database, worker, and
  pending/running queue state.
- `GET /runs` is bounded and paginated; run detail includes the trace. Metrics,
  dashboard list/detail, liveness, and readiness have dedicated routes.
- `POST /runs` requires JSON content type and bounded length, exactly one of
  non-empty UTF-8 `.thread` source or an IR object, and bounded string inputs.
  Source is parsed+compiled and IR is strictly loaded before the canonical
  definition is enqueued. Capacity exhaustion returns 429; validation returns
  4xx without creating a row.
- `make_server(...)` validates bind/auth/admission settings. `serve(...)`
  starts the exclusive `WorkerPool` and HTTP server together and stops both on
  shutdown. The `threadlang-serve` CLI defaults to loopback + dry-run and
  exposes provider, worker, queue, retention, timeout, and auth-token-env knobs.

### `dashboard.py` — pure HTML renderers

- `render_run_list(runs: List[RunRecord], aggregate: Optional[AggregateMetrics])
  -> str` (`dashboard.py`) — table of id/program/status/output-or-error
  with an aggregate metrics chip panel (`_aggregate_panel`, `dashboard.py`);
  meta-refresh every 1s while any run is pending/running (`dashboard.py`).
- `render_run_detail(record, events, metrics=None) -> str` (`dashboard.py`)
  — header (status badge, inputs, output/error), per-run metric chips
  (`_run_metrics_panel`, `dashboard.py`; warn styling for tool errors /
  denials / resumed steps), then the phase-colored `TraceEvent` timeline
  (`_PHASE_COLOR`, `dashboard.py`). If `metrics` is omitted it is derived
  from `events` alone (`dashboard.py`).
- Every interpolated value passes `_esc` = `html.escape(..., quote=True)`
  (`dashboard.py`) — model output and trace data are untrusted.

### `metrics.py` — derived metrics

- `RunMetrics` (`metrics.py`) — deterministic block: `context_vars`,
  `steps_completed`, `agent_steps`, `agent_turns`, `model_calls`
  (= complete calls + agent turns, `metrics.py`), `tool_calls`,
  `tool_errors`, `denials`, `resumed_steps`, `status`; observational block:
  `duration_ms`, `input_tokens`, `output_tokens` (all Optional — `None` means
  "not recorded", not zero). Properties `ok`, `total_tokens`; `to_dict()`
  nests `{deterministic, observational}` (`metrics.py`).
- `compute_metrics(trace, *, status=None, duration_ms=None) -> RunMetrics`
  (`metrics.py`) — a pure fold matching exactly the event shapes
  `runtime.run_program` appends (phase/message patterns, `metrics.py`).
  Token usage is read from any event `data.usage` dict; the built-in clients
  don't emit it yet (`metrics.py`).
- `trace_span_ms(timestamps) -> Optional[float]` (`metrics.py`) — first-to-
  last ISO timestamp span; `None` under two parseable stamps (pre-v0.8 rows).
- `AggregateMetrics` (`metrics.py`) + `aggregate(items:
  Sequence[Tuple[str, RunMetrics]])` (`metrics.py`) — `by_status`,
  `success_rate` = completed/(completed+failed) over terminal runs only,
  `avg_duration_ms`, call/error/denial totals, and a `by_program` breakdown.

### `apps/support_triage/` — vertical slice

- `triage.thread` — `SupportTriage`: agent step `investigate`
  (`deepseek-chat`, `tools [ classify_priority, search_kb ]`, `max_iters 5`)
  → llm step `draft` → `emit text { steps.draft.output }`.
- `app.py` — `PROGRAM_PATH` points at the bundled program (`app.py`;
  shipped via package-data, `pyproject.toml`); `load_program()`
  (`app.py`); `main()` (`app.py`) with subcommands:
  - `serve --store ... [--host --port --workers --backend --base-url]` →
    `serve(..., tools=build_registry())` (`app.py`).
  - `run --ticket ... [--store triage-runs.db] [--dry-run --backend --base-url]`
    → `run_durable(load_program(), {"ticket": ...}, store, tools=registry)`;
    prints `run_id`/status + output; exit 0 iff completed (`app.py`).
- `tools.py` — `classify_priority`: deterministic keyword rules mapping ticket
  text to P0/P1/P2 (`_P0_SIGNALS`/`_P1_SIGNALS`, `tools.py`; `_classify`,
  `tools.py`). `search_kb`: token-overlap scoring over the bundled articles,
  tag hits weighted double, top 2 returned (`_score`, `tools.py`;
  `_search_kb`, `tools.py`). `build_registry()` = `default_registry()` +
  both (`tools.py`).
- `kb.py` — `Article(id, title, body, tags)` (`kb.py`) and the four-article
  in-process `ARTICLES` list (`kb.py`). Swapping in a real store is a
  tool-implementation detail (`kb.py`).

### `ir.py` — versioned canonical IR

`IR_VERSION = "threadlang.ir/v1"`, `LANGUAGE_VERSION = "threadlang/v0.12"`
(`ir.py`). IR v1 losslessly represents the v0.12 source AST for inspection,
stable serialization, and definition fingerprints.

**It is not a second interpreter.** The docstring is explicit: the existing runtime
stays authoritative and execution goes through a strict IR→AST compatibility
bridge; a native IR interpreter is deferred until its execution contract is
separately reviewed and verified. Read that as a deliberate refusal, not a gap.

- Frozen node types mirroring the AST: `IRContextEntry`, `IRTerm`, `IRExpression`,
  `IRExpectation`, `IRRouteArm`, `IRLLMStep`, `IRAgentStep`, `IRRouteStep`,
  `IREmit`, and the `WorkflowIR` root (`ir.py`).
- `compile_program(program: Program) -> WorkflowIR` (`ir.py`) — AST → IR.
- `program_from_ir(workflow: WorkflowIR) -> Program` (`ir.py`) — the bridge back;
  this is what lets a stored definition execute on the existing runtime.
- `run_ir(workflow, inputs, llm_client=None, tools=None) -> RuntimeResult`
  (`ir.py`) is the explicit compatibility execution entry point.
- `load_ir_bytes(payload: bytes) -> WorkflowIR` (`ir.py`) — parse + validate;
  raises `IRCompileError`.
- `canonical_ir_bytes(workflow) -> bytes` (`ir.py`) — UTF-8 JSON with `sort_keys` and
  `(",", ":")` separators. The canonicalization is the point: identity must not
  change because a dict happened to iterate differently.
- `workflow_fingerprint(workflow) -> str` (`ir.py`) — SHA-256 of those bytes.

Imported by `store.py`, `server.py`, `control.py`, `cli.py`, and re-exported from
`__init__.py`, which makes it a load-bearing contract rather than a utility.

### `cli.py` — `threadlang` entry point

- `main() -> int` (`cli.py`) — args: `source` (positional Path),
  `--input k=v` (repeatable, `_parse_inputs` splits on first `=`, `cli.py`),
  `--backend dry-run|anthropic|openai` (default **anthropic**), `--dry-run`
  (shorthand), `--base-url`, provider limits, `--store PATH`, `--resume
  RUN_ID`, `--probe N`, `--trace`, `--metrics`, `--from-ir`, and `--emit-ir`.
- Client selection is dry-run / OpenAI-compatible / Anthropic. A workflow that
  needs a model fails closed when the selected real client is unavailable; a
  pure `emit text` workflow can continue because it never calls the client.
- With `--store`: establishes the run id up front so it can be reported even on
  a crash, then runs `run_durable`; provider-call failures print a shell-quoted
  resume command preserving source/IR mode, provider, endpoint, token limit,
  and timeout, then exit 1. Deterministic runtime failures exit 1 without an
  unusable retry hint. Definition/input/status refusals exit 2. Without a store:
  plain `run_program`, metrics computed from the in-memory trace with
  `status="completed"` (`cli.py`).
- Output to stdout; `run_id`, trace lines, and metrics JSON to stderr.

## Data Models

### `.thread` source contract

The normative grammar is `docs/grammar.ebnf`; this excerpt shows the execution
shape:

```
program     = "thread" name "{" context [ steps ] emit "}"
context     = "context" "{" { name "=" string } "}"
steps       = "steps" "{" { step } "}"
step        = "step" name "{" llm_body | agent_body | route_body "}"
llm_body    = "llm" string "{" expression [ expect ] [ then_decl ] "}"
agent_body  = "agent" string "{" [ "tools" "[" name {"," name} "]" ]
                               [ "max_iters" int ] expression [ then_decl ] "}"
route_body  = "route" string "{" expression arm { arm } [ else_decl ] "}"
arm         = "on" string "->" target
expect      = "expect" "{" expect_rule { expect_rule } "}"
expect_rule = one_of | matches | max_chars | nonempty
then_decl   = "then" "->" target
target      = name | "end"
emit        = "emit" "text" "{" expression "}"
            | "emit" "llm" string "{" expression "}"
expression  = term { "+" term }
term        = string | "context." name | "inputs." name
            | "steps." name ".output" [ "?" ]
```

Constraints: `context` required, `steps` optional, exactly one `emit`, unique
step names, bounded `max_iters`, tool names must be identifiers, and all graph
edges/references must be statically valid and forward-only.

### sqlite schema (`store.py`)

```sql
runs (id TEXT PK, program_name TEXT, status TEXT,   -- created|pending|running|completed|failed
      inputs_json TEXT, source TEXT,                -- source set when enqueued via the API
      program_sha256 TEXT, inputs_sha256 TEXT,      -- reproducibility of what ran
      definition_json TEXT,                         -- the canonical IR (ir.py)
      definition_sha256 TEXT, ir_version TEXT,      -- workflow identity + IR schema
      output TEXT, error TEXT, created_at TEXT, updated_at TEXT)
events (run_id TEXT, seq INTEGER, phase TEXT, message TEXT,
        data_json TEXT, ts TEXT,                    -- ts nullable for pre-v0.8 rows
        PRIMARY KEY (run_id, seq))
step_outputs (run_id TEXT, step_name TEXT, output TEXT,
              PRIMARY KEY (run_id, step_name))
llm_journal (run_id TEXT, call_seq INTEGER,             -- append order within the run
             request_fingerprint TEXT,                  -- sha256 of canonical request JSON
             occurrence INTEGER,                        -- per-attempt ordinal of the fingerprint
             request_json TEXT, response_json TEXT, created_at TEXT,
             PRIMARY KEY (run_id, call_seq))
```

### HTTP JSON contracts (`server.py`)

- `POST /runs` accepts exactly one of `{"source": "<thread program>"}` or
  `{"ir": <workflow object>}`, plus optional `{str: str}` inputs. It returns
  `201 {"run_id", "status": "pending"}`; malformed, oversized,
  non-UTF-8-encodable, or policy-invalid submissions fail before enqueue.
- Run summary (`_run_summary`, `server.py`):
  `{id, program_name, status, inputs, output, error, created_at, updated_at,
  program_sha256, inputs_sha256, definition_sha256, ir_version}`;
  `GET /runs/{id}` adds `trace: [{phase, message, data}]`.
- `GET /runs?limit=N&offset=N` is bounded/paginated. `/healthz` verifies the
  store; `/readyz` also verifies worker liveness and reports queue depth.
- `GET /runs/{id}/metrics` → `{"run_id", "metrics": {deterministic: {...},
  observational: {...}}}` (shape in `metrics.py`).
- `GET /metrics` → `{total_runs, by_status, success_rate, avg_duration_ms,
  total_model_calls, total_tool_calls, total_tool_errors, total_denials,
  by_program: {name: {runs, completed, failed, success_rate,
  avg_duration_ms}}}` (`metrics.py`).

### TraceEvent payloads (by phase)

- `context` — `{name, value}` (`runtime.py`)
- `step` — call `{step, model, prompt}`; output `{step, output}`; resume
  `{step, output, resumed: true}` (`runtime.py`)
- `agent` — started `{step, model, prompt, tools, max_iters}`; turn
  `{step, turn, text, tool_calls: [{name, arguments}]}`; tool call
  `{step, tool, arguments, result}`; finished `{step, turns, output}`
- `route` — decision attempts/violations and chosen `{step, label, target}`
- `contract` — rejected llm output plus the violated rules and retry attempt
- `denial` — `{step, tool, arguments, code, result}` (`runtime.py`)
- `runtime` — term eval `{source, value}` (emit-text only, `runtime.py`)
- `emit` — llm call `{model, prompt}`; final `{output}` (`runtime.py`)

The metrics fold (`metrics.py`) and the dashboard timeline both consume
exactly these shapes — change them in lockstep.

## Main Control Flow

### Queued path (control plane, the production shape)

1. `threadlang-serve --store runs.db ...` (`server.py`) builds a backend
   client, starts `WorkerPool.start()` + `ThreadingHTTPServer`
   (`server.py`).
2. `POST /runs` validates exactly one source/IR definition, canonicalizes it,
   binds its digest with the inputs, then inserts a `pending` row.
3. A worker's `_loop` (`control.py`) calls `process_one`:
   `claim_next_pending()` atomically flips pending→running; the canonical IR
   is loaded through the compatibility bridge; `run_durable` executes with a
   `_WriteThroughTrace` (every event lands in `events` as it happens) and a
   step-checkpoint hook (`store.py`).
4. Inside `run_program`: context → steps (llm calls and/or agent tool-use
   loops) → emit, as detailed above. Success → `mark_completed`; failure →
   `mark_failed` with the error string.
5. Clients poll `GET /runs/{id}` (status + trace) or watch
   `/ui/runs/{id}` — which meta-refreshes until the run settles
   (`dashboard.py`). Metrics on `/metrics` are recomputed from the same
   rows on each request (`store.py`).
6. Crash recovery: after exclusive store ownership is reacquired on process
   restart, sourced/IR `running` rows are requeued. The next claim resumes with
   bound-definition checks and skips checkpointed steps. A completed id
   replays without model calls.

### One-shot CLI path

`threadlang file.thread --input k=v [--store runs.db]` → read + parse →
select client → `run_durable` (durable) or `run_program` (ephemeral) → print
output; on a retryable durable provider-call failure print `resume with:
--store ... --resume <id>` with the original backend/endpoint/limit settings
and a shell-quoted source.

## Error Handling

- **Parse** → `ParseError(ValueError)`: bad thread wrapper (`parser.py`),
  missing context, invalid assignments, unbalanced braces, duplicate or invalid
  steps, bad tool names or iteration bounds, and missing prompt/emit/terms. The
  API converts these to HTTP 400 before enqueuing (`server.py`).
- **Runtime** → `RuntimeError(ValueError)`: non-agent client on an agent step
  (`runtime.py`), unknown allow-listed tools, iteration exhaustion, unknown
  context/input/step references, and unknown emit kinds.
- **Model-call failures are wrapped**: exceptions from `complete`/`agent_step`
  re-raise as `RuntimeError` naming the step/phase, original chained via
  `from exc` (`runtime.py`).
- **Tool failures are observable, not fatal**: a raising tool yields an
  `"error: ..."` result string fed back to the model (`runtime.py`);
  disallowed/unregistered tools yield traced denials (`runtime.py`).
- **Client construction/transport** → `LLMError(RuntimeError)`: SDK missing /
  no key, invalid or insecure keyed endpoint, HTTP status, unreachable host,
  non-JSON body, or malformed choices (`llm.py`).
- **Durable runs**: any exception → `mark_failed` + re-raise (`store.py`);
  `process_one` swallows it so the worker survives (`control.py`); the CLI
  prints a resume command only for provider-call failures, where retrying the
  same bound definition and inputs can make progress.
- **CLI boundaries**: provider/runtime failures exit 1; invalid arguments,
  source/IR, resume identity, filesystem, and sqlite failures exit 2. Common
  operator errors are rendered as one-line diagnostics, not tracebacks.
- **HTTP boundaries**: malformed request targets, JSON/Unicode, wrong content
  type, invalid Host/origin/auth, oversized bodies/inputs, invalid source/IR,
  and capacity exhaustion become explicit 4xx responses. A bad submission
  creates no run; an execution failure becomes a failed run without killing
  its worker.

## Config / Env Surface

### `policy.py` — fail-closed resource limits

Module-level constants, no env override, imported by `ir.py`, `llm.py`,
`parser.py`, `runtime.py`, and `server.py`. Its docstring is the scope
statement worth keeping:
these are **conservative defaults for the single-node runtime, not
distributed-runtime service-level guarantees** — a workload that needs more should
split programs or put an authenticated admission layer in front of the server,
rather than raise the numbers.

| Constant | Value | Bounds |
|---|---|---|
| `MAX_SOURCE_BYTES` | 256 KiB | `.thread` source accepted by the parser |
| `MAX_IR_BYTES` | 1 MiB | serialized IR accepted by `load_ir_bytes` |
| `MAX_STRING_CHARS` | 64 Ki | any single string value |
| `MAX_AGENT_ITERS` | 32 | agent-step loop ceiling |
| `MAX_REGEX_PATTERN_CHARS` / `MAX_REGEX_INPUT_CHARS` | 512 / 64 Ki | regex surface |
| `REGEX_TIMEOUT_SECONDS` | 1.0 | per-match wall clock — the ReDoS floor |
| `MAX_REQUEST_BYTES` | 1 MiB | HTTP body |
| `MAX_PROVIDER_RESPONSE_BYTES` | 8 MiB | OpenAI-compatible response body |
| `MAX_INPUTS` / `MAX_INPUT_KEY_CHARS` / `MAX_INPUT_VALUE_CHARS` | 128 / 128 / 64 Ki | run inputs |
| `DEFAULT_MAX_PENDING_RUNS` / `DEFAULT_MAX_RETAINED_RUNS` | 1 000 / 10 000 | queue + retention |
| `DEFAULT_LIST_LIMIT` / `MAX_LIST_LIMIT` | 100 / 1 000 | list pagination |

Fail-closed means a value over the limit is rejected, never truncated — a silently
clipped program would execute something the author did not write.

### Environment variables

- `ANTHROPIC_API_KEY` — `AnthropicClient` when no `api_key` passed
  (`llm.py`).
- `THREADLANG_BASE_URL` — default endpoint for `OpenAICompatClient`
  (falls back to DeepSeek, `llm.py`).
- `THREADLANG_API_KEY` — generic bearer token for the configured
  OpenAI-compatible endpoint; optional for local servers and refused over
  plain HTTP except on loopback.
- `OPENAI_API_KEY` — fallback only for `https://api.openai.com`, never for
  DeepSeek or an arbitrary compatible host.
- `THREADLANG_AUTH_TOKEN` — default control-plane bearer-token variable;
  `--auth-token-env` selects a different variable name.

CLI flags: see `cli.py` (`threadlang`), `server.py` (`threadlang-serve`),
`app.py` (`support-triage`). Backend defaults differ deliberately:
`threadlang` defaults to `anthropic` and fails closed when a model workflow
cannot construct it; `threadlang-serve` and `support-triage` default to
explicit `dry-run`.

Programmatic knobs: `AnthropicClient(api_key, max_tokens=1024)`;
`OpenAICompatClient(base_url, api_key, max_tokens=1024, timeout=120.0)`;
`WorkerPool(n_workers=2, poll_interval=0.05)`; `serve(host="127.0.0.1",
port=8765, n_workers=2, llm_client, tools)`; `run_program(tools=...)` /
`run_durable(run_id=..., journal_llm=...)`.

Packaging: zero runtime deps (`pyproject.toml`); optional extra
`anthropic>=0.40,<1.0` (`pyproject.toml`); `requires-python >= 3.11`
(`pyproject.toml`); `triage.thread` ships as package data
(`pyproject.toml`). No settings file or dotenv loading exists.
