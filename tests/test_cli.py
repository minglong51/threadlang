from __future__ import annotations

import shlex
import sqlite3
import sys
from pathlib import Path
from typing import NoReturn

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang import cli  # noqa: E402
from threadlang.ir import canonical_ir_bytes, compile_program  # noqa: E402
from threadlang.llm import LLMError  # noqa: E402
from threadlang.parser import parse_program  # noqa: E402
from threadlang.store import RunStore  # noqa: E402


def _invoke_cli(monkeypatch: pytest.MonkeyPatch, *args: object) -> int:
    monkeypatch.setattr(sys, "argv", ["threadlang", *(str(arg) for arg in args)])
    return cli.main()


def _unavailable(*args: object, **kwargs: object) -> NoReturn:
    raise LLMError("provider unavailable")


@pytest.mark.parametrize(
    "source_text",
    [
        'thread T { context {} emit llm "m" { "secret" } }',
        'thread T { context {} steps { step choose { route "m" { "pick" on "yes" -> end else -> end } } } emit text { steps.choose.output } }',
        'thread T { context {} steps { step act { agent "m" { tools [ echo ] max_iters 2 "act" } } } emit text { steps.act.output } }',
    ],
)
def test_anthropic_unavailable_fails_closed(
    source_text: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "model.thread"
    source.write_text(source_text, encoding="utf-8")
    monkeypatch.setattr(cli, "AnthropicClient", _unavailable)

    assert _invoke_cli(monkeypatch, source) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "provider unavailable" in captured.err
    assert "dry-run" not in captured.out
    assert "falling back" not in captured.err


def test_unused_provider_configuration_is_lazy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "text.thread"
    source.write_text('thread T { context {} emit text { "ok" } }', encoding="utf-8")
    calls = 0

    def unexpected_provider(*args: object, **kwargs: object) -> NoReturn:
        nonlocal calls
        calls += 1
        raise AssertionError("provider should not be constructed")

    monkeypatch.setattr(cli, "OpenAICompatClient", unexpected_provider)

    assert (
        _invoke_cli(
            monkeypatch,
            source,
            "--backend",
            "openai",
            "--base-url",
            "not-a-url",
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == "ok\n"
    assert captured.err == ""
    assert calls == 0


def test_completed_replay_does_not_require_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "model.thread"
    source.write_text('thread T { context {} emit llm "m" { "prompt" } }', encoding="utf-8")
    store_path = tmp_path / "runs.db"

    assert _invoke_cli(monkeypatch, source, "--dry-run", "--store", store_path) == 0
    first = capsys.readouterr()
    store = RunStore(str(store_path))
    try:
        run_id = store.list_runs()[0].id
    finally:
        store.close()

    def unexpected_provider(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("provider should not be constructed during replay")

    monkeypatch.setattr(cli, "AnthropicClient", unexpected_provider)
    assert _invoke_cli(monkeypatch, source, "--store", store_path, "--resume", run_id) == 0
    replay = capsys.readouterr()
    assert replay.out == first.out
    assert "provider unavailable" not in replay.err


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_cli_rejects_non_finite_timeout(
    timeout: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "text.thread"
    source.write_text('thread T { context {} emit text { "ok" } }', encoding="utf-8")

    assert _invoke_cli(monkeypatch, source, "--dry-run", f"--timeout={timeout}") == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "must be positive" in captured.err


@pytest.mark.parametrize("bad_input", ["broken", "=value"])
def test_malformed_input_precedes_provider_setup(
    bad_input: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "model.thread"
    source.write_text('thread T { context {} emit llm "m" { "prompt" } }', encoding="utf-8")
    store_path = tmp_path / "runs.db"
    calls = 0

    def counted_provider(*args: object, **kwargs: object) -> NoReturn:
        nonlocal calls
        calls += 1
        raise LLMError("provider should not be constructed")

    monkeypatch.setattr(cli, "AnthropicClient", counted_provider)
    assert (
        _invoke_cli(
            monkeypatch,
            source,
            "--input",
            bad_input,
            "--store",
            store_path,
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "Invalid --input format" in captured.err
    assert calls == 0
    assert not store_path.exists()


def test_deterministic_durable_failure_has_no_resume_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "input.thread"
    source.write_text("thread T { context {} emit text { inputs.required } }", encoding="utf-8")
    store_path = tmp_path / "runs.db"
    monkeypatch.setattr(cli, "AnthropicClient", _unavailable)

    assert _invoke_cli(monkeypatch, source, "--store", store_path) == 1
    captured = capsys.readouterr()
    assert "Missing input value: required" in captured.err
    assert "resume with:" not in captured.err
    store = RunStore(str(store_path))
    try:
        assert store.list_runs()[0].status == "failed"
    finally:
        store.close()


def test_cli_formats_store_open_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "text.thread"
    source.write_text('thread T { context {} emit text { "ok" } }', encoding="utf-8")
    store_path = tmp_path / "missing" / "runs.db"
    monkeypatch.setattr(cli, "AnthropicClient", _unavailable)

    assert _invoke_cli(monkeypatch, source, "--store", store_path) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert "Traceback" not in captured.err


def test_cli_closes_store_when_durable_run_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "text.thread"
    source.write_text('thread T { context {} emit text { "ok" } }', encoding="utf-8")
    stores: list[RunStore] = []

    class TrackingStore(RunStore):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    def open_store(path: str) -> TrackingStore:
        store = TrackingStore(path)
        stores.append(store)
        return store

    def fail_run(*args: object, **kwargs: object) -> NoReturn:
        raise sqlite3.OperationalError("write failed")

    monkeypatch.setattr(cli, "RunStore", open_store)
    monkeypatch.setattr(cli, "run_durable", fail_run)

    assert _invoke_cli(monkeypatch, source, "--store", tmp_path / "runs.db") == 2
    assert len(stores) == 1
    assert isinstance(stores[0], TrackingStore) and stores[0].closed
    assert "error: write failed" in capsys.readouterr().err


def test_probe_closes_store_when_report_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "text.thread"
    source.write_text('thread T { context {} emit text { "ok" } }', encoding="utf-8")
    stores: list[RunStore] = []

    class TrackingStore(RunStore):
        closed = False

        def close(self) -> None:
            self.closed = True
            super().close()

    def open_store(path: str) -> TrackingStore:
        store = TrackingStore(path)
        stores.append(store)
        return store

    def fail_report(*args: object, **kwargs: object) -> NoReturn:
        raise OSError("report failed")

    monkeypatch.setattr(cli, "RunStore", open_store)
    monkeypatch.setattr(cli, "probe_report", fail_report)

    assert (
        _invoke_cli(
            monkeypatch,
            source,
            "--dry-run",
            "--store",
            tmp_path / "runs.db",
            "--probe",
            "1",
        )
        == 2
    )
    assert len(stores) == 1
    assert isinstance(stores[0], TrackingStore) and stores[0].closed
    assert "error: report failed" in capsys.readouterr().err


def test_provider_failure_resume_hint_round_trips_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    ir_path = Path("-workflow with ' quote.ir.json")
    workflow = compile_program(
        parse_program('thread T { context {} emit llm "m" { "prompt:" + inputs.value } }')
    )
    ir_path.write_bytes(canonical_ir_bytes(workflow))
    store_dir = Path("state with ' quote")
    store_dir.mkdir()
    store_path = store_dir / "runs.db"
    base_url = "http://127.0.0.1:9/v1?x=1&y=2"

    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, model: str, prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                raise LLMError("temporary provider failure")
            return f"recovered:{prompt}"

    client = FlakyClient()
    monkeypatch.setattr(cli, "OpenAICompatClient", lambda **kwargs: client)
    assert (
        _invoke_cli(
            monkeypatch,
            "--backend",
            "openai",
            "--base-url",
            base_url,
            "--max-tokens",
            "7",
            "--timeout",
            "2.5",
            "--store",
            store_path,
            "--input",
            "value=kept",
            "--from-ir",
            "--",
            ir_path,
        )
        == 1
    )
    failed = capsys.readouterr()
    hint = next(
        line.split("resume with:", 1)[1].strip()
        for line in failed.err.splitlines()
        if "resume with:" in line
    )
    argv = shlex.split(hint)
    assert argv[0] == "threadlang"
    assert argv[argv.index("--store") + 1] == str(store_path)
    assert argv[argv.index("--backend") + 1] == "openai"
    assert argv[argv.index("--max-tokens") + 1] == "7"
    assert argv[argv.index("--timeout") + 1] == "2.5"
    assert argv[argv.index("--base-url") + 1] == base_url
    assert "--from-ir" in argv
    assert "--input" not in argv
    assert argv[-2:] == ["--", str(ir_path)]

    assert _invoke_cli(monkeypatch, *argv[1:]) == 0
    resumed = capsys.readouterr()
    assert resumed.out == "recovered:prompt:kept\n"
    assert client.calls == 2
    store = RunStore(str(store_path))
    try:
        assert store.list_runs()[0].status == "completed"
    finally:
        store.close()
