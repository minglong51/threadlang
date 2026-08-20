"""Command-line interface for running ThreadLang programs."""

from __future__ import annotations

import argparse
import math
import shlex
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Dict, List

from . import __version__
from .ir import canonical_ir_bytes, compile_program, load_ir_bytes, program_from_ir
from .llm import (
    AgentTurn,
    AnthropicClient,
    DryRunClient,
    LLMClient,
    LLMError,
    Message,
    OpenAICompatClient,
)
from .metrics import compute_metrics
from .parser import parse_program
from .probe import ProbeRunData, probe_report
from .runtime import RuntimeError as TLRuntimeError
from .runtime import RuntimeResult, run_program
from .store import RunStore, run_durable
from .tools import ToolSpec


def _parse_inputs(input_flags: List[str]) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for item in input_flags:
        if "=" not in item:
            raise ValueError(f"Invalid --input format: {item!r}; expected key=value")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"Invalid --input format: {item!r}; key cannot be empty")
        parsed[key] = value
    return parsed


class _LazyClient:
    def __init__(self, factory: Callable[[], LLMClient]) -> None:
        self._factory = factory
        self._client: LLMClient | None = None

    def _get(self) -> LLMClient:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def complete(self, model: str, prompt: str) -> str:
        return self._get().complete(model, prompt)

    def agent_step(
        self, model: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        agent_step = getattr(self._get(), "agent_step", None)
        if agent_step is None:
            raise LLMError("selected backend does not support agent steps")
        return agent_step(model, messages, tools)

    def route(self, model: str, prompt: str, options: Sequence[str]) -> str:
        client = self._get()
        route = getattr(client, "route", None)
        if route is None:
            return client.complete(model, prompt)
        return route(model, prompt, options)


def _should_offer_resume(exc: BaseException) -> bool:
    return isinstance(exc, LLMError) or (
        isinstance(exc, TLRuntimeError)
        and str(exc).startswith(("LLM call failed", "Agent call failed"))
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a ThreadLang source file.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("source", type=Path, help="Path to a .thread source file or IR JSON")
    parser.add_argument(
        "--from-ir",
        action="store_true",
        help="Treat SOURCE as a canonical Workflow IR JSON document instead of .thread source.",
    )
    parser.add_argument(
        "--emit-ir",
        metavar="PATH",
        help="Compile/normalize SOURCE to canonical Workflow IR JSON and exit. Use '-' for stdout.",
    )
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
        "(e.g. http://<host>:11434/v1 for a local Ollama).",
    )
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=120.0, help="Provider timeout in seconds")
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
        help="Resume a prior run by id from --store, skipping completed steps. Requires --store.",
    )
    parser.add_argument(
        "--probe",
        type=int,
        default=None,
        metavar="N",
        help="Controllability probe: run the program N times, persist every run "
        "to --store, and print a stability report (per-step variance, route-label "
        "distribution, violation and failure rates) as JSON. Requires --store; "
        "failed runs are counted as data, not errors.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print structured trace events to stderr after the output.",
    )
    parser.add_argument(
        "--metrics",
        action="store_true",
        help="Print run metrics (derived from the trace) as JSON to stderr "
        "after the output. With --store, includes wall-clock latency.",
    )
    args = parser.parse_args()

    if args.resume and not args.store:
        print("error: --resume requires --store", file=sys.stderr)
        return 2
    if args.emit_ir and (args.resume or args.probe is not None or args.store):
        print(
            "error: --emit-ir cannot be combined with --store, --resume, or --probe",
            file=sys.stderr,
        )
        return 2
    if args.max_tokens < 1 or not math.isfinite(args.timeout) or args.timeout <= 0:
        print("error: --max-tokens and --timeout must be positive", file=sys.stderr)
        return 2
    if args.probe is not None:
        if args.probe < 1:
            print("error: --probe N must be >= 1", file=sys.stderr)
            return 2
        if not args.store:
            print(
                "error: --probe requires --store (the report is a fold over the persisted runs)",
                file=sys.stderr,
            )
            return 2
        if args.resume:
            print("error: --probe and --resume are incompatible", file=sys.stderr)
            return 2

    try:
        return _run(args)
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    if args.from_ir:
        workflow = load_ir_bytes(args.source.read_bytes())
        program = program_from_ir(workflow)
        source_text = None
    else:
        source_text = args.source.read_text(encoding="utf-8")
        program = parse_program(source_text)
        workflow = compile_program(program)

    if args.emit_ir:
        payload = canonical_ir_bytes(workflow)
        if args.emit_ir == "-":
            sys.stdout.buffer.write(payload + b"\n")
        else:
            Path(args.emit_ir).write_bytes(payload + b"\n")
        return 0

    inputs = _parse_inputs(args.input)
    backend = "dry-run" if args.dry_run else args.backend

    client: LLMClient
    if backend == "dry-run":
        client = DryRunClient()
    elif backend == "openai":
        client = _LazyClient(
            lambda: OpenAICompatClient(
                base_url=args.base_url,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
        )
    else:
        client = _LazyClient(
            lambda: AnthropicClient(max_tokens=args.max_tokens, timeout=args.timeout)
        )

    if args.probe is not None:
        return _probe(args, program, inputs, client)
    result: RuntimeResult
    if args.store:
        store = RunStore(args.store)
        try:
            if args.resume:
                prior = store.get_run(args.resume)
                if prior is not None:
                    inputs = {**prior.inputs, **inputs}
            run_id = args.resume or store.create_run(program.thread_name, inputs)
            print(f"run_id: {run_id}", file=sys.stderr)
            try:
                durable = run_durable(
                    program,
                    inputs,
                    store,
                    llm_client=client,
                    run_id=run_id,
                    source=source_text,
                )
            except (LLMError, TLRuntimeError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                if _should_offer_resume(exc):
                    resume_args = [
                        "threadlang",
                        "--store",
                        str(args.store),
                        "--resume",
                        run_id,
                        "--backend",
                        backend,
                        "--max-tokens",
                        str(args.max_tokens),
                        "--timeout",
                        str(args.timeout),
                    ]
                    if args.from_ir:
                        resume_args.append("--from-ir")
                    if backend == "openai" and args.base_url:
                        resume_args.extend(("--base-url", args.base_url))
                    resume_args.extend(("--", str(args.source)))
                    print(
                        f"  run failed; resume with: {shlex.join(resume_args)}",
                        file=sys.stderr,
                    )
                return 1
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            result = durable.result
            run_metrics = store.run_metrics(run_id) if args.metrics else None
        finally:
            store.close()
    else:
        try:
            result = run_program(program, inputs=inputs, llm_client=client)
        except (LLMError, TLRuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        run_metrics = compute_metrics(result.trace, status="completed") if args.metrics else None
    print(result.output)

    if args.trace:
        for event in result.trace:
            print(f"  [{event.phase}] {event.message}: {event.data}", file=sys.stderr)
    if args.metrics and run_metrics is not None:
        import json

        print(json.dumps(run_metrics.to_dict(), indent=2), file=sys.stderr)
    return 0


def _probe(args: argparse.Namespace, program, inputs: Dict[str, str], client: LLMClient) -> int:
    """Run the program N times durably and print the stability report. Every
    run — failures included — is an ordinary persisted run, visible on the
    dashboard and recomputable later; the report is a pure fold over them."""
    import json

    store = RunStore(args.store)
    try:
        runs = []
        for i in range(args.probe):
            run_id = store.create_run(program.thread_name, inputs)
            print(f"probe run {i + 1}/{args.probe}: {run_id}", file=sys.stderr)
            try:
                run_durable(program, inputs, store, llm_client=client, run_id=run_id)
            except (LLMError, TLRuntimeError) as exc:
                print(f"  failed: {exc}", file=sys.stderr)
            record = store.get_run(run_id)
            metrics = store.run_metrics(run_id)
            if record is None or metrics is None:
                raise RuntimeError(f"probe run disappeared from store: {run_id}")
            runs.append(
                ProbeRunData(
                    status=record.status,
                    output=record.output,
                    step_outputs=store.load_step_outputs(run_id),
                    metrics=metrics,
                )
            )
        report = probe_report(program, runs)
    finally:
        store.close()
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
