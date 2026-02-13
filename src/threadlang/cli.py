"""Command-line interface for ThreadLang."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict

from .parser import parse_program
from .runtime import run_program


def main() -> None:
    parser = argparse.ArgumentParser(prog="thread")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a .thread program")
    run_parser.add_argument("file", type=Path, help="Path to .thread source file")
    run_parser.add_argument(
        "--inputs",
        nargs="*",
        default=[],
        metavar="key=value",
        help="Input bindings available via inputs.<key>",
    )

    args = parser.parse_args()

    if args.command == "run":
        source = args.file.read_text(encoding="utf-8")
        program = parse_program(source)
        result = run_program(program, inputs=_parse_inputs(args.inputs))
        print(result.output)


def _parse_inputs(items: list[str]) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Input must be key=value, got: {item!r}")
        key, value = item.split("=", 1)
        parsed[key] = value
    return parsed


if __name__ == "__main__":
    main()
