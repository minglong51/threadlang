"""Provider failures must not persist or expose upstream response bodies."""

from __future__ import annotations

import io
from email.message import Message
from pathlib import Path
import sys
import urllib.error

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.llm import LLMError, OpenAICompatClient  # noqa: E402


class _StaticOpenAI(OpenAICompatClient):
    def __init__(self, response: dict[str, object]) -> None:
        super().__init__(base_url="https://provider.invalid/v1")
        self.response = response

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        return self.response


def test_openai_http_error_body_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "fake-secret-that-must-not-be-persisted"

    def fail(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://provider.invalid/v1/chat/completions",
            500,
            "boom",
            Message(),
            io.BytesIO(secret.encode()),
        )

    monkeypatch.setattr("urllib.request.urlopen", fail)
    client = OpenAICompatClient(base_url="https://provider.invalid/v1", api_key="placeholder")
    with pytest.raises(LLMError) as raised:
        client.complete("m", "hello")
    assert "HTTP 500" in str(raised.value)
    assert secret not in str(raised.value)


def test_openai_truncation_and_empty_text_fail_closed() -> None:
    truncated = _StaticOpenAI(
        {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
    )
    with pytest.raises(LLMError, match="truncated"):
        truncated.complete("m", "hello")

    empty = _StaticOpenAI({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
    with pytest.raises(LLMError, match="no text"):
        empty.complete("m", "hello")
