"""Support-triage app — the vertical slice (L6).

One concrete multi-agent product built on the whole stack, proving the platform
end to end:

    author (L0)   triage.thread: agent `investigate` -> llm `draft`
    execute (L1)  the deterministic runtime + trace stream
    act     (L2)  the agent step calls this app's own tools (classify_priority,
                  search_kb over a bundled KB)
    persist (L3)  every run is a durable, resumable record in a sqlite store
    submit  (L4)  tickets are enqueued over HTTP; a worker pool drains them
    inspect (L5)  each run + its trace is readable on the dashboard

Nothing here re-implements any layer; the app is just a program, a tool
registry, and a thin entrypoint. Two modes:

    support-triage serve  --store runs.db [...]      # API + workers + dashboard
    support-triage run    --ticket "..." [--dry-run] # one ticket, durably, in-process
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from ...llm import AnthropicClient, DryRunClient, LLMClient, LLMError, OpenAICompatClient
from ...parser import parse_program
from ...server import serve
from ...store import RunStore, run_durable
from .tools import build_registry

PROGRAM_PATH = Path(__file__).with_name("triage.thread")


def load_program():
    """Parse the bundled triage program."""
    return parse_program(PROGRAM_PATH.read_text())


def _make_client(
    backend: str, base_url: Optional[str], max_tokens: int, timeout: float
) -> LLMClient:
    if backend == "dry-run":
        return DryRunClient()
    if backend == "openai":
        return OpenAICompatClient(base_url=base_url, max_tokens=max_tokens, timeout=timeout)
    return AnthropicClient(max_tokens=max_tokens, timeout=timeout)


def _add_backend_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--backend",
        choices=["dry-run", "openai", "anthropic"],
        default="dry-run",
        help="LLM backend (default dry-run — deterministic, no key). "
        "openai = DeepSeek/Ollama/any /v1; agent tool-calling needs a native "
        "tool-calling model (DeepSeek or Claude).",
    )
    p.add_argument("--base-url", default=None, help="endpoint for --backend openai")
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--timeout", type=float, default=120.0)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="support-triage", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="run the API + worker pool + dashboard")
    p_serve.add_argument("--store", required=True, help="path to the sqlite run store")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--workers", type=int, default=2)
    p_serve.add_argument("--auth-token-env", default="THREADLANG_AUTH_TOKEN")
    _add_backend_args(p_serve)

    p_run = sub.add_parser("run", help="triage one ticket durably, in-process")
    p_run.add_argument("--ticket", required=True, help="the support ticket text")
    p_run.add_argument("--store", default="triage-runs.db", help="sqlite run store path")
    p_run.add_argument("--dry-run", action="store_true", help="shorthand for --backend dry-run")
    _add_backend_args(p_run)

    args = parser.parse_args(argv)
    if args.max_tokens < 1 or args.timeout <= 0:
        parser.error("--max-tokens and --timeout must be positive")
    registry = build_registry()

    try:
        if args.cmd == "serve":
            client = _make_client(args.backend, args.base_url, args.max_tokens, args.timeout)
            serve(
                args.store,
                host=args.host,
                port=args.port,
                n_workers=args.workers,
                llm_client=client,
                tools=registry,
                auth_token=os.environ.get(args.auth_token_env),
            )
            return 0

        # cmd == "run"
        backend = "dry-run" if args.dry_run else args.backend
        client = _make_client(backend, args.base_url, args.max_tokens, args.timeout)
        store = RunStore(args.store)
        try:
            durable = run_durable(
                load_program(),
                {"ticket": args.ticket},
                store,
                llm_client=client,
                tools=registry,
            )
        finally:
            store.close()
        record = RunStore(args.store)
        try:
            final = record.get_run(durable.run_id)
        finally:
            record.close()
        print(f"run_id: {durable.run_id}  status: {final.status if final else '?'}\n")
        print(durable.result.output)
        return 0 if final and final.status == "completed" else 1
    except (LLMError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
