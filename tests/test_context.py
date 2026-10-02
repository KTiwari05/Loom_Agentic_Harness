"""Phase 5 tests — attachment channel + context assembler (module spec §5, schema §5)."""

from __future__ import annotations

import json
import os
import time

import loom.models as models
from loom.context import (
    ContextAssembler,
    context_block,
    messages_from_records,
    render_attachment,
)
from loom.eventlog import EventLog
from loom.models import ModelResponse, ToolCall
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore

SLUG = "T--ctx"


# ── attachment rendering (deterministic) ─────────────────────────────────────────

def test_render_task_reminder():
    body = render_attachment({"type": "task_reminder", "itemCount": 2, "content": [
        {"id": "1", "subject": "Parse input", "status": "completed"},
        {"id": "2", "subject": "Emit output", "status": "in_progress"},
    ]})
    assert "[x] #1 Parse input" in body and "[~] #2 Emit output" in body


def test_render_each_type_is_deterministic():
    samples = {
        "file": {"type": "file", "filename": "a.py", "content": "print(1)"},
        "edited_text_file": {"type": "edited_text_file", "filename": "a.py", "snippet": "new"},
        "skill_listing": {"type": "skill_listing", "names": ["commit", "review"]},
        "deferred_tools_delta": {"type": "deferred_tools_delta", "addedNames": ["grep"]},
        "agent_listing_delta": {"type": "agent_listing_delta", "addedTypes": ["explore"]},
        "date_change": {"type": "date_change", "newDate": "2026-07-07"},
        "goal_status": {"type": "goal_status", "met": False, "condition": "ship it",
                        "feedback": "not done"},
        "hook_context": {"type": "hook_context", "content": "post-write note"},
    }
    for payload in samples.values():
        assert render_attachment(payload) == render_attachment(payload)  # stable
    assert 'path="a.py"' in render_attachment(samples["file"])
    assert "changed outside the agent" in render_attachment(samples["edited_text_file"])
    assert "commit, review" in render_attachment(samples["skill_listing"])
    assert "2026-07-07" in render_attachment(samples["date_change"])
    assert "NOT yet met" in render_attachment(samples["goal_status"])
    assert render_attachment({"type": "unknown"}) == ""


def test_context_block_wraps():
    assert context_block("file", "x") == '<context type="file">\nx\n</context>'


# ── the shared message builder ───────────────────────────────────────────────────

def _assistant(model="m", content=None):
    return {"role": "assistant", "model": model,
            "content": content or [{"type": "text", "text": "hi"}]}


