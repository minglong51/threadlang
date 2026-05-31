# ThreadLang — High-Level Design

## Purpose

ThreadLang is a small DSL for **deterministic LLM-workflow programs**: you
declare a `context` block of fixed string values, an ordered list of `steps`
(each a single LLM call whose prompt is built from string concatenation), and a
final `emit` block that either concatenates a string (`emit text`) or makes one
more model call (`emit llm`). Execution follows a fixed pipeline —
**parse → AST → runtime → emit** — and every phase appends a structured
`TraceEvent`, so a run is fully inspectable. The language is held deliberately
narrow (no loops, branching, recursion, streaming, tool use, or real type
system; see `docs/spec.md:19`) so the workflow shape runs end-to-end before the
surface grows. It ships as a Python library plus a `threadlang` CLI, with zero
required runtime dependencies and an *optional* `anthropic` extra for real
Claude calls (`pyproject.toml:13`, `pyproject.toml:16`).

## System Context

```
  .thread source file
        │
        ▼
  threadlang CLI  ──parse──►  AST (frozen dataclasses)
  (cli.py:main)                       │
        │                             ▼
        │                       run_program (runtime.py:48)
        │                             │  calls .complete(model, prompt)
        │                             ▼
        │                       LLMClient (Protocol)
        │                        ├─ DryRunClient  (no deps, deterministic echo)
        │                        └─ AnthropicClient ──► Anthropic SDK ──► Claude API
        ▼                                                  (optional dep)      (external)
  stdout: program output
  stderr: trace events (--trace)
```

External dependencies:

- **Anthropic Python SDK + Claude API** — only when `AnthropicClient` is used
  (real model calls). Optional; gated behind the `anthropic` extra and an
  `ANTHROPIC_API_KEY` env var (`llm.py:38`, `llm.py:53`).
- **`ANTHROPIC_API_KEY`** environment variable — read by `AnthropicClient`
  (`llm.py:53`).
- Build/test tooling: `setuptools>=61` (`pyproject.toml:2`), `pytest` for tests
  (`tests/test_v1_llm.py:18`). No other runtime deps (`pyproject.toml:13`).

There is no database, network service, scheduler, web server, or bot. It is a
local library + CLI.

## Component Map

| Path | Responsibility |
|------|----------------|
| `src/threadlang/__init__.py` | Public API surface: re-exports `parse_program`, `run_program`, `RuntimeResult`, the `LLMClient`/`AnthropicClient`/`DryRunClient` family, and the `ParseError`/`RuntimeError`/`LLMError` exceptions (`__init__.py:3`). |
| `src/threadlang/ast.py` | Frozen-dataclass AST node definitions: `Program`, `ContextBlock`/`ContextAssignment`, `StepsBlock`/`Step`, `EmitBlock`, `Expression` and its term union (`StringLiteral`, `ContextRef`, `InputsRef`, `StepsRef`). The contract between parser and runtime. |
| `src/threadlang/parser.py` | Regex-driven parser. `parse_program(source) -> Program` plus block sub-parsers. Raises `ParseError` on malformed source, duplicate step names, empty/unsupported expressions. |
| `src/threadlang/runtime.py` | Deterministic interpreter. `run_program(program, inputs, llm_client) -> RuntimeResult`. Builds context, runs steps in order, evaluates emit, wraps client failures in `RuntimeError`, and appends trace events. |
| `src/threadlang/llm.py` | LLM client abstraction. `LLMClient` Protocol (`complete(model, prompt) -> str`), `DryRunClient` (echo), `AnthropicClient` (real Claude), `default_client()`. |
| `src/threadlang/trace.py` | `TraceEvent` frozen dataclass (`phase`, `message`, `data`) and the `Trace = List[TraceEvent]` alias — the structured execution record. |
| `src/threadlang/cli.py` | `threadlang` console entry point (`pyproject.toml:22`). Parses argv, reads the source file, builds a client (with soft dry-run fallback), runs the program, prints output and optional trace. |
| `docs/spec.md`, `docs/grammar.ebnf` | Authoritative language spec and EBNF grammar. |
| `examples/*.thread` | Runnable sample programs: `hello.thread` (v0 text-only), `summarize.thread` (`emit llm`), `two_step.thread` (chained steps). |
| `tests/` | `test_golden_hello.py` (v0 back-compat) and `test_v1_llm.py` (emit-llm rendering, step chaining, error paths, dry-run protocol). |

## Runtime / Deploy Model

ThreadLang is **not a long-running service**. It runs as a one-shot CLI process
or is imported as a library.

- **As a CLI**: installed via `pip install threadlang`, exposing the
  `threadlang` console script that maps to `threadlang.cli:main`
  (`pyproject.toml:22`). Each invocation reads one `.thread` file, executes it
  to completion synchronously, prints the result to stdout, and exits 0
  (`cli.py:67`, `cli.py:74`).
- **As a library**: `from threadlang import parse_program, run_program` and
  drive the pipeline directly, passing any object satisfying the `LLMClient`
  protocol (`__init__.py:3`).
- **Model backend selection** happens at the edge (CLI or caller), not in the
  runtime. With `--dry-run`, a `DryRunClient` echo is used (`cli.py:50`). Without
  it, the CLI tries to build an `AnthropicClient`; if the SDK or key is missing
  it **soft-falls-back** to `DryRunClient`, warning only when the program
  actually needs an LLM (has steps or `emit llm`) (`cli.py:53`–`cli.py:64`).
- **Determinism**: parsing and `emit text` are fully deterministic and need no
  network. Only real model calls introduce nondeterminism; `DryRunClient` keeps
  even those reproducible (`llm.py:34`).

## How It's Used

Primary invocation paths (from `README.md:38` and `docs/spec.md:89`):

```bash
# v0 — pure string interpolation, no LLM, no key needed
threadlang examples/hello.thread --input name=world      # → Hello, world!

# v1 — real Claude call (needs ANTHROPIC_API_KEY + the anthropic extra)
threadlang examples/summarize.thread --input text="The cat sat on the mat..."

# v1 dry-run — runs steps/emit-llm without a key; LLM calls become echoes
threadlang examples/two_step.thread --input text="..." --dry-run

# show the structured trace (printed to stderr after output)
threadlang examples/two_step.thread --input text="..." --dry-run --trace
```

- `--input key=value` is **repeatable**; each becomes referenceable as
  `inputs.<key>` inside the program (`cli.py:27`, `cli.py:14`).
- `--dry-run` forces the echo client even when a real one is available
  (`cli.py:50`).
- `--trace` dumps every `TraceEvent` to stderr (`cli.py:69`).

Library use mirrors the tests: parse a source string, then `run_program(...,
llm_client=YourClient())` and read `result.output` / `result.step_outputs` /
`result.trace` (`tests/test_v1_llm.py:42`–`tests/test_v1_llm.py:57`). Any backend
(OpenAI, Ollama, a mock) plugs in by implementing `complete(model, prompt) ->
str` (`llm.py:22`, `README.md:91`).
