# Phase 7 — Controllability probes (v0.10): reliability as a measurement

## Why now

Since v0.9 the repo *claims* that structure — routing, output contracts,
decomposition — reduces behavioral variance. Nothing in the stack could
demonstrate it. The probe harness closes that gap: run the same program N
times, persist every run, and fold the runs into stability numbers. Change a
prompt, a contract, or a decomposition; re-probe; compare numbers instead of
impressions.

The methodology is CogniConsole's (repeated runs under a fixed control
structure, variance and failure rate as the measured outcomes) pointed at our
own programs. It is also the promised consumer of the metrics layer: v0.8
said metrics are "the substrate a data-driven self-improvement loop reads" —
this is that loop's first reader.

## The shape

One new module and one CLI flag; the store is untouched.

- **`probe.py`** — `probe_report(program, runs) -> ProbeReport`, a **pure
  fold over N persisted runs** (the L7 doctrine, one level up). Inputs are
  `ProbeRunData` records assembled from ordinary store queries: terminal
  status, final output, checkpointed step outputs, folded `RunMetrics`.
  Per step (declaration order): executions, distinct outputs, mode
  frequency, and — for route steps only, whose output space is closed — the
  full label histogram. Per report: failure rate, total route violations,
  final-output stability, average duration.

- **`--probe N`** — runs the program N times via the existing
  `create_run` + `run_durable(run_id=...)` path, so every probe run is an
  ordinary durable run: dashboard-visible, resumable in principle, and
  recomputable later. A failed run is **data** (failure rate), not an error
  that aborts the probe. Requires `--store` — the report must be a fold over
  persisted runs, or it would be a separately-reported number.

## Interpreting the numbers

`mode_frequency` is the share of executions producing the single most common
output: 1.0 is perfectly stable (the dry-run baseline), 1/runs is maximally
unstable. A step routing skipped in every run reports `runs=0` and a `None`
mode frequency — absent, never "stable". Route-label histograms show
decision distribution directly; `route_violations` counts contract rejections
before retry resolution.

## Deliberate cuts

- **Exact-match variance only** — distinct counts and mode frequency, no
  embedding distance or fuzzy similarity. Contracted outputs (route labels)
  make exact match meaningful; for freeform steps the numbers are coarse but
  honest. Semantic variance metrics would import a model into the measuring
  instrument — the thing this stack exists to avoid.
- **No perturbation generator** — CogniConsole's probes vary inputs
  (noise, adversarial cases); ours re-run fixed inputs. Input perturbation
  composes on top later (probe once per input variant); generating variants
  belongs to a harness, not the fold.
- **No A/B in the report** — comparing two programs is two probes and a
  diff of two JSON reports. A comparison view can land on the dashboard once
  there is a real workflow that wants it.
- **Serial runs** — N sequential durable runs. Parallelizing through the
  worker pool is possible (the queue exists) but adds coordination for no
  measurement benefit at small N.
