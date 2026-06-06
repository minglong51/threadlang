"""AST node definitions for ThreadLang."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union


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


@dataclass(frozen=True)
class StepsRef:
    """Reference to a prior step's output: `steps.<step_name>.output`."""

    step_name: str


ExpressionTerm = Union[StringLiteral, ContextRef, InputsRef, StepsRef]


@dataclass(frozen=True)
class Expression:
    terms: List[ExpressionTerm]


@dataclass(frozen=True)
class Step:
    """A single-shot LLM transformation: `llm "<model>" { <prompt> }`.

    Renders its prompt once, calls the model, binds the response to
    `steps.<name>.output`. No tools, no loop — deterministic chaining.
    """

    name: str
    model: str
    prompt: Expression


@dataclass(frozen=True)
class AgentStep:
    """A tool-using agent: `agent "<model>" { tools [...] max_iters N <prompt> }`.

    Renders its prompt as the opening instruction, then runs a tool-use loop
    (model → tool calls → observations → model) up to `max_iters` turns. The
    model may only call tools named in `tools`. The final text is bound to
    `steps.<name>.output`.
    """

    name: str
    model: str
    prompt: Expression
    tools: Tuple[str, ...] = ()
    max_iters: int = 6


StepNode = Union[Step, AgentStep]


@dataclass(frozen=True)
class StepsBlock:
    steps: List[StepNode] = field(default_factory=list)


@dataclass(frozen=True)
class EmitBlock:
    """Final output expression.

    kind=text  → emit text { <expression> } — string concat of terms.
    kind=llm   → emit llm "<model>" { <prompt expression> } — call model,
                 return its response as the program output.
    """

    kind: str  # "text" | "llm"
    expression: Expression
    model: Optional[str] = None  # only set when kind == "llm"


@dataclass(frozen=True)
class Program:
    thread_name: str
    context: ContextBlock
    steps: StepsBlock
    emit: EmitBlock
