"""v1 LLM-workflow tests.

Uses a deterministic mock client so the tests work offline and without
an Anthropic API key. The actual LLM client is exercised separately
(manual + by AnthropicClient construction in test_imports below).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import List

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # type: ignore  # noqa: E402

from threadlang.llm import DryRunClient, LLMClient  # noqa: E402
from threadlang.parser import ParseError, parse_program  # noqa: E402
from threadlang.runtime import RuntimeError as TLRuntimeError, run_program  # noqa: E402


@dataclass
class MockClient:
    """Records every call and returns scripted responses in order."""

    responses: List[str]
    calls: List[tuple[str, str]] = field(default_factory=list)

    def complete(self, model: str, prompt: str) -> str:
        self.calls.append((model, prompt))
        if not self.responses:
            raise AssertionError(f"MockClient ran out of responses on call: ({model!r}, {prompt!r})")
        return self.responses.pop(0)


# ───────── emit llm ─────────


def test_emit_llm_renders_prompt_and_returns_response() -> None:
    source = """
    thread Hi {
      context { tone = "warm" }
      emit llm "claude-haiku-4-5" {
        "Say hi to " + inputs.name + " in a " + context.tone + " tone."
      }
    }
    """
    client = MockClient(responses=["Hi Alex — welcome in."])
    result = run_program(parse_program(source), inputs={"name": "Alex"}, llm_client=client)

    assert result.output == "Hi Alex — welcome in."
    assert client.calls == [
        ("claude-haiku-4-5", "Say hi to Alex in a warm tone."),
    ]


def test_emit_llm_falls_back_to_dryrun_client_when_omitted() -> None:
    source = """
    thread Hi {
      context { tone = "warm" }
      emit llm "claude-haiku-4-5" {
        "Hi " + inputs.name
      }
    }
    """
    result = run_program(parse_program(source), inputs={"name": "Sam"})
    # DryRunClient echoes the prompt with a model tag.
    assert result.output == "[dry-run:claude-haiku-4-5] Hi Sam"


# ───────── steps + steps.X.output ─────────


def test_steps_run_in_order_and_chain_outputs() -> None:
    source = """
    thread Pipeline {
      context { audience = "5-year-old" }

      steps {
        step extract {
          llm "model-a" {
            "Extract claims from: " + inputs.text
          }
        }
        step retell {
          llm "model-b" {
            "Rewrite for " + context.audience + ": " + steps.extract.output
          }
        }
      }

      emit text {
        steps.retell.output
      }
    }
    """
    client = MockClient(responses=["claim1; claim2", "Once upon a time..."])
    result = run_program(
        parse_program(source),
        inputs={"text": "The sky is blue. Water is wet."},
        llm_client=client,
    )

    assert result.output == "Once upon a time..."
    assert result.step_outputs == {
        "extract": "claim1; claim2",
        "retell": "Once upon a time...",
    }
    assert client.calls == [
        ("model-a", "Extract claims from: The sky is blue. Water is wet."),
        ("model-b", "Rewrite for 5-year-old: claim1; claim2"),
    ]


def test_steps_then_emit_llm_combines_both() -> None:
    source = """
    thread MixedFinal {
      context { tone = "concise" }
      steps {
        step facts {
          llm "extractor" {
            "Pull 3 facts from: " + inputs.text
          }
        }
      }
      emit llm "summarizer" {
        "In a " + context.tone + " way, summarize: " + steps.facts.output
      }
    }
    """
    client = MockClient(responses=["A;B;C", "Three things."])
    result = run_program(
        parse_program(source),
        inputs={"text": "Whatever"},
        llm_client=client,
    )
    assert result.output == "Three things."
    assert [c[0] for c in client.calls] == ["extractor", "summarizer"]


# ───────── error paths ─────────


def test_forward_reference_to_step_raises() -> None:
    source = """
    thread Bad {
      context { x = "y" }
      steps {
        step later {
          llm "m" {
            "use " + steps.never_defined.output
          }
        }
      }
      emit text { steps.later.output }
    }
    """
    with pytest.raises(TLRuntimeError, match="step 'never_defined' before it ran"):
        run_program(parse_program(source), inputs={}, llm_client=MockClient(responses=["x"]))


def test_duplicate_step_names_rejected_at_parse_time() -> None:
    source = """
    thread Dup {
      context { x = "y" }
      steps {
        step a { llm "m" { "1" } }
        step a { llm "m" { "2" } }
      }
      emit text { "out" }
    }
    """
    with pytest.raises(ParseError, match="Duplicate step name"):
        parse_program(source)


def test_client_exception_wraps_into_runtime_error() -> None:
    class Boom:
        def complete(self, model: str, prompt: str) -> str:
            raise ConnectionError("network down")

    source = """
    thread X {
      context { x = "y" }
      emit llm "m" { "hi" }
    }
    """
    with pytest.raises(TLRuntimeError, match="LLM call failed during emit"):
        run_program(parse_program(source), inputs={}, llm_client=Boom())


# ───────── back-compat: original v0 hello still works ─────────


def test_v0_hello_still_runs_without_a_client() -> None:
    source = (REPO_ROOT / "examples" / "hello.thread").read_text(encoding="utf-8")
    result = run_program(parse_program(source), inputs={"name": "world"})
    assert result.output == "Hello, world!"
    assert result.step_outputs == {}


# ───────── DryRunClient as the documented test default ─────────


def test_dry_run_client_is_deterministic() -> None:
    c: LLMClient = DryRunClient()
    assert c.complete("m", "hello") == "[dry-run:m] hello"
    assert c.complete("m", "hello") == "[dry-run:m] hello"
