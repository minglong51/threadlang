"""The support-triage app's own tools — the part a real product brings to the
platform.

The core ships two deterministic built-ins (`echo`, `calculator`) so the agent
loop is demonstrable. A real app registers domain tools instead. This module is
exactly that: two tools over the bundled KB, plus `build_registry()` which
returns the default registry *extended* with them. The agent step references
them by name (`tools [ classify_priority, search_kb ]`); the registry is the
only thing that can turn those names into code — the same allow-list boundary
the core defines, now carrying app logic.

Both tools are pure functions of their arguments (the KB is in-process,
read-only), so the whole app runs end to end under `--dry-run` with no network
or key — and deterministically, which is what makes it golden-testable.
"""

from __future__ import annotations

import re
from typing import List, Mapping

from ...tools import FunctionTool, ToolRegistry, ToolSpec, default_registry
from .kb import ARTICLES, Article

# Words that signal a sev-0 (urgent, service-impacting) ticket vs. a sev-1
# (functional problem) — rule-based on purpose: deterministic and inspectable,
# no model call needed to classify. Everything else is sev-2.
_P0_SIGNALS = {
    "outage", "down", "offline", "unreachable", "500", "503", "critical",
    "urgent", "asap", "emergency", "data loss", "cannot access", "can't access",
    "locked out", "breach", "security",
}
_P1_SIGNALS = {
    "error", "failed", "failing", "broken", "bug", "billing", "refund", "charge",
    "overcharged", "429", "rate limit", "cannot log in", "can't log in", "reset",
}

_PRIORITY_LABELS = {
    "P0": "P0 — urgent, service-impacting",
    "P1": "P1 — functional problem, not service-wide",
    "P2": "P2 — question or minor issue",
}


def _classify(text: str) -> str:
    low = text.lower()
    if any(sig in low for sig in _P0_SIGNALS):
        return "P0"
    if any(sig in low for sig in _P1_SIGNALS):
        return "P1"
    return "P2"


def _classify_priority(args: Mapping[str, object]) -> str:
    text = str(args.get("text", "")).strip()
    if not text:
        return "error: empty ticket text"
    code = _classify(text)
    return f"{code}: {_PRIORITY_LABELS[code]}"


_WORD = re.compile(r"[a-z0-9]+")


def _score(query_tokens: set, article: Article) -> int:
    haystack = " ".join([article.title.lower(), article.body.lower(), *article.tags])
    hay_tokens = set(_WORD.findall(haystack))
    tag_tokens = {t.lower() for t in article.tags}
    # Tag hits weigh double — they are the curated keywords for the article.
    return len(query_tokens & hay_tokens) + len(query_tokens & tag_tokens)


def _search_kb(args: Mapping[str, object]) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return "error: empty query"
    query_tokens = set(_WORD.findall(query.lower()))
    scored = [(s, a) for a in ARTICLES if (s := _score(query_tokens, a)) > 0]
    scored.sort(key=lambda sa: (-sa[0], sa[1].id))
    if not scored:
        return "no matching articles"
    lines: List[str] = []
    for _, a in scored[:2]:
        lines.append(f"{a.id} — {a.title}: {a.body}")
    return "\n".join(lines)


_CLASSIFY_PRIORITY = FunctionTool(
    spec=ToolSpec(
        name="classify_priority",
        description=(
            "Classify a support ticket's priority (P0/P1/P2) from its text using "
            "deterministic keyword rules."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The ticket's full text."}
            },
            "required": ["text"],
        },
    ),
    _fn=_classify_priority,
)

_SEARCH_KB = FunctionTool(
    spec=ToolSpec(
        name="search_kb",
        description=(
            "Search the support knowledge base for articles relevant to a query; "
            "returns the top matches as 'id — title: body', or 'no matching articles'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords describing the customer's problem.",
                }
            },
            "required": ["query"],
        },
    ),
    _fn=_search_kb,
)


def build_registry() -> ToolRegistry:
    """The default built-ins extended with this app's domain tools — the
    registry the triage program's agent step draws from."""
    registry = default_registry()
    registry.register(_CLASSIFY_PRIORITY)
    registry.register(_SEARCH_KB)
    return registry
