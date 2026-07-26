# Security policy

## Supported version

Security fixes are applied to the latest release line. The v0.12 production profile is limited to one POSIX host, one process per local SQLite store, and the constraints in [`docs/production.md`](docs/production.md).

## Reporting

Do not disclose suspected vulnerabilities, credentials, prompts containing private data, or database contents in a public issue. Contact the repository owner privately through GitHub's security-advisory interface. Include the affected version, reproduction steps, impact, and whether secrets or persisted run data may have been exposed.

## Operator responsibilities

- Configure a high-entropy `THREADLANG_AUTH_TOKEN` for every non-loopback bind.
- Put public deployments behind TLS; the built-in server does not terminate TLS.
- Restrict filesystem access to the SQLite database, WAL, shared-memory, and worker-lock files.
- Treat program source, inputs, outputs, tool observations, and traces as sensitive plaintext.
- Use only trusted custom Python tool implementations. ThreadLang enforces an allow-list and durable idempotency declarations but does not sandbox arbitrary Python code.
- Rotate provider and bearer credentials outside ThreadLang; do not pass secrets as CLI arguments or workflow inputs.

## Built-in controls

- Fail-closed parser, runtime, request, queue, and retention limits.
- Bearer authentication with constant-time comparison.
- Loopback-only operation when authentication is absent.
- SQLite integrity bindings for source and inputs, CAS resume ownership, and a single-process worker lock.
- Provider error-body redaction.
- Isolated, time-bounded regex contract evaluation.
- Durable rejection of side-effecting non-idempotent tools.

See `docs/production.md` for limitations and recovery semantics.
