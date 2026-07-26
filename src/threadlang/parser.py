"""Position-aware recursive-descent parser for ThreadLang v0.12.

Grammar (informal)::

    program     = "thread" name "{" context [ steps ] emit "}"
    context     = "context" "{" { name "=" string } "}"
    steps       = "steps" "{" { step } "}"
    step        = "step" name "{" ( llm_body | agent_body | route_body ) "}"
    llm_body    = "llm" string "{" expression [ expect ] [ then ] "}"
    expect      = "expect" "{" rule { rule } "}"
    rule        = "one_of" string { "," string } | "matches" string
                | "max_chars" number | "nonempty"
    agent_body  = "agent" string "{" [ tools ] [ max_iters ] expression [ then ] "}"
    tools       = "tools" "[" [ name { "," name } ] "]"
    max_iters   = "max_iters" number
    route_body  = "route" string "{" expression arm { arm } [ else ] "}"
    arm         = "on" string "->" ( name | "end" )
    else        = "else" "->" ( name | "end" )
    then        = "then" "->" ( name | "end" )
    emit        = "emit" ( "text" | "llm" string ) "{" expression "}"
    expression  = term { "+" term }
    term        = string | "context." name | "inputs." name
                | "steps." name ".output" [ "?" ]

Strings may contain braces, plus signs, comments, and directive words without
changing structure. ``#`` and ``//`` comments are recognized outside strings.
All source must be consumed; unknown syntax is never silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .ast import (
    END_TARGET,
    AgentStep,
    ContextAssignment,
    ContextBlock,
    ContextRef,
    EmitBlock,
    ExpectRule,
    Expression,
    InputsRef,
    Program,
    RouteArm,
    RouteStep,
    Step,
    StepNode,
    StepsBlock,
    StepsRef,
    StringLiteral,
)
from .policy import (
    MAX_AGENT_ITERS,
    MAX_REGEX_PATTERN_CHARS,
    MAX_SOURCE_BYTES,
    MAX_STRING_CHARS,
)

DEFAULT_MAX_ITERS = 6


class ParseError(ValueError):
    """Raised when source does not match the supported ThreadLang grammar."""


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    line: int
    column: int

    @property
    def where(self) -> str:
        return f"line {self.line}, column {self.column}"


_PUNCTUATION = {
    "{": "LBRACE",
    "}": "RBRACE",
    "[": "LBRACKET",
    "]": "RBRACKET",
    "(": "LPAREN",
    ")": "RPAREN",
    "+": "PLUS",
    "=": "EQUAL",
    ",": "COMMA",
    ".": "DOT",
    "?": "QUESTION",
}


def _lex(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    i = 0
    line = 1
    column = 1

    def advance(text: str) -> None:
        nonlocal line, column
        newlines = text.count("\n")
        if newlines:
            line += newlines
            column = len(text.rsplit("\n", 1)[-1]) + 1
        else:
            column += len(text)

    while i < len(source):
        char = source[i]
        if char.isspace():
            advance(char)
            i += 1
            continue
        if char == "#" or source.startswith("//", i):
            end = source.find("\n", i)
            if end == -1:
                advance(source[i:])
                i = len(source)
            else:
                advance(source[i:end])
                i = end
            continue

        token_line, token_column = line, column
        if source.startswith("->", i):
            tokens.append(_Token("ARROW", "->", token_line, token_column))
            advance("->")
            i += 2
            continue
        if char in _PUNCTUATION:
            tokens.append(_Token(_PUNCTUATION[char], char, token_line, token_column))
            advance(char)
            i += 1
            continue
        if char == '"':
            i += 1
            advance('"')
            value: list[str] = []
            while i < len(source) and source[i] != '"':
                current = source[i]
                if current == "\n":
                    raise ParseError(
                        f"Unterminated string at line {token_line}, column {token_column}"
                    )
                if current == "\\":
                    if i + 1 >= len(source):
                        raise ParseError(
                            f"Unterminated escape at line {token_line}, column {token_column}"
                        )
                    escaped = source[i + 1]
                    decoded = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
                    # Preserve unknown escapes (notably regex \d, \s, \b) so
                    # regex contracts do not require double escaping twice.
                    value.append(decoded.get(escaped, "\\" + escaped))
                    advance(source[i : i + 2])
                    i += 2
                else:
                    value.append(current)
                    advance(current)
                    i += 1
                if len(value) > MAX_STRING_CHARS:
                    raise ParseError(
                        f"String literal exceeds {MAX_STRING_CHARS} characters at "
                        f"line {token_line}, column {token_column}"
                    )
            if i >= len(source):
                raise ParseError(f"Unterminated string at line {token_line}, column {token_column}")
            advance('"')
            i += 1
            tokens.append(_Token("STRING", "".join(value), token_line, token_column))
            continue
        if char.isdigit():
            start = i
            while i < len(source) and source[i].isdigit():
                i += 1
            text = source[start:i]
            tokens.append(_Token("INTEGER", text, token_line, token_column))
            advance(text)
            continue
        if char.isalpha() or char == "_":
            start = i
            while i < len(source) and (source[i].isalnum() or source[i] == "_"):
                i += 1
            text = source[start:i]
            tokens.append(_Token("IDENT", text, token_line, token_column))
            advance(text)
            continue
        raise ParseError(f"Unexpected character {char!r} at line {line}, column {column}")

    tokens.append(_Token("EOF", "", line, column))
    return tokens


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self.tokens = tokens
        self.position = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.position]

    def _advance(self) -> _Token:
        token = self.current
        if token.kind != "EOF":
            self.position += 1
        return token

    def _error(self, message: str, token: _Token | None = None) -> ParseError:
        point = token or self.current
        return ParseError(f"{message} at {point.where}")

    def _accept_kind(self, kind: str) -> _Token | None:
        if self.current.kind == kind:
            return self._advance()
        return None

    def _expect_kind(self, kind: str, description: str) -> _Token:
        token = self._accept_kind(kind)
        if token is None:
            raise self._error(f"Expected {description}; found {self.current.value!r}")
        return token

    def _at_word(self, word: str) -> bool:
        return self.current.kind == "IDENT" and self.current.value == word

    def _accept_word(self, word: str) -> _Token | None:
        if self._at_word(word):
            return self._advance()
        return None

    def _expect_word(self, word: str) -> _Token:
        token = self._accept_word(word)
        if token is None:
            raise self._error(f"Expected {word!r}; found {self.current.value!r}")
        return token

    def _expect_name(self, description: str = "identifier") -> str:
        return self._expect_kind("IDENT", description).value

    def parse(self) -> Program:
        self._expect_word("thread")
        thread_name = self._expect_name("thread name")
        self._expect_kind("LBRACE", "'{' after thread name")
        context = self._parse_context()
        steps = self._parse_steps() if self._at_word("steps") else StepsBlock(steps=[])
        emit = self._parse_emit()
        self._expect_kind("RBRACE", "'}' after thread body")
        self._expect_kind("EOF", "end of source")
        _validate_program(context, steps, emit)
        return Program(thread_name=thread_name, context=context, steps=steps, emit=emit)

    def _parse_context(self) -> ContextBlock:
        self._expect_word("context")
        self._expect_kind("LBRACE", "'{' after context")
        assignments: list[ContextAssignment] = []
        seen: set[str] = set()
        while self.current.kind != "RBRACE":
            name = self._expect_name("context assignment name")
            if name in seen:
                raise self._error(f"Duplicate context assignment: {name}")
            seen.add(name)
            self._expect_kind("EQUAL", "'=' in context assignment")
            value = self._expect_kind("STRING", "string context value").value
            assignments.append(ContextAssignment(name=name, value=value))
        self._advance()
        return ContextBlock(assignments=assignments)

    def _parse_steps(self) -> StepsBlock:
        self._expect_word("steps")
        self._expect_kind("LBRACE", "'{' after steps")
        steps: list[StepNode] = []
        seen: set[str] = set()
        while self.current.kind != "RBRACE":
            if not self._at_word("step"):
                raise self._error("Expected 'step' declaration")
            self._advance()
            name = self._expect_name("step name")
            if name == END_TARGET:
                raise self._error("'end' is reserved and cannot be a step name")
            if name in seen:
                raise self._error(f"Duplicate step name: {name}")
            seen.add(name)
            self._expect_kind("LBRACE", "'{' after step name")
            steps.append(self._parse_step_body(name))
            self._expect_kind("RBRACE", "'}' after step declaration")
        self._advance()
        _validate_targets(steps)
        return StepsBlock(steps=steps)

    def _parse_step_body(self, name: str) -> StepNode:
        if self._accept_word("llm"):
            model = self._expect_kind("STRING", "quoted model name").value
            return self._parse_llm(name, model)
        if self._accept_word("agent"):
            model = self._expect_kind("STRING", "quoted model name").value
            return self._parse_agent(name, model)
        if self._accept_word("route"):
            model = self._expect_kind("STRING", "quoted model name").value
            return self._parse_route(name, model)
        raise self._error(
            f"step '{name}': expected llm, agent, or route body; found {self.current.value!r}"
        )

    def _parse_llm(self, name: str, model: str) -> Step:
        self._expect_kind("LBRACE", "'{' after llm model")
        prompt: Expression | None = None
        expect: tuple[ExpectRule, ...] = ()
        next_target: str | None = None
        while self.current.kind != "RBRACE":
            if self._at_word("expect"):
                if expect:
                    raise self._error(f"step '{name}': multiple expect blocks")
                expect = self._parse_expect(name)
            elif self._at_word("then"):
                if next_target is not None:
                    raise self._error(f"step '{name}': multiple then -> edges")
                next_target = self._parse_edge("then")
            elif prompt is None:
                prompt = self._parse_expression()
            else:
                raise self._error(f"step '{name}': unexpected token {self.current.value!r}")
        self._advance()
        if prompt is None:
            raise self._error(f"step '{name}': llm body must include a prompt expression")
        return Step(name=name, model=model, prompt=prompt, next_target=next_target, expect=expect)

    def _parse_expect(self, name: str) -> tuple[ExpectRule, ...]:
        self._expect_word("expect")
        self._expect_kind("LBRACE", "'{' after expect")
        rules: list[ExpectRule] = []
        seen: set[str] = set()
        while self.current.kind != "RBRACE":
            kind = self._expect_name("expect rule")
            if kind != "matches" and kind in seen:
                raise self._error(f"step '{name}': duplicate expect rule: {kind}")
            if kind == "nonempty":
                rule = ExpectRule(kind=kind)
            elif kind == "max_chars":
                limit = int(self._expect_kind("INTEGER", "positive max_chars value").value)
                if limit < 1:
                    raise self._error(f"step '{name}': max_chars must be >= 1")
                rule = ExpectRule(kind=kind, limit=limit)
            elif kind == "matches":
                pattern = self._expect_kind("STRING", "quoted regex pattern").value
                _validate_regex(name, pattern, self.current)
                rule = ExpectRule(kind=kind, pattern=pattern)
            elif kind == "one_of":
                values = [self._expect_kind("STRING", "quoted one_of value").value]
                while self._accept_kind("COMMA"):
                    values.append(self._expect_kind("STRING", "quoted one_of value").value)
                if len({value.casefold() for value in values}) != len(values):
                    raise self._error(f"step '{name}': one_of has duplicate values")
                rule = ExpectRule(kind=kind, values=tuple(values))
            else:
                raise self._error(f"step '{name}': invalid expect rule: {kind}")
            seen.add(kind)
            rules.append(rule)
        self._advance()
        if not rules:
            raise self._error(f"step '{name}': expect block must contain at least one rule")
        return tuple(rules)

    def _parse_agent(self, name: str, model: str) -> AgentStep:
        self._expect_kind("LBRACE", "'{' after agent model")
        prompt: Expression | None = None
        tools: tuple[str, ...] = ()
        tools_seen = False
        max_iters = DEFAULT_MAX_ITERS
        iters_seen = False
        next_target: str | None = None
        while self.current.kind != "RBRACE":
            if self._at_word("expect"):
                raise self._error(
                    f"step '{name}': expect blocks are only supported on llm steps "
                    "(an agent step's contract is its own shape)"
                )
            if self._accept_word("tools"):
                if tools_seen:
                    raise self._error(f"agent '{name}': multiple tools declarations")
                tools_seen = True
                tools = self._parse_tools(name)
            elif self._accept_word("max_iters"):
                if iters_seen:
                    raise self._error(f"agent '{name}': multiple max_iters declarations")
                iters_seen = True
                max_iters = int(self._expect_kind("INTEGER", "positive max_iters value").value)
                if not 1 <= max_iters <= MAX_AGENT_ITERS:
                    raise self._error(
                        f"agent '{name}': max_iters must be between 1 and {MAX_AGENT_ITERS}"
                    )
            elif self._at_word("then"):
                if next_target is not None:
                    raise self._error(f"step '{name}': multiple then -> edges")
                next_target = self._parse_edge("then")
            elif prompt is None:
                prompt = self._parse_expression()
            else:
                raise self._error(f"agent '{name}': unexpected token {self.current.value!r}")
        self._advance()
        if prompt is None:
            raise self._error(f"agent '{name}': missing prompt expression")
        return AgentStep(
            name=name,
            model=model,
            prompt=prompt,
            tools=tools,
            max_iters=max_iters,
            next_target=next_target,
        )

    def _parse_tools(self, name: str) -> tuple[str, ...]:
        self._expect_kind("LBRACKET", "'[' after tools")
        values: list[str] = []
        if self.current.kind != "RBRACKET":
            values.append(self._expect_name("tool name"))
            while self._accept_kind("COMMA"):
                values.append(self._expect_name("tool name"))
        self._expect_kind("RBRACKET", "']' after tools")
        if len(set(values)) != len(values):
            raise self._error(f"agent '{name}': duplicate tool name")
        return tuple(values)

    def _parse_route(self, name: str, model: str) -> RouteStep:
        self._expect_kind("LBRACE", "'{' after route model")
        prompt: Expression | None = None
        arms: list[RouteArm] = []
        else_target: str | None = None
        while self.current.kind != "RBRACE":
            if self._at_word("expect"):
                raise self._error(
                    f"step '{name}': expect blocks are only supported on llm steps "
                    "(a route step's contract is its own shape)"
                )
            if self._accept_word("on"):
                label = self._expect_kind("STRING", "quoted route label").value
                self._expect_kind("ARROW", "'->' after route label")
                target = self._expect_name("route target")
                arms.append(RouteArm(label=label, target=target))
            elif self._at_word("else"):
                if else_target is not None:
                    raise self._error(f"route '{name}': multiple else -> edges")
                else_target = self._parse_edge("else")
            elif prompt is None:
                prompt = self._parse_expression()
            else:
                raise self._error(f"route '{name}': unexpected token {self.current.value!r}")
        self._advance()
        if prompt is None:
            raise self._error(f"route '{name}': missing prompt expression")
        if not arms:
            raise self._error(
                f"route '{name}': needs at least one arm: on \"<label>\" -> <step|end>"
            )
        return RouteStep(
            name=name,
            model=model,
            prompt=prompt,
            arms=tuple(arms),
            else_target=else_target,
        )

    def _parse_edge(self, keyword: str) -> str:
        self._expect_word(keyword)
        self._expect_kind("ARROW", f"'->' after {keyword}")
        return self._expect_name(f"{keyword} target")

    def _parse_emit(self) -> EmitBlock:
        self._expect_word("emit")
        if self._accept_word("text"):
            kind = "text"
            model = None
        elif self._accept_word("llm"):
            kind = "llm"
            model = self._expect_kind("STRING", "quoted emit model").value
        else:
            raise self._error("Expected 'text' or 'llm' after emit")
        self._expect_kind("LBRACE", "'{' after emit declaration")
        expression = self._parse_expression()
        self._expect_kind("RBRACE", "'}' after emit expression")
        return EmitBlock(kind=kind, expression=expression, model=model)

    def _parse_expression(self) -> Expression:
        terms = [self._parse_term()]
        while self._accept_kind("PLUS"):
            terms.append(self._parse_term())
        return Expression(terms=terms)

    def _parse_term(self):
        string = self._accept_kind("STRING")
        if string is not None:
            return StringLiteral(value=string.value)
        root = self._expect_kind("IDENT", "expression term")
        self._expect_kind("DOT", "'.' in reference")
        if root.value == "context":
            return ContextRef(name=self._expect_name("context reference name"))
        if root.value == "inputs":
            return InputsRef(name=self._expect_name("input reference name"))
        if root.value == "steps":
            step_name = self._expect_name("step reference name")
            self._expect_kind("DOT", "'.output' in step reference")
            self._expect_word("output")
            return StepsRef(step_name=step_name, optional=self._accept_kind("QUESTION") is not None)
        raise self._error(f"Unsupported expression root: {root.value}", root)


def _validate_regex(step_name: str, pattern: str, token: _Token) -> None:
    if len(pattern) > MAX_REGEX_PATTERN_CHARS:
        raise ParseError(
            f"step '{step_name}': regex exceeds {MAX_REGEX_PATTERN_CHARS} characters at {token.where}"
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ParseError(f"step '{step_name}': matches has an invalid regex: {exc}") from exc


def _iter_refs(expression: Expression) -> Iterable[ContextRef | StepsRef]:
    for term in expression.terms:
        if isinstance(term, (ContextRef, StepsRef)):
            yield term


def _validate_program(context: ContextBlock, steps: StepsBlock, emit: EmitBlock) -> None:
    context_names = {assignment.name for assignment in context.assignments}
    step_names = {step.name for step in steps.steps}
    prior: set[str] = set()
    for step in steps.steps:
        for ref in _iter_refs(step.prompt):
            if isinstance(ref, ContextRef) and ref.name not in context_names:
                raise ParseError(
                    f"step '{step.name}' references unknown context value '{ref.name}'"
                )
            if isinstance(ref, StepsRef) and ref.step_name not in prior:
                raise ParseError(
                    f"step '{step.name}' references step '{ref.step_name}' before it is available"
                )
        prior.add(step.name)
    for ref in _iter_refs(emit.expression):
        if isinstance(ref, ContextRef) and ref.name not in context_names:
            raise ParseError(f"emit references unknown context value '{ref.name}'")
        if isinstance(ref, StepsRef) and ref.step_name not in step_names:
            raise ParseError(f"emit references unknown step '{ref.step_name}'")


def _validate_targets(steps: list[StepNode]) -> None:
    index = {step.name: i for i, step in enumerate(steps)}

    def check(origin: str, position: int, target: str, kind: str) -> None:
        if target == END_TARGET:
            return
        if target not in index:
            raise ParseError(f"step '{origin}': {kind} -> unknown step '{target}'")
        if index[target] <= position:
            raise ParseError(
                f"step '{origin}': {kind} -> '{target}' jumps backward; "
                "targets must be declared after the step that jumps to them"
            )

    for position, step in enumerate(steps):
        if isinstance(step, RouteStep):
            labels: set[str] = set()
            normalized: set[str] = set()
            for arm in step.arms:
                folded = arm.label.casefold()
                if folded in normalized:
                    raise ParseError(f"step '{step.name}': duplicate route label \"{arm.label}\"")
                labels.add(arm.label)
                normalized.add(folded)
                check(step.name, position, arm.target, f'on "{arm.label}"')
            if step.else_target is not None:
                check(step.name, position, step.else_target, "else")
        elif step.next_target is not None:
            check(step.name, position, step.next_target, "then")


def parse_program(source: str) -> Program:
    """Parse and semantically validate one complete ThreadLang program."""
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ParseError(f"Source exceeds {MAX_SOURCE_BYTES} bytes")
    return _Parser(_lex(source)).parse()
