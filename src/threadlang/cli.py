"""Command-line interface for running ThreadLang programs."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List

from .llm import AnthropicClient, DryRunClient, LLMClient, LLMError
from .parser import parse_program
from .runtime import run_program


def _parse_inputs(input_flags: List[str]) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for item in input_flags:
        if "=" not in item:
            raise ValueError(f"Invalid --input format: {item!r}; expected key=value")
        key, value = item.split("=", 1)
        parsed[key] = value
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a ThreadLang source file.")
    parser.add_argument("source", type=Path, help="Path to a .thread source file")
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Input value in key=value form (repeatable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use the deterministic echo client instead of calling a real LLM. "
        "Lets you run a program with steps / emit llm without an API key.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print structured trace events to stderr after the output.",
    )
    args = parser.parse_args()

    source_text = args.source.read_text(encoding="utf-8")
    program = parse_program(source_text)

    client: LLMClient
    if args.dry_run:
        client = DryRunClient()
    else:
        try:
            client = AnthropicClient()
        except LLMError as exc:
            # Soft fallback: a program with no steps and `emit text` doesn't
            # need an LLM client. Warn only if the program will actually
            # need one and we couldn't build it.
            client = DryRunClient()
            if program.steps.steps or program.emit.kind == "llm":
                print(
                    f"warning: {exc}\n  (falling back to dry-run; output is the echoed prompt, not real LLM output)",
                    flush=True,
                )

    result = run_program(program, inputs=_parse_inputs(args.input), llm_client=client)
    print(result.output)

    if args.trace:
        import sys

        for event in result.trace:
            print(f"  [{event.phase}] {event.message}: {event.data}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
