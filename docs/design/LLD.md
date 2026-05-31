# ThreadLang — Low-Level Design

Reverse-engineered from source. All line references are `file:line` into
`src/threadlang/`.

## Module Breakdown

### `ast.py` — AST node contract

Frozen dataclasses (immutable, hashable) shared by parser and runtime:

- `ContextAssignment(name: str, value: str)` (`ast.py:7`)
- `ContextBlock(assignments: List[ContextAssignment])` (`ast.py:13`)
- Expression terms (the `ExpressionTerm` union, `ast.py:40`):
  - `StringLiteral(value: str)` (`ast.py:18`)
  - `ContextRef(name: str)` (`ast.py:23`)
  - `InputsRef(name: str)` (`ast.py:28`)
  - `StepsRef(step_name: str)` — `steps.<step_name>.output` (`ast.py:33`)
- `Expression(terms: List[ExpressionTerm])` (`ast.py:43`)
- `Step(name: str, model: str, prompt: Expression)` (`ast.py:48`)
- `StepsBlock(steps: List[Step] = [])` (`ast.py:61`)
- `EmitBlock(kind: str, expression: Expression, model: Optional[str] = None)` —
  `kind` is `"text"` or `"llm"`; `model` set only when `kind == "llm"`
  (`ast.py:66`)
- `Program(thread_name: str, context: ContextBlock, steps: StepsBlock, emit: EmitBlock)`
  (`ast.py:80`)

### `parser.py` — regex parser

- `parse_program(source: str) -> Program` (`parser.py:70`) — top-level. Full-matches
  `_THREAD_RE` to extract the thread name and body, then dispatches to the three
  block parsers. Raises `ParseError` (`parser.py:43`) if the outer
  `thread <Name> { ... }` shape doesn't match (`parser.py:72`).
- `_parse_context_block(body) -> ContextBlock` (`parser.py:90`) — searches
  `_CONTEXT_RE`; splits the block body by lines; each non-blank line must
  full-match `_CONTEXT_ASSIGN_RE` (`name = "value"`). Missing block or a bad
  assignment raises `ParseError` (`parser.py:93`, `parser.py:102`).
- `_parse_steps_block(body) -> StepsBlock` (`parser.py:110`) — optional. If no
  `_STEPS_RE` match, returns an empty `StepsBlock`. Otherwise iterates `_STEP_RE`
  matches, tracking `seen_names` to reject duplicates (`parser.py:122`). If the
  block exists but no valid step parsed, raises `ParseError` rather than silently
  dropping code (`parser.py:129`).
- `_parse_emit_block(body) -> EmitBlock` (`parser.py:138`) — tries `_EMIT_LLM_RE`
  first, then `_EMIT_TEXT_RE`. Joins multi-line expression text into one line
  before parsing. Empty expression bodies and a missing emit block raise
  `ParseError` (`parser.py:148`, `parser.py:163`).
- `_parse_expression(expression_text) -> Expression` (`parser.py:168`) — splits on
  `+`, strips each term, and classifies via per-term full-match regexes into
  `StringLiteral` / `ContextRef` / `InputsRef` / `StepsRef`. Unknown term shape
  raises `ParseError` (`parser.py:184`).

Key regexes (`parser.py:47`–`parser.py:67`): `_THREAD_RE`, `_CONTEXT_RE`,
`_STEPS_RE` (permissive nested-brace match), `_STEP_RE`, `_EMIT_TEXT_RE`,
`_EMIT_LLM_RE`, `_CONTEXT_ASSIGN_RE`, `_STEPS_REF_RE`.

### `runtime.py` — interpreter

- `run_program(program, inputs, llm_client=None) -> RuntimeResult` (`runtime.py:48`)
  — orchestrates the whole execution; defaults to `DryRunClient()` when no client
  is given (`runtime.py:63`).
- `RuntimeResult(output: str, trace: Trace, step_outputs: Dict[str, str])` —
  frozen dataclass returned to callers (`runtime.py:41`).
- `_build_context(program, trace) -> Dict[str, str]` (`runtime.py:75`) — materializes
  the context map and emits a `context` trace event per assignment.
- `_run_steps(program, context, inputs, client, trace) -> Dict[str, str]`
  (`runtime.py:89`) — for each step: render prompt, emit a "Calling LLM" trace
  event, call `client.complete(...)`, bind the response to `step_outputs[name]`,
  emit a "produced output" trace event. Client exceptions are wrapped into
  `RuntimeError` (`runtime.py:108`).
