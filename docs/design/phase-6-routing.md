# Phase 6 — Routing (v0.9): the step list becomes a node graph

## Why now

ThreadLang's steps have been a straight line since v0.1. Real workflows
branch: classify a request, then take different paths. The design question
was how to add branching without giving up the founding bet — every run a
replayable, inspectable trace — or the machinery built on it (step-name
checkpoints, resume, replay, metrics-as-trace-fold).

The design is informed by the CogniConsole paper (Figueiredo & Franceschi,
arXiv 2026, "Externalizing Inference-Time Control as a Formal Abstraction"),
whose empirical claim maps directly onto this repo's thesis: reliability
failures in LLM systems come largely from under-specified inference-time
control, not model capability. Its concrete mechanisms — single-decision
routing nodes, closed-enum output contracts, deterministic arm dispatch,
violation → bounded recovery — are what v0.9 adopts. Its vocabulary
(console/cartridge/ladder) is not.

## The shape

Three additions, one invariant:

1. **`route` step** — a model call whose output contract is the closed set
   of its arm labels:

   ```thread
   step classify {
     route "deepseek-chat" {
       "Decide how to handle this request: " + inputs.task
       on "math" -> solve_math
       on "writing" -> draft
       else -> draft
     }
   }
   ```

   The runtime renders the prompt, appends a contract suffix generated from
   the arms ("Reply with exactly one of: ..."), normalizes the reply
   (trim/case), and requires label equality — no substring matching. A miss
   is traced (`route` phase, "output rejected") and retried once with the
   violation fed back; a second miss takes `else ->` or fails loud. The
   chosen label is the step's output; **the jump is deterministic code**.
   The model picks a label, never a control flow.

2. **`then -> <step|end>` edge** on `llm`/`agent` steps — explicit branch
   termination. Default remains fall-through, so every pre-v0.9 program is
   unchanged. `end` skips to emit.

3. **Optional refs** — `steps.<name>.output?` renders as `""` when the step
   was skipped by routing. This is how emit (or a join step) reads outputs
   from branches that may not have run; the non-optional form on a skipped
   step still fails loud.

**The invariant: the graph is a forward-only DAG.** Every jump target must
be declared after the step that jumps to it (parser-enforced), so each step
runs at most once per run. That single restriction is what lets everything
downstream survive untouched:

- **Checkpoints/resume** — step outputs stay keyed by name. A resumed route
  step re-derives its jump from the stored label deterministically, with no
  model call (`test_resume_re_derives_route_jump_without_model_call`).
- **Replay** — a completed run still replays from the store with zero model
  calls.
- **Metrics** — the fold gains `route_steps` and `route_violations`; both
  are pure functions of the new `route`-phase events, retroactive like
  every other metric.

## Client surface

`route` rides `complete()` for real backends — the contract lives in the
prompt, so any `LLMClient` works unchanged. A client may expose
`route(model, prompt, options)` (detected via `getattr`, like `agent_step`)
to answer with knowledge of the closed label set: `DryRunClient` picks the
first arm, which keeps whole routed graphs — dry-run agent loops included —
running deterministically offline. A future constrained-decoding backend
would hook the same method.

## Deliberate cuts

- **No cycles / no `while`** — revisiting a step breaks name-keyed
  checkpoints and unbounded model-driven loops break the determinism story.
  Iteration stays inside `agent` steps, where `max_iters` bounds it. If
  cycles ever land, they arrive with execution-indexed checkpoints, not by
  loosening this DAG.
- **One retry, fixed** — not configurable until a real program needs it.
- **No aggregate route metrics** — per-run only for now; the aggregate
  rollup can grow them later without schema changes (it's a fold).
- **Regex parser stretched, again** — `on`/`else`/`then` clauses are
  extracted the same way `tools`/`max_iters` are, with the same known
  limitation (a prompt literal containing `then -> x` would be eaten). The
  recursive-descent rewrite stays queued behind real grammar pressure; this
  phase deliberately does not trigger it.

## Trade-off noted from the paper

CogniConsole's probes show strict contracts reduce flexibility on noisy
input (their P4). The mitigations here: normalization absorbs trivial
noise, the retry feeds the violation back, and `else ->` gives every route
a designated recovery edge. A controllability-probe harness (repeated runs,
per-step variance from stored traces) is the natural v0.10+ follow-up and
would measure this trade-off on our own stack.
