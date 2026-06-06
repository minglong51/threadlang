"""Control-plane HTTP API (L4) — stdlib only, no web framework.

A thin JSON API over the durable run store and worker pool:

    POST /runs        {"source": "<thread program>", "inputs": {...}}
                      -> 201 {"run_id": "...", "status": "pending"}
    GET  /runs        -> 200 {"runs": [{id, status, program_name, ...}, ...]}
    GET  /runs/{id}   -> 200 {id, status, output, error, inputs, trace: [...]}
                         404 if unknown
    GET  /healthz     -> 200 {"ok": true}

`serve()` starts this API and a `WorkerPool` against the same store, so a POST
enqueues a run and the workers drain it; poll `GET /runs/{id}` to watch it move
pending -> running -> completed. Built on stdlib `http.server`, keeping the
project's zero-dependency promise.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .control import WorkerPool
from .dashboard import render_run_detail, render_run_list
from .llm import LLMClient
from .parser import ParseError, parse_program
from .store import RunStore
from .tools import ToolRegistry


class _Handler(BaseHTTPRequestHandler):
    # ThreadingHTTPServer gives each request its own thread; each opens its own
    # RunStore (sqlite connections are per-thread).
    server_version = "threadlang/0.6"

    def _store(self) -> RunStore:
        return RunStore(self.server.store_path)  # type: ignore[attr-defined]

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code: int, html_text: str) -> None:
        body = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # quiet by default
        pass

    def do_GET(self) -> None:
        store = self._store()
        try:
            # ---- dashboard (HTML, read-only) ----
            if self.path in ("/", "/ui"):
                self._send_html(200, render_run_list(store.list_runs()))
                return
            if self.path.startswith("/ui/runs/"):
                run_id = self.path[len("/ui/runs/"):]
                record = store.get_run(run_id)
                if record is None:
                    self._send_html(404, f"<h1>404</h1><p>unknown run: {run_id}</p>")
                    return
                self._send_html(200, render_run_detail(record, store.load_events(run_id)))
                return
            # ---- JSON API ----
            if self.path == "/healthz":
                self._send(200, {"ok": True})
            elif self.path == "/runs":
                self._send(200, {"runs": [_run_summary(r) for r in store.list_runs()]})
            elif self.path.startswith("/runs/"):
                run_id = self.path[len("/runs/"):]
                record = store.get_run(run_id)
                if record is None:
                    self._send(404, {"error": f"unknown run_id: {run_id}"})
                    return
                payload = _run_summary(record)
                payload["trace"] = [
                    {"phase": e.phase, "message": e.message, "data": e.data}
                    for e in store.load_events(run_id)
                ]
                self._send(200, payload)
            else:
                self._send(404, {"error": "not found"})
        finally:
            store.close()

    def do_POST(self) -> None:
        if self.path != "/runs":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "body must be JSON"})
            return
        source = body.get("source")
        if not isinstance(source, str) or not source.strip():
            self._send(400, {"error": "missing 'source' (program text)"})
            return
        inputs = body.get("inputs") or {}
        if not isinstance(inputs, dict):
            self._send(400, {"error": "'inputs' must be an object"})
            return
        try:
            program = parse_program(source)  # validate before enqueuing
        except ParseError as exc:
            self._send(400, {"error": f"parse error: {exc}"})
            return
        store = self._store()
        try:
            run_id = store.enqueue_run(
                program.thread_name, source, {str(k): str(v) for k, v in inputs.items()}
            )
        finally:
            store.close()
        self._send(201, {"run_id": run_id, "status": "pending"})


def _run_summary(record) -> dict:
    return {
        "id": record.id,
        "program_name": record.program_name,
        "status": record.status,
        "inputs": record.inputs,
        "output": record.output,
        "error": record.error,
    }


def make_server(store_path: str, host: str = "127.0.0.1", port: int = 8765):
    """Build (but don't start) the HTTP server bound to a store path."""
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.store_path = store_path  # type: ignore[attr-defined]
    return httpd


def serve(
    store_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    n_workers: int = 2,
    llm_client: Optional[LLMClient] = None,
    tools: Optional[ToolRegistry] = None,
) -> None:
    """Start the worker pool + HTTP API and block serving requests.

    `tools` is the registry the workers' agent steps draw from; pass an app's
    custom registry (e.g. the support-triage app's) to serve domain programs.
    Defaults to the deterministic built-ins.
    """
    pool = WorkerPool(store_path, n_workers=n_workers, llm_client=llm_client, tools=tools)
    pool.start()
    httpd = make_server(store_path, host, port)
    print(f"threadlang control plane on http://{host}:{port}  "
          f"(store={store_path}, workers={n_workers})\n"
          f"  dashboard: http://{host}:{port}/    ·    API: POST http://{host}:{port}/runs",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pool.stop()
        httpd.server_close()


def main() -> int:
    import argparse

    from .llm import AnthropicClient, DryRunClient, LLMError, OpenAICompatClient

    parser = argparse.ArgumentParser(description="Run the ThreadLang control-plane server.")
    parser.add_argument("--store", required=True, help="Path to the sqlite run store.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--backend", choices=["dry-run", "anthropic", "openai"], default="dry-run",
        help="LLM backend the workers use (default dry-run — deterministic, no key).",
    )
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible endpoint for --backend openai.")
    args = parser.parse_args()

    client: LLMClient
    if args.backend == "dry-run":
        client = DryRunClient()
    elif args.backend == "openai":
        client = OpenAICompatClient(base_url=args.base_url)
    else:
        try:
            client = AnthropicClient()
        except LLMError as exc:
            print(f"error: {exc}", flush=True)
            return 1

    serve(args.store, host=args.host, port=args.port, n_workers=args.workers, llm_client=client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
