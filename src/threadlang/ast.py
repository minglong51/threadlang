"""AST node definitions for ThreadLang."""

from dataclasses import dataclass
from typing import List, Union


@dataclass(frozen=True)
class Program:
    thread_name: str
    context: "ContextBlock"
    emit: "EmitBlock"


@dataclass(frozen=True)
class ContextAssignment:
    name: str
    value: str


@dataclass(frozen=True)
class ContextBlock:
    assignments: List[ContextAssignment]


@dataclass(frozen=True)
class StringLiteral:
    value: str


@dataclass(frozen=True)
class ContextRef:
    name: str


@dataclass(frozen=True)
class InputsRef:
    name: str


ExpressionTerm = Union[StringLiteral, ContextRef, InputsRef]


@dataclass(frozen=True)
class Expression:
    terms: List[ExpressionTerm]


@dataclass(frozen=True)
class EmitBlock:
    kind: str
    expression: Expression
