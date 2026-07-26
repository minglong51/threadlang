# ThreadLang — Low-Level Design

Refreshed for v0.12. The supported boundary is one POSIX process and one local
SQLite store; see [`../production.md`](../production.md). Historical line
references elsewhere in this document are explanatory and not API contracts.

## Module Breakdown

### `ast.py` — AST node contract

Frozen dataclasses (immutable) shared by parser and runtime:

- `ContextAssignment(name: str, value: str)` (`ast.py:7`);
  `ContextBlock(assignments: List[ContextAssignment])` (`ast.py:13`)
- Expression terms (`ExpressionTerm` union, `ast.py:40`): `StringLiteral(value)`
  (`ast.py:18`), `ContextRef(name)` (`ast.py:23`), `InputsRef(name)`
  (`ast.py:28`), `StepsRef(step_name)` — `steps.<name>.output` (`ast.py:33`)
- `Expression(terms: List[ExpressionTerm])` (`ast.py:43`)
- `Step(name, model, prompt: Expression)` — single-shot llm step (`ast.py:48`)
- `AgentStep(name, model, prompt, tools: Tuple[str, ...] = (), max_iters: int = 6)`
  — tool-use loop (`ast.py:61`); `StepNode = Union[Step, AgentStep]` (`ast.py:78`)
- `StepsBlock(steps: List[StepNode] = [])` (`ast.py:81`)
- `EmitBlock(kind: str, expression: Expression, model: Optional[str] = None)` —
  `kind ∈ {"text","llm"}`; `model` only for `llm` (`ast.py:86`)
- `Program(thread_name, context, steps, emit)` (`ast.py:100`)

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
  (`runtime.py:51`). Defaults: `DryRunClient()` (`runtime.py:84`),
  `default_registry()` (`runtime.py:85`). The three keyword hooks are the
  durability seam used by `store.run_durable` — the runtime never sees storage
  (`runtime.py:71`).
- `RuntimeResult(output: str, trace: Trace, step_outputs: Dict[str, str])`
  (`runtime.py:44`).
- `_build_context(program, trace) -> Dict[str, str]` (`runtime.py:99`) — one
  `context` trace event per assignment.
- `_run_steps(...)` (`runtime.py:113`) — declaration order. If a step name is
  in `resume_outputs` it is skipped, its stored output reused, and the skip
  traced with `resumed: True` (`runtime.py:125`). Dispatches `AgentStep` vs
  `Step`; calls `on_step_complete(name, output)` after each fresh step
  (`runtime.py:145`).
- `_run_llm_step(...)` (`runtime.py:150`) — render prompt, trace
  "Calling LLM for step", `client.complete(model, prompt)` (any exception
  wrapped in `RuntimeError`, `runtime.py:168`), trace "produced output".
- `_run_agent_step(...) -> str` (`runtime.py:182`) — the tool-use loop:
  1. Requires the client to expose `agent_step` (duck-typed via `getattr`;
     `RuntimeError` otherwise, `runtime.py:196`).
  2. Validates every allow-listed tool exists in the registry
     (`runtime.py:203`); collects `specs` and the `allowed` set.
  3. Seeds `messages = [{"role": "user", "content": prompt}]`, traces
     "Agent ... started" with tools + max_iters (`runtime.py:213`).
  4. Up to `max_iters` turns: call `agent_step(model, messages, tools=specs)`;
     trace the turn (text + tool_calls). Empty `tool_calls` → trace
     "finished", return `response.text` (`runtime.py:251`).
  5. Otherwise append the assistant message and, per call: if allowed **and**
     registered, `registry.get(name).run(arguments)` — a raising tool becomes
     an observable `"error: ..."` string, not a crash (`runtime.py:267`);
     else a `phase="denial"` event with `DenialCode.TOOL_NOT_ALLOWED` /
     `TOOL_NOT_REGISTERED` (`runtime.py:283`). Results feed back as
     `{"role": "tool", "tool_call_id": ...}` messages (`runtime.py:302`).
  6. Loop exhaustion raises `RuntimeError("... exceeded max_iters ...")`
     (`runtime.py:306`).
