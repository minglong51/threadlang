"""Tools an agent step can call, behind a safe execution boundary.

The agentic core (v0.3) adds an `agent` step whose model can *act*, not just
transform text. Acting means calling tools. This module defines:

- `ToolSpec` — the name/description/JSON-schema a model sees when deciding
  whether and how to call a tool.
- `Tool` — the runtime contract: `.spec` plus `.run(args) -> str`.
- `ToolRegistry` — the allow-list. An agent step references tools *by name*;
  the registry is the only thing that can turn a name into executable code.
  A tool the registry doesn't hold cannot run — that is the boundary.
- `default_registry()` — two deterministic, side-effect-free built-ins
  (`echo`, `calculator`) so the loop is demonstrable without network, files,
  or an API key. Real deployments register their own tools.

The boundary is deliberately narrow for v0.3: tools are pure functions of
their arguments, the model can only reach the ones a step allow-lists, and a
tool that raises is caught and surfaced to the model as an observable error
string rather than crashing the run. Sandboxing, resource limits, and
side-effecting tools (network/fs) are later layers — added when a use case
earns the risk, not before.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, Protocol


@dataclass(frozen=True)
class ToolSpec:
    """What the model sees: a name, a one-line description, and a JSON-schema
    object describing the arguments. The schema is passed straight through to
    the provider's tool-use API."""

    name: str
    description: str
    parameters: Dict[str, object]


class Tool(Protocol):
    spec: ToolSpec

    def run(self, args: Mapping[str, object]) -> str: ...


@dataclass(frozen=True)
class FunctionTool:
    """A Tool backed by a plain Python callable. The callable takes the parsed
    argument mapping and returns a string the agent observes."""

    spec: ToolSpec
    _fn: Callable[[Mapping[str, object]], str]

    def run(self, args: Mapping[str, object]) -> str:
        return self._fn(args)


class ToolRegistry:
    """The allow-list mapping tool names to executable tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")
        self._tools[name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def specs(self, names: List[str]) -> List[ToolSpec]:
        return [self.get(name).spec for name in names]

    def names(self) -> List[str]:
        return list(self._tools)


# ───────── built-in deterministic tools ─────────


def _echo(args: Mapping[str, object]) -> str:
    return str(args.get("text", ""))


_ECHO = FunctionTool(
    spec=ToolSpec(
        name="echo",
        description="Return the given text verbatim. Useful for testing the tool loop.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo back."}},
            "required": ["text"],
        },
    ),
    _fn=_echo,
)

# Whitelist of arithmetic operators. Deliberately *excludes* power (`ast.Pow` /
# `**`): `9**9**9` is a one-line denial-of-service (a number with hundreds of
# millions of digits), and arithmetic on an agent tool does not need it.
_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_arithmetic(node: ast.AST) -> float:
    """Evaluate a parsed arithmetic expression by walking the AST. Only number
    literals and the whitelisted operators are reachable — names, calls,
    attributes, and `**` raise, so there is no path to arbitrary code or to a
    DoS-sized computation. This replaces `eval`, which a character allow-list
    could not safely contain (`*` permits `**`)."""
    if isinstance(node, ast.Expression):
        return _eval_arithmetic(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"non-numeric literal: {node.value!r}")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        return _BINARY_OPS[type(node.op)](
            _eval_arithmetic(node.left), _eval_arithmetic(node.right)
        )
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_arithmetic(node.operand))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def _calculator(args: Mapping[str, object]) -> str:
    """Evaluate a basic arithmetic expression. Side-effect-free: the expression
    is parsed and walked, never `eval`-ed, and only number literals plus the
    whitelisted operators (+ - * / // % and unary +/-) are permitted. Anything
    else — names, calls, `**` — is rejected as an observable error."""
    expression = str(args.get("expression", "")).strip()
    if not expression:
        return "error: empty expression"
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_arithmetic(tree)
    except Exception as exc:  # parse / arithmetic errors are observable, not fatal
        return f"error: {type(exc).__name__}: {exc}"
    return str(value)


_CALCULATOR = FunctionTool(
    spec=ToolSpec(
        name="calculator",
        description="Evaluate a basic arithmetic expression (e.g. '12 * (3 + 4)').",
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "An arithmetic expression using + - * / % ( ) and numbers.",
                }
            },
            "required": ["expression"],
        },
    ),
    _fn=_calculator,
)


def default_registry() -> ToolRegistry:
    """A registry with the deterministic built-ins, so the agent loop runs
    end-to-end without network or an API key."""
    registry = ToolRegistry()
    registry.register(_ECHO)
    registry.register(_CALCULATOR)
    return registry
