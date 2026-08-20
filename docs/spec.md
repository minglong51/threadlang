# ThreadLang Specification (language v1, runtime v0.13)

## Overview

ThreadLang is a small DSL for deterministic LLM workflow programs.
Execution is parse → AST → runtime → emit, with structured trace events at
every phase.

## Design principles

- Deterministic parsing — same source produces the same AST.
- Explicit, inspectable AST nodes (frozen dataclasses).
- Runtime traceability — every step appends a `TraceEvent`.
- No hidden magic; clarity over cleverness.
- Zero required runtime dependencies. The OpenAI-compatible backend uses
  stdlib HTTP; the `anthropic` extra is required only for `AnthropicClient`.

## Non-goals in v1

- Cyclic control flow, loops, and recursion.
- Parallel step scheduling.
- Streaming output.
- External events, human approval, cancellation, and in-flight migration.
- Distributed execution.
- Source-level system prompts (plain llm/emit calls send one user-role prompt;
  agent message history is runtime-owned).
- An advanced type system (expression values are strings).

These are deliberate. The point of v1 is that the workflow shape (context
→ steps → emit) actually executes; the surface area is held narrow on
purpose so it doesn't outgrow the parser before the model layer earns
extension.

## Supported syntax

```
program     = "thread" name "{" context [ steps ] emit_block "}"
context     = "context" "{" { name "=" string } "}"
steps       = "steps" "{" { step } "}"
step        = "step" name "{" ( llm_body | agent_body | route_body ) "}"
llm_body    = "llm" string "{" expression [ expect ] [ then ] "}"
agent_body  = "agent" string "{" [ tools ] [ max_iters ] expression [ then ] "}"
tools       = "tools" "[" [ name { "," name } ] "]"
max_iters   = "max_iters" integer
expect      = "expect" "{" rule { rule } "}"
rule        = "one_of" string { "," string } | "matches" string
            | "max_chars" integer | "nonempty"
route_body  = "route" string "{" expression arm { arm } [ "else" "->" target ] "}"
arm         = "on" string "->" target
then        = "then" "->" target
target      = name | "end"
emit_block  = emit_text | emit_llm
emit_text   = "emit" "text" "{" expression "}"
emit_llm    = "emit" "llm" string "{" expression "}"
expression  = term { "+" term }
term        = string | "context." name | "inputs." name
            | "steps." name ".output" [ "?" ]
integer     = digit { digit }
```

- `context` block: name → string-literal map. Required.
- `steps` block: zero or more forward-only `llm`, `agent`, or `route` step
  definitions. Optional. Each executed step binds its output to
  `steps.<step_name>.output`; route edges can skip later steps.
- `emit` block: required. Either `emit text` (string concatenation over
  expression terms) or `emit llm "<model>" { ... }` (rendered prompt sent
  to the model; response becomes the program output).
- Step names within a single `steps` block must be unique. `end` is a
  reserved jump target and cannot be a step name.
- Source bytes, string literals, regex patterns, and `max_iters` are bounded by
  normative fail-closed policy; values over those limits are rejected.
- Comments and delimiters inside quoted strings are lexical content, not
  structure. The parser consumes all input and reports line/column errors.

### Durability and deployment (v0.13)

The language semantics are independent of storage. The bundled durable runtime
provides step-boundary checkpoints on one POSIX process and one local SQLite
store. It binds a v0.13 run to its canonical Workflow IR and canonical inputs;
the source digest is retained as metadata and as the legacy resume fence for
rows without IR identity. Concurrent resume is rejected with a compare-and-swap
transition. A hard crash may repeat the current incomplete `llm`, `agent`, or
`route` step, or an incomplete `emit llm`; this is not deterministic
event-history replay. Side-effecting tools must be declared idempotent to run
durably. The full operational contract is [`production.md`](production.md).

### Step graph (v0.9)

Steps form a **forward-only DAG**. Every step has an outgoing edge:

- default — fall through to the next declared step;
- `then -> <step|end>` on an `llm`/`agent` body — an explicit edge;
- a `route` body's `on "<label>" -> <target>` arms — conditional edges,
  picked by the model under an output contract (see below);
- `end` — skip the remaining steps and go to emit.

All targets must be declared *after* the step that jumps to them
(parser-enforced), so every step runs at most once per run. This is what
keeps step-name checkpoints, resume, and replay correct with routing.

A `route` step calls its model with the rendered prompt plus a generated
output contract ("Reply with exactly one of: ...") derived from its arm
labels. The reply is normalized (whitespace/quote trim, case-insensitive)
and must equal an arm label. A miss is traced as a rejection and retried
once with the violation fed back; a second miss takes the `else ->` edge
(binding the raw reply as the step output) or fails the run if there is
none. The chosen label is bound to `steps.<name>.output`; the jump itself
is deterministic code.

`steps.<name>.output?` (optional reference) renders as `""` when the step
was skipped by routing — how emit or a join step reads branch outputs.
The non-optional form on a skipped step fails the run.

### Output contracts (v0.11)

An `llm` body may carry an `expect { ... }` block — a conjunction of rules
its reply must satisfy, one per line:

- `one_of "a", "b"` — the reply must be one of the listed values, matched
  with the same normalization as route labels (whitespace/quote trim,
  case-insensitive); the canonical value is what gets bound.
- `matches "<regex>"` — the whole bound output must match the pattern
  (`re.fullmatch`); may appear more than once.
