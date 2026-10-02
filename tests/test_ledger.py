"""Phase 4 tests — task ledger + task tools (docs/v2 module spec §4, schema §9)."""

from __future__ import annotations

import json
import os
import time

import pytest

import loom.models as models
from loom.ledger import TaskLedger, render
from loom.models import ModelResponse, ToolCall
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore

SESSION = "sess-led"


# ── the ledger module ────────────────────────────────────────────────────────────

def test_create_allocates_sequential_ids(tmp_path):
    led = TaskLedger(tmp_path, SESSION)
    assert led.create("first") == "1"
    assert led.create("second") == "2"
    assert led.create("third") == "3"
    assert [t["subject"] for t in led.list()] == ["first", "second", "third"]


def test_file_matches_schema_shape(tmp_path):
    led = TaskLedger(tmp_path, SESSION)
    tid = led.create("Add retry", description="wrap the client", active_form="Adding retry")
    path = tmp_path / "tasks" / SESSION / f"{tid}.json"
    raw = path.read_text(encoding="utf-8")
    assert "\n" in raw and "  " in raw  # pretty-printed (indent=2)
    data = json.loads(raw)
    assert set(data) == {"id", "subject", "description", "activeForm",
                         "status", "blocks", "blockedBy"}
    assert data == {"id": "1", "subject": "Add retry", "description": "wrap the client",
                    "activeForm": "Adding retry", "status": "pending",
                    "blocks": [], "blockedBy": []}


def test_update_status_and_aliases(tmp_path):
    led = TaskLedger(tmp_path, SESSION)
    led.create("job")
    led.update("1", status="in_progress")
    assert led.get("1")["status"] == "in_progress"
    led.update("1", active_form="Doing job", blocked_by=["2"])  # snake_case aliases
    data = led.get("1")
    assert data["activeForm"] == "Doing job" and data["blockedBy"] == ["2"]


def test_invalid_status_rejected(tmp_path):
    led = TaskLedger(tmp_path, SESSION)
    led.create("job")
    with pytest.raises(ValueError):
        led.update("1", status="almost-done")


def test_update_missing_task_raises(tmp_path):
    led = TaskLedger(tmp_path, SESSION)
    with pytest.raises(KeyError):
        led.update("99", status="completed")


def test_delete_and_id_continues(tmp_path):
    led = TaskLedger(tmp_path, SESSION)
    led.create("a")
    led.create("b")
    assert led.delete("1") is True
    assert led.delete("1") is False
    # next id is still max+1 (2 remains) -> 3, ids don't get reused
    assert led.create("c") == "3"


def test_survives_new_instance(tmp_path):
    TaskLedger(tmp_path, SESSION).create("persisted")
    reopened = TaskLedger(tmp_path, SESSION)
    assert [t["subject"] for t in reopened.list()] == ["persisted"]


def test_reminder_payload_and_render(tmp_path):
    led = TaskLedger(tmp_path, SESSION)
    led.create("one")
    led.update("1", status="completed")
    payload = led.reminder_payload()
    assert payload["itemCount"] == 1 and payload["content"][0]["status"] == "completed"
    text = render(led.list())
    assert "#1" in text and "completed" in text
    assert render([]) == "No tasks yet."


# ── the tools, driven by a scripted model through a real SessionRunner ────────────

def _tool_call(cid, name, args):
    a = json.dumps(args)
    return ModelResponse(
        text="", tool_calls=[ToolCall(id=cid, name=name, arguments=a)],
        assistant_message={"role": "assistant", "content": "",
                           "tool_calls": [{"id": cid, "type": "function",
                                           "function": {"name": name, "arguments": a}}]},
        usage={"prompt_tokens": 3, "completion_tokens": 1}, cost=0.0, model="fake/model")


def _final(text):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 4, "completion_tokens": 2}, cost=0.0,
                         model="fake/model")


def test_task_tools_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    responses = iter([
        _tool_call("t1", "task_create", {"subject": "Build the parser",
                                         "description": "recursive descent"}),
        _tool_call("t2", "task_update", {"task_id": "1", "status": "in_progress"}),
        _final("Plan set up; starting now."),
    ])
    monkeypatch.setattr(models, "complete", lambda *a, **k: next(responses))

    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake/model")

    prev = os.getcwd()
    try:
        runner = SessionRunner(meta["id"], store)
        runner.send("plan the parser work")
        deadline = time.time() + 8
        while time.time() < deadline and (runner.busy or not runner.ledger.list()):
            time.sleep(0.05)
    finally:
        os.chdir(prev)

    # the ledger reflects the tool calls
    tasks = runner.ledger.list()
    assert len(tasks) == 1
    assert tasks[0]["subject"] == "Build the parser" and tasks[0]["status"] == "in_progress"

    # a task_reminder attachment was recorded for the next turn
    reminders = [r for r in runner.log.records("attachment")
                 if r["attachment"]["type"] == "task_reminder"]
    assert reminders, "no task_reminder attachment was written"
    assert reminders[-1]["attachment"]["itemCount"] == 1

    # the tool calls appear in the derived transcript
    tool_names = [e["name"] for e in store.transcript(meta["id"]) if e["type"] == "tool"]
    assert "task_create" in tool_names and "task_update" in tool_names
