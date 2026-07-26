"""Authenticated single-node HTTP control plane for ThreadLang.

The server is deliberately small and stdlib-only. Its production boundary is a
single process backed by one SQLite file. Non-loopback binds fail closed unless
a bearer token is configured; liveness/readiness probes expose no run data.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from .control import WorkerPool
from .dashboard import render_run_detail, render_run_list
from .ir import (
    IRCompileError,
    canonical_ir_bytes,
    compile_program,
    load_ir_bytes,
    program_from_ir,
    workflow_fingerprint,
)
from .llm import LLMClient
from .parser import ParseError, parse_program
from .policy import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_MAX_PENDING_RUNS,
    DEFAULT_MAX_RETAINED_RUNS,
    MAX_INPUT_KEY_CHARS,
    MAX_INPUT_VALUE_CHARS,
    MAX_INPUTS,
    MAX_LIST_LIMIT,
    MAX_REQUEST_BYTES,
    MAX_SOURCE_BYTES,
)
from .store import RunStore, RunStoreCapacityError
from .tools import ToolRegistry


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "threadlang/0.13"

    def _store(self) -> RunStore:
        return RunStore(self.server.store_path)  # type: ignore[attr-defined]

    def _send_headers(self, code: int, content_type: str, length: int) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_headers(code, "application/json", len(body))
        self.wfile.write(body)

    def _send_html(self, code: int, html_text: str) -> None:
        body = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = self.server.auth_token  # type: ignore[attr-defined]
        if token is None:
            return True
        supplied = self.headers.get("Authorization", "")
        prefix = "Bearer "
        valid = supplied.startswith(prefix) and hmac.compare_digest(supplied[len(prefix) :], token)
        if not valid:
            body = json.dumps({"error": "unauthorized"}, separators=(",", ":")).encode()
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="threadlang"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        return valid

    def _valid_browser_origin(self) -> bool:
        """Protect tokenless loopback mode from DNS-rebinding/browser POSTs."""
        if self.server.auth_token is not None:  # type: ignore[attr-defined]
            return True
        host = self.headers.get("Host", "")
        hostname = urlsplit(f"http://{host}").hostname
        if hostname is None or not _is_loopback_host(hostname):
            self._send(421, {"error": "invalid Host for loopback-only server"})
            return False
        if self.command == "POST":
            origin = self.headers.get("Origin")
            if origin:
                origin_host = urlsplit(origin).hostname
                if origin_host is None or not _is_loopback_host(origin_host):
                    self._send(403, {"error": "cross-origin POST denied"})
                    return False
        return True

    def log_message(self, format: str, *args: object) -> None:
        # Structured logs intentionally exclude headers, bodies, inputs, traces,
        # and auth material.
        print(
            json.dumps(
                {
                    "component": "threadlang-http",
                    "client": self.client_address[0],
                    "method": self.command,
                    "path": urlsplit(self.path).path,
                    "message": format % args,
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
            flush=True,
        )

    def do_GET(self) -> None:
        if not self._valid_browser_origin():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/healthz":
            try:
                store = self._store()
                store._conn.execute("SELECT 1").fetchone()
                store.close()
            except Exception:
                self._send(503, {"ok": False, "database": "unavailable"})
                return
            self._send(200, {"ok": True, "database": "ok"})
            return
        if path == "/readyz":
            pool = self.server.worker_pool  # type: ignore[attr-defined]
            healthy = pool is not None and pool.is_healthy()
            store = self._store()
            try:
                queue = store.counts_by_status()
            finally:
                store.close()
            self._send(
                200 if healthy else 503,
                {
                    "ok": healthy,
                    "workers": pool.status() if pool is not None else None,
                    "queue": {
                        "pending": queue.get("pending", 0),
                        "running": queue.get("running", 0),
                    },
                },
            )
            return
        if not self._authorized():
            return

        store = self._store()
        try:
            if path in ("/", "/ui"):
                self._send_html(
                    200,
                    render_run_list(
                        store.list_runs(limit=DEFAULT_LIST_LIMIT),
                        store.aggregate_metrics(),
                    ),
                )
                return
            if path.startswith("/ui/runs/"):
                run_id = path[len("/ui/runs/") :]
                record = store.get_run(run_id)
                if record is None:
                    self._send_html(404, "<h1>404</h1><p>unknown run</p>")
                    return
                self._send_html(
                    200,
                    render_run_detail(record, store.load_events(run_id), store.run_metrics(run_id)),
                )
                return
            if path == "/metrics":
                self._send(200, store.aggregate_metrics().to_dict())
                return
            if path == "/runs":
                query = parse_qs(parsed.query)
                try:
                    limit = int(query.get("limit", [str(DEFAULT_LIST_LIMIT)])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    self._send(400, {"error": "limit and offset must be integers"})
                    return
                if not 1 <= limit <= MAX_LIST_LIMIT or offset < 0:
                    self._send(
                        400,
                        {"error": f"limit must be 1..{MAX_LIST_LIMIT}; offset must be >= 0"},
                    )
                    return
                runs = store.list_runs(limit=limit, offset=offset)
                self._send(
                    200,
                    {
                        "runs": [_run_summary(record) for record in runs],
                        "limit": limit,
                        "offset": offset,
                    },
                )
                return
            if path.startswith("/runs/") and path.endswith("/metrics"):
                run_id = path[len("/runs/") : -len("/metrics")]
                metrics = store.run_metrics(run_id)
                if metrics is None:
                    self._send(404, {"error": "unknown run_id"})
                    return
                self._send(200, {"run_id": run_id, "metrics": metrics.to_dict()})
                return
            if path.startswith("/runs/"):
                run_id = path[len("/runs/") :]
                record = store.get_run(run_id)
                if record is None:
                    self._send(404, {"error": "unknown run_id"})
                    return
                payload = _run_summary(record)
                payload["trace"] = [
                    {"phase": event.phase, "message": event.message, "data": event.data}
                    for event in store.load_events(run_id)
                ]
                self._send(200, payload)
                return
            self._send(404, {"error": "not found"})
        finally:
            store.close()

    def do_POST(self) -> None:
        if not self._valid_browser_origin():
            return
        path = urlsplit(self.path).path
        if path != "/runs":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self._send(415, {"error": "Content-Type must be application/json"})
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._send(411, {"error": "Content-Length required"})
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if length < 0:
            self._send(400, {"error": "invalid Content-Length"})
            return
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            self._send(413, {"error": f"request exceeds {MAX_REQUEST_BYTES} bytes"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, {"error": "body must be valid JSON"})
            return
        if not isinstance(body, dict):
            self._send(400, {"error": "body must be a JSON object"})
            return
        source = body.get("source")
        ir_object = body.get("ir")
        if (source is None) == (ir_object is None):
            self._send(400, {"error": "provide exactly one of 'source' or 'ir'"})
            return
        inputs = body.get("inputs", {})
        error = _validate_inputs(inputs)
        if error is not None:
            self._send(400, {"error": error})
            return

        try:
            if source is not None:
                if not isinstance(source, str) or not source.strip():
                    self._send(400, {"error": "'source' must be non-empty program text"})
                    return
                if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
                    self._send(413, {"error": f"source exceeds {MAX_SOURCE_BYTES} bytes"})
                    return
                program = parse_program(source)
                workflow = compile_program(program)
            else:
                ir_bytes = json.dumps(
                    ir_object, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                workflow = load_ir_bytes(ir_bytes)
                program = program_from_ir(workflow)
        except (ParseError, IRCompileError) as exc:
            self._send(400, {"error": f"invalid workflow: {exc}"})
            return

        definition_bytes = canonical_ir_bytes(workflow)
        definition_json = definition_bytes.decode("utf-8")
        definition_sha256 = workflow_fingerprint(workflow)
        store = self._store()
        try:
            try:
                enqueue_kwargs = {
                    "max_pending": self.server.max_pending,  # type: ignore[attr-defined]
                    "max_retained": self.server.max_retained,  # type: ignore[attr-defined]
                }
                if source is not None:
                    run_id = store.enqueue_run(
                        program.thread_name,
                        source,
                        dict(inputs),
                        definition_json=definition_json,
                        definition_sha256=definition_sha256,
                        ir_version=workflow.ir_version,
                        **enqueue_kwargs,
                    )
                else:
                    run_id = store.enqueue_ir(
                        program.thread_name,
                        definition_json,
                        definition_sha256,
                        workflow.ir_version,
                        dict(inputs),
                        **enqueue_kwargs,
                    )
            except RunStoreCapacityError as exc:
                self._send(429, {"error": str(exc)})
                return
        finally:
            store.close()
        self._send(201, {"run_id": run_id, "status": "pending"})


def _validate_inputs(value: object) -> Optional[str]:
    if not isinstance(value, dict):
        return "'inputs' must be an object"
    if len(value) > MAX_INPUTS:
        return f"inputs exceeds {MAX_INPUTS} entries"
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            return "input keys and values must be strings"
        if not key or len(key) > MAX_INPUT_KEY_CHARS:
            return f"input keys must be 1..{MAX_INPUT_KEY_CHARS} characters"
        if len(item) > MAX_INPUT_VALUE_CHARS:
            return f"input values must be <= {MAX_INPUT_VALUE_CHARS} characters"
    return None


def _run_summary(record) -> dict:
    return {
        "id": record.id,
        "program_name": record.program_name,
        "status": record.status,
        "inputs": record.inputs,
        "output": record.output,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "program_sha256": record.program_sha256,
        "inputs_sha256": record.inputs_sha256,
        "definition_sha256": record.definition_sha256,
        "ir_version": record.ir_version,
    }


def make_server(
    store_path: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    auth_token: Optional[str] = None,
    worker_pool: Optional[WorkerPool] = None,
    max_pending: int = DEFAULT_MAX_PENDING_RUNS,
    max_retained: int = DEFAULT_MAX_RETAINED_RUNS,
) -> ThreadingHTTPServer:
    """Build but do not start the control-plane server."""
    if not _is_loopback_host(host) and not auth_token:
        raise ValueError("non-loopback bind requires a bearer auth token")
    if auth_token is not None and len(auth_token) < 16:
        raise ValueError("auth token must be at least 16 characters")
    if max_pending < 1 or max_retained < 0:
        raise ValueError("max_pending must be >= 1 and max_retained must be >= 0")
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.store_path = store_path  # type: ignore[attr-defined]
    httpd.auth_token = auth_token  # type: ignore[attr-defined]
    httpd.worker_pool = worker_pool  # type: ignore[attr-defined]
    httpd.max_pending = max_pending  # type: ignore[attr-defined]
    httpd.max_retained = max_retained  # type: ignore[attr-defined]
    return httpd


def serve(
    store_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    n_workers: int = 2,
    llm_client: Optional[LLMClient] = None,
    tools: Optional[ToolRegistry] = None,
    auth_token: Optional[str] = None,
    max_pending: int = DEFAULT_MAX_PENDING_RUNS,
    max_retained: int = DEFAULT_MAX_RETAINED_RUNS,
) -> None:
    pool = WorkerPool(store_path, n_workers=n_workers, llm_client=llm_client, tools=tools)
    pool.start()
    try:
        httpd = make_server(
            store_path,
            host,
            port,
            auth_token=auth_token,
            worker_pool=pool,
            max_pending=max_pending,
            max_retained=max_retained,
        )
    except Exception:
        pool.stop()
        raise
    print(
        f"threadlang control plane on http://{host}:{port} "
        f"(store={store_path}, workers={n_workers}, auth={'enabled' if auth_token else 'loopback-only'})",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        pool.stop()


def main() -> int:
    import argparse

    from .llm import AnthropicClient, DryRunClient, LLMError, OpenAICompatClient

    parser = argparse.ArgumentParser(description="Run the ThreadLang control-plane server.")
    parser.add_argument("--store", required=True, help="Path to the sqlite run store.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--backend", choices=["dry-run", "anthropic", "openai"], default="dry-run")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--auth-token-env",
        default="THREADLANG_AUTH_TOKEN",
        help="Environment variable containing the bearer token (never pass tokens on argv).",
    )
    parser.add_argument("--max-pending", type=int, default=DEFAULT_MAX_PENDING_RUNS)
    parser.add_argument("--max-retained", type=int, default=DEFAULT_MAX_RETAINED_RUNS)
    args = parser.parse_args()

    client: LLMClient
    if args.backend == "dry-run":
        client = DryRunClient()
    elif args.backend == "openai":
        client = OpenAICompatClient(
            base_url=args.base_url,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
    else:
        try:
            client = AnthropicClient(max_tokens=args.max_tokens, timeout=args.timeout)
        except LLMError as exc:
            print(f"error: {exc}", file=sys.stderr, flush=True)
            return 1

    if args.max_tokens < 1 or args.timeout <= 0:
        print("error: --max-tokens and --timeout must be positive", file=sys.stderr)
        return 2
    auth_token = os.environ.get(args.auth_token_env)
    try:
        serve(
            args.store,
            host=args.host,
            port=args.port,
            n_workers=args.workers,
            llm_client=client,
            auth_token=auth_token,
            max_pending=args.max_pending,
            max_retained=args.max_retained,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
