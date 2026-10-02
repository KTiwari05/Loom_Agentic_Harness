"""Phase 9 tests — sidechain subagents (module spec §9, schema §1)."""

from __future__ import annotations

import json
import time

import loom.models as models
from loom.eventlog import EventLog, new_sidechain_id
from loom.models import ModelResponse, ToolCall
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore, rebuild_messages

SLUG = "T--side"


# ── units: the sidechain scope on the event log ────────────────────────────────────

def test_sidechain_records_are_stamped_and_isolated(tmp_path):
    log = EventLog(tmp_path, SLUG)
    main_user = log.user("parent turn", origin="human")

    with log.sidechain("sc_test000001"):
        side_root = log.user("child goal", origin="human", sidechainAgentType="explore")
        side_reply = log.assistant({"role": "assistant", "model": "fake",
                                    "content": [{"type": "text", "text": "child says hi"}]})

    main_next = log.user("parent continues", origin="human")

    records = {r["uuid"]: r for r in log.read_all()}
    assert records[side_root]["isSidechain"] and records[side_root]["sidechainId"] == "sc_test000001"
    assert records[side_root]["parentUuid"] is None          # its own tree root
    assert records[side_reply]["parentUuid"] == side_root    # chained within the sidechain
    assert records[main_next]["parentUuid"] == main_user     # parent head was restored
    assert not records[main_next].get("isSidechain")

    mainline_uuids = [r["uuid"] for r in log.mainline()]
    assert side_root not in mainline_uuids and side_reply not in mainline_uuids
    assert mainline_uuids == [main_user, main_next]


def test_resume_head_skips_sidechain_leaf(tmp_path):
    log = EventLog(tmp_path, SLUG)
    main_user = log.user("parent", origin="human")
    with log.sidechain(new_sidechain_id()):
        log.user("child goal", origin="human")
    # the FILE ends on a sidechain record; a resumed log must still chain off mainline
    resumed = EventLog(tmp_path, SLUG, log.session_id)
    assert resumed.head_uuid == main_user
    nxt = resumed.user("after resume", origin="human")
    assert {r["uuid"]: r for r in resumed.read_all()}[nxt]["parentUuid"] == main_user


# ── live: a task-tool run records parent + child in ONE file ───────────────────────

def _final(text):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 4, "completion_tokens": 2},
                         cost=0.001, model="fake")


def _tool_call(cid, name, args):
    a = json.dumps(args)
    return ModelResponse(text="", tool_calls=[ToolCall(id=cid, name=name, arguments=a)],
                         assistant_message={"role": "assistant", "content": "",
                                            "tool_calls": [{"id": cid, "type": "function",
                                                            "function": {"name": name,
                                                                         "arguments": a}}]},
                         usage={"prompt_tokens": 3, "completion_tokens": 1},
                         cost=0.001, model="fake")


def _run_and_wait(runner, text):
    runner.send(text)
    deadline = time.time() + 8
    while time.time() < deadline and runner.busy:
        time.sleep(0.05)
    while runner.busy:
        time.sleep(0.05)


def test_task_tool_records_sidechain_in_same_log(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.delenv("LOOM_STOP_CONDITION", raising=False)
    script = iter([
        _tool_call("t1", "task", {"description": "find where config loads",
                                  "agent_type": "explore"}),
        _final("CHILD-REPORT-MARK: config loads in config.py"),   # the subagent's turn
        _final("PARENT-DONE"),
    ])
    monkeypatch.setattr(models, "complete",
                        lambda model, messages, tools, temperature: next(script))
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)
    _run_and_wait(runner, "who loads the config?")

    log = store.log_for(meta["id"])
    records = log.read_all()

    # one file holds both: a sidechain tree with its own root, stamped and grouped
    side = [r for r in records if r.get("isSidechain")]
    assert side, "no sidechain records were written"
    sids = {r["sidechainId"] for r in side}
    assert len(sids) == 1
    root = next(r for r in side if r["parentUuid"] is None)
    assert root["sidechainAgentType"] == "explore"
    assert any("CHILD-REPORT-MARK" in json.dumps(r.get("message", {})) for r in side)

    # the mainline stays clean: child turns are NOT parent conversation...
    mainline = log.mainline()
    assert not any(r.get("isSidechain") for r in mainline)
    msgs = rebuild_messages(mainline, str(project))
    child_as_assistant = [m for m in msgs if m["role"] == "assistant"
                          and "CHILD-REPORT-MARK" in str(m.get("content", ""))]
    assert not child_as_assistant
    # ...but the parent's task tool RESULT carries the child's report
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert any("CHILD-REPORT-MARK" in str(m["content"]) for m in tool_msgs)

    # the transcript renders the child's activity nested under a subagent tag
    events = store.transcript(meta["id"])
    nested = [e for e in events if e.get("subagent") == "explore"]
    assert any(e["type"] == "assistant_text" and "CHILD-REPORT-MARK" in e["text"]
               for e in nested)
    assert any(e["type"] == "tool" and e["name"] == "task" for e in events)

    assert log.verify().ok


def test_session_resumes_cleanly_after_sidechain(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    script = iter([
        _tool_call("t1", "task", {"description": "look around", "agent_type": "explore"}),
        _final("child looked around"),
        _final("parent done"),
        _final("second turn works"),          # after resume
    ])
    monkeypatch.setattr(models, "complete",
                        lambda model, messages, tools, temperature: next(script))
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)
    _run_and_wait(runner, "delegate something")

    # a FRESH runner (process restart) must rebuild the parent view and keep working
    runner2 = SessionRunner(meta["id"], store)
    assert not any("child looked around" in str(m.get("content", ""))
                   for m in runner2.agent.messages if m["role"] == "assistant")
    _run_and_wait(runner2, "and continue")
    events = store.transcript(meta["id"])
    assert any(e["type"] == "assistant_text" and "second turn works" in e["text"]
               for e in events)
