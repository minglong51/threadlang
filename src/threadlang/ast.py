"""AST nodes for the ThreadLang v0.1 grammar."""

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Program:
    thread_name: str
    context: List["ContextAssign"]
    emits: List["Emit"]


@dataclass(frozen=True)
class ContextAssign:
    key: str
    value: str


@dataclass(frozen=True)
class Emit:
    target: str
    expression: "Expr"


class Expr:
    """Marker base class for expressions."""


@dataclass(frozen=True)
class StringLiteral(Expr):
    value: str


@dataclass(frozen=True)
class VariableRef(Expr):
    scope: str
    key: str


@dataclass(frozen=True)
class Concat(Expr):
    parts: List[Expr]
