"""Runtime trace structures for ThreadLang."""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass(frozen=True)
class TraceEvent:
    phase: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)


Trace = List[TraceEvent]
