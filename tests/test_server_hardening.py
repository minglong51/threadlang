"""Control-plane admission, authentication, and readiness tests."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import sys
import threading

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.control import WorkerPool, process_one  # noqa: E402
from threadlang.ir import canonical_ir_bytes, compile_program, load_ir_bytes  # noqa: E402
from threadlang.llm import DryRunClient  # noqa: E402
from threadlang.parser import parse_program  # noqa: E402
from threadlang.policy import MAX_REQUEST_BYTES  # noqa: E402
import threadlang.server as server_module  # noqa: E402
from threadlang.server import make_server  # noqa: E402
from threadlang.store import RunStore  # noqa: E402

TOKEN = "production-test-token-32-characters"
SOURCE = 'thread T { context {} emit text { "ok" } }'


class _RunningServer:
    def __init__(self, tmp_path: Path, *, pool: WorkerPool | None = None, max_pending: int = 1000):
        self.path = str(tmp_path / "runs.db")
        self.pool = pool
        self.server = make_server(
            self.path,
            "127.0.0.1",
            0,
            auth_token=TOKEN,
            worker_pool=pool,
            max_pending=max_pending,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        authorized: bool = False,
        content_type: str = "application/json",
    ) -> tuple[int, dict]:
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": content_type}
        if authorized:
            headers["Authorization"] = f"Bearer {TOKEN}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        status = response.status
        conn.close()
        return status, data

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def test_non_loopback_bind_requires_auth_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-loopback bind requires"):
        make_server(str(tmp_path / "x.db"), "0.0.0.0", 0)


@pytest.mark.parametrize("port", [-1, 65536])
def test_make_server_rejects_out_of_range_ports(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError, match="port must be between 0 and 65535"):
        make_server(str(tmp_path / "x.db"), "127.0.0.1", port)


def test_unauthenticated_loopback_rejects_dns_rebinding_and_cross_origin(tmp_path: Path) -> None:
    server = make_server(str(tmp_path / "local.db"), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        body = json.dumps({"source": SOURCE, "inputs": {}}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("GET", "/healthz", skip_host=True)
        conn.putheader("Host", "evil.example")
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 421
        response.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/runs",
            body=body,
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": "[",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        assert response.status == 403
        response.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("GET", "/healthz", skip_host=True)
        conn.putheader("Host", "[")
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 421
        response.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/runs",
            body=body,
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        assert response.status == 403
        response.read()
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_malformed_request_target_returns_400_and_server_survives(tmp_path: Path) -> None:
    running = _RunningServer(tmp_path)
    try:
        with socket.create_connection(("127.0.0.1", running.port), timeout=5) as connection:
            connection.sendall(
                b"GET http://[ HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            response = b""
            while chunk := connection.recv(4096):
                response += chunk
        assert response.startswith(b"HTTP/1.0 400")
        assert b"invalid request target" in response
        assert running.request("GET", "/healthz") == (200, {"ok": True, "database": "ok"})
    finally:
        running.close()


def test_health_is_public_but_data_routes_require_bearer(tmp_path: Path) -> None:
    running = _RunningServer(tmp_path)
    try:
        status, health = running.request("GET", "/healthz")
        assert status == 200 and health == {"ok": True, "database": "ok"}
        status, ready = running.request("GET", "/readyz")
        assert status == 503 and ready["ok"] is False
        assert running.request("GET", "/runs")[0] == 401
        status, payload = running.request("GET", "/runs", authorized=True)
        assert status == 200 and payload["runs"] == []
    finally:
        running.close()


def test_post_validates_auth_inputs_and_queue_capacity(tmp_path: Path) -> None:
    running = _RunningServer(tmp_path, max_pending=1)
    try:
        payload = {"source": SOURCE, "inputs": {"x": "1"}}
        assert running.request("POST", "/runs", payload=payload)[0] == 401
        store = RunStore(running.path)
        assert store.list_runs() == []
        store.close()

        status, created = running.request("POST", "/runs", payload=payload, authorized=True)
        assert status == 201 and created["status"] == "pending"
        assert running.request("POST", "/runs", payload=payload, authorized=True)[0] == 429
        bad = {"source": SOURCE, "inputs": {"x": 1}}
        assert running.request("POST", "/runs", payload=bad, authorized=True)[0] == 400
    finally:
        running.close()


def test_post_accepts_validated_ir_and_worker_executes_it(tmp_path: Path) -> None:
    running = _RunningServer(tmp_path)
    workflow = compile_program(parse_program(SOURCE))
    ir_object = json.loads(canonical_ir_bytes(workflow))
    try:
        status, created = running.request(
            "POST",
            "/runs",
            payload={"ir": ir_object, "inputs": {}},
            authorized=True,
        )
        assert status == 201 and created["status"] == "pending"

        store = RunStore(running.path)
        record = store.get_run(created["run_id"])
        assert record is not None and record.source is None
        assert record.definition_json is not None
        assert load_ir_bytes(record.definition_json.encode("utf-8")) == workflow

        durable = process_one(store, llm_client=DryRunClient())
        assert durable is not None and durable.run_id == created["run_id"]
        completed = store.get_run(created["run_id"])
        assert completed is not None and completed.status == "completed"
        store.close()
    finally:
        running.close()


def test_post_requires_exactly_one_workflow_representation(tmp_path: Path) -> None:
    running = _RunningServer(tmp_path)
    ir_object = json.loads(canonical_ir_bytes(compile_program(parse_program(SOURCE))))
    try:
        status, _ = running.request(
            "POST",
            "/runs",
            payload={"source": SOURCE, "ir": ir_object},
            authorized=True,
        )
        assert status == 400
        status, _ = running.request("POST", "/runs", payload={"inputs": {}}, authorized=True)
        assert status == 400
    finally:
        running.close()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"source": "\ud800", "inputs": {}}, "workflow text must be valid Unicode"),
        (
            {"source": SOURCE, "inputs": {"x": "\ud800"}},
            "input keys and values must be valid Unicode",
        ),
        (
            {"source": SOURCE, "inputs": {"\ud800": "x"}},
            "input keys and values must be valid Unicode",
        ),
        ({"ir": {"name": "\ud800"}, "inputs": {}}, "workflow text must be valid Unicode"),
    ],
)
def test_malformed_unicode_returns_400_and_server_survives(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    running = _RunningServer(tmp_path)
    try:
        status, response = running.request("POST", "/runs", payload=payload, authorized=True)
        assert status == 400
        assert response == {"error": message}
        assert running.request("GET", "/healthz") == (200, {"ok": True, "database": "ok"})
    finally:
        running.close()


@pytest.mark.parametrize(
    "body",
    [
        b'{"source":"thread T { context {} emit text { \\"ok\\" } }","inputs":{},"n":'
        + b"9" * 5000
        + b"}",
    ],
)
def test_json_parser_failures_return_400_and_server_survives(tmp_path: Path, body: bytes) -> None:
    running = _RunningServer(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
        conn.request(
            "POST",
            "/runs",
            body=body,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
        )
        response = conn.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "body must be valid JSON"}
        conn.close()
        assert running.request("GET", "/healthz") == (200, {"ok": True, "database": "ok"})
    finally:
        running.close()


def test_run_list_is_paginated_and_bounded(tmp_path: Path) -> None:
    running = _RunningServer(tmp_path)
    try:
        for index in range(3):
            status, _ = running.request(
                "POST",
                "/runs",
                payload={"source": SOURCE, "inputs": {"x": str(index)}},
                authorized=True,
            )
            assert status == 201
        status, payload = running.request("GET", "/runs?limit=2&offset=1", authorized=True)
        assert status == 200
        assert len(payload["runs"]) == 2
        assert payload["limit"] == 2 and payload["offset"] == 1
        assert running.request("GET", "/runs?limit=1001", authorized=True)[0] == 400
    finally:
        running.close()


def test_oversized_request_is_rejected_before_body_read(tmp_path: Path) -> None:
    running = _RunningServer(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", running.port, timeout=5)
        conn.putrequest("POST", "/runs")
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
        conn.endheaders()
        response = conn.getresponse()
        assert response.status == 413
        response.read()
        conn.close()
    finally:
        running.close()


def test_readiness_reflects_live_workers(tmp_path: Path) -> None:
    path = str(tmp_path / "workers.db")
    pool = WorkerPool(path, n_workers=1, llm_client=DryRunClient(), poll_interval=0.01)
    pool.start()
    running = _RunningServer(tmp_path, pool=pool)
    try:
        status, payload = running.request("GET", "/readyz")
        assert status == 200
        assert payload["ok"] is True
        assert payload["workers"]["alive"] == 1
    finally:
        running.close()
        pool.stop()


def test_readiness_returns_503_when_database_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = _RunningServer(tmp_path)

    class UnavailableStore:
        def __init__(self, path: str) -> None:
            raise OSError("database unavailable")

    try:
        monkeypatch.setattr(server_module, "RunStore", UnavailableStore)
        status, payload = running.request("GET", "/readyz")
        assert status == 503
        assert payload == {"ok": False, "database": "unavailable", "workers": None}
    finally:
        running.close()


def test_server_main_formats_bind_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_serve(*args: object, **kwargs: object) -> None:
        raise OSError("bind failed")

    monkeypatch.setattr(server_module, "serve", fail_serve)
    monkeypatch.setattr(sys, "argv", ["threadlang-serve", "--store", str(tmp_path / "runs.db")])
    assert server_module.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: bind failed\n"


@pytest.mark.parametrize("base_url", ["not-a-url", "http://[::1"])
def test_server_main_formats_provider_setup_errors(
    base_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "threadlang-serve",
            "--store",
            str(tmp_path / "runs.db"),
            "--backend",
            "openai",
            "--base-url",
            base_url,
        ],
    )
    assert server_module.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: OpenAI-compatible base URL")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_server_main_rejects_non_finite_timeout_before_provider_setup(
    timeout: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_calls = 0
    serve_calls = 0

    def unexpected_provider(*args: object, **kwargs: object) -> None:
        nonlocal provider_calls
        provider_calls += 1

    def unexpected_serve(*args: object, **kwargs: object) -> None:
        nonlocal serve_calls
        serve_calls += 1
        raise AssertionError("server should not start")

    monkeypatch.setattr("threadlang.llm.OpenAICompatClient", unexpected_provider)
    monkeypatch.setattr(server_module, "serve", unexpected_serve)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "threadlang-serve",
            "--store",
            str(tmp_path / "runs.db"),
            "--backend",
            "openai",
            f"--timeout={timeout}",
        ],
    )
    assert server_module.main() == 2
    assert provider_calls == 0
    assert serve_calls == 0
    assert "must be positive" in capsys.readouterr().err


def test_server_main_validates_bounds_before_provider_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "threadlang-serve",
            "--store",
            str(tmp_path / "runs.db"),
            "--backend",
            "openai",
            "--base-url",
            "not-a-url",
            "--max-tokens",
            "0",
        ],
    )
    assert server_module.main() == 2
    captured = capsys.readouterr()
    assert "must be positive" in captured.err
    assert "base URL" not in captured.err