- `_evaluate_emit(...)` (`runtime.py:311`) — `text` → concat with per-term
  tracing; `llm` → render, assert model (parser invariant, `runtime.py:323`),
  call the client (wrapped on failure); unknown kind raises.
- `_render_expression(expression, context, inputs, step_outputs, trace=None)`
  (`runtime.py:340`) — resolves terms; missing context/input or a
  forward/unknown step reference raises `RuntimeError` (`runtime.py:354`,
  `runtime.py:359`, `runtime.py:363`). Per-term trace events only when `trace`
  is passed — only `emit text` passes it (`runtime.py:320`).
- `RuntimeError(ValueError)` (`runtime.py:40`) — shadows the builtin
  deliberately; re-exported by the package (`__init__.py:23`).

### `llm.py` — client backends

Two protocols, both implemented by every backend:

- `LLMClient.complete(model: str, prompt: str) -> str` (`llm.py:28`) — plain
  `llm` steps and `emit llm`.
- `AgentLLMClient.agent_step(model, messages: Sequence[Message],
  tools: Sequence[ToolSpec]) -> AgentTurn` (`llm.py:63`).

Shapes:

- `ToolCall(id, name, arguments: Dict[str, object])` (`llm.py:35`)
- `AgentTurn(text, tool_calls: Sequence[ToolCall] = ())` — empty `tool_calls`
  means done (`llm.py:45`)
- `Message = Dict[str, object]` — the runtime-owned normalized message; roles
  `user` / `assistant` (text + tool_calls) / `tool` (tool_call_id + content)
  (`llm.py:56`)
- `LLMError(RuntimeError)` (`llm.py:73`)

Backends:

- `DryRunClient` (`llm.py:92`) — `complete` returns
  `f"[dry-run:{model}] {prompt}"`. `agent_step` is a deterministic two-phase
  loop: if tools exist and no tool has run yet, call the *first* tool with
  placeholder args deterministically derived from its JSON schema
  (`_placeholder_args`, `llm.py:77`); otherwise finalize, echoing the latest
  tool/user observation (`llm.py:99`). Makes agent programs golden-testable.
- `AnthropicClient(api_key=None, max_tokens=1024)` (`llm.py:128`) — lazy-imports
  the SDK (`LLMError` if absent, `llm.py:136`), reads `ANTHROPIC_API_KEY`
  (`llm.py:143`). `agent_step` maps normalized messages ↔ Anthropic content
  blocks (`tool_use` / `tool_result`) via `_to_anthropic_messages`
  (`llm.py:194`).
- `OpenAICompatClient(base_url=None, api_key=None, max_tokens=1024,
  timeout=120.0)` (`llm.py:232`) — any `/v1/chat/completions` server over
  stdlib `urllib` (`_post`, `llm.py:269`). Defaults: `THREADLANG_BASE_URL` or
  DeepSeek (`llm.py:229`); key from `THREADLANG_API_KEY` or `OPENAI_API_KEY`,
  optional for local servers (`llm.py:261`). HTTP/URL/JSON failures raise
  `LLMError` with truncated detail (`llm.py:280`). Tool-calling rides the
  OpenAI `tools`/`tool_calls` shape (`_to_openai_messages`, `llm.py:351`;
  defensive `choices[0].message` extraction in `_openai_message`,
  `llm.py:340`; malformed tool-call arguments degrade to `{}`, `llm.py:326`).
- `default_client() -> LLMClient` returns `AnthropicClient()` (`llm.py:387`).

Both HTTP clients hold no mutable per-call state — the thread-safety contract
the shared-client `WorkerPool` relies on (`control.py:66`).

### `tools.py` — the execution boundary

- `ToolSpec(name, description, parameters: Dict[str, object])` — the JSON
  schema the model sees (`tools.py:32`).
- `Tool` Protocol: `.spec` + `.run(args: Mapping) -> str` (`tools.py:43`);
  `FunctionTool(spec, _fn)` wraps a plain callable (`tools.py:49`).
