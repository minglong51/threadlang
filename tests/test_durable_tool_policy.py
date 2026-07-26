"""Durable execution safety contracts for custom tools."""

from __future__ import annotations

from pathlib import Path

import pytest

from threadlang.parser import parse_program
from threadlang.store import RunStore, run_durable
from threadlang.tools import FunctionTool, ToolRegistry, ToolSpec


SOURCE = """
thread Unsafe {
  context {}
  steps {
    step act {
      agent "m" {
        tools [ charge_card ]
        max_iters 2
        "charge"
      }
    }
  }
  emit text { steps.act.output }
}
"""


def test_durable_run_rejects_non_idempotent_side_effecting_tool(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        FunctionTool(
            spec=ToolSpec(
                name="charge_card",
                description="side effect",
                parameters={"type": "object"},
                side_effects=True,
                idempotent=False,
            ),
            _fn=lambda args: "charged",
        )
    )
    store = RunStore(str(tmp_path / "runs.db"))
    with pytest.raises(ValueError, match="non-idempotent side effects"):
        run_durable(parse_program(SOURCE), {}, store, tools=registry)
    assert store.list_runs() == []
    store.close()
