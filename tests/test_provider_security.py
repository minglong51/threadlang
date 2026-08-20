"""Provider failures must not persist or expose upstream response bodies."""

from __future__ import annotations

import io
import json
from email.message import Message
from pathlib import Path
import sys
import urllib.error
import urllib.request

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.llm import LLMError, OpenAICompatClient  # noqa: E402
import threadlang.llm as llm_module  # noqa: E402


class _StaticOpenAI(OpenAICompatClient):
    def __init__(self, response: dict[str, object]) -> None:
        super().__init__(base_url="https://provider.invalid/v1")
        self.response = response

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        return self.response


def test_openai_http_error_body_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "fake-secret-that-must-not-be-persisted"
    endpoint_secret = "endpoint-secret-that-must-not-be-persisted"

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://provider.invalid/v1/chat/completions",
            500,
            "boom",
            Message(),
            io.BytesIO(secret.encode()),
        )

    class FailingOpener:
        def open(self, *args: object, **kwargs: object) -> io.BytesIO:
            return fail(*args, **kwargs)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: FailingOpener())
    client = OpenAICompatClient(
        base_url=f"https://provider.invalid/{endpoint_secret}", api_key="placeholder"
    )
    with pytest.raises(LLMError) as raised:
        client.complete("m", "hello")
    assert "HTTP 500" in str(raised.value)
    assert secret not in str(raised.value)
    assert endpoint_secret not in str(raised.value)


def test_openai_truncation_and_empty_text_fail_closed() -> None:
    truncated = _StaticOpenAI(
        {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
    )
    with pytest.raises(LLMError, match="truncated"):
        truncated.complete("m", "hello")

    empty = _StaticOpenAI({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
    with pytest.raises(LLMError, match="no text"):
        empty.complete("m", "hello")


@pytest.mark.parametrize(
    "arguments",
    ["{", "[]", None, {"value": float("nan")}, {"value": "\ud800"}],
)
def test_openai_malformed_tool_arguments_fail_closed(arguments: object) -> None:
    client = _StaticOpenAI(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "record", "arguments": arguments},
                            }
                        ],
                    },
                }
            ]
        }
    )

    with pytest.raises(LLMError, match="malformed tool-call arguments"):
        client.agent_step("m", [], [])


def test_openai_provider_response_size_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    read_sizes: list[int] = []

    class OversizedResponse(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return super().read(size)

    class OversizedOpener:
        def open(self, *args: object, **kwargs: object) -> io.BytesIO:
            return OversizedResponse(b"x" * 17)

    monkeypatch.setattr(llm_module, "MAX_PROVIDER_RESPONSE_BYTES", 16)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: OversizedOpener())

    with pytest.raises(LLMError, match="provider response exceeds 16 byte limit"):
        OpenAICompatClient(base_url="https://provider.invalid/v1").complete("m", "hello")
    assert read_sizes == [17]


@pytest.mark.parametrize("content", ["\ud800", {"unexpected": "object"}])
def test_openai_invalid_agent_text_fails_closed(content: object) -> None:
    client = _StaticOpenAI(
        {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    )

    with pytest.raises(LLMError, match="invalid Unicode|invalid text"):
        client.agent_step("m", [], [])


@pytest.mark.parametrize("message", [{"content": "done"}, {"content": "done", "tool_calls": None}])
def test_openai_content_only_agent_turn_accepts_missing_tool_calls(
    message: dict[str, object],
) -> None:
    client = _StaticOpenAI({"choices": [{"finish_reason": "stop", "message": message}]})

    assert client.agent_step("m", [], []).text == "done"


def test_openai_valid_tool_call_is_preserved() -> None:
    client = _StaticOpenAI(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "record",
                                    "arguments": '{"value":"ok"}',
                                },
                            }
                        ],
                    },
                }
            ]
        }
    )

    turn = client.agent_step("m", [], [])
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].id == "call_1"
    assert turn.tool_calls[0].name == "record"
    assert turn.tool_calls[0].arguments == {"value": "ok"}