- `ToolRegistry` (`tools.py:61`) — `register` (rejects duplicates), `has`,
  `get`, `specs(names)`, `names()`. An agent step references tools by name;
  only the registry turns a name into code.
- Built-ins (`default_registry()`, `tools.py:181`): `echo` (`tools.py:95`) and
  `calculator` (`tools.py:162`). The calculator parses with `ast.parse` and
  walks the tree (`_eval_arithmetic`, `tools.py:125`) — only numeric literals
  and whitelisted operators (`+ - * / // % `, unary `+/-`); `**` is excluded as
  a DoS vector (`tools.py:108`). Errors return `"error: ..."` strings.

### `trace.py`

- `TraceEvent(phase: str, message: str, data: Dict[str, Any] = {})`
  (`trace.py:13`); `Trace = List[TraceEvent]` (`trace.py:20`).
- `DenialCode(str, Enum)`: `TOOL_NOT_ALLOWED = "tool-not-allowed"`,
  `TOOL_NOT_REGISTERED = "tool-not-registered"` (`trace.py:8`).
- Phases in use: `context`, `step`, `agent`, `denial`, `runtime`, `emit`
  (colors in `dashboard.py:80`).

### `store.py` — durable run store

- `RunStore(path)` (`store.py:86`) — stdlib sqlite, `isolation_level=None`
  (autocommit: every write durable immediately, `store.py:93`),
  `busy_timeout = 5000` for cross-thread claims (`store.py:98`), schema
  applied idempotently plus `_migrate()` which `ALTER TABLE`s `events.ts` onto
  pre-v0.8 stores (`store.py:102`).
- `RunRecord(id, program_name, status, inputs, output, error, source=None)`
  (`store.py:75`).
- Runs: `create_run(program_name, inputs) -> run_id` (status `running`,
  `store.py:130`), `get_run`, `list_runs()` (newest first, `store.py:144`),
  `mark_running/mark_completed/mark_failed` (`store.py:201`).
- Queue: `enqueue_run(program_name, source, inputs)` inserts `pending` with the
  program source (`store.py:153`); `claim_next_pending()` takes the oldest
  pending under `BEGIN IMMEDIATE` and flips it to `running` — the atomic claim
  that guarantees single execution (`store.py:168`).
- Events: `append_event(run_id, event)` assigns `seq = MAX(seq)+1` and stamps
  wall-clock `ts` (`store.py:221`); `load_events(run_id) -> Trace`
  (`store.py:232`).
- Checkpoints: `save_step_output` (upsert, `store.py:244`) /
  `load_step_outputs` (`store.py:251`).
- Metrics queries: `run_metrics(run_id) -> Optional[RunMetrics]` (fold of the
  persisted trace + timestamp span, `store.py:265`); `aggregate_metrics()`
  (`store.py:277`).
- `_WriteThroughTrace(List[TraceEvent])` (`store.py:292`) — overrides `append`
  to also persist; the runtime appends through it unknowingly.
- `run_durable(program, inputs, store, *, llm_client=None, tools=None,
  run_id=None) -> DurableRun` (`store.py:317`):
  - `run_id` of a **completed** run → replay: return the stored output, events,
    and step outputs with no model calls (`store.py:340`).
  - `run_id` of a failed/running run → resume: preload `step_outputs` as
    `resume_outputs`, `mark_running` (`store.py:350`).
  - No `run_id` → `create_run`. Then execute `run_program` with the
    write-through trace and a checkpoint closure; any exception →
    `mark_failed` + re-raise (`store.py:370`); success → `mark_completed`.
  - `DurableRun(run_id, result: RuntimeResult)` (`store.py:308`).

### `control.py` — worker pool

- `process_one(store, *, llm_client=None, tools=None) -> Optional[DurableRun]`
  (`control.py:31`) — claim, `parse_program(claimed.source)`, `run_durable`
  with the claimed id. A raising run is already marked failed; the exception is
  swallowed so one bad run never kills a worker (`control.py:55`). Returns
  `None` on empty queue.
