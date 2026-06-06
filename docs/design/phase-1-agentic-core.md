# Phase 1 — Agentic Core (v0.3)

## Where this fits

ThreadLang started as a deterministic LLM-workflow DSL: `context → steps{llm} →
emit`, with a `TraceEvent` per phase. That is the *authoring + execution core*
of a production agent platform. The platform is built up in layers, each one
preserving the founding bet:

> **Every run is a durable, replayable, inspectable trace.**

| Layer | Status |
|---|---|
| L0 DSL — authoring model | shipped (v0.1–0.2) |
| L1 Runtime — synchronous executor + trace stream | shipped (v0.1–0.2) |
| **L2 Agentic core — tools + agent loop** | **this doc (v0.3)** |
| L3 Durability — sqlite event log, resume-from-failure | next |
| L4 Control plane — API + worker pool + run queue | planned |
| L5 Observability — trace-timeline dashboard | planned |
| L6 Vertical-slice app — one concrete multi-agent product | planned |

Phase 1 is the keystone: it turns a prompt-chain DSL into something that can
*act*, which is what makes it an agent runtime rather than a templating engine.

## What shipped

A new step kind, `agent`, that runs a tool-use loop:

```thread
step solve {
  agent "claude-haiku-4-5-20251001" {
    tools [ echo, calculator ]
    max_iters 4
    "Use a tool if it helps. Task: " + inputs.task
  }
}
```

The runtime renders the prompt as the opening instruction, then loops: the
model returns either a final answer (loop ends) or tool calls (the runtime
executes them, appends the results, and calls back), bounded by `max_iters`.
The final text binds to `steps.<name>.output` exactly like an `llm` step, so
agent steps compose with everything downstream.

New modules / types:

- `tools.py` — `ToolSpec` (what the model sees), `Tool` / `FunctionTool` (the
  runtime contract), `ToolRegistry` (the allow-list), `default_registry()`
  (deterministic `echo` + `calculator`).
- `llm.py` — the `AgentLLMClient` protocol (`agent_step`), normalized
  `Message`/`ToolCall`/`AgentTurn` types, and implementations on both
  `DryRunClient` (deterministic) and `AnthropicClient` (real tool-use).
- `ast.py` — `AgentStep` node alongside `Step`.
- `runtime.py` — `_run_agent_step` (the loop), dispatched by node type.

## Key design decisions

**1. Distinct `AgentStep` node, not an overloaded `Step`.** An llm step and an
agent step have genuinely different execution semantics (one call vs. a bounded
loop with side-effecting tools). Separate types keep `isinstance` dispatch
honest and let each evolve independently.

**2. Brace-matched step parsing instead of more regex.** The v0 parser found
llm steps with a single regex `finditer`. Adding a second step kind that way
loses declaration order across mixed steps (an `agent` between two `llm`s). So
the steps block is now carved out with a brace-balanced scan, and each `step
<name> { ... }` body is dispatched by kind. This is the minimum change that
preserves order and survives the deeper nesting agent bodies introduce — short
of the full recursive-descent parser, which is deferred until control flow
(branching/loops) actually lands.

**3. Two client protocols, not one fat one.** `llm` steps keep the tight
`complete(model, prompt) -> str`. Agent steps use `agent_step(model, messages,
tools) -> AgentTurn`. Tool-use needs a richer request/response shape; forcing
it onto plain steps would tax the common case for the rarer one.

**4. Normalized messages, provider translation at the edge.** The runtime owns
a small message vocabulary (`user` / `assistant`+tool_calls / `tool`). Each
client translates to/from its provider format (`_to_anthropic_messages`). The
loop logic stays provider-agnostic.

**5. Determinism preserved through the loop.** `DryRunClient.agent_step` is
scripted, not random: if tools are available and none has run, call the first
with placeholder args derived from its JSON schema; otherwise finalize. Same
program → same trace, no API key. This is what lets the agent loop be demoed
and (later) golden-tested.

## The execution boundary

For v0.3 the boundary is narrow and explicit:

- A step's `tools [...]` is an allow-list; the model cannot reach a tool not
  named there (enforced again at call time in the runtime).
- Only the `ToolRegistry` maps a name to executable code — an unregistered name
  cannot run.
- Tools are pure functions of their arguments. The `calculator` rejects any
  character outside `0-9 + - * / % ( ) .` *before* evaluating, and evaluates
  with no builtins in scope.
- A tool that raises is caught and surfaced to the model as an `error: ...`
  observation rather than crashing the run — realistic agent behavior, and the
  error is in the trace.

Sandboxing, resource limits, and side-effecting tools (network/fs) are
deliberately *not* here. They are real risk surface and belong behind the
durability + control-plane layers, added when a use case earns them.

## Verification

- Existing suite: 10/10 pass (back-compat — no v0/v1 behavior changed).
- `examples/agent.thread` runs end-to-end under `--dry-run --trace`, emitting
  the full loop: agent turn → tool call → tool result → final answer.
- Checked directly: `calculator` computes and rejects injection; mixed
  `agent`/`llm` steps preserve declaration order; a scripted client confirms a
  tool result (`42`) feeds back into the next turn and into the output.

## Next (L3 — durability)

Persist the `TraceEvent` stream + run state to sqlite so the trace becomes the
event log: a run gets an id and status, checkpoints after each step, and
resumes from the last completed step after a crash. The trace already *is* the
record; L3 makes it durable and the execution resumable.
