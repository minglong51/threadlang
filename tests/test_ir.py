from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from threadlang.ir import (
    IRCompileError,
    WorkflowIR,
    canonical_ir_bytes,
    compile_program,
    workflow_fingerprint,
)
from threadlang.parser import parse_program


REPO_ROOT = Path(__file__).resolve().parents[1]


_FULL_SOURCE = r"""
thread Full {
  context {
    role = "reviewer"
  }
  steps {
    step classify {
      route "router" {
        context.role + inputs.request
        on "use_agent" -> investigate
        on "draft" -> compose
        else -> compose
      }
    }
    step investigate {
      agent "agent-model" {
        tools [echo, calculator]
        max_iters 4
        "Investigate " + steps.classify.output + inputs.request
        then -> finish
      }
    }
    step compose {
      llm "writer" {
        "Write " + steps.investigate.output? + inputs.request
        expect {
          one_of "accept", "reject"
          matches "[a-z]+"
          matches ".{1,20}"
          max_chars 20
          nonempty
        }
        then -> finish
      }
    }
    step finish {
      llm "finalizer" { "Finish" }
    }
  }
  emit llm "emitter" {
    context.role + steps.compose.output? + steps.finish.output
  }
}
"""


def test_compile_minimal_program_has_explicit_versions_and_tags() -> None:
    ir = compile_program(parse_program('thread T { context {} emit text { "ok" } }'))

    assert ir.ir_version == "threadlang.ir/v1"
    assert ir.language_version == "threadlang/v0.12"
    assert ir.name == "T"
    assert ir.context == ()
    assert ir.steps == ()
    assert ir.emit.kind == "text"
    assert ir.emit.expression.terms[0].kind == "literal"
    assert ir.emit.expression.terms[0].value == "ok"


def test_compile_preserves_all_current_ast_semantics() -> None:
    ir = compile_program(parse_program(_FULL_SOURCE))

    assert ir.context[0].name == "role"
    assert ir.context[0].value == "reviewer"

    route, agent, llm, final = ir.steps
    assert route.kind == "route"
    assert route.model == "router"
    assert [(arm.label, arm.target) for arm in route.arms] == [
        ("use_agent", "investigate"),
        ("draft", "compose"),
    ]
    assert route.else_target == "compose"
    assert [term.kind for term in route.prompt.terms] == ["context_ref", "input_ref"]

    assert agent.kind == "agent"
    assert agent.tools == ("echo", "calculator")
    assert agent.max_iters == 4
    assert agent.next_target == "finish"
    assert [term.kind for term in agent.prompt.terms] == [
        "literal",
        "step_ref",
        "input_ref",
    ]

    assert llm.kind == "llm"
    assert llm.next_target == "finish"
    assert llm.prompt.terms[1].kind == "step_ref"
    assert llm.prompt.terms[1].optional is True
    assert [rule.kind for rule in llm.expect] == [
        "one_of",
        "matches",
        "matches",
        "max_chars",
        "nonempty",
    ]
    assert llm.expect[0].values == ("accept", "reject")
    assert llm.expect[1].pattern == "[a-z]+"
    assert llm.expect[3].limit == 20
    assert final.next_target is None

    assert ir.emit.kind == "llm"
    assert ir.emit.model == "emitter"
    assert [term.kind for term in ir.emit.expression.terms] == [
        "context_ref",
        "step_ref",
        "step_ref",
    ]


def test_compile_fails_closed_for_unsupported_ast_node() -> None:
    program = parse_program('thread T { context {} emit text { "ok" } }')
    invalid = replace(program, steps=replace(program.steps, steps=[object()]))

    with pytest.raises(IRCompileError, match="unsupported step node"):
        compile_program(invalid)  # type: ignore[arg-type]


def test_canonical_bytes_and_fingerprint_are_deterministic() -> None:
    program = parse_program(_FULL_SOURCE)
    first = compile_program(program)
    second = compile_program(program)

    first_bytes = canonical_ir_bytes(first)
    assert first_bytes == canonical_ir_bytes(second)
    assert json.loads(first_bytes)["ir_version"] == "threadlang.ir/v1"
    assert workflow_fingerprint(first) == workflow_fingerprint(second)
    assert workflow_fingerprint(first) == hashlib.sha256(first_bytes).hexdigest()


@pytest.mark.parametrize(
    "changed_source",
    [
        _FULL_SOURCE.replace('route "router"', 'route "other-router"', 1),
        _FULL_SOURCE.replace("context.role + inputs.request", '"changed" + inputs.request', 1),
        _FULL_SOURCE.replace('on "draft" -> compose', 'on "draft" -> finish', 1),
        _FULL_SOURCE.replace('matches "[a-z]+"', 'matches "[A-Z]+"', 1),
        _FULL_SOURCE.replace("tools [echo, calculator]", "tools [echo]", 1),
        _FULL_SOURCE.replace('emit llm "emitter"', 'emit llm "other-emitter"', 1),
    ],
)
def test_semantic_changes_change_fingerprint(changed_source: str) -> None:
    baseline = workflow_fingerprint(compile_program(parse_program(_FULL_SOURCE)))
    changed = workflow_fingerprint(compile_program(parse_program(changed_source)))
    assert changed != baseline


@pytest.mark.parametrize("example", sorted((REPO_ROOT / "examples").glob("*.thread")))
def test_all_examples_compile_to_canonical_ir(example: Path) -> None:
    ir = compile_program(parse_program(example.read_text()))
    payload = json.loads(canonical_ir_bytes(ir))
    digest = workflow_fingerprint(ir)

    assert payload["ir_version"] == "threadlang.ir/v1"
    assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_ir_public_api() -> None:
    from threadlang import (
        IRCompileError as PublicIRCompileError,
        WorkflowIR as PublicWorkflowIR,
        canonical_ir_bytes as public_canonical_ir_bytes,
        compile_program as public_compile_program,
        workflow_fingerprint as public_workflow_fingerprint,
    )

    assert PublicIRCompileError is IRCompileError
    assert PublicWorkflowIR is WorkflowIR
    assert public_compile_program is compile_program
    assert public_canonical_ir_bytes is canonical_ir_bytes
    assert public_workflow_fingerprint is workflow_fingerprint
