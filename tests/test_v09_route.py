"""v0.9 routing tests.

What these guard:
  1. The step graph is a forward-only DAG — the parser rejects backward and
     unknown jump targets, so a step can never run twice and checkpoints stay
     correct.
  2. Arm dispatch is deterministic code — the model only picks a label, under
     an output contract with one retry, then `else ->` or a loud failure.
  3. Resume re-derives a route's jump from the stored label with NO model call.

All offline: the dry-run client picks the first arm deterministically; scripted
clients exercise contract violations and branch selection without an API key.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # type: ignore  # noqa: E402

from threadlang.ast import RouteStep, Step  # noqa: E402
from threadlang.llm import DryRunClient  # noqa: E402
from threadlang.metrics import compute_metrics  # noqa: E402
from threadlang.parser import ParseError, parse_program  # noqa: E402
from threadlang.runtime import RuntimeError as TLRuntimeError, run_program  # noqa: E402
from threadlang.store import RunStore, run_durable  # noqa: E402


def _route_source(else_clause: str = 'else -> draft') -> str:
    return f"""
    thread RouteDemo {{
      context {{}}
      steps {{
        step classify {{
          route "test-model" {{
            "Handle this request: " + inputs.task
            on "math" -> solve
            on "writing" -> draft
            {else_clause}
          }}
        }}
        step solve {{
          llm "test-model" {{
            "Solve: " + inputs.task
            then -> end
          }}
        }}
        step draft {{
          llm "test-model" {{ "Draft: " + inputs.task }}
        }}
      }}
      emit text {{ steps.solve.output? + steps.draft.output? }}
    }}
    """


class ScriptedClient:
    """Returns canned responses to `complete`, in order. No `route` method, so
    it exercises the plain-complete routing path and the output contract."""

    def __init__(self, responses: Sequence[str]) -> None:
        self._responses = list(responses)
        self.prompts: List[str] = []

    def complete(self, model: str, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            raise AssertionError("ScriptedClient exhausted — unexpected model call")
        return self._responses.pop(0)


class ExplodingClient:
    """Fails on any model call — proves a code path made none."""

    def complete(self, model: str, prompt: str) -> str:
        raise AssertionError("model was called")


# ───────── parsing ─────────


def test_parse_route_step_shape() -> None:
    program = parse_program(_route_source())
    classify = program.steps.steps[0]
    assert isinstance(classify, RouteStep)
    assert [(a.label, a.target) for a in classify.arms] == [
        ("math", "solve"),
        ("writing", "draft"),
    ]
    assert classify.else_target == "draft"
    solve = program.steps.steps[1]
    assert isinstance(solve, Step)
    assert solve.next_target == "end"


def test_parse_rejects_backward_target() -> None:
    source = """
    thread T {
      context {}
      steps {
        step a { llm "m" { "x" } }
        step b { route "m" { "pick" on "back" -> a } }
      }
      emit text { inputs.x }
    }
    """
    with pytest.raises(ParseError, match="jumps backward"):
        parse_program(source)


def test_parse_rejects_unknown_target() -> None:
    source = """
    thread T {
      context {}
      steps {
        step a { llm "m" { "x" then -> nowhere } }
      }
      emit text { inputs.x }
    }
    """
    with pytest.raises(ParseError, match="unknown step"):
        parse_program(source)


def test_parse_rejects_duplicate_labels() -> None:
    source = """
    thread T {
      context {}
      steps {
        step r { route "m" { "pick" on "x" -> a on "x" -> end } }
        step a { llm "m" { "y" } }
      }
      emit text { inputs.x }
    }
    """
    with pytest.raises(ParseError, match="duplicate route label"):
        parse_program(source)


def test_parse_rejects_reserved_end_name() -> None:
    source = """
    thread T {
      context {}
      steps {
        step end { llm "m" { "x" } }
      }
      emit text { inputs.x }
    }
    """
    with pytest.raises(ParseError, match="reserved"):
        parse_program(source)


def test_parse_route_requires_an_arm() -> None:
    source = """
    thread T {
      context {}
      steps {
        step r { route "m" { "pick" } }
      }
      emit text { inputs.x }
    }
    """
    with pytest.raises(ParseError, match="at least one arm"):
        parse_program(source)


# ───────── execution ─────────


def test_dry_run_takes_first_arm_and_skips_other_branch() -> None:
    program = parse_program(_route_source())
    result = run_program(program, inputs={"task": "2+2"}, llm_client=DryRunClient())
    assert result.step_outputs["classify"] == "math"
    assert "solve" in result.step_outputs
    assert "draft" not in result.step_outputs  # then -> end skipped it
    assert result.output.startswith("[dry-run:test-model] Solve:")


def test_scripted_label_takes_matching_branch() -> None:
    client = ScriptedClient(["writing", "drafted!"])
    program = parse_program(_route_source())
    result = run_program(program, inputs={"task": "poem"}, llm_client=client)
    assert result.step_outputs["classify"] == "writing"
    assert "solve" not in result.step_outputs
    assert result.output == "drafted!"
    # The routing prompt carries the output contract rendered from the arms.
    assert "exactly one of: math, writing" in client.prompts[0]


def test_label_normalization_tolerates_wrapping_noise() -> None:
    client = ScriptedClient([' "Math". ', "solved"])
    program = parse_program(_route_source())
    result = run_program(program, inputs={"task": "2+2"}, llm_client=client)
    assert result.step_outputs["classify"] == "math"


def test_violation_retries_once_with_feedback_then_matches() -> None:
    client = ScriptedClient(["banana", "math", "solved"])
    program = parse_program(_route_source())
    result = run_program(program, inputs={"task": "2+2"}, llm_client=client)
    assert result.step_outputs["classify"] == "math"
    assert "was not one of the allowed labels" in client.prompts[1]
    rejected = [e for e in result.trace if "output rejected" in e.message]
    assert len(rejected) == 1


def test_double_violation_falls_to_else() -> None:
    client = ScriptedClient(["banana", "pear", "drafted"])
    program = parse_program(_route_source())
    result = run_program(program, inputs={"task": "??"}, llm_client=client)
    assert result.step_outputs["classify"] == "pear"
    assert result.output == "drafted"


def test_double_violation_without_else_fails_loud() -> None:
    client = ScriptedClient(["banana", "pear"])
    program = parse_program(_route_source(else_clause=""))
    with pytest.raises(TLRuntimeError, match="matches no arm"):
        run_program(program, inputs={"task": "??"}, llm_client=client)


def test_skipped_step_ref_without_question_mark_fails() -> None:
    source = """
    thread T {
      context {}
      steps {
        step r { route "m" { "pick" on "a" -> a on "b" -> b } }
        step a { llm "m" { "x" then -> end } }
        step b { llm "m" { "y" } }
      }
      emit text { steps.b.output }
    }
    """
    program = parse_program(source)
    with pytest.raises(TLRuntimeError, match="before it ran"):
        run_program(program, inputs={}, llm_client=DryRunClient())


def test_route_is_deterministic_under_dry_run() -> None:
    program = parse_program(_route_source())
    first = run_program(program, inputs={"task": "2+2"}, llm_client=DryRunClient())
    second = run_program(program, inputs={"task": "2+2"}, llm_client=DryRunClient())
    assert [(e.phase, e.message, e.data) for e in first.trace] == [
        (e.phase, e.message, e.data) for e in second.trace
    ]


# ───────── durability ─────────


def test_resume_re_derives_route_jump_without_model_call(tmp_path: Path) -> None:
    program = parse_program(_route_source())
    store = RunStore(str(tmp_path / "runs.db"))

    # First attempt: route resolves to "writing", then the draft step dies.
    class DiesOnDraft:
        def complete(self, model: str, prompt: str) -> str:
            if prompt.startswith("Draft:"):
                raise ConnectionError("boom")
            return "writing"

    with pytest.raises(TLRuntimeError):
        run_durable(program, {"task": "poem"}, store, llm_client=DiesOnDraft())
    run_id = store.list_runs()[0].id
    assert store.get_run(run_id).status == "failed"
    assert store.load_step_outputs(run_id) == {"classify": "writing"}

    # Resume: the route's stored label re-derives the jump; the only model
    # call is the incomplete draft step.
    resumed = run_durable(
        program, {"task": "poem"}, store,
        llm_client=ScriptedClient(["drafted"]), run_id=run_id,
    )
    assert resumed.result.output == "drafted"
    assert store.get_run(run_id).status == "completed"
    decision = [
        e for e in resumed.result.trace
        if e.phase == "route" and " chose " in e.message
    ]
    assert decision and decision[0].data["resumed"] is True


def test_completed_route_run_replays_without_any_model_call(tmp_path: Path) -> None:
    program = parse_program(_route_source())
    store = RunStore(str(tmp_path / "runs.db"))
    first = run_durable(program, {"task": "2+2"}, store, llm_client=DryRunClient())
    replay = run_durable(
        program, {"task": "2+2"}, store,
        llm_client=ExplodingClient(), run_id=first.run_id,
    )
    assert replay.result.output == first.result.output


# ───────── metrics ─────────


def test_metrics_fold_counts_routes_and_violations() -> None:
    client = ScriptedClient(["banana", "math", "solved"])
    program = parse_program(_route_source())
    result = run_program(program, inputs={"task": "2+2"}, llm_client=client)
    metrics = compute_metrics(result.trace, status="completed")
    assert metrics.route_steps == 1
    assert metrics.route_violations == 1
    # Two routing attempts + one llm step = three model calls.
    assert metrics.model_calls == 3
    # classify + solve completed; draft never ran.
    assert metrics.steps_completed == 2
