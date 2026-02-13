"""Command-line interface for running ThreadLang programs."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List

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
    args = parser.parse_args()

    source_text = args.source.read_text(encoding="utf-8")
    program = parse_program(source_text)
    result = run_program(program, inputs=_parse_inputs(args.input))
    print(result.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
