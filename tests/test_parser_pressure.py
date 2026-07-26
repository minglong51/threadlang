"""Adversarial parser cases that the pre-v0.12 regex parser misparsed."""

from __future__ import annotations

from pathlib import Path
import sys
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.ast import AgentStep, StringLiteral
from threadlang.parser import ParseError, parse_program
from threadlang.policy import MAX_AGENT_ITERS, MAX_SOURCE_BYTES
from threadlang.runtime import RuntimeError as TLRuntimeError, run_program


class _ResponseClient:
    def __init__(self, response: str) -> None:
        self.response = response

    def complete(self, model: str, prompt: str) -> str:
        return self.response


def test_plus_and_braces_inside_strings_are_data() -> None:
    program = parse_program(
        'thread T { context { brace = "{" } steps {} '
        'emit text { "what is 2+2? " + context.brace } }'
    )
    literal = program.emit.expression.terms[0]
    assert literal == StringLiteral(value="what is 2+2? ")
    assert program.context.assignments[0].value == "{"


def test_directive_words_inside_prompt_do_not_change_agent_policy() -> None:
    program = parse_program(
        'thread T { context {} steps { step a { agent "m" { '
        '"Explain max_iters 999 and then -> end" } } } '
        "emit text { steps.a.output } }"
    )
    step = program.steps.steps[0]
    assert isinstance(step, AgentStep)
    assert step.max_iters == 6
    assert step.next_target is None
    assert step.prompt.terms == [StringLiteral(value="Explain max_iters 999 and then -> end")]


def test_unknown_step_syntax_is_not_silently_dropped() -> None:
    source = (
        'thread T { context {} steps { sttep bad { llm "m" { "x" } } '
        'step ok { llm "m" { "ok" } } } emit text { steps.ok.output } }'
    )
    with pytest.raises(ParseError, match="Expected 'step' declaration"):
        parse_program(source)


def test_escaped_strings_and_comments() -> None:
    program = parse_program(
        r"""
        # leading comment
        thread T {
          context { pattern = "\d+" } // inline comment
          steps {}
          emit text { "line1\nline2: \"quoted\" \\" + context.pattern }
        }
        """
    )
    assert program.context.assignments[0].value == r"\d+"
    assert program.emit.expression.terms[0] == StringLiteral(value='line1\nline2: "quoted" \\')


def test_unknown_top_level_and_trailing_tokens_fail() -> None:
    with pytest.raises(ParseError, match="Expected 'emit'"):
        parse_program('thread T { context {} mystery {} emit text { "x" } }')
    with pytest.raises(ParseError, match="end of source"):
        parse_program('thread T { context {} emit text { "x" } } garbage')


def test_unknown_context_and_step_references_fail_at_parse_time() -> None:
    with pytest.raises(ParseError, match="unknown context value 'missing'"):
        parse_program("thread T { context {} emit text { context.missing } }")
    with pytest.raises(ParseError, match="unknown step 'missing'"):
        parse_program("thread T { context {} emit text { steps.missing.output } }")


def test_agent_iteration_ceiling() -> None:
    source = (
        'thread T { context {} steps { step a { agent "m" { '
        f'max_iters {MAX_AGENT_ITERS + 1} "x" '
        "} } } emit text { steps.a.output } }"
    )
    with pytest.raises(ParseError, match=f"between 1 and {MAX_AGENT_ITERS}"):
        parse_program(source)


def test_source_size_ceiling() -> None:
    source = 'thread T { context {} emit text { "' + ("x" * MAX_SOURCE_BYTES) + '" } }'
    with pytest.raises(ParseError, match="Source exceeds"):
        parse_program(source)


def test_route_labels_are_unique_case_insensitively() -> None:
    source = """
    thread T {
      context {}
      steps {
        step r { route "m" { "pick" on "Yes" -> end on "yes" -> end } }
      }
      emit text { steps.r.output }
    }
    """
    with pytest.raises(ParseError, match="duplicate route label"):
        parse_program(source)


def test_catastrophic_regex_is_killed_by_runtime_deadline() -> None:
    source = r"""
    thread T {
      context {}
      steps {
        step check {
          llm "m" {
            "validate"
            expect { matches "(a+)+$" }
          }
        }
      }
      emit text { steps.check.output }
    }
    """
    started = time.monotonic()
    with pytest.raises(TLRuntimeError, match="regex contract exceeded"):
        run_program(
            parse_program(source),
            inputs={},
            llm_client=_ResponseClient("a" * 50_000 + "!"),
        )
    assert time.monotonic() - started < 3.0