def test_builder_maps_roles_and_tool_calls(tmp_path):
    log = EventLog(tmp_path, SLUG)
    a = log.assistant(_assistant(content=[
        {"type": "text", "text": "calling"},
        {"type": "tool_use", "id": "tc1", "name": "grep", "input": {"pattern": "x"}}]))
    log.tool_result("tc1", "match", {"tool": "grep", "ok": True, "target": "x",
                                     "category": "search", "detail": {}}, a)
    log.user("thanks", origin="human")

    msgs = messages_from_records(log.mainline(), str(tmp_path), render_attachments=False)
    assert msgs[0]["role"] == "system"
    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["name"] == "grep"
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"pattern": "x"}
    tool_msg = next(m for m in msgs if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "tc1" and tool_msg["content"] == "match"


def test_builder_coalesces_attachments_into_next_user_message(tmp_path):
    log = EventLog(tmp_path, SLUG)
    log.assistant(_assistant())
    log.attachment("task_reminder", {"itemCount": 1, "content": [
        {"id": "1", "subject": "do it", "status": "pending"}]})
    log.user("next please", origin="human")

    with_render = messages_from_records(log.mainline(), str(tmp_path), render_attachments=True)
    user_msg = [m for m in with_render if m["role"] == "user"][-1]
    assert "do it" in user_msg["content"] and "next please" in user_msg["content"]
    # no consecutive user messages were produced
    roles = [m["role"] for m in with_render]
    assert not any(roles[i] == roles[i + 1] == "user" for i in range(len(roles) - 1))

    # without rendering, the attachment is absent
    plain = messages_from_records(log.mainline(), str(tmp_path), render_attachments=False)
    assert not any("do it" in m.get("content", "") for m in plain)


def test_assembler_build_includes_reminder(tmp_path):
    log = EventLog(tmp_path, SLUG)
    log.user("start", origin="human")
    log.attachment("task_reminder", {"itemCount": 1, "content": [
        {"id": "1", "subject": "write tests", "status": "in_progress"}]})
    log.user("go on", origin="human")
    msgs = ContextAssembler(log, str(tmp_path)).build()
    assert any("write tests" in m.get("content", "") for m in msgs)


# ── external-edit detection ──────────────────────────────────────────────────────

def test_scan_detects_external_edit(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("v1", encoding="utf-8")
    asm = ContextAssembler(EventLog(tmp_path, SLUG), str(tmp_path))
    asm.track("code.py")
    assert asm.scan_external_edits() == []          # unchanged → nothing
    time.sleep(0.01)
    f.write_text("v2 changed", encoding="utf-8")     # external edit
    edits = asm.scan_external_edits()
    assert len(edits) == 1
    assert edits[0]["filename"] == "code.py" and "v2 changed" in edits[0]["snippet"]
    assert asm.scan_external_edits() == []          # already reported → no repeat


def test_track_from_log_baselines_touched_files(tmp_path):
    f = tmp_path / "seen.py"
    f.write_text("orig", encoding="utf-8")
    log = EventLog(tmp_path, SLUG)
    a = log.assistant(_assistant(content=[
        {"type": "tool_use", "id": "t", "name": "read_file", "input": {"path": "seen.py"}}]))
    log.tool_result("t", "orig", {"tool": "read_file", "ok": True, "target": "seen.py",
                                  "category": "file_read", "detail": {}}, a)
    asm = ContextAssembler(log, str(tmp_path))
    asm.track_from_log()
    f.write_text("edited on disk", encoding="utf-8")
    edits = asm.scan_external_edits()
    assert [e["filename"] for e in edits] == ["seen.py"]


# ── live integration: the model actually receives injected context ───────────────

def _tool_call(cid, name, args):
    a = json.dumps(args)
    return ModelResponse(text="", tool_calls=[ToolCall(id=cid, name=name, arguments=a)],
                         assistant_message={"role": "assistant", "content": "",
                                            "tool_calls": [{"id": cid, "type": "function",
                                                            "function": {"name": name, "arguments": a}}]},
                         usage={"prompt_tokens": 3, "completion_tokens": 1}, cost=0.0, model="fake")


def _final(text):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 4, "completion_tokens": 2}, cost=0.0, model="fake")


def _run_and_wait(runner, text, captured, target_len):
    start = len(captured)
    runner.send(text)
    deadline = time.time() + 8
    while time.time() < deadline and (runner.busy or len(captured) <= start):
        time.sleep(0.05)


def test_ledger_reminder_reaches_the_model(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    captured: list[list[dict]] = []
    responses = iter([
        _tool_call("t1", "task_create", {"subject": "Add caching"}),
        _final("Planned."),
        _final("Continuing."),
    ])

    def fake(model, messages, tools, temperature):
        captured.append([dict(m) for m in messages])
        return next(responses)

    monkeypatch.setattr(models, "complete", fake)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")

    prev = os.getcwd()
    try:
        runner = SessionRunner(meta["id"], store)
        _run_and_wait(runner, "plan it", captured, 1)
        while runner.busy:
            time.sleep(0.05)
        _run_and_wait(runner, "keep going", captured, 1)
        while runner.busy:
            time.sleep(0.05)
    finally:
        os.chdir(prev)

    # the LAST turn's messages must contain the injected task reminder
    last_turn = captured[-1]
    assert any("Add caching" in m.get("content", "") for m in last_turn), \
        "the task reminder was not injected into the model's context"


def test_external_edit_reaches_the_model(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.setenv("LOOM_AUTO_APPROVE", "1")
    captured: list[list[dict]] = []
    responses = iter([
        _tool_call("w1", "write_file", {"path": "notes.txt", "content": "agent version"}),
        _final("Wrote notes."),
        _final("Acknowledged."),
    ])

    def fake(model, messages, tools, temperature):
        captured.append([dict(m) for m in messages])
        return next(responses)

    monkeypatch.setattr(models, "complete", fake)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")

    prev = os.getcwd()
    try:
        runner = SessionRunner(meta["id"], store)
        _run_and_wait(runner, "write notes.txt", captured, 1)
        while runner.busy:
            time.sleep(0.05)
        # user edits the file outside the agent between turns
        (project / "notes.txt").write_text("HUMAN EDITED THIS", encoding="utf-8")
        _run_and_wait(runner, "what changed?", captured, 1)
        while runner.busy:
            time.sleep(0.05)
    finally:
        os.chdir(prev)

    last_turn = captured[-1]
    joined = "\n".join(m.get("content", "") for m in last_turn if isinstance(m.get("content"), str))
    assert "changed outside the agent" in joined and "HUMAN EDITED THIS" in joined
