"""A tiny bundled knowledge base for the support-triage app.

Deliberately in-process and side-effect-free: the vertical slice is about
proving the *platform* end to end, not about a real datastore. An `Article` is
id + title + body + tags; `ARTICLES` is the whole KB. The app's `search_kb`
tool scores a query against these. Swapping this for a real vector store or DB
is a tool-implementation detail — the program, runtime, store, control plane,
and dashboard above it do not change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    body: str
    tags: List[str] = field(default_factory=list)


ARTICLES: List[Article] = [
    Article(
        id="kb-001",
        title="Reset your password",
        body=(
            "If you cannot log in, use the 'Forgot password' link on the sign-in "
            "page to receive a reset email. Reset links expire after one hour."
        ),
        tags=["password", "login", "reset", "access", "signin", "account"],
    ),
    Article(
        id="kb-002",
        title="Billing, charges, and refunds",
        body=(
            "Invoices are issued monthly. A charge can be refunded within 14 days "
            "from Settings → Billing. Disputed charges are reviewed within 2 "
            "business days."
        ),
        tags=["billing", "refund", "charge", "invoice", "payment", "subscription"],
    ),
    Article(
        id="kb-003",
        title="API rate limits and 429s",
        body=(
            "The API allows 600 requests/minute per key. Exceeding it returns HTTP "
            "429; back off and retry with exponential delay. Higher limits are "
            "available on the Pro plan."
        ),
        tags=["api", "rate", "limit", "429", "throttle", "requests"],
    ),
    Article(
        id="kb-004",
        title="Service outages and 5xx errors",
        body=(
            "If the app is unreachable or returns 500/503 errors, check the status "
            "page for active incidents. Most incidents are resolved within 30 "
            "minutes; subscribe to status updates for notifications."
        ),
        tags=["outage", "down", "500", "503", "error", "unavailable", "incident"],
    ),
]
