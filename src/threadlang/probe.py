"""Controllability probes (L8) — reliability as a measurement, not a claim.

A probe runs the same program N times against a real backend and folds the
stored runs into a report of *behavioral stability*: how often each step
executed, how many distinct outputs it produced, how concentrated those
outputs are, how route decisions distributed, and how often output contracts
were violated or runs failed outright.

This follows the metrics doctrine (L7) one level up: a probe report is a
**pure fold over N persisted runs** — recomputable from the store, never a
separately-reported number. The methodology is CogniConsole's (repeated runs
under a fixed control structure, variance as the measured outcome) applied to
this stack's own programs: change a prompt, a contract, or a decomposition,
re-probe, and compare numbers instead of impressions.

Variance here is deliberately exact-match only — distinct-output counts and
mode frequency, no embedding distance. Contracted steps (route labels) make
exact match meaningful; for freeform steps the numbers are a coarse but
honest signal. `mode_frequency` is the share of executions producing the
single most common output: 1.0 is perfectly stable, 1/runs is maximally
unstable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

from .ast import AgentStep, Program, RouteStep
from .metrics import RunMetrics


@dataclass(frozen=True)
class ProbeRunData:
    """One run's contribution to a probe: its terminal status, final output,
    checkpointed step outputs (exactly the steps that executed), and folded
    metrics. The CLI assembles these from the store; tests build them
    directly."""

    status: str
    output: Optional[str]
    step_outputs: Mapping[str, str]
    metrics: RunMetrics


@dataclass(frozen=True)
class StepProbe:
    """Stability of one step across the probe's runs. `runs` counts the runs
    in which the step executed at all (routing may skip it). `label_counts`
    is populated for route steps only — their output space is closed, so the
    full histogram is small and meaningful."""

    step: str
    kind: str  # "llm" | "agent" | "route"
    runs: int
    distinct_outputs: int
    mode_frequency: Optional[float]  # None when the step never ran
    label_counts: Optional[Dict[str, int]] = None

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "step": self.step,
            "kind": self.kind,
            "runs": self.runs,
            "distinct_outputs": self.distinct_outputs,
            "mode_frequency": self.mode_frequency,
        }
        if self.label_counts is not None:
            out["label_counts"] = self.label_counts
        return out


@dataclass(frozen=True)
class ProbeReport:
    """The fold of N runs of one program: run-level failure rates, total
    contract violations, per-step stability in declaration order, and
    final-output stability across completed runs."""

    program: str
    runs: int
    completed: int
    failed: int
    failure_rate: Optional[float]  # None when runs == 0
    route_violations: int
    steps: List[StepProbe]
    output_distinct: int
    output_mode_frequency: Optional[float]  # None when no run completed
    avg_duration_ms: Optional[float]

    def to_dict(self) -> Dict[str, object]:
        return {
            "program": self.program,
            "runs": self.runs,
            "completed": self.completed,
            "failed": self.failed,
            "failure_rate": self.failure_rate,
            "route_violations": self.route_violations,
            "steps": [s.to_dict() for s in self.steps],
            "output": {
                "distinct": self.output_distinct,
                "mode_frequency": self.output_mode_frequency,
            },
            "avg_duration_ms": self.avg_duration_ms,
        }


def _step_kind(step: object) -> str:
    if isinstance(step, RouteStep):
        return "route"
    if isinstance(step, AgentStep):
        return "agent"
    return "llm"


def _mode_frequency(counts: Counter) -> Optional[float]:
    total = sum(counts.values())
    if total == 0:
        return None
    return max(counts.values()) / total


def probe_report(program: Program, runs: Sequence[ProbeRunData]) -> ProbeReport:
    """Fold N runs of `program` into a `ProbeReport`. Pure: same runs in, same
    report out. Steps appear in declaration order; a step routing never
    reached still appears, with `runs=0` — silent omission would read as
    stability."""
    completed = sum(1 for r in runs if r.status == "completed")
    failed = sum(1 for r in runs if r.status == "failed")
    route_violations = sum(r.metrics.route_violations for r in runs)

    steps: List[StepProbe] = []
    for step in program.steps.steps:
        outputs = Counter(
            r.step_outputs[step.name] for r in runs if step.name in r.step_outputs
        )
        kind = _step_kind(step)
        steps.append(
            StepProbe(
                step=step.name,
                kind=kind,
                runs=sum(outputs.values()),
                distinct_outputs=len(outputs),
                mode_frequency=_mode_frequency(outputs),
                label_counts=dict(outputs) if kind == "route" else None,
            )
        )

    final_outputs = Counter(
        r.output for r in runs if r.status == "completed" and r.output is not None
    )
    durations = [
        r.metrics.duration_ms for r in runs if r.metrics.duration_ms is not None
    ]

    return ProbeReport(
        program=program.thread_name,
        runs=len(runs),
        completed=completed,
        failed=failed,
        failure_rate=(failed / len(runs)) if runs else None,
        route_violations=route_violations,
        steps=steps,
        output_distinct=len(final_outputs),
        output_mode_frequency=_mode_frequency(final_outputs),
        avg_duration_ms=(sum(durations) / len(durations)) if durations else None,
    )
