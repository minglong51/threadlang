# ThreadLang Specification (v0 Prototype)

## Overview

ThreadLang is an experimental DSL for deterministic, structured AI workflow definitions.

This v0 prototype implements a narrow, deterministic slice of the language with the following architecture:

1. Parse source text.
2. Build explicit AST objects.
3. Execute runtime semantics.
4. Emit final output.
5. Record structured trace events.

## Design principles

- Deterministic parsing.
- Explicit, inspectable AST nodes.
- Runtime traceability.
- No hidden magic.
- Clarity over cleverness.
- No external dependencies.

## Non-goals in v0

- Loops.
- Recursion.
- Network calls.
- Real LLM integration.
- Advanced type system.

## Supported syntax (prototype)

A single thread program containing:

- `thread <Name> { ... }`
- `context` block with string assignments.
- `emit text` block with string concatenation over:
  - string literals,
  - `context.<name>`,
  - `inputs.<name>`.

## Runtime behavior

- Context assignments are resolved into a deterministic map.
- `emit text` expression is evaluated left-to-right with strict token forms.
- Runtime returns:
  - emitted output (`str`),
  - structured trace events.
