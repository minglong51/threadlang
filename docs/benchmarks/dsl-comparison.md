# ThreadLang semantic comparison

This is a primary-source **semantics matrix**, not a model-quality or performance score. `Full`, `Partial`, and `No` describe documented behavior; they are not weighted or aggregated.

| Capability | ThreadLang v0.13 | Temporal | AWS Step Functions / ASL | LangGraph | AutoGen GraphFlow | Dapr Workflow |
|---|---|---|---|---|---|---|
| Deterministic control structure | Full: parsed forward-only graph | Full: replay-compatible workflow commands | Full: declarative state-machine interpreter | Partial: graph/super-step control, not Temporal replay | Partial: deterministic routing structure; experimental | Full: replay-compatible workflow code |
| Crash durability | Partial: SQLite step-boundary checkpoint | Full event-history reconstruction | Full for Standard Workflows | Full with persistent checkpointer | Application-managed save/load | Full event-sourced state via Dapr |
| Incomplete call redelivery | At-least-once LLM/agent step | Activities may re-execute | Standard task semantics plus explicit Retry | Node may restart from beginning | No documented durable retry contract | Activities at-least-once |
| Idempotency contract | Side-effecting non-idempotent tools rejected on durable path | Activities expected to be idempotent | StartExecution idempotency and retry policy | Docs require idempotent side effects/keys | Application responsibility | Activities should be idempotent |
| Typed/shared state | Strings plus output contracts | Language types + data converters | Dynamic JSON | TypedDict/dataclass/Pydantic schemas | Component state models, no equivalent shared-state schema | Language types across serialized boundaries |
| Human approval | No | Signals/Updates | Task tokens | Durable interrupts | Blocking user proxy or app-managed saved state | Durable external events |
| Safe code evolution | No in-flight migration contract | Worker Versioning/patching/replay tests | Immutable versions and aliases | Deployment-level; no surveyed replay patch contract | Experimental; no in-flight graph migration contract | Named versions and replay-aware patching |
| Built-in observability | SQLite trace, metrics, HTML dashboard | Event History, Visibility, UI/CLI | History, console, CloudWatch, X-Ray | Checkpoints/time travel; LangSmith | Logging + OpenTelemetry | CLI history + OpenTelemetry |
| Test support | pytest, dry-run/probes, crash/security regression cases | time skipping, mocking, history replay | TestState and integration mocking | pytest graph/node guidance | ordinary Python tests | local/integration SDK testing |
| Deployment | Single POSIX process + SQLite/Docker | Workers + Cloud/self-hosted service | Managed AWS | Library/server; managed or self-hosted options | Embedded Python application | App + Dapr sidecar/state store |

## Design conclusions

1. ThreadLang should describe v0.13 as **step-checkpoint durability**, not durable replay.
2. LLM and agent calls are nondeterministic activities and may execute more than once after a hard crash.
3. The current product boundary is intentionally smaller than Temporal, ASL, LangGraph, or Dapr: a compact textual agent DSL with local execution and inspection.
4. Durable external events, version pinning/migrations, typed state, and call-level idempotency keys belong in later versioned designs, not undocumented semantics.
5. AutoGen GraphFlow is the closest graph-control comparator, but it is experimental and does not supply ThreadLang's desired production durability boundary by itself.

## Primary sources

### Temporal
- Workflow determinism/replay: https://docs.temporal.io/workflow-definition
- Retries/idempotency: https://docs.temporal.io/encyclopedia/retry-policies
- Signals/Queries/Updates: https://docs.temporal.io/encyclopedia/workflow-message-passing
- Safe deployments: https://docs.temporal.io/develop/safe-deployments
- Testing: https://docs.temporal.io/develop/python/best-practices/testing-suite

### AWS Step Functions / ASL
- ASL specification: https://states-language.net/spec.html
- Standard vs Express: https://docs.aws.amazon.com/step-functions/latest/dg/choosing-workflow-type.html
- Retry/Catch: https://docs.aws.amazon.com/step-functions/latest/dg/concepts-error-handling.html
- Task-token callbacks: https://docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html
- Versions: https://docs.aws.amazon.com/step-functions/latest/dg/concepts-state-machine-version.html
- TestState: https://docs.aws.amazon.com/step-functions/latest/dg/test-state-isolation.html

### LangGraph
- Graph/state semantics: https://docs.langchain.com/oss/python/langgraph/graph-api
- Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- Interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- Testing: https://docs.langchain.com/oss/python/langgraph/test

### Microsoft AutoGen GraphFlow
- GraphFlow: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/graph-flow.html
- State save/load: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/state.html
- Human-in-the-loop: https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/tutorial/human-in-the-loop.html

### Dapr Workflow
- Features/semantics: https://docs.dapr.io/developing-applications/building-blocks/workflow/workflow-features-concepts/
- Architecture: https://docs.dapr.io/developing-applications/building-blocks/workflow/workflow-architecture/
- Versioning: https://docs.dapr.io/developing-applications/building-blocks/workflow/workflow-versioning/
