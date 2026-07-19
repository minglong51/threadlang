"""LLM client abstraction for ThreadLang.

Two backends:

- `AnthropicClient` — real Claude calls. Requires the `anthropic` optional
  dep (`pip install 'threadlang[anthropic]'`) and `ANTHROPIC_API_KEY` in env.
- `DryRunClient` — deterministic echo backend that returns the prompt as
  the response (prefixed). Used by tests and by `threadlang --dry-run`.

Plain `llm` steps use `.complete(model, prompt) -> str`. Agent steps (v0.3)
use `.agent_step(model, messages, tools) -> AgentTurn`, a richer shape that
carries tool definitions in and tool-call requests out. Both backends below
implement both protocols.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Protocol, Sequence

from .tools import ToolSpec


class LLMClient(Protocol):
    def complete(self, model: str, prompt: str) -> str: ...


# ───────── agent (tool-use) protocol ─────────


@dataclass(frozen=True)
class ToolCall:
    """A model's request to run a tool. `id` correlates the call with its
    result across turns."""

    id: str
    name: str
    arguments: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentTurn:
    """One model response inside an agent loop. `tool_calls` empty means the
    model is done and `text` is the final answer; non-empty means the runtime
    must run those tools and call back with the results."""

    text: str
    tool_calls: Sequence[ToolCall] = ()


# A normalized message is a dict the runtime owns; clients translate it to/from
# their provider format. Roles:
#   {"role": "user",      "content": str}
#   {"role": "assistant", "text": str, "tool_calls": Sequence[ToolCall]}
#   {"role": "tool",      "tool_call_id": str, "content": str}
Message = Dict[str, object]


class AgentLLMClient(Protocol):
    """A client that can run one turn of a tool-use loop. Separate from
    `LLMClient.complete` on purpose: plain `llm` steps don't need tools, and
    tool-use needs a richer request/response shape."""

    def agent_step(
        self, model: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AgentTurn: ...


class RouteLLMClient(Protocol):
    """Optional protocol for `route` steps. A client exposing `route` answers a
    routing prompt knowing the closed set of admissible labels; clients without
    it are called through plain `complete` (the prompt already carries the
    output contract). The runtime detects it with `getattr`, like
    `agent_step`."""

    def route(self, model: str, prompt: str, options: Sequence[str]) -> str: ...


class LLMError(RuntimeError):
    """Raised when an LLM call fails."""


def _placeholder_args(schema: Mapping[str, object]) -> Dict[str, object]:
    """Deterministically fill a tool's required arguments from its JSON schema.
    Used by the dry-run client so the agent loop is reproducible without a
    real model deciding arguments."""
    props = schema.get("properties", {}) if isinstance(schema, Mapping) else {}
    required = schema.get("required") if isinstance(schema, Mapping) else None
    keys: List[str] = list(required) if isinstance(required, list) else list(props)  # type: ignore[arg-type]
    out: Dict[str, object] = {}
    for key in keys:
        prop = props.get(key, {}) if isinstance(props, Mapping) else {}
        ptype = prop.get("type", "string") if isinstance(prop, Mapping) else "string"
        out[key] = 0 if ptype in ("number", "integer") else "dry-run"
    return out


class DryRunClient:
    """Returns a deterministic echo of the prompt. Used by tests so they
    don't need an API key or network, and by `threadlang --dry-run`."""

    def complete(self, model: str, prompt: str) -> str:
        return f"[dry-run:{model}] {prompt}"

    def route(self, model: str, prompt: str, options: Sequence[str]) -> str:
        """Deterministically pick the first arm label, so routed programs run
        end-to-end under --dry-run just like agent loops do."""
        return options[0] if options else ""

    def agent_step(
        self, model: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        """Deterministic two-phase loop: if tools are available and none has
        run yet, call the first tool with placeholder args; otherwise finalize,
        echoing the latest observation. No randomness, no network — the same
        program always produces the same trace."""
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if tools and not has_tool_result:
            spec = tools[0]
            return AgentTurn(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_0",
                        name=spec.name,
                        arguments=_placeholder_args(spec.parameters),
                    ),
                ),
            )
        observation = ""
        for message in reversed(messages):
            role = message.get("role")
            if role in ("tool", "user"):
                observation = str(message.get("content", ""))
                break
        return AgentTurn(text=f"[dry-run:{model}] {observation}", tool_calls=())


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

    def agent_step(
        self, model: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        kwargs: Dict[str, object] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "messages": _to_anthropic_messages(messages),
        }
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        resp = self._client.messages.create(**kwargs)  # type: ignore[arg-type]
        text_parts: List[str] = []
        calls: List[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)  # type: ignore[attr-defined]
            elif btype == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.id,  # type: ignore[attr-defined]
                        name=block.name,  # type: ignore[attr-defined]
                        arguments=dict(block.input),  # type: ignore[attr-defined]
                    )
                )
        return AgentTurn(text="".join(text_parts), tool_calls=tuple(calls))