- `max_chars N` — reply length cap.
- `nonempty` — the reply must contain non-whitespace text.

The rendered contract is appended to the prompt, so the contract the
runtime enforces is the contract the model was shown. With any `expect`
present the bound output is whitespace-stripped. `one_of` is applied
first regardless of declaration order, so the other rules validate the
output that will actually be bound. A violating reply is
traced (`contract` phase), retried once with each violation named in the
feedback, and a second violation fails the run — contracts are hard
requirements; there is no `else` edge for llm steps. Rules are validated
at parse time (regexes compile, `max_chars >= 1`, no duplicate values or
rule kinds — `matches` excepted).

A step whose contract includes `one_of` is a closed-enum call and is
dispatched through the client's optional `route(model, prompt, options)`
protocol when present — the dry-run client answers with the first value,
keeping contracted programs runnable offline. `expect` is only valid on
`llm` bodies: a route step's contract is its arms, and an agent step's
final answer is shaped by its tool loop.

## Runtime behavior

1. Execute source through `run_program`, or strictly load Workflow IR v1 and
   bridge it through `program_from_ir`/`run_ir` to the same authoritative AST
   interpreter.
2. Build the deterministic context map.
3. Traverse the forward-only step graph from the first declared step:
   - `llm` renders its prompt and calls `complete` (or optional `route` for a
     `one_of` contract). Contracts are validated, retried once with feedback,
     and then fail closed.
   - `agent` calls `agent_step` in a bounded loop, executes only allow-listed
     tools, feeds observations back to the client, and binds the tool-free
     final answer.
   - `route` calls optional `route` or falls back to `complete`, normalizes the
     closed-label result, retries one rejection, takes the matching forward
     edge, uses `else` after a second miss, or fails if no `else` is declared.
   - After step completion and outgoing-edge resolution, bind the output and
     invoke the optional checkpoint callback. Resumed outputs skip their
     model/tool work.
4. Evaluate the emit block:
   - `emit text` — concatenate expression terms.
   - `emit llm`  — render prompt expression, call model, return response.
5. Append the final emit event and return
   `RuntimeResult(output, trace, step_outputs)`.

Trace phases cover context construction, llm and agent turns, tool calls and
denials, routing, contract rejection, `emit text` term evaluation, checkpoint
reuse, and final emission. A resumed route re-derives its edge from the stored
output without another model call.

## LLM client protocol

```python
class LLMClient(Protocol):
    def complete(self, model: str, prompt: str) -> str: ...

class AgentLLMClient(Protocol):
    def agent_step(
        self,
        model: str,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec],
    ) -> AgentTurn: ...

class RouteLLMClient(Protocol):
    def route(
        self,
        model: str,
        prompt: str,
        options: Sequence[str],
    ) -> str: ...
```

`complete` is the baseline protocol for `llm`, `emit llm`, and the fallback
route path. `agent_step` is required only by `agent` steps. `route` is optional;
when absent, the runtime sends the same closed-label contract through
`complete`.

Built-in implementations:

- `DryRunClient` — deterministic `complete`, first-option routing, and a
  two-phase agent stub. Used by tests and explicit `threadlang --dry-run`.
- `AnthropicClient` — Claude `complete` and native tool use via the optional
  `anthropic` SDK. Requires `pip install 'threadlang[anthropic]'` and
  `ANTHROPIC_API_KEY`.
- `OpenAICompatClient` — dependency-free stdlib HTTP client implementing
  `complete` and `agent_step` through OpenAI `tools`/`tool_calls`. It defaults
  to DeepSeek and can target OpenAI, Ollama, vLLM, or another compatible `/v1`
  endpoint.

Any object satisfying only `complete(model, prompt) -> str` can run llm/emit
work and routes through the fallback. Agent programs require `agent_step`.

## CLI

```
threadlang [--version] [--from-ir] [--emit-ir PATH]
           [--input k=v ...]
           [--backend {dry-run,anthropic,openai}] [--dry-run]
           [--base-url URL] [--max-tokens N] [--timeout SECONDS]
           [--store PATH] [--resume RUN_ID] [--probe N]
           [--trace] [--metrics]
           <SOURCE>
```

- `--input` is repeatable. Keys are referenced as `inputs.<key>`.
- `--from-ir` strictly loads canonical Workflow IR instead of source;
  `--emit-ir PATH` compiles or normalizes IR and exits (`-` writes stdout). It
  cannot combine with `--store`, `--resume`, or `--probe`.
- `--backend` selects a real provider or deterministic dry-run and defaults to
  `anthropic`; `--max-tokens` and `--timeout` configure provider calls.
  `--dry-run` is shorthand for `--backend dry-run`. `--base-url` configures
  the OpenAI-compatible endpoint.
- `--store` enables durable trace/checkpoint persistence. `--resume` requires
  `--store`, verifies the current definition and effective inputs against
  stored identity, and reuses completed checkpoints. `--probe N` also requires
  `--store`, cannot combine with `--resume`, and prints a persisted stability
  report.
- `--trace` prints structured trace events to stderr; `--metrics` prints
  metrics derived from that trace.
- If the selected real provider cannot be constructed and the program needs a
  model call, the CLI exits with an error. It never turns a real run into
  synthetic dry-run output; use `--dry-run` explicitly for plumbing checks.
- A program with no model steps and `emit text` still runs without a provider,
  because its selected client is never called.
