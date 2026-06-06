"""v0.3 agent-step tests.

Two things these guard that the architecture sells:
  1. Determinism — the same agent program produces the same trace, twice.
  2. The tool boundary — the calculator cannot be turned into a DoS or a
     code-execution vector, and an agent cannot reach a tool it was not given.

All offline: the dry-run client is deterministic and a small scripted client
exercises real tool feedback without an API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # type: ignore  # noqa: E402

from threadlang.ast import AgentStep, Step  # noqa: E402
from threadlang.llm import AgentTurn, DryRunClient, ToolCall  # noqa: E402
from threadlang.parser import ParseError, parse_program  # noqa: E402
from threadlang.runtime import RuntimeError as TLRuntimeError, run_program  # noqa: E402
from threadlang.tools import ToolRegistry, default_registry  # noqa: E402


def _agent_source(tools: str = "[ echo, calculator ]", max_iters: int = 4) -> str:
    return f"""
    thread Researcher {{
      context {{ persona = "a precise assistant" }}
      steps {{
        step solve {{
          agent "test-model" {{
            tools {tools}
            max_iters {max_iters}
            "You are " + context.persona + ". Task: " + inputs.task
          }}
        }}
      }}
      emit text {{ steps.solve.output }}
    }}
    """


# ───────── determinism ─────────


def test_agent_dry_run_is_deterministic() -> None:
    program = parse_program(_agent_source())
    first = run_program(program, inputs={"task": "2+2"}, llm_client=DryRunClient())
    second = run_program(program, inputs={"task": "2+2"}, llm_client=DryRunClient())

    assert first.output == second.output
    # The whole trace — every phase, message, and data payload — is identical.
    assert [(e.phase, e.message, e.data) for e in first.trace] == [
        (e.phase, e.message, e.data) for e in second.trace
    ]


def test_agent_dry_run_runs_the_tool_loop() -> None:
    result = run_program(
        parse_program(_agent_source()), inputs={"task": "anything"}, llm_client=DryRunClient()
    )
    phases = [e.message for e in result.trace]
    assert "Agent step 'solve' started" in phases
    assert any(m.startswith("Tool 'echo' called") for m in phases)
    assert "Agent 'solve' finished" in phases


# ───────── tool feedback (scripted client, no API key) ─────────


@dataclass
class ScriptedAgent:
    """Turn 1 asks for a tool; turn 2 answers using what the tool observed."""

    turns: int = 0

    def complete(self, model: str, prompt: str) -> str:
        return "unused"

    def agent_step(self, model: str, messages: Sequence[dict], tools) -> AgentTurn:
        self.turns += 1
        if self.turns == 1:
            return AgentTurn(
                text="",
                tool_calls=(ToolCall(id="c1", name="calculator", arguments={"expression": "21*2"}),),
            )
        observation = [m for m in messages if m.get("role") == "tool"][-1]["content"]
        return AgentTurn(text=f"the answer is {observation}", tool_calls=())


def test_tool_result_feeds_back_into_next_turn() -> None:
    source = """
    thread Calc {
      context { x = "y" }
      steps { step solve { agent "m" { tools [ calculator ] max_iters 4 "solve 21*2" } } }
      emit text { steps.solve.output }
    }
    """
    result = run_program(parse_program(source), inputs={}, llm_client=ScriptedAgent())
    assert result.output == "the answer is 42"
    assert any(
        e.message.startswith("Tool 'calculator'") and e.data["result"] == "42"
        for e in result.trace
    )


# ───────── tool boundary / calculator safety ─────────


def test_calculator_computes() -> None:
    calc = default_registry().get("calculator")
    assert calc.run({"expression": "12 * (3 + 4)"}) == "84"
    assert calc.run({"expression": "10 // 3"}) == "3"


def test_calculator_rejects_power_operator_dos() -> None:
    # `**` must NOT evaluate — `9**9**9` would otherwise be a one-line DoS.
    result = default_registry().get("calculator").run({"expression": "9**9**9"})
    assert result.startswith("error:")
    assert "9" * 50 not in result  # no giant number leaked through


def test_calculator_rejects_code_execution() -> None:
    result = default_registry().get("calculator").run({"expression": "__import__('os').getcwd()"})
    assert result.startswith("error:")


def test_calculator_division_by_zero_is_observable_not_fatal() -> None:
    result = default_registry().get("calculator").run({"expression": "1/0"})
    assert result.startswith("error:")


def test_agent_cannot_call_a_tool_it_was_not_given() -> None:
    # The agent only has `echo`, but the model tries to call `calculator`.
    class RogueAgent:
        def complete(self, model, prompt):  # pragma: no cover - unused
            return ""

        def agent_step(self, model, messages, tools):
            if not any(m.get("role") == "tool" for m in messages):
                return AgentTurn(
                    text="",
                    tool_calls=(ToolCall(id="c1", name="calculator", arguments={"expression": "2+2"}),),
                )
            return AgentTurn(text="done", tool_calls=())

    source = """
    thread Locked {
      context { x = "y" }
      steps { step s { agent "m" { tools [ echo ] max_iters 3 "go" } } }
      emit text { steps.s.output }
    }
    """
    result = run_program(parse_program(source), inputs={}, llm_client=RogueAgent())
    blocked = [
        e for e in result.trace
        if e.message.startswith("Tool 'calculator'") and "not available" in e.data["result"]
    ]
    assert blocked, "calculator should have been refused — it was not in the allow-list"


# ───────── parser: mixed steps, ordering, validation ─────────


def test_agent_and_llm_steps_preserve_declaration_order() -> None:
    source = """
    thread Mixed {
      context { who = "tester" }
      steps {
        step a { llm "m1" { "first " + inputs.x } }
        step b { agent "m2" { tools [ calculator ] max_iters 3 "compute" } }
        step c { llm "m3" { "third " + steps.a.output } }
      }
      emit text { steps.b.output }
    }
    """
    program = parse_program(source)
    kinds = [(type(s).__name__, s.name) for s in program.steps.steps]
    assert kinds == [("Step", "a"), ("AgentStep", "b"), ("Step", "c")]
    agent_step = program.steps.steps[1]
    assert isinstance(agent_step, AgentStep)
    assert agent_step.tools == ("calculator",)
    assert agent_step.max_iters == 3


def test_agent_defaults_max_iters_when_omitted() -> None:
    source = """
    thread D {
      context { x = "y" }
      steps { step s { agent "m" { tools [ echo ] "go" } } }
      emit text { steps.s.output }
    }
    """
    step = parse_program(source).steps.steps[0]
    assert isinstance(step, AgentStep)
    assert step.max_iters == 6


def test_unknown_tool_reference_raises_at_runtime() -> None:
    source = """
    thread U {
      context { x = "y" }
      steps { step s { agent "m" { tools [ no_such_tool ] max_iters 2 "go" } } }
      emit text { steps.s.output }
    }
    """
    with pytest.raises(TLRuntimeError, match="unknown tool: no_such_tool"):
        run_program(parse_program(source), inputs={}, llm_client=DryRunClient())


def test_agent_step_with_non_agent_client_raises() -> None:
    class CompleteOnly:
        def complete(self, model: str, prompt: str) -> str:
            return "x"

    source = """
    thread N {
      context { x = "y" }
      steps { step s { agent "m" { tools [ echo ] max_iters 2 "go" } } }
      emit text { steps.s.output }
    }
    """
    with pytest.raises(TLRuntimeError, match="agent-capable client"):
        run_program(parse_program(source), inputs={}, llm_client=CompleteOnly())


def test_agent_exhausting_max_iters_raises() -> None:
    class NeverStops:
        def complete(self, model, prompt):  # pragma: no cover - unused
            return ""

        def agent_step(self, model, messages, tools):
            return AgentTurn(
                text="",
                tool_calls=(ToolCall(id="c", name="echo", arguments={"text": "again"}),),
            )

    source = """
    thread Loop {
      context { x = "y" }
      steps { step s { agent "m" { tools [ echo ] max_iters 2 "go" } } }
      emit text { steps.s.output }
    }
    """
    with pytest.raises(TLRuntimeError, match="exceeded max_iters"):
        run_program(parse_program(source), inputs={}, llm_client=NeverStops())
