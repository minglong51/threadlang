"""v0.11 expect-contract tests.

What these guard:
  1. The contract is parsed and validated at parse time — a bad rule fails
     before any model call.
  2. The contract the runtime enforces is the contract the model was shown
     (rendered into the prompt), with one feedback retry and a loud failure.
  3. A one_of contract is a closed-enum call: canonicalized binding, and the
     dry-run client resolves it deterministically through the route protocol.
  4. Violations are first-class trace events that metrics and probe reports
     fold, exactly like route violations.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Sequence

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.ast import ExpectRule  # noqa: E402
from threadlang.llm import DryRunClient  # noqa: E402
from threadlang.metrics import compute_metrics  # noqa: E402
from threadlang.parser import ParseError, parse_program  # noqa: E402
from threadlang.probe import ProbeRunData, probe_report  # noqa: E402
from threadlang.runtime import RuntimeError as TLRuntimeError, run_program  # noqa: E402

VERDICT_SOURCE = """
thread ExpectDemo {
  context {}
  steps {
    step verdict {
      llm "test-model" {
        "Should this ship? " + inputs.change
        expect {
          one_of "ship", "hold"
        }
      }
    }
  }
  emit text { steps.verdict.output }
}
"""

SHAPE_SOURCE = """
thread ExpectShape {
  context {}
  steps {
    step summary {
      llm "test-model" {
        "Summarize: " + inputs.text
        expect {
          nonempty
          matches "[A-Z].*"
          max_chars 80
        }
        then -> end
      }
    }
    step unused {
      llm "test-model" { "never: " + inputs.text }
    }
  }
  emit text { steps.summary.output }
}
"""


class ScriptedClient:
    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, model: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._responses.pop(0)


# ───────── parser ─────────


def test_parse_expect_rules_in_order_alongside_then() -> None:
    program = parse_program(SHAPE_SOURCE)
    step = program.steps.steps[0]
    assert step.next_target == "end"
    assert step.expect == (
        ExpectRule(kind="nonempty"),
        ExpectRule(kind="matches", pattern="[A-Z].*"),
        ExpectRule(kind="max_chars", limit=80),
    )
    assert [t.value for t in step.prompt.terms if hasattr(t, "value")] == ["Summarize: "]


def test_parse_one_of_values() -> None:
    program = parse_program(VERDICT_SOURCE)
    (rule,) = program.steps.steps[0].expect
    assert rule == ExpectRule(kind="one_of", values=("ship", "hold"))


@pytest.mark.parametrize(
    "rules, error_part",
    [
        ("frobnicate 3", "invalid expect rule"),
        ('matches "["', "invalid regex"),
        ("max_chars 0", "max_chars must be >= 1"),
        ("nonempty\n        nonempty", "duplicate expect rule"),
        ('one_of "a", "A"', "duplicate values"),
        ("", "at least one rule"),
    ],
)
def test_parse_expect_rejects_bad_rules(rules: str, error_part: str) -> None:
    source = SHAPE_SOURCE.replace(
        'nonempty\n          matches "[A-Z].*"\n          max_chars 80', rules
    )
    with pytest.raises(ParseError, match=error_part):
        parse_program(source)


def test_parse_rejects_multiple_expect_blocks() -> None:
    source = VERDICT_SOURCE.replace(
        'expect {\n          one_of "ship", "hold"\n        }',
        'expect { nonempty }\n        expect { nonempty }',
    )
    with pytest.raises(ParseError, match="multiple expect blocks"):
        parse_program(source)


def test_parse_rejects_expect_on_agent_and_route() -> None:
    agent_source = """
    thread T {
      context {}
      steps {
        step a {
          agent "m" {
            tools [ echo ]
            expect { nonempty }
            "go: " + inputs.x
          }
        }
      }
      emit text { steps.a.output }
    }
    """
    with pytest.raises(ParseError, match="only supported on llm steps"):
        parse_program(agent_source)

    route_source = """
    thread T {
      context {}
      steps {
        step r {
          route "m" {
            "pick: " + inputs.x
            expect { nonempty }
            on "a" -> end
          }
        }
      }
      emit text { steps.r.output }
    }
    """
    with pytest.raises(ParseError, match="only supported on llm steps"):
        parse_program(route_source)


# ───────── runtime ─────────


def test_contract_rendered_into_prompt() -> None:
    program = parse_program(SHAPE_SOURCE)
    client = ScriptedClient(["Fine.", "Fine."])
    run_program(program, {"text": "x"}, llm_client=client)
    assert "Reply with at most 80 characters." in client.prompts[0]
    assert "must match this regular expression: [A-Z].*" in client.prompts[0]
    assert "must not be empty" in client.prompts[0]


def test_one_of_canonicalizes_noisy_reply() -> None:
    program = parse_program(VERDICT_SOURCE)
    client = ScriptedClient([' "Ship". '])
    result = run_program(program, {"change": "x"}, llm_client=client)
    assert result.step_outputs["verdict"] == "ship"
    assert result.output == "ship"


def test_violation_retries_with_feedback_then_succeeds() -> None:
    program = parse_program(VERDICT_SOURCE)
    client = ScriptedClient(["probably ship it", "hold"])
    result = run_program(program, {"change": "x"}, llm_client=client)
    assert result.output == "hold"
    assert "violated the output contract" in client.prompts[1]
    assert "reply is not one of: ship, hold" in client.prompts[1]

    metrics = compute_metrics(result.trace, status="completed")
    assert metrics.contract_violations == 1
    assert metrics.route_violations == 0
    assert metrics.model_calls == 2


def test_double_violation_fails_loud() -> None:
    program = parse_program(SHAPE_SOURCE)
    client = ScriptedClient(["lowercase start", "still lowercase"])
    with pytest.raises(TLRuntimeError, match="violated its output contract after retry"):
        run_program(program, {"text": "x"}, llm_client=client)


def test_max_chars_and_matches_enforced_on_stripped_reply() -> None:
    program = parse_program(SHAPE_SOURCE)
    client = ScriptedClient(["  Good summary.\n"])
    result = run_program(program, {"text": "x"}, llm_client=client)
    assert result.step_outputs["summary"] == "Good summary."


def test_one_of_canonicalizes_before_other_rules_regardless_of_order() -> None:
    source = VERDICT_SOURCE.replace(
        'one_of "ship", "hold"',
        'matches "ship|hold"\n          max_chars 4\n          one_of "ship", "hold"',
    )
    program = parse_program(source)
    client = ScriptedClient([' "Ship". '])
    result = run_program(program, {"change": "x"}, llm_client=client)
    assert result.output == "ship"
    assert compute_metrics(result.trace, status="completed").contract_violations == 0


def test_dry_run_resolves_one_of_via_route_protocol() -> None:
    program = parse_program(VERDICT_SOURCE)
    result = run_program(program, {"change": "x"}, llm_client=DryRunClient())
    assert result.output == "ship"


# ───────── probe fold ─────────


def test_probe_report_sums_contract_violations() -> None:
    program = parse_program(VERDICT_SOURCE)
    runs = []
    for script in (["ship"], ["nope", "hold"]):
        result = run_program(program, {"change": "x"}, llm_client=ScriptedClient(script))
        runs.append(
            ProbeRunData(
                status="completed",
                output=result.output,
                step_outputs=result.step_outputs,
                metrics=compute_metrics(result.trace, status="completed"),
            )
        )
    report = probe_report(program, runs)
    assert report.contract_violations == 1
    assert report.route_violations == 0
