# ThreadLang

A small, deterministic DSL exploration. Long-term direction is structured
LLM-workflow programs (context → steps → emit, with trace events). **The v0
prototype implements only the language skeleton** — parser, AST, deterministic
runtime, and trace — without any LLM calls or network I/O.

If you came expecting a working LLM-workflow language, ThreadLang isn't that
yet. If you came looking at the *shape* of one — parser, AST, evaluator — read
on.

## What v0 actually does

A program is `thread <Name> { context { ... } emit text { ... } }`. The
context block binds string literals to names; the emit block concatenates
string literals, `context.<name>`, and `inputs.<name>`.

```thread
thread Hello {
  context {
    greeting = "Hello"
  }

  emit text {
    context.greeting + ", " + inputs.name + "!"
  }
}
```

```
$ threadlang examples/hello.thread --input name=world
Hello, world!
```

That is the entire language surface today.

## What's deliberately not in v0

Per [`docs/spec.md`](docs/spec.md), v0 lists these as non-goals:

- Real LLM integration
- Network calls
- External dependencies (Python stdlib only)
- Loops, recursion, types

The `rules` and `steps` blocks named in the language sketch below are
**planned** — not parsed, not implemented.

## Language sketch (the larger shape)

```
context  — deterministic values available during execution      [v0: yes]
rules    — constraints and invariants                            [v0: no]
steps    — ordered workflow transformations (LLM calls etc.)     [v0: no]
emit     — final output expressions                              [v0: text only]
```

Execution model: **parse → AST → runtime → emit → trace**.

## Project shape

- Zero runtime dependencies.
- Frozen dataclass AST nodes (`src/threadlang/ast.py`).
- Regex-driven parser (`src/threadlang/parser.py`) — narrow, explicit, no
  parser-generator dependency.
- Deterministic runtime (`src/threadlang/runtime.py`) returns `(output,
  trace)`. Every evaluation step appends a structured `TraceEvent`.
- Golden test (`tests/test_golden_hello.py`) parses + runs the example and
  asserts the literal output.

## Roadmap

v1 (next): `steps` block + `emit llm "<model>" { ... }` so the language can
actually call a model. That's when the "AI-native DSL" framing earns the
name; until then, this is a compiler skeleton.

## License

MIT — see [LICENSE](LICENSE).
