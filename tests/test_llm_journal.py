"""LLM response journal tests — per-call checkpointing inside a step.

What these guard — the narrowed crash window the journal claims:
  1. A run that crashes mid-step replays the step's already-journaled model
     calls from the store on resume instead of re-calling the provider; at
     most the single in-flight call re-executes (asserted by counting calls).
  2. Replay is keyed by request fingerprint + per-attempt occurrence: a
     fingerprint the journal has never seen falls through to a live call, a
     fresh run always calls live, and identical requests keep distinct rows.
  3. The wrapper preserves the client's duck-typed capability surface and
     round-trips `AgentTurn` through serialization.
  4. `journal_llm=False` restores the plain step-level replay window.

All offline: scripted clients crash one specific call, so resume is exercised
deterministically without a network or an API key.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Dict, List, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # type: ignore  # noqa: E402

from threadlang.journal import JournaledLLMClient  # noqa: E402
from threadlang.llm import AgentTurn, DryRunClient, ToolCall  # noqa: E402
from threadlang.parser import parse_program  # noqa: E402
from threadlang.store import RunStore, run_durable  # noqa: E402
from threadlang.tools import ToolSpec  # noqa: E402

_ONE_STEP = """
thread Pipe {
  context {}
  steps { step a { llm "m1" { "A:" + inputs.x } } }
  emit text { steps.a.output }
}
"""

_AGENT_PIPE = """
thread Pipe {
  context {}
  steps {
    step a { llm "m1" { "A:" + inputs.x } }
    step b { agent "m2" { tools [ echo ] max_iters 4 "B:" + steps.a.output } }
  }
  emit text { steps.b.output }
}
"""


class _CountingCompleteClient:
    """A `complete`-only client that counts provider invocations and returns a
    distinct response per call, so a replay is detectable by value."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    def complete(self, model: str, prompt: str) -> str:
        self.calls.append(prompt)
        return f"response-{len(self.calls)}"


class _CrashyAgentClient:
    """Answers `complete` for step a; for the agent step, requests the echo
    tool on turns 1–2, raises on turn 3 while armed (the simulated crash), and
    answers with a final text once disarmed. Counts every provider call."""

    def __init__(self) -> None:
        self.complete_calls = 0
        self.agent_calls = 0
        self._armed = True

    def complete(self, model: str, prompt: str) -> str:
        self.complete_calls += 1
        return f"[{model}] {prompt}"

    def agent_step(
        self, model: str, messages: Sequence[Dict[str, object]], tools: Sequence[ToolSpec]
    ) -> AgentTurn:
        self.agent_calls += 1
        if len(messages) < 5:
            return AgentTurn(
                text="",
                tool_calls=(
                    ToolCall(id=f"call_{len(messages)}", name="echo", arguments={"text": "hi"}),
                ),
            )
        if self._armed:
            self._armed = False
            raise RuntimeError("simulated crash in agent step b")
        return AgentTurn(text="final answer", tool_calls=())


def _journal_rows(store: RunStore, run_id: str) -> int:
    row = store._conn.execute(
        "SELECT COUNT(*) AS c FROM llm_journal WHERE run_id = ?", (run_id,)
    ).fetchone()
    return row["c"]


def test_resume_replays_journaled_calls_within_the_interrupted_step(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_AGENT_PIPE)
    client = _CrashyAgentClient()

    # First attempt: step a completes and is checkpointed; the agent step's
    # turns 1–2 complete (and are journaled); turn 3 raises mid-step.
    with pytest.raises(Exception, match="simulated crash"):
        run_durable(program, {"x": "hi"}, store, llm_client=client)
    run_id = store.list_runs()[0].id
    assert set(store.load_step_outputs(run_id)) == {"a"}
    assert (client.complete_calls, client.agent_calls) == (1, 3)

    # Resume: step a is skipped from its checkpoint, turns 1–2 replay from the
    # journal (their requests hash identically — turn 2's request carries the
    # assistant/tool message history), and only turn 3 calls live.
    durable = run_durable(program, {"x": "hi"}, store, llm_client=client, run_id=run_id)
    assert durable.result.output == "final answer"
    assert store.get_run(run_id).status == "completed"  # type: ignore[union-attr]
    assert client.complete_calls == 1, "checkpointed step a must not re-call the provider"
    assert client.agent_calls == 4, "resume must re-execute only the in-flight call"
    # Journal: step a + turns 1–2 written pre-crash; turn 3 written on resume.
    assert _journal_rows(store, run_id) == 4
    store.close()


