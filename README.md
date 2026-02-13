# ThreadLang

ThreadLang is an experimental **AI-native domain-specific language (DSL)** for building structured, deterministic LLM workflow definitions.

It is a language project focused on a small interpreter pipeline, not an application framework or prompt wrapper.

## What ThreadLang models

A ThreadLang program is organized into explicit blocks:

- `context`: deterministic values available during execution.
- `rules`: constraints and invariants (planned for future milestones).
- `steps`: ordered workflow transformations (planned for future milestones).
- `emit`: final output expressions.

The execution model is intentionally simple and explicit:

**parse → AST → runtime → emit → trace**

## Hello world

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

With input `name = "world"`, this emits:

```text
Hello, world!
```

## Project goals

- Deterministic pipeline behavior.
- Explicit, typed-in-spirit language structures.
- Traceable execution with structured runtime events.
- Clear internals that are easy to evolve into a larger language.

## Current status

ThreadLang is an **early prototype**. The current implementation is intentionally minimal and supports only a subset of planned syntax needed for the initial milestone.

## License

ThreadLang is licensed under the MIT License. See [LICENSE](LICENSE).
