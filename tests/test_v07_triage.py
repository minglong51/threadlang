"""v0.7 vertical-slice (support-triage) tests.

The app is one concrete product on the whole stack. These guard the parts that
are the app's own — the domain tools and the program — plus that it runs end to
end on the platform it sits on:

  1. The custom tools (classify_priority, search_kb) are deterministic + correct.
  2. The registry extends the built-ins with them.
  3. The bundled program parses and uses the agent + llm steps.
  4. It runs end to end under dry-run, and the agent step actually calls a
     custom tool (the trace proves it).
  5. It runs on the durable + queued path (enqueue -> worker pool -> completed),
     with the app's registry wired into the workers.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.apps.support_triage.app import load_program, main as app_main  # noqa: E402
from threadlang.apps.support_triage.tools import build_registry  # noqa: E402
from threadlang.ast import AgentStep, Step  # noqa: E402
from threadlang.control import WorkerPool  # noqa: E402
from threadlang.llm import DryRunClient  # noqa: E402
from threadlang.runtime import run_program  # noqa: E402
from threadlang.store import RunStore  # noqa: E402


def _run(tool, **args) -> str:
    return tool.run(args)


def test_classify_priority_rules() -> None:
    reg = build_registry()
    prio = reg.get("classify_priority")
    assert _run(prio, text="The whole site is down with 500 errors").startswith("P0")
    assert _run(prio, text="I was overcharged on my last invoice").startswith("P1")
    assert _run(prio, text="How do I change my display name?").startswith("P2")
    assert _run(prio, text="").startswith("error")


def test_search_kb_finds_relevant_article() -> None:
    reg = build_registry()
    search = reg.get("search_kb")
    assert "kb-001" in _run(search, query="I can't log in and need to reset my password")
    assert "kb-003" in _run(search, query="getting 429 rate limit on the api")
    assert _run(search, query="zzzz nonsense xyzzy") == "no matching articles"


def test_registry_extends_builtins() -> None:
    names = set(build_registry().names())
    assert {"echo", "calculator", "classify_priority", "search_kb"} <= names


def test_program_parses_with_agent_then_llm() -> None:
    program = load_program()
    assert program.thread_name == "SupportTriage"
    kinds = [type(s) for s in program.steps.steps]
    assert kinds == [AgentStep, Step]  # investigate (agent) -> draft (llm)
    investigate = program.steps.steps[0]
    assert set(investigate.tools) == {"classify_priority", "search_kb"}


def test_end_to_end_dry_run_calls_a_custom_tool() -> None:
    program = load_program()
    result = run_program(
        program,
        {"ticket": "Everything is down, urgent, 500 errors everywhere"},
        DryRunClient(),
        tools=build_registry(),
    )
    assert result.output  # produced a drafted reply
    # The agent step must have actually invoked one of the app's tools.
    tool_calls = [e for e in result.trace if e.phase == "agent" and e.message.startswith("Tool '")]
    assert any(e.data.get("tool") in {"classify_priority", "search_kb"} for e in tool_calls), (
        "the agent step should have called a custom triage tool"
    )


def test_durable_queued_path(tmp_path) -> None:
    db = str(tmp_path / "triage.db")
    program = load_program()
    store = RunStore(db)
    try:
        run_id = store.enqueue_run(
            program.thread_name,
            (REPO_ROOT / "src/threadlang/apps/support_triage/triage.thread").read_text(),
            {"ticket": "I need a refund for a duplicate charge on my invoice"},
        )
        # Drain the queue synchronously with the app's registry wired in.
        pool = WorkerPool(db, llm_client=DryRunClient(), tools=build_registry())
        processed = pool.drain(store)
        assert processed == 1
        record = store.get_run(run_id)
        assert record is not None and record.status == "completed"
        assert record.output
        # The persisted trace includes the agent step and a custom-tool call.
        events = store.load_events(run_id)
        assert any(
            e.phase == "agent" and e.data.get("tool") in {"classify_priority", "search_kb"}
            for e in events
        )
    finally:
        store.close()


def test_run_formats_store_open_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store_path = tmp_path / "missing" / "runs.db"
    assert app_main(["run", "--ticket", "test", "--store", str(store_path), "--dry-run"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_rejects_non_finite_timeout(timeout: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as raised:
        app_main(
            [
                "run",
                "--ticket",
                "x",
                "--store",
                str(tmp_path / "runs.db"),
                "--dry-run",
                f"--timeout={timeout}",
            ]
        )
    assert raised.value.code == 2


def test_serve_validates_bounds_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    def unexpected_provider(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr("threadlang.apps.support_triage.app._make_client", unexpected_provider)
    with pytest.raises(SystemExit) as raised:
        app_main(
            [
                "serve",
                "--store",
                "x.db",
                "--workers",
                "0",
                "--backend",
                "openai",
                "--base-url",
                "not-a-url",
            ]
        )
    assert raised.value.code == 2
    assert calls == 0
    captured = capsys.readouterr()
    assert "workers must be >= 1" in captured.err
    assert "base URL" not in captured.err