- `WorkerPool(store_path, *, n_workers=2, llm_client=None, tools=None,
  poll_interval=0.05)` (`control.py:61`) — `start()` spawns daemon threads
  (`control.py:87`); each `_loop` opens its **own** `RunStore` (sqlite
  connections are per-thread, `control.py:94`) and polls `process_one`,
  waiting `poll_interval` on empty. `stop(timeout=5.0)` joins; `drain(store,
  max_runs=10_000)` processes synchronously in the current thread (tests /
  batch mode, `control.py:112`).

### `server.py` — HTTP API + dashboard host

- `_Handler(BaseHTTPRequestHandler)` (`server.py:32`) on a
  `ThreadingHTTPServer`; each request opens/closes its own `RunStore`
  (`server.py:37`, `server.py:112`).
- `do_GET` (`server.py:59`): `/` and `/ui` → `render_run_list(list_runs,
  aggregate_metrics)`; `/ui/runs/{id}` → `render_run_detail(record, events,
  run_metrics)` (HTML 404 for unknown); `/healthz` → `{"ok": true}`;
  `/metrics` → `aggregate_metrics().to_dict()`; `/runs/{id}/metrics`;
  `/runs` (summaries); `/runs/{id}` (summary + full `trace` array). JSON 404
  otherwise.
- `do_POST /runs` (`server.py:115`): validates JSON body, requires non-empty
  string `source` and dict `inputs`, **parses the program before enqueuing**
  (400 with `parse error: ...` on `ParseError`, `server.py:134`), stringifies
  input keys/values, returns `201 {"run_id", "status": "pending"}`.
- `make_server(store_path, host="127.0.0.1", port=8765)` (`server.py:159`) —
  builds the server, stashing `store_path` on it.
- `serve(store_path, *, host, port, n_workers=2, llm_client=None, tools=None)`
  (`server.py:166`) — starts the `WorkerPool` then blocks in
  `serve_forever()`; `tools` is the seam apps use to serve their own registries
  (`server.py:177`). `main()` (`server.py:197`) is the `threadlang-serve`
  script: `--store` (required), `--host`, `--port`, `--workers`,
  `--backend dry-run|anthropic|openai` (default **dry-run**), `--base-url`.

### `dashboard.py` — pure HTML renderers

- `render_run_list(runs: List[RunRecord], aggregate: Optional[AggregateMetrics])
  -> str` (`dashboard.py:167`) — table of id/program/status/output-or-error
  with an aggregate metrics chip panel (`_aggregate_panel`, `dashboard.py:149`);
  meta-refresh every 1s while any run is pending/running (`dashboard.py:195`).
- `render_run_detail(record, events, metrics=None) -> str` (`dashboard.py:199`)
  — header (status badge, inputs, output/error), per-run metric chips
  (`_run_metrics_panel`, `dashboard.py:129`; warn styling for tool errors /
  denials / resumed steps), then the phase-colored `TraceEvent` timeline
  (`_PHASE_COLOR`, `dashboard.py:80`). If `metrics` is omitted it is derived
  from `events` alone (`dashboard.py:208`).
- Every interpolated value passes `_esc` = `html.escape(..., quote=True)`
  (`dashboard.py:90`) — model output and trace data are untrusted.

### `metrics.py` — derived metrics

- `RunMetrics` (`metrics.py:46`) — deterministic block: `context_vars`,
  `steps_completed`, `agent_steps`, `agent_turns`, `model_calls`
  (= complete calls + agent turns, `metrics.py:169`), `tool_calls`,
  `tool_errors`, `denials`, `resumed_steps`, `status`; observational block:
  `duration_ms`, `input_tokens`, `output_tokens` (all Optional — `None` means
  "not recorded", not zero). Properties `ok`, `total_tokens`; `to_dict()`
  nests `{deterministic, observational}` (`metrics.py:82`).
- `compute_metrics(trace, *, status=None, duration_ms=None) -> RunMetrics`
  (`metrics.py:105`) — a pure fold matching exactly the event shapes
  `runtime.run_program` appends (phase/message patterns, `metrics.py:128`).
  Token usage is read from any event `data.usage` dict; the built-in clients
  don't emit it yet (`metrics.py:27`).
