# Phase 8 — Generalized output contracts (v0.11): `expect` blocks

## Why now

v0.9 gave route steps an output contract — a closed enum enforced with a
feedback retry — and v0.10 showed the payoff: contracted outputs are the
steps a probe can measure meaningfully. But the contract machinery was
locked inside routing; an ordinary `llm` step still accepted anything the
model said. `expect` generalizes the same enforcement loop to any llm
step, so a program can declare what an acceptable reply *is* and let the
runtime — not the prompt author's hope — hold the line.

## The shape

```
step verdict {
  llm "model" {
    "Should this ship? " + inputs.change
    expect {
      one_of "ship", "hold"
    }
  }
}
```

Four rule kinds, a conjunction, one per line: `one_of` (closed set,
route-label normalization, canonical value bound), `matches` (fullmatch
regex, repeatable), `max_chars`, `nonempty`. All validated at parse time.

Enforcement mirrors route exactly: the rendered contract is appended to
the prompt (the model sees what will be enforced), a violating reply is
traced under the new `contract` phase and retried once with every
violation named, and a second violation fails the run. Violations fold
into `RunMetrics.contract_violations` and the probe report, next to
`route_violations` — a probe can now show whether adding a contract
bought the stability it claims to.

A `one_of` contract makes the step a closed-enum call, so it reuses the
optional `route(model, prompt, options)` client protocol — the dry-run
client answers deterministically with the first value, keeping contracted
programs runnable offline.

## Deliberate cuts

- **llm steps only.** A route step's contract *is* its arms; an agent
  step's final answer emerges from a tool loop where a retry means
  re-running tools — different machinery, different phase. Both reject
  `expect` with a pointed parse error.
- **No `else` edge on violation.** Routing exists for soft dispatch;
  `expect` is a hard requirement. Failing loud keeps the two ideas from
  blurring into each other.
- **One retry, same as route.** More retries buy tail latency, not
  correctness; a model that misses twice under explicit feedback is the
  probe's business to expose, not the runtime's to hide.
- **No JSON/schema validation.** `matches` covers light structure; real
  schema contracts deserve their own design (typed outputs, not string
  rules) rather than a regex bolted into v0.11.
- **Dry-run may violate `matches`/`max_chars`.** The echo client's reply
  is the prompt itself, which honestly fails tight contracts — the
  machinery fires and the run fails loud. Only `one_of` (via the route
  protocol) and `nonempty` are dry-run-safe; the shipped example sticks
  to those.
