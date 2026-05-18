"""LLM client abstraction for ThreadLang.

Two backends:

- `AnthropicClient` — real Claude calls. Requires the `anthropic` optional
  dep (`pip install 'threadlang[anthropic]'`) and `ANTHROPIC_API_KEY` in env.
- `DryRunClient` — deterministic echo backend that returns the prompt as
  the response (prefixed). Used by tests and by `threadlang --dry-run`.

The runtime accepts any object with `.complete(model, prompt) -> str`.
Keeping the protocol that tight on purpose: a DSL doesn't need streaming,
tool use, or system prompts to demonstrate the workflow shape. Add those
when an actual use case earns them.
"""

from __future__ import annotations

import os
from typing import Protocol


class LLMClient(Protocol):
    def complete(self, model: str, prompt: str) -> str: ...


class LLMError(RuntimeError):
    """Raised when an LLM call fails."""


class DryRunClient:
    """Returns a deterministic echo of the prompt. Used by tests so they
    don't need an API key or network, and by `threadlang --dry-run`."""

    def complete(self, model: str, prompt: str) -> str:
        return f"[dry-run:{model}] {prompt}"


class AnthropicClient:
    """Real Claude calls via the official Anthropic SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        max_tokens: int = 1024,
    ) -> None:
        try:
            from anthropic import Anthropic  # type: ignore
        except ImportError as exc:
            raise LLMError(
                "anthropic SDK not installed. Install with: pip install 'threadlang[anthropic]'"
            ) from exc

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError(
                "ANTHROPIC_API_KEY not set in environment and no api_key passed."
            )
        self._client = Anthropic(api_key=key)
        self._max_tokens = max_tokens

    def complete(self, model: str, prompt: str) -> str:
        resp = self._client.messages.create(
            model=model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        # Take the first text block; ignore tool-use / other types for v1.
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                return block.text  # type: ignore[attr-defined]
        return ""


def default_client() -> LLMClient:
    """Used by CLI when --dry-run is not passed. Raises if the SDK or key
    are missing — call sites should catch and fall back to DryRunClient
    if they want a soft mode."""
    return AnthropicClient()
