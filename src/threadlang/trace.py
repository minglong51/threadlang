"""Trace event structures used by parser/runtime flows."""

from dataclasses import dataclass, field
from typing import Any, Dict


@dataclass(frozen=True)
class TraceEvent:
    name: str
    data: Dict[str, Any] = field(default_factory=dict)