@pytest.mark.parametrize(
    ("base_url", "threadlang_key", "expected_authorization"),
    [
        ("https://api.openai.com/v1", None, "Bearer ambient-openai-key"),
        ("http://api.openai.com/v1", None, None),
        ("https://api.openai.com.evil.invalid/v1", None, None),
        ("https://provider.invalid/v1", None, None),
        ("https://provider.invalid/v1", "generic-provider-key", "Bearer generic-provider-key"),
        ("http://127.0.0.1:11434/v1", "local-provider-key", "Bearer local-provider-key"),
        ("http://localhost:11434/v1", "local-provider-key", "Bearer local-provider-key"),
        ("http://[::1]:11434/v1", "local-provider-key", "Bearer local-provider-key"),
    ],
)
def test_openai_api_key_is_scoped_to_official_https_host(
    base_url: str,
    threadlang_key: str | None,
    expected_authorization: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[urllib.request.Request] = []
    configured_handlers: list[object] = []
    response = json.dumps(
        {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}
    ).encode()

    def capture(request: urllib.request.Request, **kwargs: object) -> io.BytesIO:
        requests.append(request)
        return io.BytesIO(response)

    class CapturingOpener:
        def open(self, request: urllib.request.Request, **kwargs: object) -> io.BytesIO:
            return capture(request, **kwargs)

    def capture_opener(*handlers: object) -> CapturingOpener:
        configured_handlers.extend(handlers)
        return CapturingOpener()

    monkeypatch.setenv("OPENAI_API_KEY", "ambient-openai-key")
    if threadlang_key is None:
        monkeypatch.delenv("THREADLANG_API_KEY", raising=False)
    else:
        monkeypatch.setenv("THREADLANG_API_KEY", threadlang_key)
    monkeypatch.setattr(urllib.request, "build_opener", capture_opener)

    assert OpenAICompatClient(base_url=base_url).complete("m", "hello") == "ok"
    original = requests[0]
    assert original.get_header("Authorization") == expected_authorization
    redirect_handlers = [
        handler
        for handler in configured_handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]
    assert len(redirect_handlers) == 1
    assert (
        redirect_handlers[0].redirect_request(
            original,
            None,
            302,
            "Found",
            Message(),
            "https://redirect.invalid/chat/completions",
        )
        is None
    )
    proxy_handlers = [
        handler
        for handler in configured_handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    if base_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        assert len(proxy_handlers) == 1
        assert proxy_handlers[0].proxies == {}
    else:
        assert proxy_handlers == []


@pytest.mark.parametrize("key_source", ["explicit", "environment"])
def test_api_key_is_rejected_for_remote_http_endpoint(
    key_source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("THREADLANG_API_KEY", raising=False)
    kwargs: dict[str, str] = {}
    if key_source == "explicit":
        kwargs["api_key"] = "secret"
    else:
        monkeypatch.setenv("THREADLANG_API_KEY", "secret")

    with pytest.raises(LLMError, match="non-HTTPS, non-loopback"):
        OpenAICompatClient(base_url="http://provider.invalid/v1", **kwargs)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://user:secret@provider.invalid/v1",
        "https://provider.invalid/v1?api_key=secret",
        "https://provider.invalid/v1#secret",
    ],
)
def test_openai_rejects_credential_bearing_base_urls(base_url: str) -> None:
    with pytest.raises(LLMError, match="credentials|query or fragment"):
        OpenAICompatClient(base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://provider.invalid:notaport/v1",
        "https://provider.invalid:70000/v1",
        "https://bad host.invalid/v1",
    ],
)
def test_openai_rejects_malformed_base_urls(base_url: str) -> None:
    with pytest.raises(LLMError, match="base URL must|provider endpoint"):
        OpenAICompatClient(base_url=base_url)
