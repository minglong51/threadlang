"""Command-line interface for running ThreadLang programs."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, List

from .llm import AnthropicClient, DryRunClient, LLMClient, LLMError, OpenAICompatClient
from .parser import parse_program
from .runtime import RuntimeError as TLRuntimeError, RuntimeResult, run_program
from .store import RunStore, run_durable


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
        "--backend",
        choices=["dry-run", "anthropic", "openai"],
        default="anthropic",
        help="Which LLM backend to use. 'dry-run' is the deterministic echo "
        "client (no key). 'anthropic' calls Claude (ANTHROPIC_API_KEY). 'openai' "
        "calls any OpenAI-compatible endpoint — hosted DeepSeek by default, or a "
        "local Ollama via THREADLANG_BASE_URL (THREADLANG_API_KEY optional).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Shorthand for --backend dry-run: the deterministic echo client. "
        "Lets you run a program with steps / emit llm without an API key.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Override the OpenAI-compatible endpoint for --backend openai "
        "(e.g. http://100.76.118.28:11434/v1 for a local Ollama).",
    )
    parser.add_argument(
        "--store",
        default=None,
        metavar="PATH",
        help="Persist the run to a sqlite store at PATH (durable trace + step "
        "checkpoints). Prints the run id to stderr; a crashed run can be resumed.",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="RUN_ID",
        help="Resume a prior run by id from --store, skipping completed steps. "
        "Requires --store.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print structured trace events to stderr after the output.",
    )
    args = parser.parse_args()

    if args.resume and not args.store:
        print("error: --resume requires --store", file=__import__("sys").stderr)
        return 2

    source_text = args.source.read_text(encoding="utf-8")
    program = parse_program(source_text)

    backend = "dry-run" if args.dry_run else args.backend

    client: LLMClient
    if backend == "dry-run":
        client = DryRunClient()
    elif backend == "openai":
        client = OpenAICompatClient(base_url=args.base_url)
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

    import sys

    inputs = _parse_inputs(args.input)
    result: RuntimeResult
    if args.store:
        store = RunStore(args.store)
        # Establish the run id up front (create fresh, or reuse the one being
        # resumed) so we can report it even if the run crashes mid-flight.
        run_id = args.resume or store.create_run(program.thread_name, inputs)
        print(f"run_id: {run_id}", file=sys.stderr)
        try:
            durable = run_durable(program, inputs, store, llm_client=client, run_id=run_id)
        except (LLMError, TLRuntimeError) as exc:
            # The run is marked 'failed' and its completed steps are checkpointed;
            # tell the user how to resume from exactly where it died.
            print(f"error: {exc}", file=sys.stderr)
            print(
                f"  run failed; resume with: --store {args.store} --resume {run_id}",
                file=sys.stderr,
            )
            store.close()
            return 1
        result = durable.result
        store.close()
    else:
        try:
            result = run_program(program, inputs=inputs, llm_client=client)
        except (LLMError, TLRuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    print(result.output)

    if args.trace:
        for event in result.trace:
            print(f"  [{event.phase}] {event.message}: {event.data}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