- `_evaluate_emit(emit, context, inputs, step_outputs, client, trace) -> str`
  (`runtime.py:123`) — `text` → concatenate (with per-term tracing); `llm` →
  render prompt, assert model present (parser invariant, `runtime.py:135`), call
  the client (failures wrapped, `runtime.py:145`); unknown kind raises
  (`runtime.py:149`).
- `_render_expression(expression, context, inputs, step_outputs, trace=None) -> str`
  (`runtime.py:152`) — resolves each term to a string and joins. Missing
  context/input value or a forward step reference each raise `RuntimeError`
  (`runtime.py:166`, `runtime.py:171`, `runtime.py:176`). Per-term trace events are
  appended only when `trace` is passed (so step-prompt renders are not double-traced;
  only `emit text` passes `trace`, `runtime.py:132`).
- `RuntimeError(ValueError)` (`runtime.py:37`) — runtime failure type (note: shadows
  the builtin; re-exported as the package `RuntimeError`, `__init__.py:5`).

### `llm.py` — client backends

- `LLMClient` Protocol: `complete(self, model: str, prompt: str) -> str`
  (`llm.py:22`).
- `LLMError(RuntimeError)` — raised on client construction/call failure
  (`llm.py:26`).
- `DryRunClient.complete(model, prompt) -> str` returns
  `f"[dry-run:{model}] {prompt}"` (`llm.py:34`).
- `AnthropicClient(api_key=None, max_tokens=1024)` (`llm.py:41`) — lazy-imports the
  SDK (raising `LLMError` if absent), reads `ANTHROPIC_API_KEY` if no key passed,
  and on `.complete(...)` calls `messages.create(...)` with a single user-role
  message, returning the first text block (`llm.py:61`).
- `default_client() -> LLMClient` returns `AnthropicClient()`; raises if SDK/key
  missing (`llm.py:74`).

### `cli.py` — entry point

- `main() -> int` (`cli.py:24`) — argparse over `source` (positional `Path`),
  `--input` (append), `--dry-run`, `--trace`. Reads the file, parses, selects a
  client, runs, prints output, optionally prints trace, returns 0.
- `_parse_inputs(input_flags) -> Dict[str, str]` (`cli.py:14`) — splits each
  `key=value` on the first `=`; raises `ValueError` if no `=` present.

### `trace.py`

- `TraceEvent(phase: str, message: str, data: Dict[str, Any] = {})` (`trace.py:7`)
- `Trace = List[TraceEvent]` (`trace.py:14`)

## Data Models

### Source-language contract (the `.thread` file)

Per `docs/grammar.ebnf` and `docs/spec.md:34`:

```
program     = "thread" name "{" context [ steps ] emit "}"
context     = "context" "{" { name "=" string } "}"
steps       = "steps" "{" { step } "}"
step        = "step" name "{" "llm" string "{" expression "}" "}"
emit_text   = "emit" "text" "{" expression "}"
emit_llm    = "emit" "llm" string "{" expression "}"
expression  = term { "+" term }
term        = string | "context." name | "inputs." name | "steps." name ".output"
```

Constraints enforced by the parser: `context` block required; `steps` optional;
exactly one `emit` block required; step names unique within a block; expression
terms must be one of the four supported shapes.

### Runtime value model

- **context**: `Dict[str, str]` — string→string, sourced from context
  assignments (`runtime.py:75`).
- **inputs**: `Mapping[str, str]` — from `--input k=v` flags (`cli.py:14`) or the
  caller's dict.
- **step_outputs**: `Dict[str, str]` — step name → model response, built in
  declaration order (`runtime.py:96`).
- **RuntimeResult**: `(output, trace, step_outputs)` (`runtime.py:41`).

### TraceEvent shape

`{phase, message, data}` where `phase ∈ {"context", "step", "runtime", "emit"}`.
Example `data` payloads: context binding `{name, value}`; step call `{step,
model, prompt}`; step output `{step, output}`; term eval `{source, value}`;
final emit `{output}` (`runtime.py:79`–`runtime.py:191`).

### No persisted schemas

No database, no config file format, no YAML/JSON contract. The only external
config surface is the `ANTHROPIC_API_KEY` env var and CLI flags.

## Main Control Flow

Primary path: `threadlang <file> --input ...` (a two-step LLM program such as
`examples/two_step.thread`):

