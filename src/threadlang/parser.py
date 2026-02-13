"""Deterministic parser for the ThreadLang v0 prototype."""

from __future__ import annotations

import re
from typing import List

from .ast import (
    ContextAssignment,
    ContextBlock,
    ContextRef,
    EmitBlock,
    Expression,
    InputsRef,
    Program,
    StringLiteral,
)


class ParseError(ValueError):
    """Raised when source text does not match the supported ThreadLang syntax."""


def parse_program(source: str) -> Program:
    match = re.fullmatch(
        r"\s*thread\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{(.*)\}\s*",
        source,
        flags=re.DOTALL,
    )
    if not match:
        raise ParseError("Expected: thread <Name> { ... }")

    thread_name = match.group(1)
    body = match.group(2)

    context_block = _parse_context_block(body)
    emit_block = _parse_emit_block(body)

    return Program(thread_name=thread_name, context=context_block, emit=emit_block)


def _parse_context_block(body: str) -> ContextBlock:
    match = re.search(r"context\s*\{(.*?)\}", body, flags=re.DOTALL)
    if not match:
        raise ParseError("Missing context block")

    assignments: List[ContextAssignment] = []
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        assign_match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"', stripped)
        if not assign_match:
            raise ParseError(f"Invalid context assignment: {stripped}")
        assignments.append(
            ContextAssignment(name=assign_match.group(1), value=assign_match.group(2))
        )

    return ContextBlock(assignments=assignments)


def _parse_emit_block(body: str) -> EmitBlock:
    match = re.search(r"emit\s+text\s*\{(.*?)\}", body, flags=re.DOTALL)
    if not match:
        raise ParseError("Missing emit text block")

    expression_text = " ".join(line.strip() for line in match.group(1).splitlines()).strip()
    if not expression_text:
        raise ParseError("emit text block must include an expression")

    return EmitBlock(kind="text", expression=_parse_expression(expression_text))


def _parse_expression(expression_text: str) -> Expression:
    raw_terms = [term.strip() for term in expression_text.split("+")]
    if not raw_terms:
        raise ParseError("Expression is empty")

    parsed_terms = []
    for term in raw_terms:
        if re.fullmatch(r'"[^"]*"', term):
            parsed_terms.append(StringLiteral(value=term[1:-1]))
        elif re.fullmatch(r"context\.[A-Za-z_][A-Za-z0-9_]*", term):
            parsed_terms.append(ContextRef(name=term.split(".", 1)[1]))
        elif re.fullmatch(r"inputs\.[A-Za-z_][A-Za-z0-9_]*", term):
            parsed_terms.append(InputsRef(name=term.split(".", 1)[1]))
        else:
            raise ParseError(f"Unsupported expression term: {term}")

    return Expression(terms=parsed_terms)
