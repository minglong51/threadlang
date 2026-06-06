# ThreadLang

A small DSL for **deterministic, fully-traceable LLM and agent workflows** —
the authoring layer of an agent platform whose bet is that every run should be
a replayable, inspectable trace. Execution is **parse → AST → runtime → emit**,
and every phase (context binding, step call, agent turn, tool call, tool
result) appends a structured `TraceEvent`. The trace is the durable record of
what happened.

As of **v0.3** a step can be an `agent`: a model that runs a tool-use loop, not
just a single prompt. See [Agentic steps](#agentic-steps-v03) and the build
plan in [`docs/design/phase-1-agentic-core.md`](docs/design/phase-1-agentic-core.md).

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

# v0.3 agent step — runs a tool-use loop (dry-run needs no API key)
threadlang examples/agent.thread --input task="what is 21*2?" --dry-run --trace
```

## Agentic steps (v0.3)

An `agent` step is a model that can *act*. It runs a tool-use loop — model →
tool calls → observations → model — until the model returns a tool-free
answer or `max_iters` is hit:

```thread
step solve {
  agent "claude-haiku-4-5-20251001" {
    tools [ echo, calculator ]
    max_iters 4
    "Use a tool if it helps. Task: " + inputs.task
  }
}
```

- **`tools [...]`** is an allow-list. The model can only call tools named here,
  and only the `ToolRegistry` can turn a name into executable code — that pair
  is the execution boundary.
- **`max_iters`** bounds the loop (default `6`).
- Built-in deterministic tools: `echo`, `calculator` (side-effect-free, so the
  loop runs end-to-end under `--dry-run`). Register your own via
  `run_program(..., tools=my_registry)`.
- Every model turn, tool call, and tool result is a `TraceEvent` — the entire
  agent run reconstructs from the trace.

## Language

```
context   — deterministic values available during execution     [yes]
steps     — ordered transformations: llm or agent               [yes]
  · llm   — call a model with a rendered prompt                 [yes]
  · agent — tool-use loop over an allow-listed tool registry    [v0.3]
emit text — string concatenation over expression terms          [yes]
emit llm  — call a model with a rendered prompt                 [yes]
rules     — constraints and invariants                          [planned]
```

Expression terms (joined with `+`):

- string literal: `"hello"`
- context value: `context.<name>`
- input value: `inputs.<name>`
- prior step output: `steps.<step_name>.output`

Full grammar in [`docs/grammar.ebnf`](docs/grammar.ebnf); semantics in
[`docs/spec.md`](docs/spec.md).

## What this deliberately does not have yet

Still narrow on purpose: no branching/recursion, no streaming, no system
prompts, no real type system, and tools are pure functions with no sandbox or
resource limits. Each is a real addition with its own design surface, sequenced
behind the platform layers below rather than bolted on early.

## Project shape

- Zero runtime dependencies. `anthropic` is an *optional* extra; the
  `DryRunClient` lets you run any program — agent steps included — without it.
- Frozen dataclass AST nodes (`src/threadlang/ast.py`); `Step` (llm) and
  `AgentStep` are distinct node types.
- Parser (`src/threadlang/parser.py`) — regex for the flat blocks, plus a
  brace-balanced scan for steps so `llm` and `agent` bodies interleave in
  declaration order. (A hand-written recursive-descent parser is the next
  move when control flow lands.)
- Deterministic runtime (`src/threadlang/runtime.py`) returns
  `(output, trace, step_outputs)`. Every binding, step call, agent turn, tool
  call, and tool result appends a `TraceEvent`.
- LLM-client protocols (`src/threadlang/llm.py`) — `complete(model, prompt)`
  for `llm` steps, `agent_step(model, messages, tools)` for `agent` steps.
  Implement either to plug in any backend (OpenAI, Ollama, etc.).
- Tools (`src/threadlang/tools.py`) — `ToolRegistry` allow-list +
  `FunctionTool` wrapper + deterministic built-ins.

## Roadmap — the platform layers

ThreadLang is the authoring + execution core of a production agent platform.
The remaining layers each keep the determinism/trace bet:

1. **Agentic core** *(v0.3, shipped)* — tools + agent tool-use loop.
2. **Durability** — sqlite run store; the trace becomes an event log, so runs
   checkpoint and resume from failure.
3. **Control plane** — an API + worker pool draining a durable run queue.
4. **Observability** — a read-only trace-timeline dashboard.
5. **Vertical-slice app** — one concrete multi-agent product proving it end to end.

## License

MIT — see [LICENSE](LICENSE).
