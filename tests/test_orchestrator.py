"""End-to-end tests of the agent loop with a scripted fake model (no API key).

Covers the riskiest wiring: assistant-message shape, tool dispatch, tool_call_id threading,
chat persistence across turns, checkpoints/rewind, the critic loop, and escalation routing.
"""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

import loom.models as models
from loom.config import Config
from loom.models import ModelResponse, ToolCall
from loom.orchestrator import Orchestrator


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


def _script(monkeypatch, *responses):
    it = iter(responses)
    calls: list[str] = []

    def fake_complete(model, messages, tools, temperature=0.2):  # noqa: ARG001
        calls.append(model)
        return next(it)

    monkeypatch.setattr(models, "complete", fake_complete)
    return calls


def _agent(tmp_path, **overrides) -> Orchestrator:
    config = Config(workdir=str(tmp_path), auto_approve=True, **overrides)
    return Orchestrator(config, Console(file=io.StringIO()))


def test_loop_executes_tool_then_finishes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, _write_call("out.txt", "hello from loom"), _final("Done."))
    agent = _agent(tmp_path, critic_enabled=False)
    agent.run("create out.txt that says hello")

    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hello from loom"
    assert agent.total_cost == pytest.approx(0.0002)
    assert agent.baseline_cost > agent.total_cost  # the savings meter has a baseline
    assert agent.recorder.path.exists()


def test_chat_history_persists_across_turns(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, _final("First answer."), _final("Second answer."))
    agent = _agent(tmp_path, critic_enabled=False)
    agent.run("first question")
    agent.run("second question")

    contents = [m.get("content", "") for m in agent.messages]
    assert "first question" in contents and "second question" in contents
    assert "First answer." in contents  # earlier assistant turn still in history
    assert sum(1 for m in agent.messages if m["role"] == "system") == 1


def test_rewind_restores_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("original", encoding="utf-8")
    _script(monkeypatch, _write_call("a.txt", "overwritten"), _final("Done."))
    agent = _agent(tmp_path, critic_enabled=False)
    agent.run("overwrite a.txt")
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "overwritten"

    restored = agent.rewind()
    assert restored == ["a.txt"]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "original"


def test_rewind_deletes_files_created_by_the_turn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _script(monkeypatch, _write_call("new.txt", "brand new"), _final("Done."))
    agent = _agent(tmp_path, critic_enabled=False)
    agent.run("create new.txt")
    assert (tmp_path / "new.txt").exists()

    agent.rewind()
    assert not (tmp_path / "new.txt").exists()


def test_critic_rejects_then_approves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = _script(
        monkeypatch,
        _write_call("f.py", "def f(): pass"),
        _final("Done."),                       # implementer claims done -> critic runs
        _final("REVISE: f() should return 1"),  # critic verdict 1: reject
        _write_call("f.py", "def f(): return 1", call_id="c2"),
        _final("Fixed."),                      # implementer claims done again
        _final("APPROVE"),                     # critic verdict 2: approve
    )
    agent = _agent(
        tmp_path,
        critic_enabled=True,
        model="cheap-model",
        model_strong="strong-model",
        model_critic="critic-model",
    )
    agent.run("write f() that returns 1")

    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "def f(): return 1"
    assert calls[2] == "critic-model" and calls[5] == "critic-model"
    assert calls[3] == "strong-model"  # rejection escalated the implementer
    # The critic's feedback was fed back into the chat:
    assert any("critic review" in str(m.get("content", "")) for m in agent.messages)


def test_loop_stops_on_budget(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    expensive = _write_call("x.txt", "x")
    expensive.cost = 5.0
    monkeypatch.setattr(models, "complete", lambda *a, **k: expensive)

    agent = _agent(tmp_path, critic_enabled=False, max_cost_usd=1.0)
    agent.run("loop forever")
    assert agent.total_cost >= 1.0
