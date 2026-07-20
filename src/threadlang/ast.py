"""AST node definitions for ThreadLang."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

# Reserved jump target: `-> end` skips the remaining steps and goes to emit.
# No step may be named `end`.
END_TARGET = "end"


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
    """Reference to a prior step's output: `steps.<step_name>.output`.

    `optional=True` (written `steps.<step_name>.output?`) renders as the empty
    string when the step was skipped by routing, instead of failing the run —
    how a join/emit reads outputs from branches that may not have executed.
    """

    step_name: str
    optional: bool = False


ExpressionTerm = Union[StringLiteral, ContextRef, InputsRef, StepsRef]


@dataclass(frozen=True)
class Expression:
    terms: List[ExpressionTerm]


@dataclass(frozen=True)
class ExpectRule:
    """One clause of an llm step's output contract (`expect { ... }`).

    kind=one_of    → values holds the closed set of admissible replies.
    kind=matches   → pattern holds a regex the reply must fullmatch.
    kind=max_chars → limit holds the maximum reply length.
    kind=nonempty  → the reply must contain non-whitespace text.
    """

    kind: str  # "one_of" | "matches" | "max_chars" | "nonempty"
    values: Tuple[str, ...] = ()
    pattern: Optional[str] = None
    limit: Optional[int] = None


@dataclass(frozen=True)
class Step:
    """A single-shot LLM transformation: `llm "<model>" { <prompt> }`.

    Renders its prompt once, calls the model, binds the response to
    `steps.<name>.output`. No tools, no loop — deterministic chaining.

    `next_target` (`then -> <step|end>` in source) is the step's outgoing
    edge; None means fall through to the next declared step.

    `expect` is the step's output contract: every rule must hold for the
    reply to be accepted. A violating reply is retried once with the
    violations fed back; a second violation fails the run.
    """

    name: str
    model: str
    prompt: Expression
    next_target: Optional[str] = None
    expect: Tuple[ExpectRule, ...] = ()


@dataclass(frozen=True)
class AgentStep:
    """A tool-using agent: `agent "<model>" { tools [...] max_iters N <prompt> }`.

    Renders its prompt as the opening instruction, then runs a tool-use loop
    (model → tool calls → observations → model) up to `max_iters` turns. The
    model may only call tools named in `tools`. The final text is bound to
    `steps.<name>.output`.

    `next_target` (`then -> <step|end>` in source) is the step's outgoing
    edge; None means fall through to the next declared step.
    """

    name: str
    model: str
    prompt: Expression
    tools: Tuple[str, ...] = ()
    max_iters: int = 6
    next_target: Optional[str] = None


@dataclass(frozen=True)
class RouteArm:
    """One conditional edge of a route step: `on "<label>" -> <target>`."""

    label: str
    target: str


@dataclass(frozen=True)
class RouteStep:
    """A routing decision: `route "<model>" { <prompt> on "<label>" -> <step> ... }`.

    The model call carries an output contract: it must reply with exactly one
    of the arm labels. The chosen label is bound to `steps.<name>.output` and
    execution jumps to that arm's target. A reply matching no label is retried
    once with the violation fed back; if still unmatched, execution takes
    `else_target` when present, otherwise the run fails. Arm dispatch itself
    is deterministic — the model only picks the label.
    """

    name: str
    model: str
    prompt: Expression
    arms: Tuple[RouteArm, ...] = ()
    else_target: Optional[str] = None


StepNode = Union[Step, AgentStep, RouteStep]


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