- `trace_span_ms(timestamps) -> Optional[float]` (`metrics.py:181`) — first-to-
  last ISO timestamp span; `None` under two parseable stamps (pre-v0.8 rows).
- `AggregateMetrics` (`metrics.py:198`) + `aggregate(items:
  Sequence[Tuple[str, RunMetrics]])` (`metrics.py:228`) — `by_status`,
  `success_rate` = completed/(completed+failed) over terminal runs only,
  `avg_duration_ms`, call/error/denial totals, and a `by_program` breakdown.

### `apps/support_triage/` — vertical slice

- `triage.thread` — `SupportTriage`: agent step `investigate`
  (`deepseek-chat`, `tools [ classify_priority, search_kb ]`, `max_iters 5`)
  → llm step `draft` → `emit text { steps.draft.output }`
  (`triage.thread:1`).
- `app.py` — `PROGRAM_PATH` points at the bundled program (`app.py:34`;
  shipped via package-data, `pyproject.toml:23`); `load_program()`
  (`app.py:37`); `main()` (`app.py:60`) with subcommands:
  - `serve --store ... [--host --port --workers --backend --base-url]` →
    `serve(..., tools=build_registry())` (`app.py:83`).
  - `run --ticket ... [--store triage-runs.db] [--dry-run --backend --base-url]`
    → `run_durable(load_program(), {"ticket": ...}, store, tools=registry)`;
    prints `run_id`/status + output; exit 0 iff completed (`app.py:107`).
- `tools.py` — `classify_priority`: deterministic keyword rules mapping ticket
  text to P0/P1/P2 (`_P0_SIGNALS`/`_P1_SIGNALS`, `tools.py:28`; `_classify`,
  `tools.py:45`). `search_kb`: token-overlap scoring over the bundled articles,
  tag hits weighted double, top 2 returned (`_score`, `tools.py:65`;
  `_search_kb`, `tools.py:73`). `build_registry()` = `default_registry()` +
  both (`tools.py:128`).
- `kb.py` — `Article(id, title, body, tags)` (`kb.py:17`) and the four-article
  in-process `ARTICLES` list (`kb.py:25`). Swapping in a real store is a
  tool-implementation detail (`kb.py:6`).

### `cli.py` — `threadlang` entry point

- `main() -> int` (`cli.py:26`) — args: `source` (positional Path),
  `--input k=v` (repeatable, `_parse_inputs` splits on first `=`, `cli.py:16`),
  `--backend dry-run|anthropic|openai` (default **anthropic**), `--dry-run`
  (shorthand), `--base-url`, `--store PATH`, `--resume RUN_ID` (requires
  `--store`, exit 2 otherwise, `cli.py:83`), `--trace`, `--metrics`.
- Client selection (`cli.py:92`): dry-run / openai / anthropic; a failed
  `AnthropicClient()` soft-falls-back to `DryRunClient`, warning only if the
  program actually needs a model (`cli.py:100`).
- With `--store`: establishes the run id up front so it can be reported even on
  a crash (`cli.py:119`), runs `run_durable`; on `LLMError`/`RuntimeError`
  prints the exact resume command and exits 1 (`cli.py:126`). Without:
  plain `run_program`, metrics computed from the in-memory trace with
  `status="completed"` (`cli.py:144`).
- Output to stdout; `run_id`, trace lines, and metrics JSON to stderr.

## Data Models

### `.thread` source contract

Per `docs/grammar.ebnf` and `docs/spec.md` (spec text predates v0.3 — the
grammar file and parser are current):

```
program     = "thread" name "{" context [ steps ] emit "}"
context     = "context" "{" { name "=" string } "}"
steps       = "steps" "{" { step } "}"
step        = "step" name "{" llm_body | agent_body "}"
llm_body    = "llm" string "{" expression "}"
agent_body  = "agent" string "{" [ "tools" "[" name {"," name} "]" ]
                               [ "max_iters" int ] expression "}"
emit        = "emit" "text" "{" expression "}"
            | "emit" "llm" string "{" expression "}"
expression  = term { "+" term }
term        = string | "context." name | "inputs." name | "steps." name ".output"
```

