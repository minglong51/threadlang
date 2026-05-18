# ThreadLang

A small DSL for deterministic LLM-workflow programs. Execution is
**parse → AST → runtime → emit**, with structured trace events at every phase.

```thread
thread TwoStep {
  context {
    audience = "a curious 10-year-old"
  }

  steps {
    step extract {
      llm "claude-haiku-4-5-20251001" {
        "Extract the three most important claims from this text. Reply as a numbered list, no preamble. Text:\n" + inputs.text
      }
    }
    step retell {
      llm "claude-haiku-4-5-20251001" {
        "Rewrite the following claims for " + context.audience + ". Keep the meaning, change the words. Claims:\n" + steps.extract.output
      }
    }
  }

  emit text {
    steps.retell.output
  }
}
```

## Install

```bash
pip install threadlang                   # core only
pip install 'threadlang[anthropic]'      # + AnthropicClient (real Claude calls)
```

## Run

```bash
# v0 string interpolation (no LLM)
threadlang examples/hello.thread --input name=world
# → Hello, world!

# v1 with a real Claude call (needs ANTHROPIC_API_KEY)
threadlang examples/summarize.thread --input text="The cat sat on the mat..."

# v1 dry-run — works without an API key; LLM calls are deterministic echoes
threadlang examples/two_step.thread --input text="..." --dry-run

# show structured trace events
threadlang examples/two_step.thread --input text="..." --dry-run --trace
```

## Language

```
context   — deterministic values available during execution     [v1: yes]
steps     — ordered LLM transformations                         [v1: yes]
emit text — string concatenation over expression terms          [v1: yes]
emit llm  — call a model with a rendered prompt                 [v1: yes]
rules     — constraints and invariants                          [planned]
```

Expression terms (joined with `+`):

- string literal: `"hello"`
- context value: `context.<name>`
- input value: `inputs.<name>`
- prior step output: `steps.<step_name>.output`

Full grammar in [`docs/grammar.ebnf`](docs/grammar.ebnf); semantics in
[`docs/spec.md`](docs/spec.md).

## What v1 deliberately does not have

Per the spec: loops, recursion, branching, streaming, tool use, system
prompts, real type system. Held narrow on purpose. Each one is a real
addition with its own design surface; v1 wanted the workflow shape to
actually run end-to-end before expanding.

## Project shape

- Zero runtime dependencies. `anthropic` is an *optional* extra; the
  `DryRunClient` lets you run any program without it.
- Frozen dataclass AST nodes (`src/threadlang/ast.py`).
- Regex-driven parser (`src/threadlang/parser.py`) — small enough that a
  parser-generator dependency would be cost without benefit.
- Deterministic runtime (`src/threadlang/runtime.py`) returns
  `(output, trace, step_outputs)`. Every context binding, step call, and
  expression term appends a `TraceEvent`.
- LLM-client protocol (`src/threadlang/llm.py`) — implement
  `complete(model, prompt) -> str` to plug in any backend (OpenAI,
  Ollama, etc.).
- 10 tests (`tests/`) cover v0 backward-compat, emit-llm rendering, step
  chaining, forward-reference / duplicate-name errors, exception
  wrapping, and the dry-run protocol.

## Roadmap

Next likely additions, ordered by useful-surface ranking:

- `rules` block — pre-/post-conditions per step (e.g., output regex
  constraints, length bounds). Rejection retriggers the step with a
  feedback prompt up to N times.
- Multiple emit blocks (`emit text { ... } emit json { ... }`).
- System-prompt declarations per `llm` call.
- Hand-written recursive-descent parser when the grammar outgrows regex.

## License

MIT — see [LICENSE](LICENSE).
