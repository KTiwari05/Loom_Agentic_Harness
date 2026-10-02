"""Phase 10 tests — activity aggregation, ai-titles, last-prompt, context tokens,
and the new session REST endpoints (module spec §11–§12)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

import loom.models as models
from loom.ledger import TaskLedger
from loom.models import ModelResponse
from loom.server.app import create_app
from loom.server.bridge import SessionRunner
from loom.server.eventstore import (
    EventStore,
    activity_from_records,
    summarize_activity,
)

FIXTURE = Path(__file__).parent / "fixtures" / "activity_log.jsonl"


# ── activity aggregation (golden, module spec §12) ─────────────────────────────────

def test_activity_golden_fixture():
    records = [json.loads(line) for line in
               FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    turns = activity_from_records(records)
    assert [t["summary"] for t in turns] == [
        "Read 2 files · Edited 2 files · Searched 1 time · Ran 2 commands",
        "Delegated 1 subagent",
        "No tool activity",
    ]
    assert turns[0]["files_read"] == 2          # duplicate read of a.py collapsed
    assert turns[0]["files_edited"] == 2        # edit + write are both "edited"
    assert turns[1]["subagents"] == 1
    assert turns[2]["prompt"].startswith("thanks")


def test_summarize_singular_plural():
    base = {"files_read": 0, "files_edited": 0, "searches": 0, "commands": 0, "subagents": 0}
    assert summarize_activity({**base, "files_read": 1}) == "Read 1 file"
    assert summarize_activity({**base, "searches": 2, "commands": 1}) == \
        "Searched 2 times · Ran 1 command"
    assert summarize_activity(base) == "No tool activity"


# ── live: titles, last-prompt, turn_end payload ─────────────────────────────────────

def _final(text, cost=0.001):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 4, "completion_tokens": 2},
                         cost=cost, model="fake")


def _run_and_wait(runner, text):
    runner.send(text)
    deadline = time.time() + 8
    while time.time() < deadline and runner.busy:
        time.sleep(0.05)
    while runner.busy:
        time.sleep(0.05)


def test_ai_title_written_and_throttled(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.setenv("LOOM_AI_TITLES", "1")
    calls = {"n": 0}
    script = iter([
        _final("I refactored the parser."),   # turn 1
        _final("Parser Refactor Session"),    # the title call
        _final("More work done."),            # turn 2 (title throttled: no extra call)
    ])

    def fake(model, messages, tools, temperature):
        calls["n"] += 1
        return next(script)

    monkeypatch.setattr(models, "complete", fake)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)

    _run_and_wait(runner, "refactor it")
    assert store.get(meta["id"])["meta"]["title"] == "Parser Refactor Session"
    assert calls["n"] == 2

    _run_and_wait(runner, "keep going")
    assert calls["n"] == 3  # agent call only — the title was NOT regenerated
    assert store.get(meta["id"])["meta"]["title"] == "Parser Refactor Session"


def test_last_prompt_and_turn_end_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.delenv("LOOM_AI_TITLES", raising=False)
    monkeypatch.setattr(models, "complete",
                        lambda model, messages, tools, temperature: _final("ok"))
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)
    _run_and_wait(runner, "the very last prompt")

    m = store.get(meta["id"])["meta"]
    assert m["last_prompt"] == "the very last prompt"
    assert m["context_tokens"] > 0

    # the live turn_end event carries the ring/queue/activity payload
    events = []
    try:
        while True:
            events.append(runner.queue.get_nowait())
    except Exception:  # noqa: BLE001 — queue.Empty ends the drain
        pass
    end = next(e for e in events if e["type"] == "turn_end")
    assert end["context_tokens"] > 0
    assert end["queued"] == 0
    assert "activity" in end


# ── the new REST endpoints ──────────────────────────────────────────────────────────

def test_session_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    store = EventStore(tmp_path / "store")
    app = create_app(store=store)
    with TestClient(app) as client:
        project = tmp_path / "proj"
        project.mkdir()
        meta = client.post("/api/sessions",
                           json={"workdir": str(project), "model": "fake"}).json()
        sid = meta["id"]

        # seed some records + ledger tasks directly
        log = store.log_for(sid)
        a = log.user("do things", origin="human")
        assert a
        ledger = TaskLedger(store.root, sid)
        ledger.create("Ship the feature")
        ledger.update("1", status="in_progress")

        tasks = client.get(f"/api/sessions/{sid}/tasks").json()
        assert tasks == [{"id": "1", "subject": "Ship the feature", "description": "",
                          "activeForm": "Ship the feature", "status": "in_progress",
                          "blocks": [], "blockedBy": []}]

        activity = client.get(f"/api/sessions/{sid}/activity").json()
        assert activity[0]["summary"] == "No tool activity"

        records = client.get(f"/api/sessions/{sid}/records").json()
        assert records and records[-1]["uuid"] == log.head_uuid
        after = client.get(f"/api/sessions/{sid}/records",
                           params={"after": records[0]["uuid"]}).json()
        assert len(after) == len(records) - 1

        state = client.get("/api/state").json()
        assert state["compact_at"] > 0