Constraints: `context` required, `steps` optional, exactly one `emit`, unique
step names, `max_iters >= 1`, tool names must be identifiers.

### sqlite schema (`store.py:41`)

```sql
runs (id TEXT PK, program_name TEXT, status TEXT,   -- pending|running|completed|failed
      inputs_json TEXT, source TEXT,                -- source set when enqueued via the API
      output TEXT, error TEXT, created_at TEXT, updated_at TEXT)
events (run_id TEXT, seq INTEGER, phase TEXT, message TEXT,
        data_json TEXT, ts TEXT,                    -- ts nullable for pre-v0.8 rows
        PRIMARY KEY (run_id, seq))
step_outputs (run_id TEXT, step_name TEXT, output TEXT,
              PRIMARY KEY (run_id, step_name))
```

### HTTP JSON contracts (`server.py:5`)

- `POST /runs` body `{"source": "<thread program>", "inputs": {str: str}}` →
  `201 {"run_id", "status": "pending"}`; `400` on bad JSON / missing source /
  non-object inputs / parse error.
- Run summary (`_run_summary`, `server.py:148`):
  `{id, program_name, status, inputs, output, error}`; `GET /runs/{id}` adds
  `trace: [{phase, message, data}]`.
- `GET /runs/{id}/metrics` → `{"run_id", "metrics": {deterministic: {...},
  observational: {...}}}` (shape in `metrics.py:82`).
- `GET /metrics` → `{total_runs, by_status, success_rate, avg_duration_ms,
  total_model_calls, total_tool_calls, total_tool_errors, total_denials,
  by_program: {name: {runs, completed, failed, success_rate,
  avg_duration_ms}}}` (`metrics.py:214`).

### TraceEvent payloads (by phase)

- `context` — `{name, value}` (`runtime.py:103`)
- `step` — call `{step, model, prompt}`; output `{step, output}`; resume
  `{step, output, resumed: true}` (`runtime.py:129`)
- `agent` — started `{step, model, prompt, tools, max_iters}`; turn
  `{step, turn, text, tool_calls: [{name, arguments}]}`; tool call
  `{step, tool, arguments, result}`; finished `{step, turns, output}`
- `denial` — `{step, tool, arguments, code, result}` (`runtime.py:289`)
- `runtime` — term eval `{source, value}` (emit-text only, `runtime.py:373`)
- `emit` — llm call `{model, prompt}`; final `{output}` (`runtime.py:93`)

The metrics fold (`metrics.py:128`) and the dashboard timeline both consume
exactly these shapes — change them in lockstep.

## Main Control Flow

### Queued path (control plane, the production shape)

1. `threadlang-serve --store runs.db ...` (`server.py:197`) builds a backend
   client, starts `WorkerPool.start()` + `ThreadingHTTPServer`
   (`server.py:181`).
2. `POST /runs` (`server.py:115`) validates + parses the source, then
   `store.enqueue_run(...)` inserts a `pending` row → `201 {run_id}`.
3. A worker's `_loop` (`control.py:93`) calls `process_one`:
   `claim_next_pending()` atomically flips pending→running (`store.py:168`);
   the source is re-parsed (`control.py:45`); `run_durable` executes with a
   `_WriteThroughTrace` (every event lands in `events` as it happens) and a
   step-checkpoint hook (`store.py:355`).
4. Inside `run_program`: context → steps (llm calls and/or agent tool-use
   loops) → emit, as detailed above. Success → `mark_completed`; failure →
   `mark_failed` with the error string.
5. Clients poll `GET /runs/{id}` (status + trace) or watch
   `/ui/runs/{id}` — which meta-refreshes until the run settles
   (`dashboard.py:244`). Metrics on `/metrics` are recomputed from the same
   rows on each request (`store.py:277`).
6. Crash recovery: a worker death leaves the run `running` with checkpoints
   intact; calling `run_durable` with the same id resumes, skipping
   checkpointed steps (`store.py:350`, `runtime.py:125`). A completed id
   replays without model calls (`store.py:340`).

