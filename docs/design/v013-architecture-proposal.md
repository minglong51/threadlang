# ThreadLang v0.13 architecture proposal

**Status:** pressure-tested draft; first compatibility slice approved for implementation

## Problem

v0.12 makes the existing single-process runtime safer, but its parsed AST is simultaneously the source representation, runtime instruction set, and durable program identity. That coupling becomes unsafe once ThreadLang adds typed data, retries, interrupts, parallelism, or workflow versioning. Changing parser dataclasses could silently change persisted execution meaning.

## Decision

Introduce a **versioned, canonical workflow IR** between the source AST and execution. Keep the current textual DSL as the authoring surface and keep the v0.12 interpreter operational while the IR is proven.

```text
.thread source -> parser -> source AST -> compiler -> WorkflowIR v1
                                                   |-> canonical JSON
                                                   |-> SHA-256 definition identity
                                                   `-> future IR interpreter
```

The first slice is intentionally non-executing: it compiles every existing v0.12 construct into lossless canonical IR and exposes stable serialization/fingerprinting. `run_program` and durable execution remain unchanged until differential tests prove an IR interpreter is equivalent.

## Why this boundary

- **Source compatibility:** parser syntax can evolve without redefining old persisted workflows.
- **Safe evolution:** an IR version names execution semantics explicitly.
- **Inspection:** canonical JSON is easier to diff, sign, store, and test than Python dataclasses.
- **Migration:** future compilers can target old or new IR versions; active runs can remain pinned.
- **Narrow risk:** the first slice adds no scheduler, event store, distributed claim, or new DSL syntax.

## IR v1 requirements

1. Preserve thread name, ordered context, ordered steps, prompt expressions, output contracts, graph edges, tool allow-lists, iteration limits, and emit behavior.
2. Represent every expression term with a tagged object; never encode semantics in ambiguous strings.
3. Use explicit step kinds: `llm`, `agent`, and `route`.
4. Include `ir_version` and `language_version` at the root.
5. Serialize deterministically with sorted object keys and compact separators while preserving list order.
6. Fingerprint the canonical UTF-8 bytes with SHA-256.
7. Reject unsupported AST node types rather than dropping data.
8. Do not include deployment-specific provider credentials, runtime state, or mutable defaults.

## Pressure-test results

| Pressure | Result | Design consequence |
|---|---|---|
| Existing eight `.thread` examples | All current constructs can be represented losslessly | Compile every example in tests before runtime migration |
| Forward-only routing | Ordered nodes and explicit targets are sufficient | Do not generalize to arbitrary graph cycles in IR v1 |
| Optional branch output | Must survive compilation as data | Expression refs carry `optional: true|false` |
| Multiple regex contracts | Map/dict representation would lose order or duplicates | Contracts remain an ordered list |
| Context ordering | Runtime does not need it, but source inspection and stable diffs do | Context remains an ordered list, not an object map |
| Model/provider separation | Current model strings may be aliases, not deployment identities | IR records logical `model`; provider resolution remains deployment policy |
| Tool safety | Tool metadata lives in registry, not source AST | IR records requested tool names only; runtime policy still validates registry metadata |
| Retry semantics | v0.12 hard-codes one contract retry | Do not invent generic retry fields in IR v1; add a later version with explicit semantics |
| Crash recovery | Current checkpoint key is step name | IR requires unique stable node IDs; v1 uses existing step names |
| Workflow upgrades | Recompiling changed source under the same running ID is unsafe | Persist IR version and definition digest before IR execution ships |
| Parallelism and joins | Existing optional refs are not sufficient to define scheduling/failure policy | Defer parallel/map/join until execution and cancellation semantics are designed together |
| Human interrupts | Require durable external-event identity and authorization | Defer; do not model approval as an ordinary agent/tool call |
| Event-history replay | Model/tool calls are nondeterministic activities | Future history must record scheduled/started/completed/failed activity transitions, not pretend model calls replay deterministically |

## Rejected alternatives

### Extend the current AST directly

Rejected because Python parser dataclasses would remain a durable execution contract without an explicit version or canonical representation.

### Adopt Temporal, LangGraph, or ASL as ThreadLang's internal format

Rejected for the first slice. They solve broader execution problems but would either make ThreadLang a thin frontend to another product or import semantics that the local runtime cannot honestly provide. A future backend adapter remains possible after the IR is stable.

### Build an event-sourced scheduler immediately

Rejected as too large and poorly isolated. Canonical IR and fingerprints are prerequisites for naming which definition an event history belongs to.

### Add typed state, retries, parallelism, and interrupts in one IR

Rejected because syntax is the easy part; cancellation, redelivery, migration, and partial-failure semantics need separate decisions and tests.

## Versioning rules

- `ir_version` identifies the shape and interpretation of canonical IR.
- `language_version` identifies accepted source semantics.
- Existing v0.12 programs compile to `threadlang.ir/v1`.
- Existing IR bytes never change meaning in place.
- New optional fields require either a specified default that preserves canonical meaning or a new IR version.
- An executing durable run must eventually persist the canonical definition digest, not a digest of parser implementation objects.

## Incremental delivery

### Slice A — canonical compatibility IR

- Add immutable IR dataclasses.
- Compile the current AST into IR.
- Add canonical JSON and SHA-256 helpers.
- Compile every checked-in example.
- Test term, contract, route, agent, and emit preservation.
- Do not change runtime behavior.

### Slice B — differential interpreter

- Implement an IR interpreter behind an opt-in library entrypoint.
- Run AST and IR interpreters against identical scripted clients.
- Compare outputs, step outputs, call order, prompts, and trace semantics.
- Keep durable production execution on the old interpreter until equivalence is demonstrated.

### Slice C — durable definition binding

- Store canonical IR bytes/version/digest with newly created runs.
- Resume from the stored definition rather than recompiling supplied source.
- Define migration behavior for v0.12 database rows.

### Slice D — explicit activity policy

- Design activity timeout, retry, backoff, idempotency-key, cancellation, and redelivery semantics together.
- Add syntax only after the IR and runtime contract are reviewed.

## Approval boundary

Approved now: Slice A, because it is additive and cannot alter execution.

Blocked pending separate design reviews: changing `run_program`, changing durable database identity, arbitrary cycles, parallel scheduling, external events, or distributed execution.
