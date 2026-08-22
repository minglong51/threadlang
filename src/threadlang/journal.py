"""Per-call LLM response journaling for durable runs.

Step checkpoints (store.py) bound crash recovery at step granularity: a crash
*inside* a step re-runs that whole step on resume, re-calling the provider for
every call the step had already completed. This module narrows that window.
`run_durable` wraps the run's LLM client in a `JournaledLLMClient`, which
records each model call — canonical request fingerprint plus response — in the
store's `llm_journal` table. On resume, a call whose fingerprint matches a
journaled row replays the recorded response instead of hitting the provider,
so a hard crash re-executes at most the single in-flight call of the
interrupted step. Tool calls are not journaled and re-execute; exactly-once
external effects remain impossible client-side and stay out of scope
(docs/production.md).

Crash semantics: a fresh run gets a fresh run_id, so its journal is empty and
every call goes live — the journal is write-only on a first attempt. Only a
resumed run (same run_id, re-executing an incomplete step) replays
fingerprint-matched responses.

The replay key is `(run_id, request_fingerprint, occurrence)`. The fingerprint
is SHA-256 over the canonical JSON of the full request — `kind` plus `model`
and the verb's arguments (prompt / options / messages+tools) — so distinct
requests never collide. `occurrence` is the per-attempt ordinal of that
fingerprint, so a run issuing the identical request twice keeps two rows, each
replaying its own response.

The wrapper is transparent to the runtime's duck-typing: it exposes `complete`
unconditionally and `route`/`agent_step` only when the wrapped client has them
(the runtime probes with `getattr`). It is created per run inside
`run_durable`, so its mutable per-attempt ordinal map is never shared across
worker threads — the shared-stateless-client contract the `WorkerPool` relies
on is untouched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Callable, Dict, List, Sequence, TypeVar, cast

from .llm import AgentTurn, LLMClient, Message, ToolCall
from .tools import ToolSpec

if TYPE_CHECKING:
    from .store import RunStore

_T = TypeVar("_T")


def _sha256_json(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _message_json(message: Message) -> Dict[str, object]:
    """JSON-safe rendering of a runtime-owned message: `ToolCall` dataclasses
    become plain dicts; every other key passes through. Deterministic under
    `sort_keys`, so a resumed attempt hashes an identical request identically."""
    rendered: Dict[str, object] = {}
    for key, value in message.items():
        if key == "tool_calls":
            rendered[key] = [asdict(call) for call in cast(Sequence[ToolCall], value)]
        else:
            rendered[key] = value
    return rendered


def _agent_turn_json(turn: AgentTurn) -> Dict[str, object]:
    return {"text": turn.text, "tool_calls": [asdict(call) for call in turn.tool_calls]}


def _agent_turn_from_json(payload: object) -> AgentTurn:
    """Rebuild an `AgentTurn` from its journaled payload. The journal is written
    only by `_agent_turn_json`; the isinstance checks keep a hand-edited store
    from crashing a resume rather than from being wrong."""
    data = cast(Dict[str, object], payload) if isinstance(payload, dict) else {}
    calls: List[ToolCall] = []
    for raw in cast(List[object], data.get("tool_calls", [])):
        if isinstance(raw, dict):
            calls.append(
                ToolCall(
                    id=str(raw.get("id", "")),
                    name=str(raw.get("name", "")),
                    arguments=cast(Dict[str, object], raw.get("arguments", {})),
                )
            )
    return AgentTurn(text=str(data.get("text", "")), tool_calls=tuple(calls))


class JournaledLLMClient:
    """A per-run journaling wrapper around an LLM client. Each call is looked
    up in the store's `llm_journal` table by (run_id, request fingerprint,
    occurrence); a hit replays the recorded response without a provider call,
    a miss calls through and persists the response."""

    # Assigned in __init__ only when the wrapped client exposes the verb, so
    # the runtime's `getattr(client, "route"/"agent_step", None)` probes see
    # exactly the wrapped client's capability surface.
    route: Callable[..., str]
    agent_step: Callable[..., AgentTurn]

    def __init__(self, client: LLMClient, store: RunStore, run_id: str) -> None:
        self._client = client
        self._store = store
        self._run_id = run_id
        self._occurrences: Dict[str, int] = {}
        if getattr(client, "route", None) is not None:
            self.route = self._route
        if getattr(client, "agent_step", None) is not None:
            self.agent_step = self._agent_step

    def complete(self, model: str, prompt: str) -> str:
        return self._call_journaled(
            {"kind": "complete", "model": model, "prompt": prompt},
            lambda: self._client.complete(model=model, prompt=prompt),
            lambda response: response,
            str,
        )

    def _route(self, model: str, prompt: str, options: Sequence[str]) -> str:
        route_fn = getattr(self._client, "route")  # present: see __init__
        return self._call_journaled(
            {"kind": "route", "model": model, "prompt": prompt, "options": list(options)},
            lambda: route_fn(model=model, prompt=prompt, options=list(options)),
            lambda response: response,
            str,
        )

    def _agent_step(
        self, model: str, messages: Sequence[Message], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        agent_fn = getattr(self._client, "agent_step")  # present: see __init__
        return self._call_journaled(
            {
                "kind": "agent_step",
                "model": model,
                "messages": [_message_json(message) for message in messages],
                "tools": [asdict(spec) for spec in tools],
            },
            lambda: agent_fn(model=model, messages=messages, tools=tools),
            _agent_turn_json,
            _agent_turn_from_json,
        )

    def _call_journaled(
        self,
        request: Dict[str, object],
        call: Callable[[], _T],
        encode: Callable[[_T], object],
        decode: Callable[[object], _T],
    ) -> _T:
        fingerprint = _sha256_json(request)
        occurrence = self._occurrences.get(fingerprint, 0)
        self._occurrences[fingerprint] = occurrence + 1
        stored = self._store.load_llm_journal_entry(self._run_id, fingerprint, occurrence)
        if stored is not None:
            return decode(json.loads(stored))
        response = call()
        self._store.save_llm_journal_entry(
            self._run_id,
            fingerprint,
            occurrence,
            json.dumps(request, sort_keys=True, separators=(",", ":")),
            json.dumps(encode(response), sort_keys=True, separators=(",", ":")),
        )
        return response
