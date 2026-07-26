# ThreadLang v0.12 production profile

ThreadLang v0.12 supports a deliberately narrow **single-node, POSIX, local-filesystem** production profile. It is not a distributed durable-execution engine and does not claim Temporal/Dapr-style event-history replay.

## Supported boundary

- One `threadlang-serve` process per SQLite store.
- Linux or macOS/POSIX filesystem with working advisory file locks.
- SQLite WAL on a local disk; network filesystems are unsupported.
- Step-boundary checkpoints. A process death can rerun the current incomplete LLM/agent step.
- LLM calls are therefore at-least-once. Durable runs reject custom tools declared as both side-effecting and non-idempotent.
- Forward-only graphs only; `max_iters` is capped by runtime policy.

## Start safely

Loopback development mode needs no token:

```bash
threadlang-serve --store ./runs.db --backend dry-run
```

Any non-loopback bind requires a bearer token supplied through an environment variable, never argv:

```bash
export THREADLANG_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
threadlang-serve --store /data/threadlang.db --host 0.0.0.0 --backend anthropic
```

All data, dashboard, metrics, and submission routes require `Authorization: Bearer …` when a token is configured. `/healthz` and `/readyz` expose only health/queue state.

## Operational controls

- `--max-pending`: queue admission limit; excess submissions receive HTTP 429.
- `--max-retained`: number of terminal runs retained during admission cleanup.
- `--max-tokens` and `--timeout`: provider response and deadline policy.
- `--workers`: local worker-thread count.
- `THREADLANG_AUTH_TOKEN`: default bearer-token environment variable.
- `THREADLANG_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`: provider credentials. Never store these in source or inputs.

The worker pool owns `<store>.worker.lock`. A second process fails startup rather than requeueing work active in the first process. Kernel lock release after process death makes startup orphan requeue safe within the supported local-filesystem boundary.

## Health

- `GET /healthz`: verifies SQLite access.
- `GET /readyz`: additionally requires the configured worker threads to be alive and reports only pending/running counts.

## Durability contract

- Program source and canonical inputs are SHA-256 bound to a run.
- Resume rejects changed source/inputs and uses a compare-and-swap status transition.
- Only validated step outputs and resolved route labels are checkpointed.
- Regex output contracts execute in a killable isolated interpreter with size and time limits.
- Non-idempotent side-effecting tools are rejected on the durable path.

LLM/agent steps remain at-least-once across a hard crash. Tool authors must truthfully declare `ToolSpec.side_effects` and `ToolSpec.idempotent`. Exactly-once external effects are out of scope.

## Data and secrets

SQLite stores inputs, outputs, traces, and tool observations in plaintext. Protect the database and lock file using OS permissions and encrypted storage where required. Provider HTTP bodies are not copied into durable errors. Retention is count-based; legal/time-based erasure remains an operator responsibility.

## Upgrade from v0.11

1. Stop every writer and back up the SQLite database using the online backup API or a clean shutdown/copy.
2. Install v0.12 and start exactly one process. `RunStore` performs additive, idempotent column/index migrations.
3. Runs created before source hashing cannot be safely resumed if their original source is unavailable. Inspect or fail them explicitly rather than fabricating source.
4. Revalidate custom programs: v0.12 rejects ignored tokens, malformed strings/comments, backward references, unavailable `steps.*` references, duplicate route labels, and out-of-policy sizes that older parsers could accept or misparse.
5. Downgrade after opening a store with v0.12 is not supported without restoring the backup.

## Backup and restore

Use SQLite's online backup API or stop the server before copying the database. Do not copy only the main database while WAL writes are active. Restore the database and start exactly one worker pool; startup requeues sourced runs left `running` after a crash.

## Container

The supplied image runs as a non-root user and stores state under `/data`. Because its default bind is `0.0.0.0`, `THREADLANG_AUTH_TOKEN` is mandatory at startup.

## Explicit non-goals / future work

- Distributed workers, leases across hosts, and network filesystems.
- Temporal/Dapr-compatible replay, workflow patch markers, or in-flight version migration.
- Durable human approval/events.
- Per-call token/usage persistence and full provider request IDs.
- Sandboxing arbitrary custom Python tool implementations.