def test_replay_is_keyed_by_fingerprint_and_occurrence(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    run_id = store.create_run("Pipe", {"x": "hi"})
    client = _CountingCompleteClient()

    first = JournaledLLMClient(client, store, run_id)
    assert first.complete(model="m", prompt="p") == "response-1"
    assert first.complete(model="m", prompt="p") == "response-2"  # occurrence 1, live
    assert len(client.calls) == 2

    # A new wrapper over the same run_id is the resume shape: both occurrences
    # replay their own recorded responses without touching the provider.
    resumed = JournaledLLMClient(client, store, run_id)
    assert resumed.complete(model="m", prompt="p") == "response-1"
    assert resumed.complete(model="m", prompt="p") == "response-2"
    assert len(client.calls) == 2, "fingerprint-matched calls must replay from the journal"

    # A request the journal has never seen falls through to a live call.
    assert resumed.complete(model="m", prompt="changed") == "response-3"
    assert len(client.calls) == 3

    # A fresh run_id has an empty journal, so it always calls live.
    fresh = JournaledLLMClient(client, store, store.create_run("Pipe", {"x": "hi"}))
    assert fresh.complete(model="m", prompt="p") == "response-4"
    assert len(client.calls) == 4
    store.close()


def test_wrapper_mirrors_the_wrapped_client_capability_surface(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    run_id = store.create_run("Pipe", {})

    bare = JournaledLLMClient(_CountingCompleteClient(), store, run_id)
    assert getattr(bare, "route", None) is None
    assert getattr(bare, "agent_step", None) is None

    full = JournaledLLMClient(DryRunClient(), store, run_id)
    assert getattr(full, "route", None) is not None
    assert getattr(full, "agent_step", None) is not None
    store.close()


def test_route_replays_from_the_journal(tmp_path: Path) -> None:
    class _CountingRouteClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, model: str, prompt: str) -> str:
            raise AssertionError("route steps with a route-capable client never complete")

        def route(self, model: str, prompt: str, options: Sequence[str]) -> str:
            self.calls += 1
            return options[-1]

    store = RunStore(str(tmp_path / "runs.db"))
    run_id = store.create_run("Pipe", {})
    client = _CountingRouteClient()

    first = JournaledLLMClient(client, store, run_id)
    assert first.route(model="m", prompt="p", options=["a", "b"]) == "b"
    resumed = JournaledLLMClient(client, store, run_id)
    assert resumed.route(model="m", prompt="p", options=["a", "b"]) == "b"
    assert client.calls == 1
    # Options are part of the fingerprint: a different closed set calls live.
    assert resumed.route(model="m", prompt="p", options=["a", "c"]) == "c"
    assert client.calls == 2
    store.close()


def test_agent_step_round_trips_through_the_journal(tmp_path: Path) -> None:
    class _ScriptedAgentClient:
        def __init__(self) -> None:
            self.turns = 0

        def agent_step(
            self, model: str, messages: Sequence[Dict[str, object]], tools: Sequence[ToolSpec]
        ) -> AgentTurn:
            self.turns += 1
            return AgentTurn(
                text="done",
                tool_calls=(ToolCall(id="c1", name="echo", arguments={"text": "hi"}),),
            )

    store = RunStore(str(tmp_path / "runs.db"))
    run_id = store.create_run("Pipe", {})
    client = _ScriptedAgentClient()
    tools = [ToolSpec(name="echo", description="echo", parameters={"type": "object"})]
    messages: List[Dict[str, object]] = [{"role": "user", "content": "go"}]

    turn = JournaledLLMClient(client, store, run_id).agent_step(
        model="m", messages=messages, tools=tools
    )
    assert client.turns == 1

    replayed = JournaledLLMClient(client, store, run_id).agent_step(
        model="m", messages=messages, tools=tools
    )
    assert client.turns == 1, "the replayed turn must not call the provider"
    assert replayed == turn
    assert replayed.tool_calls[0].arguments == {"text": "hi"}
    store.close()


def test_journal_opt_out_restores_step_level_replay(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    program = parse_program(_AGENT_PIPE)
    client = _CrashyAgentClient()

    with pytest.raises(Exception, match="simulated crash"):
        run_durable(program, {"x": "hi"}, store, llm_client=client, journal_llm=False)
    run_id = store.list_runs()[0].id

    durable = run_durable(
        program, {"x": "hi"}, store, llm_client=client, run_id=run_id, journal_llm=False
    )
    assert durable.result.output == "final answer"
    assert client.complete_calls == 1
    assert client.agent_calls == 6, "opt-out re-executes every call of the interrupted step"
    assert _journal_rows(store, run_id) == 0
    store.close()


def test_terminal_prune_deletes_journal_rows(tmp_path: Path) -> None:
    store = RunStore(str(tmp_path / "runs.db"))
    durable = run_durable(parse_program(_ONE_STEP), {"x": "hi"}, store, llm_client=DryRunClient())
    assert _journal_rows(store, durable.run_id) == 1

    # Retention pruning removes the terminal run's journal along with its
    # events and checkpoints (explicit deletes, for pre-foreign-key stores).
    store.enqueue_run("Pipe", _ONE_STEP, {"x": "2"}, max_retained=0)
    assert store.get_run(durable.run_id) is None
    assert _journal_rows(store, durable.run_id) == 0
    store.close()
