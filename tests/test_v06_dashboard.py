"""v0.6 dashboard tests.

The dashboard adds no execution surface — it renders persisted runs + trace as
HTML. These guard that:
  1. The run list shows each run and links to its detail page.
  2. The detail page shows status, output, and the trace timeline.
  3. Untrusted text (model output, trace data) is HTML-escaped — no injection.

Pure render functions, so no server or sockets needed.
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from threadlang.dashboard import render_run_detail, render_run_list  # noqa: E402
from threadlang.store import RunRecord  # noqa: E402
from threadlang.trace import TraceEvent  # noqa: E402


def _rec(**kw) -> RunRecord:
    base = dict(
        id="abc123def456",
        program_name="Demo",
        status="completed",
        inputs={"x": "1"},
        output="hello",
        error=None,
        source=None,
    )
    base.update(kw)
    return RunRecord(**base)  # type: ignore[arg-type]


def test_run_list_shows_runs_and_links() -> None:
    html = render_run_list([_rec(), _rec(id="zzz999", status="failed", output=None, error="boom")])
    assert "abc123def456"[:12] in html
    assert "/ui/runs/abc123def456" in html  # detail link
    assert "completed" in html and "failed" in html


def test_run_list_empty_state() -> None:
    html = render_run_list([])
    assert "No runs yet" in html


def test_run_list_refreshes_while_in_flight() -> None:
    running = render_run_list([_rec(status="running", output=None)])
    done = render_run_list([_rec(status="completed")])
    assert "http-equiv" in running, "a running list should auto-refresh"
    assert "http-equiv" not in done, "a settled list should not auto-refresh"


def test_run_detail_shows_status_output_and_timeline() -> None:
    events = [
        TraceEvent(phase="context", message="Context assignment", data={"name": "x"}),
        TraceEvent(phase="step", message="Step 'a' produced output", data={"output": "y"}),
    ]
    html = render_run_detail(_rec(), events)
    assert "abc123def456" in html
    assert "completed" in html
    assert "hello" in html  # output
    assert "Context assignment" in html
    assert "Step &#x27;a&#x27; produced output" in html  # escaped + present


def test_run_detail_escapes_untrusted_output() -> None:
    evil = "<script>alert(1)</script>"
    html = render_run_detail(_rec(output=evil), [])
    assert "<script>alert(1)</script>" not in html  # not rendered raw
    assert "&lt;script&gt;" in html  # escaped


def test_run_detail_escapes_trace_data() -> None:
    events = [TraceEvent(phase="step", message="x", data={"v": "</pre><script>x</script>"})]
    html = render_run_detail(_rec(), events)
    assert "<script>x</script>" not in html
    assert "&lt;" in html


def test_failed_run_detail_shows_error() -> None:
    html = render_run_detail(_rec(status="failed", output=None, error="kaboom"), [])
    assert "failed" in html
    assert "kaboom" in html
