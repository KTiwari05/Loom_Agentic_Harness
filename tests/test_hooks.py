"""Phase 8 tests — hooks (module spec §8, schema §5/§6)."""

from __future__ import annotations

import json
import time

import loom.models as models
from loom.eventlog import EventLog
from loom.hooks import HookEngine, PostToolUseHook, _parse_verdict
from loom.models import ModelResponse, ToolCall
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore

SLUG = "T--hooks"


# ── units ─────────────────────────────────────────────────────────────────────────

def test_parse_verdict():
    assert _parse_verdict("MET").met
    v = _parse_verdict("NOT MET: the tests are missing")
    assert not v.met and v.feedback == "the tests are missing"
    assert _parse_verdict("").met            # fail open
    assert _parse_verdict("gibberish").met   # fail open


def test_after_tool_pattern_and_safety(tmp_path):
    log = EventLog(tmp_path, SLUG)

    def boom(*_):
        raise RuntimeError("broken hook")

    hooks = HookEngine(log, post_hooks=[
        PostToolUseHook("write_*", lambda n, a, r: f"you wrote {a.get('path')}"),
        PostToolUseHook("write_*", boom),                 # must be swallowed
        PostToolUseHook("grep", lambda n, a, r: "never"),  # pattern miss
    ])
    hooks.after_tool("tc9", "write_file", {"path": "x.py"}, "Created 'x.py'")

    atts = [r for r in log.read_all() if r.get("type") == "attachment"]
    assert len(atts) == 1
    att = atts[0]["attachment"]
    assert att["type"] == "hook_context"
    assert att["hookName"] == "PostToolUse:write_file"
    assert att["toolUseId"] == "tc9"
    assert att["content"] == "you wrote x.py"


# ── live ──────────────────────────────────────────────────────────────────────────

def _final(text):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 4, "completion_tokens": 2},
                         cost=0.0, model="fake")


def _tool_call(cid, name, args):
    a = json.dumps(args)
    return ModelResponse(text="", tool_calls=[ToolCall(id=cid, name=name, arguments=a)],
                         assistant_message={"role": "assistant", "content": "",
                                            "tool_calls": [{"id": cid, "type": "function",
                                                            "function": {"name": name,
                                                                         "arguments": a}}]},
                         usage={"prompt_tokens": 3, "completion_tokens": 1},
                         cost=0.0, model="fake")


def _run_and_wait(runner, text):
    runner.send(text)
    deadline = time.time() + 8
    while time.time() < deadline and runner.busy:
        time.sleep(0.05)
    while runner.busy:
        time.sleep(0.05)


def test_post_tool_context_reaches_next_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.setenv("LOOM_AUTO_APPROVE", "1")
    monkeypatch.delenv("LOOM_STOP_CONDITION", raising=False)
    captured: list[list[dict]] = []
    script = iter([
        _tool_call("w1", "write_file", {"path": "mod.py", "content": "x = 1"}),
        _final("Wrote the module."),
        _final("Acknowledged."),
    ])

    def fake(model, messages, tools, temperature):
        captured.append([dict(m) for m in messages])
        return next(script)

    monkeypatch.setattr(models, "complete", fake)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)
    runner.hooks.post_hooks.append(
        PostToolUseHook("write_file", lambda n, a, r: "LINT-REMINDER: run ruff on it"))

    _run_and_wait(runner, "write mod.py")
    _run_and_wait(runner, "anything else?")

    joined = "\n".join(str(m.get("content", "")) for m in captured[-1])
    assert "LINT-REMINDER" in joined  # hook context reached the NEXT turn's model input


def test_stop_hook_vetoes_then_accepts(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.setenv("LOOM_STOP_CONDITION", "the answer file must exist")
    agent_runs = 0
    script = iter([
        _final("attempt one"),                    # agent turn 1
        _final("NOT MET: no file was created"),   # judge → veto
        _final("attempt two, file created"),      # forced continuation
        _final("MET"),                            # judge → accept
    ])

    def fake(model, messages, tools, temperature):
        nonlocal agent_runs
        if any("judge whether a coding agent met its goal" in str(m.get("content", ""))
               for m in messages):
            return next(script)
        agent_runs += 1
        return next(script)

    monkeypatch.setattr(models, "complete", fake)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)
    _run_and_wait(runner, "do the thing")

    assert agent_runs == 2  # the veto forced a second run
    log = store.log_for(meta["id"])
    records = log.read_all()
    statuses = [r["attachment"] for r in records if r.get("type") == "attachment"
                and r["attachment"].get("type") == "goal_status"]
    assert [s["met"] for s in statuses] == [False, True]
    assert statuses[0]["condition"] == "the answer file must exist"
    vetoes = [r for r in records if r.get("type") == "user"
              and r.get("origin", {}).get("kind") == "hook"]
    assert len(vetoes) == 1 and "no file was created" in vetoes[0]["message"]["content"]


def test_stop_hook_veto_budget_caps_at_three(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.setenv("LOOM_STOP_CONDITION", "impossible goal")

    def fake(model, messages, tools, temperature):
        if any("judge whether a coding agent met its goal" in str(m.get("content", ""))
               for m in messages):
            return _final("NOT MET: still impossible")
        return _final("tried again")

    monkeypatch.setattr(models, "complete", fake)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)
    _run_and_wait(runner, "attempt the impossible")

    log = store.log_for(meta["id"])
    statuses = [r for r in log.read_all() if r.get("type") == "attachment"
                and r["attachment"].get("type") == "goal_status"]
    assert len(statuses) == 3  # the budget stops an unwinnable loop
    assert not runner.busy
