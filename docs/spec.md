# ThreadLang Specification (v1)

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
- Branching / conditionals.
- Streaming output.
- Tool use / function calling.
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
step        = "step" name "{" "llm" string "{" expression "}" "}"
emit_text   = "emit" "text" "{" expression "}"
emit_llm    = "emit" "llm" string "{" expression "}"
expression  = term { "+" term }
term        = string | "context." name | "inputs." name | "steps." name ".output"
```

- `context` block: name → string-literal map. Required.
- `steps` block: zero or more `step` definitions. Optional. Each step
  calls an LLM and binds the response to `steps.<step_name>.output`.
- `emit` block: required. Either `emit text` (string concatenation over
  expression terms) or `emit llm "<model>" { ... }` (rendered prompt sent
  to the model; response becomes the program output).
- Step names within a single `steps` block must be unique.

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
