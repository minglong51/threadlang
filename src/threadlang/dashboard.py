"""Observability dashboard (L5) — a read-only view of the trace.

The control plane (L4) already persists and serves every run's status and full
event stream. L5 adds no execution surface: it renders exactly that data as
HTML — a run list, and a per-run *timeline* of the `TraceEvent` stream (context
bindings, step calls, agent turns, tool calls, tool results). The trace has
been the durable record since v0.1; this is the layer that lets a human read it.

Server-rendered, inline CSS, no client framework and no build step — in keeping
with the project's zero-dependency promise. A run that is still `pending` or
`running` emits a meta-refresh so the timeline updates live as workers drive it.

These are pure functions (record/events in, HTML string out) so the rendering
is golden-testable without a server. All interpolated values are
`html.escape`d — model output and trace data are untrusted text.
"""

from __future__ import annotations

import html
import json
from typing import List, Optional

from .metrics import AggregateMetrics, RunMetrics, compute_metrics
from .store import RunRecord
from .trace import TraceEvent

_STATUS_COLOR = {
    "pending": "#9aa0a6",
    "running": "#1a73e8",
    "completed": "#188038",
    "failed": "#d93025",
}

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 0;
       background: #0f1115; color: #e6e6e6; }
a { color: #6ab0ff; text-decoration: none; }
a:hover { text-decoration: underline; }
header { padding: 16px 24px; border-bottom: 1px solid #23262d; }
header h1 { margin: 0; font-size: 16px; font-weight: 600; }
header .sub { color: #9aa0a6; font-size: 12px; }
main { padding: 24px; max-width: 980px; margin: 0 auto; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #23262d;
         vertical-align: top; }
th { color: #9aa0a6; font-weight: 500; font-size: 12px; text-transform: uppercase;
     letter-spacing: .04em; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 999px;
         font-size: 12px; color: #fff; }
.kv { margin: 4px 0; }
.kv .k { color: #9aa0a6; }
.event { border-left: 2px solid #2b2f37; padding: 6px 0 6px 14px; margin-left: 6px;
         position: relative; }
.event::before { content: ""; position: absolute; left: -5px; top: 12px;
         width: 8px; height: 8px; border-radius: 50%; background: #2b2f37; }
.event .phase { font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
         color: #9aa0a6; }
.event .msg { font-weight: 500; }
.event pre { margin: 4px 0 0; padding: 8px 10px; background: #15181e;
         border-radius: 6px; overflow-x: auto; font-size: 12.5px; color: #c8ccd4; }
.out { padding: 10px 12px; background: #15181e; border-radius: 6px;
       white-space: pre-wrap; }
.err { border-left: 3px solid #d93025; }
.muted { color: #9aa0a6; }
.metrics { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 4px; }
.metric { background: #15181e; border: 1px solid #23262d; border-radius: 8px;
          padding: 8px 12px; min-width: 96px; }
.metric .n { font-size: 18px; font-weight: 600; font-family: ui-monospace, Menlo, monospace; }
.metric .l { font-size: 11px; color: #9aa0a6; text-transform: uppercase; letter-spacing: .04em; }
.metric.warn .n { color: #f6a609; }
.section-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
          color: #9aa0a6; margin: 14px 0 2px; }
"""

# Phase → accent dot color on the timeline.
_PHASE_COLOR = {
    "context": "#9aa0a6",
    "step": "#1a73e8",
    "agent": "#a142f4",
    "route": "#f6a609",
    "contract": "#e8710a",
    "denial": "#d93025",
    "runtime": "#5f6368",
    "emit": "#188038",
}


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _page(title: str, body: str, *, refresh: bool = False) -> str:
    meta_refresh = '<meta http-equiv="refresh" content="1">' if refresh else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title>{meta_refresh}"
        f"<style>{_CSS}</style></head><body>"
        "<header><h1><a href='/'>ThreadLang</a> · control plane</h1>"
        "<div class='sub'>read-only run observability</div></header>"
        f"<main>{body}</main></body></html>"
    )


def _badge(status: str) -> str:
    color = _STATUS_COLOR.get(status, "#5f6368")
    return f"<span class='badge' style='background:{color}'>{_esc(status)}</span>"


def _metric_chip(label: str, value: object, *, warn: bool = False) -> str:
    cls = "metric warn" if warn else "metric"
    return (
        f"<div class='{cls}'><div class='n'>{_esc(value)}</div>"
        f"<div class='l'>{_esc(label)}</div></div>"
    )


def _fmt_ms(ms: Optional[float]) -> str:
    if ms is None:
        return "—"
    return f"{ms:.0f} ms" if ms < 1000 else f"{ms / 1000:.2f} s"


def _fmt_rate(rate: Optional[float]) -> str:
    return "—" if rate is None else f"{rate * 100:.0f}%"


def _run_metrics_panel(metrics: RunMetrics) -> str:
    """Per-run metric chips: the deterministic control-flow counts plus the
    observational latency/token signals, each derived from the trace."""
    chips = [
        _metric_chip("steps", metrics.steps_completed),
        _metric_chip("model calls", metrics.model_calls),
        _metric_chip("tool calls", metrics.tool_calls),
    ]
    if metrics.route_steps:
        chips.append(_metric_chip("routes", metrics.route_steps))
    if metrics.route_violations:
        chips.append(_metric_chip("route violations", metrics.route_violations, warn=True))
    if metrics.contract_violations:
        chips.append(_metric_chip("contract violations", metrics.contract_violations, warn=True))
    if metrics.tool_errors:
        chips.append(_metric_chip("tool errors", metrics.tool_errors, warn=True))
    if metrics.denials:
        chips.append(_metric_chip("denials", metrics.denials, warn=True))
    if metrics.resumed_steps:
        chips.append(_metric_chip("resumed", metrics.resumed_steps, warn=True))
    chips.append(_metric_chip("duration", _fmt_ms(metrics.duration_ms)))
    if metrics.total_tokens is not None:
        chips.append(_metric_chip("tokens", metrics.total_tokens))
    return "<div class='metrics'>" + "".join(chips) + "</div>"


def _aggregate_panel(agg: AggregateMetrics) -> str:
    """Fleet-of-runs summary chips shown atop the run list."""
    failed = agg.by_status.get("failed", 0)
    chips = [
        _metric_chip("runs", agg.total_runs),
        _metric_chip("success", _fmt_rate(agg.success_rate)),
        _metric_chip("failed", failed, warn=bool(failed)),
        _metric_chip("avg duration", _fmt_ms(agg.avg_duration_ms)),
        _metric_chip("model calls", agg.total_model_calls),
        _metric_chip("tool calls", agg.total_tool_calls),
    ]
    if agg.total_tool_errors:
        chips.append(_metric_chip("tool errors", agg.total_tool_errors, warn=True))
    if agg.total_denials:
        chips.append(_metric_chip("denials", agg.total_denials, warn=True))
    return "<div class='metrics'>" + "".join(chips) + "</div>"


def render_run_list(runs: List[RunRecord], aggregate: Optional[AggregateMetrics] = None) -> str:
    """The run list: every run with its status, newest first. When `aggregate`
    is supplied (the server passes `store.aggregate_metrics()`), a monitoring
    summary panel is rendered above the table."""
    if not runs:
        body = "<p class='muted'>No runs yet. POST one to <code>/runs</code>.</p>"
        return _page("ThreadLang runs", body)

    rows = []
    for r in runs:
        rows.append(
            "<tr>"
            f"<td><a class='mono' href='/ui/runs/{_esc(r.id)}'>{_esc(r.id[:12])}</a></td>"
            f"<td>{_esc(r.program_name)}</td>"
            f"<td>{_badge(r.status)}</td>"
            f"<td class='mono muted'>{_esc(_short(r.output or r.error or ''))}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Run</th><th>Program</th><th>Status</th>"
        "<th>Output / error</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )
    summary = _aggregate_panel(aggregate) if aggregate is not None else ""
    # Refresh while any run is still in flight, so the list updates live.
    refresh = any(r.status in ("pending", "running") for r in runs)
    return _page("ThreadLang runs", summary + table, refresh=refresh)


def render_run_detail(
    record: RunRecord,
    events: List[TraceEvent],
    metrics: Optional[RunMetrics] = None,
) -> str:
    """One run: header (status/inputs/output), a metrics panel, then the trace
    timeline. `metrics` is optional — the server passes `store.run_metrics()`
    (which includes latency); when omitted, metrics are derived from `events`
    alone (no duration, since `TraceEvent` carries no timestamp)."""
    if metrics is None:
        metrics = compute_metrics(events, status=record.status)
    head = [
        "<p><a href='/'>← all runs</a></p>",
        f"<h2 class='mono'>{_esc(record.id)}</h2>",
        f"<p>{_badge(record.status)} &nbsp; <span class='muted'>"
        f"{_esc(record.program_name)}</span></p>",
        _run_metrics_panel(metrics),
        f"<div class='kv'><span class='k'>inputs</span> "
        f"<code>{_esc(json.dumps(record.inputs))}</code></div>",
    ]
    if record.output is not None:
        head.append(
            f"<div class='kv'><span class='k'>output</span></div>"
            f"<div class='out'>{_esc(record.output)}</div>"
        )
    if record.error:
        head.append(
            f"<div class='kv'><span class='k'>error</span></div>"
            f"<div class='out err'>{_esc(record.error)}</div>"
        )

    timeline = ["<h3>Trace</h3>"]
    for event in events:
        dot = _PHASE_COLOR.get(event.phase, "#5f6368")
        cls = "event err" if event.phase == "denial" else "event"
        data = _esc(json.dumps(event.data, indent=2)) if event.data else ""
        pre = f"<pre>{data}</pre>" if data else ""
        timeline.append(
            f"<div class='{cls}' style='--c:{dot}'>"
            f"<div class='phase' style='color:{dot}'>{_esc(event.phase)}</div>"
            f"<div class='msg'>{_esc(event.message)}</div>{pre}</div>"
        )
    if not events:
        timeline.append("<p class='muted'>No events yet.</p>")

    refresh = record.status in ("pending", "running")
    return _page(f"Run {record.id[:8]}", "".join(head) + "".join(timeline), refresh=refresh)


def _short(text: str, limit: int = 80) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
