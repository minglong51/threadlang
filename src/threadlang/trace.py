"""Runtime trace structures for ThreadLang."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List


class DenialCode(str, Enum):
    TOOL_NOT_ALLOWED = "tool-not-allowed"
    TOOL_NOT_REGISTERED = "tool-not-registered"


@dataclass(frozen=True)
class TraceEvent:
    phase: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)


Trace = List[TraceEvent]
