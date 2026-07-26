# Workflow IR production and system integration

This document defines the supported reusable boundary for canonical Workflow IR v1. It extends the v0.12 single-node production profile in [`production.md`](production.md); it does not claim distributed replay or exactly-once model/tool execution.

## Supported contract

A system may:

1. author and validate a `.thread` source program;
2. compile it to canonical `threadlang.ir/v1` JSON;
3. store, hash, review, sign, or transport those bytes through its own control plane;
4. strictly reload the IR with unknown fields and invalid types rejected;
5. execute the IR through the compatibility interpreter;
6. run it durably with canonical definition bytes, IR version, and SHA-256 identity persisted in SQLite.

The canonical definition contains workflow semantics only. It contains no provider key, bearer token, environment secret, queue ownership, runtime output, or tool implementation.

## CLI integration

Compile source to a canonical artifact without invoking a model:

```bash
threadlang examples/release_report.thread --emit-ir release-report.ir.json
```

Validate and normalize an existing IR artifact:

```bash
threadlang release-report.ir.json --from-ir --emit-ir normalized.ir.json
cmp release-report.ir.json normalized.ir.json
```

Execute canonical IR using the normal provider, input, tracing, metrics, and durable-store flags:

```bash
threadlang release-report.ir.json \
  --from-ir \
  --backend openai \
  --input stats='...' \
  --input notes='...' \
  --store runs.db \
  --trace --metrics
```

Use `--dry-run` for deterministic integration tests. `--emit-ir -` writes canonical JSON to stdout.

## Python integration

```python
from threadlang import (
    canonical_ir_bytes,
    compile_program,
    load_ir_bytes,
    parse_program,
    run_ir,
    workflow_fingerprint,
)

workflow = compile_program(parse_program(source))
artifact = canonical_ir_bytes(workflow)
definition_id = workflow_fingerprint(workflow)

# Store or transport `artifact`, then validate at the execution boundary.
loaded = load_ir_bytes(artifact)
result = run_ir(loaded, {"task": "..."}, llm_client=client, tools=registry)
```

`load_ir_bytes` accepts at most 1 MiB, requires UTF-8 JSON, rejects unknown or missing fields, validates tagged node payloads, checks supported IR/language versions, and re-runs graph/reference validation before returning a workflow.

## Durable identity

New durable runs store:

- source/program identity used by the v0.12 compatibility path;
- canonical `definition_json`;
- `definition_sha256` over the exact canonical UTF-8 bytes;
- `ir_version`;
- canonical input identity.

Resume fails closed when:

- stored IR or language version is unsupported;
- stored canonical definition cannot be parsed and validated;
- stored bytes do not match the persisted digest;
- the supplied workflow definition differs from the original run;
- source or inputs violate the existing v0.12 resume fence.

Older databases migrate additively. Existing rows keep nullable definition fields and are bound only when the current program identity can be proved under the v0.12 migration rules.

## Tool and provider reuse

IR stores logical model names and requested tool names. Provider endpoints, credentials, timeouts, output-token ceilings, and tool implementations remain deployment policy.

For system use:

- construct a `ToolRegistry` per trust domain;
- declare side effects and idempotency on every tool;
- do not register shell, filesystem, network, publishing, or production-write tools unless the enclosing system supplies its own authorization and confinement;
- keep provider credentials in environment/secret storage, never in source or IR;
- use definition fingerprints as review/admission identifiers, not as authorization by themselves.

## Production acceptance

The IR compatibility path is acceptable for the documented single-node profile when all of these stay green:

- every checked-in `.thread` example compiles to canonical IR;
- AST→IR→AST is lossless for the compatibility corpus;
- AST and IR execution return identical output, step outputs, model/tool call behavior, and traces under deterministic clients;
- malformed, oversized, unknown-version, unknown-field, and semantically invalid IR fails before model/tool execution;
- new durable runs persist and verify canonical definition identity;
- additive migration preserves v0.12 stores;
- complete pytest, Ruff, formatting, mypy, Bandit, pip-audit, package build/check/install, CLI, and authenticated control-plane smokes pass;
- container smoke passes in CI or another Docker-capable host.

## Explicitly outside this production claim

- distributed workers or a remote database;
- deterministic event-history replay;
- exactly-once LLM/tool execution;
- arbitrary cycles, parallel/map/join, timers, or child workflows;
- durable signals, human approval interrupts, or cancellation delivery;
- in-flight migration between IR versions;
- native IR execution independent of the compatibility AST interpreter;
- authorization supplied merely by possession of an IR artifact.

Those features require separately versioned execution semantics and cannot be inferred from IR v1.
