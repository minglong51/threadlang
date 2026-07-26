# ThreadLang v0.12 productionization plan

**Goal:** Make the current single-node ThreadLang runtime safe and predictable enough for a documented local/self-hosted production profile, while preserving its compact LLM-first DSL and refusing to imply Temporal-scale guarantees.

**Architecture:** Keep the parser/runtime/store/control-plane split. Replace regex-based structural parsing with a token stream and recursive-descent parser; add bounded execution policies at parse/runtime boundaries; harden SQLite and the HTTP server; publish a source-backed semantic comparison rather than a promotional composite score.

**Scope boundary:** v0.12 remains a single-process, single-SQLite runtime. Distributed exactly-once execution, arbitrary cycles, parallel fan-out, timers/signals, human interrupts, deployment versioning, and CLI-agent subprocess adapters are roadmap items, not hidden claims.

## Evidence baseline

- Current parser misparses `+` inside strings, braces inside strings, unknown step keywords, and `max_iters` text inside prompts.
- Current server has no authentication, request-size limit, worker-aware readiness, pagination, or bounded queue/retention.
- Current `matches` contracts use unbounded Python regex evaluation.
- Current durable store has no program identity check on resume and no WAL/busy-timeout setup.
- Official comparators establish useful production semantics: Temporal (activities, timeouts, cancellation, versioning), Amazon States Language (retry/catch, choice, wait, parallel, map), LangGraph (checkpoints, human-in-loop, time travel, fault tolerance), AutoGen GraphFlow (sequential/parallel/conditional/loop agent graphs, explicitly experimental), and Dapr Workflow (lifecycle operations, child workflows, timers, versioning, history security).

## Acceptance boundary

1. Existing v0.11 programs remain source-compatible.
2. Structural parser pressure cases pass and unknown syntax fails before any model call.
3. Agent iterations, regex work, source/input/body sizes, pending queue, and retained run history are bounded.
4. Non-loopback serving fails closed without an auth token; all sensitive API/UI routes enforce constant-time bearer authentication. `/healthz` stays public and `/readyz` reflects worker liveness.
5. Durable resume rejects source/input identity drift; SQLite concurrency settings and indexes are explicit.
6. Full tests, lint, type checks, package build/check/install smoke, deterministic benchmark validation, and a live control-plane smoke pass.
7. Docs state exactly what is and is not production-ready; no cross-system performance or quality claim is made without equivalent execution.

## Task 1: Lexer and recursive-descent parser

**Files:** `src/threadlang/parser.py`, `tests/test_parser.py`, `tests/test_parser_pressure.py`

- Add a position-aware lexer for identifiers, integers, strings, punctuation, `->`, and comments.
- Decode documented string escapes and preserve line/column diagnostics.
- Parse the complete v0.11 grammar without regex block extraction or substring directives.
- Reject unknown fields/tokens, duplicate declarations, and trailing content.
- Preserve existing AST and semantic graph validation.

## Task 2: Bounded execution policies

**Files:** `src/threadlang/policy.py`, `src/threadlang/parser.py`, `src/threadlang/runtime.py`, tests

- Centralize documented ceilings for source, expressions, agent iterations, regex patterns/input, request body, inputs, queue, and retention.
- Reject oversized agent loops and unsafe/oversized regex contracts at parse time.
- Evaluate accepted regex contracts in a killable spawned process with a hard timeout.
- Surface deterministic denial/contract errors without worker hangs.

## Task 3: Durable identity and worker resilience

**Files:** `src/threadlang/store.py`, `src/threadlang/control.py`, tests

- Enable WAL, foreign keys, busy timeout, indexes, and schema migration.
- Persist `source_sha256` and `inputs_sha256`; reject resume when current program/input identity differs.
- Catch parse/execution failures inside the worker boundary, mark the run failed, and keep workers alive.
- Add worker liveness and queue depth inspection.
- Bound pending work and prune only terminal runs beyond the configured retention ceiling.

## Task 4: Control-plane hardening

**Files:** `src/threadlang/server.py`, `src/threadlang/cli.py`, tests

- Require a bearer token for non-loopback bind; support token configuration without logging it.
- Authenticate API/UI routes; leave liveness/readiness probes non-sensitive.
- Enforce body/source/input limits before enqueue.
- Add pagination and bounded responses.
- Return structured errors and structured request logs.
- Report worker-aware readiness and clean shutdown.

## Task 5: Packaging and operations baseline

**Files:** `pyproject.toml`, `.github/workflows/ci.yml`, `Dockerfile`, `.dockerignore`, docs

- Publish v0.12 metadata, supported Python versions, project URLs, typed-package marker, and build/test/lint/type dev extras.
- Test Python 3.11–3.13; run ruff, mypy, pytest, package build, twine check, and install smoke.
- Add a non-root container with healthcheck and documented token/volume configuration.

## Task 6: Credible DSL benchmark and roadmap

**Files:** `benchmarks/semantic_matrix.json`, `benchmarks/validate_semantic_matrix.py`, `docs/benchmarks/dsl-comparison.md`, `docs/roadmap.md`, tests

- Freeze source-backed semantic dimensions and conservative statuses for ThreadLang and five comparators.
- Prohibit aggregate rankings and clearly distinguish documentation evidence from locally executed evidence.
- Add deterministic validator/tests for schema, primary-source URLs, evidence, and no-score policy.
- Add a local parser/runtime/store/control-plane benchmark with raw distributions and environment metadata; no cross-runtime latency claims.
- Publish a prioritized roadmap: provider routing, typed JSON output, retry/timeout policy, interrupts/signals, parallelism, versioning, and distributed durability.

## Task 7: Verification and review

- Run focused tests after every task and the complete matrix at closeout.
- Run adversarial parser/regex/control-plane cases.
- Build and install wheel/sdist in a clean environment.
- Run local authenticated and unauthenticated HTTP smokes.
- Obtain fresh read-only review from a different model family when available; coordinator verifies every finding and artifact.
- Commit on the isolated branch, push, open a PR, and leave merge/release/deploy as explicit human gates.
