"""Subagents (`task` tool): delegation, shared checkpoints/permissions, depth guard."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

import loom.models as models
from loom.config import Config
from loom.models import ModelResponse, ToolCall
from loom.orchestrator import Orchestrator


def _tool_call(name: str, args: dict, call_id: str = "c1") -> ModelResponse:
    args_json = json.dumps(args)
    return ModelResponse(
        text="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args_json)],
        assistant_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": args_json}}
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
    monkeypatch.setattr(models, "complete", lambda *a, **k: next(it))


def _agent(tmp_path, **overrides) -> Orchestrator:
    config = Config(workdir=str(tmp_path), auto_approve=True, critic_enabled=False, **overrides)
    return Orchestrator(config, Console(file=io.StringIO()))


def test_task_tool_is_registered_on_the_main_agent(tmp_path):
    agent = _agent(tmp_path)
    names = [s["function"]["name"] for s in agent.engine.schemas()]
    assert "task" in names


def test_subagents_do_not_nest(tmp_path):
    child = Orchestrator(
        Config(workdir=str(tmp_path), auto_approve=True, agent_depth=1),
        Console(file=io.StringIO()),
    )
    names = [s["function"]["name"] for s in child.engine.schemas()]
    assert "task" not in names


def test_explore_subagent_reports_back_and_parent_pays(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _script(
        monkeypatch,
        _tool_call("task", {"description": "find the config", "agent_type": "explore"}),
        _final("Config lives in config.py, line 12."),  # the child's whole run
        _final("Done — see the report."),               # parent wraps up
    )
    agent = _agent(tmp_path)
    agent.run("where is the config?")

    # the child's report was fed back to the parent as the tool result
    tool_results = [m for m in agent.messages if m.get("role") == "tool"]
    assert any("Config lives in config.py" in m["content"] for m in tool_results)
    assert any("[subagent:explore report]" in m["content"] for m in tool_results)
    # parent absorbed the child's cost (3 scripted calls * 0.0001)
    assert agent.total_cost == pytest.approx(0.0003)


def test_explore_subagent_cannot_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _script(
        monkeypatch,
        _tool_call("task", {"description": "try to write", "agent_type": "explore"}),
        _tool_call("write_file", {"path": "hack.txt", "content": "nope"}, call_id="c2"),
        _final("Could not write."),   # child gives up
        _final("Done."),              # parent
    )
    agent = _agent(tmp_path)
    agent.run("go")
    assert not (tmp_path / "hack.txt").exists()


def test_general_subagent_writes_land_in_parent_turn_rewind(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _script(
        monkeypatch,
        _tool_call("task", {"description": "create s.txt", "agent_type": "general"}),
        _tool_call("write_file", {"path": "s.txt", "content": "from sub"}, call_id="c2"),
        _final("Wrote s.txt."),  # child
        _final("Done."),         # parent
    )
    agent = _agent(tmp_path)
    agent.run("delegate the write")
    assert (tmp_path / "s.txt").read_text(encoding="utf-8") == "from sub"

    # the child's write was snapshotted into the PARENT's turn -> rewind undoes it
    restored = agent.rewind()
    assert restored == ["s.txt"]
    assert not (tmp_path / "s.txt").exists()


def test_unknown_agent_type_errors_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _script(
        monkeypatch,
        _tool_call("task", {"description": "x", "agent_type": "wizard"}),
        _final("Done."),
    )
    agent = _agent(tmp_path)
    agent.run("go")
    tool_results = [m for m in agent.messages if m.get("role") == "tool"]
    assert any("unknown agent_type" in m["content"] for m in tool_results)
