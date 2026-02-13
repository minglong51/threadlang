# ThreadLang v0.1 Specification (Skeleton)

ThreadLang is a deterministic, traceable DSL for composing threaded prompts and outputs.
This document describes the **minimal v0.1 subset** implemented in this repository.

## Design goals

- **Deterministic parsing:** a source file has one unambiguous parse tree.
- **Deterministic runtime:** evaluation order and output are stable for the same source + inputs.
- **Traceability:** runtime emits structured events (`parse_ok`, `context_set`, `emit`).

## Program shape

A program starts with a single thread block:

```threadlang
thread Name {
  ...blocks...
}
```

Supported block families in v0.1:

- `context { ... }`
- `inputs { ... }` *(placeholder in v0.1 parser; parsed as opaque block)*
- `rules { ... }` *(placeholder in v0.1 parser; parsed as opaque block)*
- `steps { ... }` *(placeholder in v0.1 parser; parsed as opaque block)*
- `emit text { ... }`

## context block

The context block contains string assignments:

```threadlang
context {
  greeting = "Hello"
}
```

These values are available via `context.<key>` inside expressions.

## emit block

v0.1 supports only `emit text { <expr> }`.

Expressions support:

- String literals: `"hello"`
- Variable references: `context.key` and `inputs.key`
- Concatenation: `<expr> + <expr>`

Example:

```threadlang
emit text { context.greeting + ", " + inputs.name + "!" }
```

## Runtime behavior

Given a parsed AST and an input dictionary, runtime:

1. Emits `parse_ok`.
2. Applies context assignments in order and emits `context_set` for each.
3. Evaluates emit blocks in order, concatenates emitted `text`, emits `emit` events.

Result: `{ output: string, trace: TraceEvent[] }`.