def _to_anthropic_messages(messages: Sequence[Message]) -> List[Dict[str, object]]:
    """Translate the runtime's normalized messages into Anthropic's content-
    block format. Assistant tool calls become `tool_use` blocks; tool results
    become a `tool_result` block on a user message, keyed by call id."""
    out: List[Dict[str, object]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            out.append({"role": "user", "content": message.get("content", "")})
        elif role == "assistant":
            content: List[Dict[str, object]] = []
            text = message.get("text")
            if text:
                content.append({"type": "text", "text": text})
            for call in message.get("tool_calls", ()):  # type: ignore[union-attr]
                content.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                )
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id"),
                            "content": message.get("content", ""),
                        }
                    ],
                }
            )
    return out


DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


class OpenAICompatClient:
    """Any OpenAI-compatible `/v1/chat/completions` endpoint — DeepSeek, a
    local Ollama server, vLLM, Together, etc. — over plain stdlib HTTP, so it
    adds no dependency.

    This is the low-cost / open-weight path. Two ways to point it:

    - Hosted DeepSeek (default): set `THREADLANG_API_KEY` (or pass `api_key`);
      `base_url` defaults to DeepSeek. Models: `deepseek-chat`, `deepseek-reasoner`.
    - Free local Ollama: `base_url="http://<host>:11434/v1"`, no key needed.
      Models: whatever is pulled, e.g. `qwen2.5-coder:14b`.

    Both `complete` (plain `llm` steps) and `agent_step` (tool-use loop) are
    implemented; tool-calling uses the OpenAI `tools` / `tool_calls` shape,
    which DeepSeek and recent Ollama models support.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int = 1024,
        timeout: float = 120.0,
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("THREADLANG_BASE_URL") or DEEPSEEK_BASE_URL
        ).rstrip("/")
        # A key is optional: local servers (Ollama) ignore it. Hosted providers
        # 401 without one, which surfaces as a clear LLMError at call time.
        self._api_key = (
            api_key
            or os.environ.get("THREADLANG_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        self._max_tokens = max_tokens
        self._timeout = timeout

    def _post(self, payload: Dict[str, object]) -> Dict[str, object]:
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LLMError(f"HTTP {exc.code} from {self._base_url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"could not reach {self._base_url}: {exc.reason}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"non-JSON response from {self._base_url}: {body[:300]}") from exc

    def complete(self, model: str, prompt: str) -> str:
        resp = self._post(
            {
                "model": model,
                "max_tokens": self._max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        return _openai_message(resp).get("content") or ""

    def agent_step(
        self, model: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        payload: Dict[str, object] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "messages": _to_openai_messages(messages),
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
        message = _openai_message(self._post(payload))
        text = message.get("content") or ""
        calls: List[ToolCall] = []
        for raw in message.get("tool_calls") or ():
            fn = raw.get("function", {}) if isinstance(raw, Mapping) else {}
            raw_args = fn.get("arguments", "{}")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError):
                arguments = {}
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{len(calls)}"),
                    name=str(fn.get("name", "")),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            )
        return AgentTurn(text=str(text), tool_calls=tuple(calls))


def _openai_message(resp: Mapping[str, object]) -> Dict[str, object]:
    """Pull `choices[0].message` from a chat-completions response, defensively."""
    choices = resp.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMError(f"no choices in response: {json.dumps(resp)[:300]}")
    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    if not isinstance(message, Mapping):
        raise LLMError("malformed choice: no message object")
    return dict(message)


def _to_openai_messages(messages: Sequence[Message]) -> List[Dict[str, object]]:
    """Translate the runtime's normalized messages into OpenAI chat format.
    Assistant tool calls become a `tool_calls` array (arguments JSON-encoded);
    tool results become `role: tool` messages keyed by `tool_call_id`."""
    out: List[Dict[str, object]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            out.append({"role": "user", "content": message.get("content", "")})
        elif role == "assistant":
            entry: Dict[str, object] = {"role": "assistant", "content": message.get("text") or ""}
            tool_calls = message.get("tool_calls") or ()
            if tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments),
                        },
                    }
                    for call in tool_calls  # type: ignore[union-attr]
                ]
            out.append(entry)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id"),
                    "content": message.get("content", ""),
                }
            )
    return out


def default_client() -> LLMClient:
    """Used by CLI when --dry-run is not passed. Raises if the SDK or key
    are missing — call sites should catch and fall back to DryRunClient
    if they want a soft mode."""
    return AnthropicClient()
