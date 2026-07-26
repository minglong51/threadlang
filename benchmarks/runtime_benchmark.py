#!/usr/bin/env python3
"""Deterministic local microbenchmark for ThreadLang parser/runtime overhead.

No provider calls are made; DryRunClient isolates DSL/runtime overhead. This is
not a cross-system performance claim. Results include environment and workload
metadata so they can be reproduced rather than pooled across unlike machines.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from pathlib import Path

from threadlang.llm import DryRunClient
from threadlang.parser import parse_program
from threadlang.runtime import run_program

WORKLOADS = {
    "linear": """
thread Linear {
  context { role = "reviewer" }
  steps {
    step first { llm "dry" { context.role + ":" + inputs.task } }
    step second { llm "dry" { steps.first.output } }
  }
  emit text { steps.second.output }
}
""",
    "route": """
thread Route {
  context {}
  steps {
    step choose {
      route "dry" { inputs.kind on "draft" -> draft on "solve" -> solve else -> draft }
    }
    step draft { llm "dry" { "draft" then -> end } }
    step solve { llm "dry" { "solve" } }
  }
  emit text { steps.draft.output? + steps.solve.output? }
}
""",
    "contract": """
thread Contract {
  context {}
  steps {
    step answer { llm "dry" { inputs.task expect { nonempty max_chars 4096 } } }
  }
  emit text { steps.answer.output }
}
""",
}


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def measure(source: str, iterations: int, warmup: int) -> dict[str, float]:
    client = DryRunClient()
    for _ in range(warmup):
        run_program(parse_program(source), {"task": "x", "kind": "draft"}, llm_client=client)
    parse_ms: list[float] = []
    run_ms: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        program = parse_program(source)
        parsed = time.perf_counter_ns()
        run_program(program, {"task": "x", "kind": "draft"}, llm_client=client)
        finished = time.perf_counter_ns()
        parse_ms.append((parsed - started) / 1_000_000)
        run_ms.append((finished - parsed) / 1_000_000)
    return {
        "parse_median_ms": statistics.median(parse_ms),
        "parse_p95_ms": percentile(parse_ms, 0.95),
        "run_median_ms": statistics.median(run_ms),
        "run_p95_ms": percentile(run_ms, 0.95),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 1 or args.warmup < 0:
        parser.error("iterations must be positive and warmup non-negative")
    report = {
        "scope": "ThreadLang parser and DryRunClient runtime overhead; no live model calls",
        "iterations": args.iterations,
        "warmup": args.warmup,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "workloads": {
            name: measure(source, args.iterations, args.warmup)
            for name, source in WORKLOADS.items()
        },
    }
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
