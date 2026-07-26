# ThreadLang Specification (language v1, runtime v0.12)

## Overview

ThreadLang is a small DSL for deterministic LLM workflow programs.
Execution is parse → AST → runtime → emit, with structured trace events at
every phase.

## Design principles

- Deterministic parsing — same source produces the same AST.
- Explicit, inspectable AST nodes (frozen dataclasses).
- Runtime traceability — every step appends a `TraceEvent`.
- No hidden magic; clarity over cleverness.
- Zero runtime dependencies for `emit text` programs; one optional dep
  (`anthropic`) for programs that call a real model.

## Non-goals in v1

- Loops.
- Recursion.
- Branching / conditionals. *(Since v0.9: forward-only routing — see below.
  Cyclic control flow remains out.)*
- Streaming output.
- Tool use / function calling. *(Since v0.3: `agent` steps run a tool-use
  loop over an allow-listed registry.)*
- System prompts (LLM calls send a single user-role message).
- Advanced type system (terms are strings, period).

These are deliberate. The point of v1 is that the workflow shape (context
→ steps → emit) actually executes; the surface area is held narrow on
purpose so it doesn't outgrow the parser before the model layer earns
extension.

## Supported syntax

```
program     = "thread" name "{" context [ steps ] emit "}"
context     = "context" "{" { name "=" string } "}"
steps       = "steps" "{" { step } "}"
step        = "step" name "{" ( llm_body | agent_body | route_body ) "}"
llm_body    = "llm" string "{" expression [ expect ] [ then ] "}"
agent_body  = "agent" string "{" [ tools ] [ max_iters ] expression [ then ] "}"
expect      = "expect" "{" rule { rule } "}"
rule        = "one_of" string { "," string } | "matches" string
            | "max_chars" number | "nonempty"
route_body  = "route" string "{" expression arm { arm } [ "else" "->" target ] "}"
arm         = "on" string "->" target
then        = "then" "->" target
target      = name | "end"
emit_text   = "emit" "text" "{" expression "}"
emit_llm    = "emit" "llm" string "{" expression "}"
expression  = term { "+" term }
term        = string | "context." name | "inputs." name
            | "steps." name ".output" [ "?" ]
```

- `context` block: name → string-literal map. Required.
- `steps` block: zero or more `step` definitions. Optional. Each step
  calls an LLM and binds the response to `steps.<step_name>.output`.
- `emit` block: required. Either `emit text` (string concatenation over
  expression terms) or `emit llm "<model>" { ... }` (rendered prompt sent
  to the model; response becomes the program output).
- Step names within a single `steps` block must be unique. `end` is a
  reserved jump target and cannot be a step name.
- Source, string, step-count, expression, contract, and `max_iters` limits are
  normative fail-closed runtime policy. Programs exceeding them are invalid.
- Comments and delimiters inside quoted strings are lexical content, not
  structure. The parser consumes all input and reports line/column errors.

### Durability and deployment (v0.12)

The language semantics are independent of storage. The bundled durable runtime
provides step-boundary checkpoints on one POSIX process and one local SQLite
store. It binds a run to hashes of its source and canonical inputs and rejects
concurrent resume with a compare-and-swap transition. A hard crash may repeat
the current incomplete LLM/agent step; this is not deterministic event-history
replay. Side-effecting tools must be declared idempotent to run durably. The
full operational contract is [`production.md`](production.md).

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

1. Build context (deterministic map).
2. For each step in declaration order:
   a. Render its prompt expression against (context, inputs, prior step outputs).
   b. Call `client.complete(model, prompt)` on the provided `LLMClient`.
   c. Bind the response to `steps.<name>.output`.
3. Evaluate the emit block:
   - `emit text` — concatenate expression terms.
   - `emit llm`  — render prompt expression, call model, return response.
4. Return `RuntimeResult(output, trace, step_outputs)`.

Trace events are appended at each context binding, each step (one for
"calling", one for "produced output"), each rendered expression term in
`emit text`, and on the final emit.

## LLM client protocol

```python
class LLMClient(Protocol):
    def complete(self, model: str, prompt: str) -> str: ...
```

Built-in implementations:

- `DryRunClient` — returns `f"[dry-run:{model}] {prompt}"`. Used by tests
  and by `threadlang --dry-run`.
- `AnthropicClient` — real Claude calls via the `anthropic` SDK. Requires
  the optional install (`pip install 'threadlang[anthropic]'`) and
  `ANTHROPIC_API_KEY` in env.

Any object satisfying the `complete(model, prompt) -> str` protocol works;
plug in OpenAI, Ollama, etc., as needed.

## CLI

```
threadlang <source.thread> [--input k=v ...] [--dry-run] [--trace]
```

- `--input` is repeatable. Keys are referenced as `inputs.<key>`.
- `--dry-run` uses `DryRunClient` even if the Anthropic SDK + API key are
  available.
- If the Anthropic SDK / key are missing and the program needs an LLM
  call, the CLI falls back to `DryRunClient` with a warning rather than
  erroring out — useful for "does my program parse and route values
  correctly" checks.
- `--trace` prints structured trace events to stderr after the output.