1. **CLI parse** (`cli.py:44`) — argparse reads `source`, `--input`, `--dry-run`,
   `--trace`.
2. **Read source** (`cli.py:46`) — `source.read_text("utf-8")`.
3. **Parse** (`cli.py:47` → `parser.py:70`) — `parse_program` returns a `Program`
   AST. Malformed source raises `ParseError` here (propagates as a traceback).
4. **Client selection** (`cli.py:49`–`cli.py:64`):
   - `--dry-run` → `DryRunClient`.
   - else try `AnthropicClient()`; on `LLMError`, fall back to `DryRunClient`,
     warning to stdout only if the program needs an LLM (`steps` present or
     `emit.kind == "llm"`).
5. **Run** (`cli.py:66` → `runtime.py:48`):
   1. `_build_context` materializes the context map + trace events
      (`runtime.py:61`).
   2. `_run_steps` iterates steps in order (`runtime.py:64`): render prompt via
      `_render_expression`, trace the call, `client.complete(model, prompt)`,
      bind output, trace the result.
   3. `_evaluate_emit` (`runtime.py:66`): `emit text` concatenates terms;
      `emit llm` renders and makes a final model call.
   4. Append the final `emit`/"Output emitted" trace event and return
      `RuntimeResult` (`runtime.py:69`).
6. **Print output** (`cli.py:67`) — `result.output` to stdout.
7. **Optional trace** (`cli.py:69`) — each `TraceEvent` to stderr as
   `[phase] message: data`.
8. **Exit 0** (`cli.py:74`).

Worked example (`tests/test_v1_llm.py:77`): a `Pipeline` thread with `extract`
then `retell` steps yields `step_outputs == {"extract": ..., "retell": ...}` and
the two client calls happen in declaration order with `steps.extract.output`
interpolated into `retell`'s prompt.

## Error Handling

- **Parse errors** → `ParseError(ValueError)` (`parser.py:43`): missing `thread`
  wrapper (`parser.py:72`), missing context block (`parser.py:93`), invalid
  assignment (`parser.py:102`), steps block with no valid step (`parser.py:129`),
  duplicate step name (`parser.py:122`), empty/missing emit (`parser.py:148`,
  `parser.py:160`, `parser.py:163`), unsupported expression term (`parser.py:184`).
- **Runtime errors** → `RuntimeError(ValueError)` (`runtime.py:37`): unknown
  context value (`runtime.py:166`), missing input (`runtime.py:171`), forward/unknown
  step reference (`runtime.py:176`), unsupported term type (`runtime.py:182`),
  unknown emit kind (`runtime.py:149`).
- **LLM-call failures are wrapped**: any exception from `client.complete(...)` in a
  step or in emit is caught and re-raised as `RuntimeError` with the originating
  step/phase named and the original chained via `from exc` (`runtime.py:108`,
  `runtime.py:145`). Verified by `test_client_exception_wraps_into_runtime_error`
  (`tests/test_v1_llm.py:180`).
- **Client construction** → `LLMError(RuntimeError)`: SDK not installed
  (`llm.py:49`) or no API key (`llm.py:55`). The CLI catches `LLMError` for its
  soft dry-run fallback (`cli.py:55`).
- **CLI input parsing** → `ValueError` for malformed `--input` (`cli.py:18`).
- Parse errors and unwrapped runtime errors surface as Python tracebacks at the
  CLI; there is no top-level catch in `main()`.

## Config / Env Surface

- **Env var**: `ANTHROPIC_API_KEY` — read by `AnthropicClient` when no explicit
  `api_key` is passed (`llm.py:53`). Required for real model calls; unset triggers
  `LLMError` (and the CLI's dry-run fallback).
- **CLI flags** (`cli.py:25`–`cli.py:43`):
  - `source` (positional) — path to a `.thread` file.
  - `--input key=value` — repeatable; populates `inputs.<key>`.
  - `--dry-run` — force `DryRunClient`.
  - `--trace` — print trace events to stderr.
- **Install extras** (`pyproject.toml:15`): base install has zero deps; the
  `anthropic` extra (`anthropic>=0.40,<1.0`) enables `AnthropicClient`.
- **Programmatic config**: `AnthropicClient(api_key=..., max_tokens=1024)` — the
  only tunable is `max_tokens`, defaulting to 1024 (`llm.py:44`).
- **Python**: requires `>=3.10` (`pyproject.toml:10`).
- No settings file, dotenv loading, or other configuration mechanism exists.
