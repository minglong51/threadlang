"""Deterministic parser for the ThreadLang v0.1 subset."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

from .ast import Concat, ContextAssign, Emit, Program, StringLiteral, VariableRef


class ParseError(ValueError):
    """Raised when source text does not match the v0.1 grammar."""


_TOKEN_RE = re.compile(
    r'\s*(?:(?P<STRING>"[^"\\]*(?:\\.[^"\\]*)*")|'
    r'(?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)|'
    r'(?P<SYMBOL>[{}.=+]))',
)


@dataclass
class Token:
    kind: str
    value: str


class TokenStream:
    def __init__(self, source: str) -> None:
        self.tokens = self._tokenize(source)
        self.pos = 0

    def _tokenize(self, source: str) -> List[Token]:
        tokens: List[Token] = []
        idx = 0
        while idx < len(source):
            match = _TOKEN_RE.match(source, idx)
            if not match:
                if source[idx:].strip() == "":
                    break
                snippet = source[idx : idx + 20]
                raise ParseError(f"Unexpected token near: {snippet!r}")
            idx = match.end()
            kind = ""
            value = ""
            for cand in ("STRING", "IDENT", "SYMBOL"):
                group = match.group(cand)
                if group is not None:
                    kind = cand
                    value = group
                    break
            if kind:
                tokens.append(Token(kind, value))
        return tokens

    def peek(self) -> Token | None:
        if self.pos >= len(self.tokens):
            return None
        return self.tokens[self.pos]

    def expect_symbol(self, symbol: str) -> None:
        token = self._next()
        if token.kind != "SYMBOL" or token.value != symbol:
            raise ParseError(f"Expected symbol {symbol!r}, got {token.value!r}")

    def expect_ident(self, value: str | None = None) -> str:
        token = self._next()
        if token.kind != "IDENT":
            raise ParseError(f"Expected identifier, got {token.value!r}")
        if value is not None and token.value != value:
            raise ParseError(f"Expected identifier {value!r}, got {token.value!r}")
        return token.value

    def expect_string(self) -> str:
        token = self._next()
        if token.kind != "STRING":
            raise ParseError(f"Expected string literal, got {token.value!r}")
        raw = token.value[1:-1]
        return bytes(raw, "utf-8").decode("unicode_escape")

    def _next(self) -> Token:
        token = self.peek()
        if token is None:
            raise ParseError("Unexpected end of input")
        self.pos += 1
        return token


def parse_program(source: str) -> Program:
    stream = TokenStream(source)
    stream.expect_ident("thread")
    thread_name = stream.expect_ident()
    stream.expect_symbol("{")

    context_items: List[ContextAssign] = []
    emits: List[Emit] = []

    while True:
        token = stream.peek()
        if token is None:
            raise ParseError("Unclosed thread block")
        if token.kind == "SYMBOL" and token.value == "}":
            stream.expect_symbol("}")
            break

        keyword = stream.expect_ident()
        if keyword == "context":
            context_items.extend(_parse_context_block(stream))
        elif keyword == "emit":
            emits.append(_parse_emit_block(stream))
        elif keyword in {"inputs", "rules", "steps"}:
            _parse_placeholder_block(stream)
        else:
            raise ParseError(f"Unsupported block type: {keyword!r}")

    if stream.peek() is not None:
        raise ParseError("Unexpected tokens after thread block")

    return Program(thread_name=thread_name, context=context_items, emits=emits)


def _parse_context_block(stream: TokenStream) -> List[ContextAssign]:
    stream.expect_symbol("{")
    assigns: List[ContextAssign] = []
    while True:
        token = stream.peek()
        if token is None:
            raise ParseError("Unclosed context block")
        if token.kind == "SYMBOL" and token.value == "}":
            stream.expect_symbol("}")
            return assigns
        key = stream.expect_ident()
        stream.expect_symbol("=")
        value = stream.expect_string()
        assigns.append(ContextAssign(key=key, value=value))


def _parse_emit_block(stream: TokenStream) -> Emit:
    target = stream.expect_ident()
    stream.expect_symbol("{")
    expr = _parse_expression(stream)
    stream.expect_symbol("}")
    return Emit(target=target, expression=expr)


def _parse_placeholder_block(stream: TokenStream) -> None:
    stream.expect_symbol("{")
    depth = 1
    while depth:
        token = stream._next()
        if token.kind == "SYMBOL" and token.value == "{":
            depth += 1
        elif token.kind == "SYMBOL" and token.value == "}":
            depth -= 1


def _parse_expression(stream: TokenStream):
    parts = [_parse_term(stream)]
    while True:
        token = stream.peek()
        if token and token.kind == "SYMBOL" and token.value == "+":
            stream.expect_symbol("+")
            parts.append(_parse_term(stream))
        else:
            break
    if len(parts) == 1:
        return parts[0]
    return Concat(parts=parts)


def _parse_term(stream: TokenStream):
    token = stream.peek()
    if token is None:
        raise ParseError("Expected expression term, found end of input")

    if token.kind == "STRING":
        return StringLiteral(stream.expect_string())

    scope = stream.expect_ident()
    if scope not in {"context", "inputs"}:
        raise ParseError(f"Unsupported variable scope: {scope!r}")
    stream.expect_symbol(".")
    key = stream.expect_ident()
    return VariableRef(scope=scope, key=key)
