"""Phase 3 tests — background jobs + prompt queue (docs/v2 module spec §2–§3, schema §10–§11).

Covers the queue in isolation, a real background subprocess turning into a notification,
and the end-to-end wiring: a job that finishes while the agent is idle auto-starts a turn
whose input is the notification.
"""

from __future__ import annotations

import sys
import time

import pytest

import loom.models as models
from loom.eventlog import EventLog
from loom.jobs import JobManager, new_job_id, notification
from loom.models import ModelResponse
from loom.queue import PromptQueue, new_item_id
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore

SLUG = "T--jobs"


def _final(text):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 5, "completion_tokens": 2}, cost=0.0,
                         model="fake/model")


# ── the queue ────────────────────────────────────────────────────────────────────

def test_queue_fifo_and_records(tmp_path):
    log = EventLog(tmp_path, SLUG)
    q = PromptQueue(log)
    a = q.enqueue("first")
    b = q.enqueue("second", origin="task-notification")
    assert len(q) == 2
    first = q.pop()
    assert first.id == a and first.content == "first" and first.origin == "human"
    second = q.pop()
    assert second.id == b and second.origin == "task-notification"
    assert q.pop() is None

    ops = [(r["operation"], r.get("origin")) for r in log.records("queue-operation")]
    assert ("enqueue", "human") in ops and ("enqueue", "task-notification") in ops
    assert ops.count(("dequeue", "human")) == 1


def test_queue_remove(tmp_path):
    q = PromptQueue(EventLog(tmp_path, SLUG))
    a = q.enqueue("keep")
    b = q.enqueue("drop")
    assert q.remove(b) is True
    assert q.remove("nonexistent") is False
    remaining = q.peek_all()
    assert [it.id for it in remaining] == [a]


def test_queue_rebuilds_from_log(tmp_path):
    log = EventLog(tmp_path, SLUG)
    q1 = PromptQueue(log)
    q1.enqueue("one")
    keep = q1.enqueue("two")
    q1.pop()  # removes "one"
    # a fresh queue over the same log recovers the pending item
    q2 = PromptQueue(EventLog(tmp_path, SLUG, log.session_id))
    items = q2.peek_all()
    assert [it.content for it in items] == ["two"]
    assert items[0].id == keep


def test_on_enqueue_fires_outside_lock(tmp_path):
    log = EventLog(tmp_path, SLUG)
    seen = []
    q = PromptQueue(log, on_enqueue=lambda item: seen.append(q.pop().id))  # re-enters pop()
    iid = q.enqueue("x")
    assert seen == [iid]  # callback could pop without deadlocking


def test_id_formats():
    for gen in (new_job_id, new_item_id):
        i = gen()
        assert len(i) == 9 and i[0].isalpha() and i.isalnum() and i.islower()


# ── real background jobs ─────────────────────────────────────────────────────────

def _wait_for(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_job_success_writes_output_and_notification(tmp_path):
    log = EventLog(tmp_path, SLUG)
    q = PromptQueue(log)
    mgr = JobManager(q, tmp_path, log.session_id)
    status = mgr.start(f'{sys.executable} -c "print(\'built ok\')"', "build", cwd=str(tmp_path))

    assert _wait_for(lambda: len(q) == 1), "job never enqueued a notification"
    item = q.pop()
    assert item.origin == "task-notification"
    assert f"<task-id>{status.id}</task-id>" in item.content
    assert "<status>completed</status>" in item.content and "<exit-code>0</exit-code>" in item.content

    out = (tmp_path / "jobs" / log.session_id / f"{status.id}.output").read_text(encoding="utf-8")
    assert "built ok" in out
    assert mgr.status(status.id).state == "completed"


def test_job_failure_is_reported(tmp_path):
    log = EventLog(tmp_path, SLUG)
    q = PromptQueue(log)
    mgr = JobManager(q, tmp_path, log.session_id)
    mgr.start(f'{sys.executable} -c "import sys; sys.exit(3)"', "boom", cwd=str(tmp_path))
    assert _wait_for(lambda: len(q) == 1)
    item = q.pop()
    assert "<status>failed</status>" in item.content and "<exit-code>3</exit-code>" in item.content


def test_notification_format():
    from loom.jobs import JobStatus
    text = notification(JobStatus("abc123xyz", "completed", "run tests", "/p/out", 0))
    assert text.startswith("<task-notification>") and text.rstrip().endswith("</task-notification>")
    assert '"run tests"' in text


# ── end-to-end: a finished job auto-starts a turn ────────────────────────────────

def test_background_job_autostarts_a_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.setenv("LOOM_AUTO_APPROVE", "1")
    monkeypatch.setattr(models, "complete", lambda *a, **k: _final("I saw the job finish."))

    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake/model")

    prev_cwd = __import__("os").getcwd()
    try:
        runner = SessionRunner(meta["id"], store)
        # a job finishes while the agent is idle -> notification -> auto turn
        runner.jobs.start(f'{sys.executable} -c "print(1)"', "quickjob", cwd=str(project))

        got = _wait_for(lambda: any(
            e["type"] == "assistant_text" and "saw the job" in e["text"].lower()
            for e in store.transcript(meta["id"])))
        assert got, "the job notification did not auto-start a turn"

        events = store.transcript(meta["id"])
        notif_turns = [e for e in events
                       if e["type"] == "user" and e.get("origin") == "task-notification"]
        assert notif_turns and "<task-notification>" in notif_turns[0]["text"]
    finally:
        __import__("os").chdir(prev_cwd)


@pytest.mark.parametrize("origin", ["human", "task-notification"])
def test_transcript_shows_both_origins(tmp_path, origin):
    store = EventStore(tmp_path / "store2")
    # write directly so we can check transcript origin handling without a full run
    meta = store.create(workdir=str(tmp_path), model="m")
    live = store.log_for(meta["id"])
    live.user("hello there", origin=origin)
    events = store.transcript(meta["id"])
    assert any(e["type"] == "user" and e.get("origin") == origin for e in events)
