"""Replay & fork tests — a recorded run reproduces itself in a fresh directory (no API key)."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

import loom.models as models
from loom.config import Config
from loom.models import ModelResponse, ToolCall
from loom.orchestrator import Orchestrator
from loom.replay import ReplayExhausted, SequenceCompleter, load_trace


def _write_call(path: str, content: str, call_id: str = "c1") -> ModelResponse:
    args = json.dumps({"path": path, "content": content})
    return ModelResponse(
        text="",
        tool_calls=[ToolCall(id=call_id, name="write_file", arguments=args)],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "write_file", "arguments": args}}
            ],
        },
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        cost=0.0001,
        model="fake",
    )


def _final(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 12, "completion_tokens": 4},
        cost=0.0001,
        model="fake",
    )


def _quiet() -> Console:
    return Console(file=io.StringIO())


def test_full_replay_reproduces_run_including_critic(tmp_path, monkeypatch):
    original = tmp_path / "original"
    replayed = tmp_path / "replayed"
    original.mkdir()
    replayed.mkdir()

    # ── live run (scripted), critic enabled: write -> final -> critic APPROVE ──
    live = SequenceCompleter([_write_call("out.txt", "woven by loom"), _final("Done."), _final("APPROVE")])
    monkeypatch.chdir(original)
    agent = Orchestrator(
        Config(workdir=str(original), auto_approve=True, critic_enabled=True), _quiet(), completer=live
    )
    agent.run("create out.txt")
    assert (original / "out.txt").read_text(encoding="utf-8") == "woven by loom"
    trace_path = agent.recorder.path

    # ── replay from the trace into a fresh directory ──
    goal, recorded = load_trace(trace_path)
    assert goal == "create out.txt"
    assert len(recorded) == 3  # implementer x2 + critic, interleaved in call order

    monkeypatch.chdir(replayed)
    agent2 = Orchestrator(
        Config(workdir=str(replayed), auto_approve=True, critic_enabled=True),
        _quiet(),
        completer=SequenceCompleter(recorded),
    )
    agent2.run(goal)
    assert (replayed / "out.txt").read_text(encoding="utf-8") == "woven by loom"


def _task_call(description: str, agent_type: str, call_id: str = "t1") -> ModelResponse:
    args = json.dumps({"description": description, "agent_type": agent_type})
    return ModelResponse(
        text="",
        tool_calls=[ToolCall(id=call_id, name="task", arguments=args)],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "task", "arguments": args}}
            ],
        },
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        cost=0.0001,
        model="fake",
    )


def test_replay_reproduces_run_with_subagent(tmp_path, monkeypatch):
    original = tmp_path / "original"
    replayed = tmp_path / "replayed"
    original.mkdir()
    replayed.mkdir()

    # ── live run (scripted): parent delegates -> child writes -> child final -> parent final ──
    live = SequenceCompleter(
        [
            _task_call("create sub.txt with 'from sub'", "general"),
            _write_call("sub.txt", "from sub", call_id="c2"),
            _final("Wrote sub.txt."),  # child's report
            _final("Done."),           # parent wraps up
        ]
    )
    monkeypatch.chdir(original)
    agent = Orchestrator(
        Config(workdir=str(original), auto_approve=True, critic_enabled=False), _quiet(), completer=live
    )
    agent.run("delegate the write")
    assert (original / "sub.txt").read_text(encoding="utf-8") == "from sub"
    trace_path = agent.recorder.path

    # the child recorded into the PARENT's trace, tagged, in exact call order
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    model_events = [e for e in events if e["type"] == "model"]
    assert [e.get("subagent") for e in model_events] == [None, "general", "general", None]
    assert all(e.get("depth") == 1 for e in model_events if e.get("subagent"))

    # ── replay from the single trace into a fresh directory ──
    goal, recorded = load_trace(trace_path)
    assert goal == "delegate the write"
    assert len(recorded) == 4  # parent + child calls, interleaved

    monkeypatch.chdir(replayed)
    agent2 = Orchestrator(
        Config(workdir=str(replayed), auto_approve=True, critic_enabled=False),
        _quiet(),
        completer=SequenceCompleter(recorded),
    )
    agent2.run(goal)
    assert (replayed / "sub.txt").read_text(encoding="utf-8") == "from sub"
    assert any("[subagent:general report]" in str(m.get("content", "")) for m in agent2.messages
               if m.get("role") == "tool")


def test_fork_hands_over_to_live_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(models, "complete", lambda *a, **k: _final("continued live"))

    completer = SequenceCompleter([_write_call("a.txt", "v1")], live_after=True)
    agent = Orchestrator(
        Config(workdir=str(tmp_path), auto_approve=True, critic_enabled=False), _quiet(), completer=completer
    )
    agent.run("do the thing")

    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "v1"  # recorded part replayed
    assert completer.went_live  # then the live model took over
    assert any("continued live" in str(m.get("content", "")) for m in agent.messages)


def test_exhausted_without_fork_raises():
    completer = SequenceCompleter([])
    with pytest.raises(ReplayExhausted):
        completer("m", [], None)


def test_load_trace_rejects_traces_without_goal(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text(json.dumps({"type": "tool", "step": 1}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_trace(p)