### One-shot CLI path

`threadlang file.thread --input k=v [--store runs.db]` → read + parse →
select client → `run_durable` (durable) or `run_program` (ephemeral) → print
output; on durable failure print `resume with: --store ... --resume <id>`
(`cli.py:126`).

## Error Handling

- **Parse** → `ParseError(ValueError)`: bad thread wrapper (`parser.py:77`),
  missing context (`parser.py:96`), invalid assignment (`parser.py:106`),
  unbalanced braces (`parser.py:126`), duplicate step (`parser.py:146`),
  invalid step body (`parser.py:180`), bad tool name / `max_iters < 1`
  (`parser.py:194`, `parser.py:203`), missing prompt/emit/term
  (`parser.py:173`, `parser.py:243`, `parser.py:264`). The API converts these
  to HTTP 400 before enqueuing (`server.py:135`).
- **Runtime** → `RuntimeError(ValueError)`: non-agent client on an agent step
  (`runtime.py:197`), unknown allow-listed tool (`runtime.py:205`), max_iters
  exhaustion (`runtime.py:306`), unknown context/input/step refs
  (`runtime.py:354`–`runtime.py:363`), unknown emit kind (`runtime.py:337`).
- **Model-call failures are wrapped**: exceptions from `complete`/`agent_step`
  re-raise as `RuntimeError` naming the step/phase, original chained via
  `from exc` (`runtime.py:168`, `runtime.py:230`, `runtime.py:333`).
- **Tool failures are observable, not fatal**: a raising tool yields an
  `"error: ..."` result string fed back to the model (`runtime.py:268`);
  disallowed/unregistered tools yield traced denials (`runtime.py:282`).
- **Client construction/transport** → `LLMError(RuntimeError)`: SDK missing /
  no key (`llm.py:138`, `llm.py:145`); HTTP status, unreachable host, non-JSON
  body, malformed choices (`llm.py:280`–`llm.py:288`, `llm.py:344`).
- **Durable runs**: any exception → `mark_failed` + re-raise (`store.py:370`);
  `process_one` swallows it so the worker survives (`control.py:55`); the CLI
  catches it and prints the resume command (`cli.py:123`).
- **CLI**: `--resume` without `--store` → exit 2 (`cli.py:83`); run failures →
  exit 1; malformed `--input` → `ValueError` (`cli.py:20`). Parse errors
  surface as tracebacks (no top-level catch in `main`).

## Config / Env Surface

Environment variables (all read in `llm.py`):

- `ANTHROPIC_API_KEY` — `AnthropicClient` when no `api_key` passed
  (`llm.py:143`).
- `THREADLANG_BASE_URL` — default endpoint for `OpenAICompatClient`
  (falls back to DeepSeek, `llm.py:256`).
- `THREADLANG_API_KEY`, then `OPENAI_API_KEY` — bearer token for
  `OpenAICompatClient`; optional for local servers (`llm.py:261`).

CLI flags: see `cli.py` (`threadlang`), `server.py:202` (`threadlang-serve`),
`app.py:64` (`support-triage`). Backend defaults differ deliberately:
`threadlang` defaults to `anthropic` with soft fallback; `threadlang-serve`
and `support-triage` default to `dry-run`.

Programmatic knobs: `AnthropicClient(api_key, max_tokens=1024)`;
`OpenAICompatClient(base_url, api_key, max_tokens=1024, timeout=120.0)`;
`WorkerPool(n_workers=2, poll_interval=0.05)`; `serve(host="127.0.0.1",
port=8765, n_workers=2, llm_client, tools)`; `run_program(tools=...)` /
`run_durable(run_id=...)`.

Packaging: zero runtime deps (`pyproject.toml:13`); optional extra
`anthropic>=0.40,<1.0` (`pyproject.toml:16`); `requires-python >= 3.10`
(`pyproject.toml:10`); `triage.thread` ships as package data
(`pyproject.toml:23`). No settings file or dotenv loading exists.
