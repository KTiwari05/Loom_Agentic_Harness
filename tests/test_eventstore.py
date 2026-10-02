"""Phase 2 tests — session runtime on the event log (docs/v2 module spec §11, §1).

These run a REAL Orchestrator with a scripted fake model and real tools through the
LogSink, then prove the three things the store must derive from the log alone:
transcript, dashboard stats, and a resumable model conversation.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

import pytest

from loom.config import Config
from loom.models import ModelResponse, ToolCall
from loom.orchestrator import Orchestrator
from loom.server.eventstore import EventStore, rebuild_messages
from loom.server.logsink import LogSink, mirror_for


@contextmanager
def _in_dir(path):
    """The real bridge chdirs into the workdir before a turn (tools resolve against CWD)."""
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


class _Seq:
    """A completer that replays scripted ModelResponses in order."""

    def __init__(self, responses):
        self._it = iter(responses)

    def __call__(self, model, messages, tools, temperature):
        return next(self._it)


def _write_call(path, content):
    args = json.dumps({"path": path, "content": content})
    return ModelResponse(
        text="Writing the file.",
        tool_calls=[ToolCall(id="c1", name="write_file", arguments=args)],
        assistant_message={"role": "assistant", "content": "Writing the file.",
                           "tool_calls": [{"id": "c1", "type": "function",
                                           "function": {"name": "write_file", "arguments": args}}]},
        usage={"prompt_tokens": 10, "completion_tokens": 5}, cost=0.0002, model="fake/model",
    )


def _final(text):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 8, "completion_tokens": 3}, cost=0.0001,
                         model="fake/model")


@pytest.fixture
def store(tmp_path):
    return EventStore(tmp_path / "store")


def _run_one_turn(store, project, completer, user_text="make a file"):
    meta = store.create(workdir=str(project), model="fake/model")
    log = store.log_for(meta["id"])
    sink = LogSink(log, asker=lambda *a: "a")  # auto-allow; no forwarding needed offline
    cfg = Config()
    cfg.workdir = str(project)
    cfg.critic_enabled = False
    agent = Orchestrator(cfg, sink=sink, completer=completer)
    log.user(user_text, origin="human")
    with _in_dir(project):
        agent.run(user_text)
    return meta["id"], log


def test_run_produces_schema_valid_log(tmp_path, store):
    project = tmp_path / "proj"
    project.mkdir()
    sid, log = _run_one_turn(store, project,
                             _Seq([_write_call("out.txt", "hello"), _final("Done.")]))
    report = log.verify()
    assert report.ok, (report.orphan_parents, report.unmatched_tool_calls, report.schema_errors)
    # the write actually happened through the real tool
    assert (project / "out.txt").read_text(encoding="utf-8") == "hello"
    # one assistant record carried the tool_use; its result links back
    assistants = [r for r in log.read_all() if r.get("type") == "assistant"]
    assert any(b.get("type") == "tool_use"
               for r in assistants for b in r["message"]["content"])


def test_transcript_matches_the_run(tmp_path, store):
    project = tmp_path / "proj"
    project.mkdir()
    sid, _ = _run_one_turn(store, project,
                           _Seq([_write_call("out.txt", "hi"), _final("All set.")]),
                           user_text="please write out.txt")
    events = store.transcript(sid)
    types = [e["type"] for e in events]
    assert types[0] == "user" and events[0]["text"] == "please write out.txt"
    tool_events = [e for e in events if e["type"] == "tool"]
    assert len(tool_events) == 1
    assert tool_events[0]["name"] == "write_file"
    assert json.loads(tool_events[0]["arguments"])["path"] == "out.txt"
    assert tool_events[0]["outcome"] == "ok"
    assert any(e["type"] == "assistant_text" and e["text"] == "All set." for e in events)


def test_meta_and_stats_derived_from_log(tmp_path, store):
    project = tmp_path / "proj"
    project.mkdir()
    sid, _ = _run_one_turn(store, project,
                           _Seq([_write_call("a.txt", "x"), _final("Finished.")]),
                           user_text="build a.txt now")
    meta = store.get(sid)["meta"]
    assert meta["title"] == "build a.txt now"
    assert meta["model"] == "fake/model"
    assert meta["message_count"] >= 2        # 1 human + >=1 assistant text
    assert meta["tokens"] == 26              # 10+5 + 8+3
    assert meta["total_cost"] == pytest.approx(0.0003, abs=1e-9)

    stats = store.stats()
    assert stats["sessions"] == 1
    assert stats["messages"] >= 3
    assert stats["tokens"] == 26
    assert stats["favorite_model"] == "fake/model"


def test_resume_rebuilds_conversation(tmp_path, store):
    project = tmp_path / "proj"
    project.mkdir()
    sid, log = _run_one_turn(store, project,
                             _Seq([_write_call("z.txt", "z"), _final("Wrote it.")]))
    rebuilt = rebuild_messages(log.mainline(), str(project))

    roles = [m["role"] for m in rebuilt]
    assert roles[0] == "system"
    assert "user" in roles and "assistant" in roles and "tool" in roles
    # the assistant tool call and the tool reply are linked by the same id
    assistant_with_call = next(m for m in rebuilt if m.get("tool_calls"))
    call_id = assistant_with_call["tool_calls"][0]["id"]
    tool_msg = next(m for m in rebuilt if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == call_id
    # a resumed agent continues: feed rebuilt history + a follow-up, model replies
    agent = Orchestrator(_cfg(project), sink=LogSink(log, asker=lambda *a: "a"),
                         completer=_Seq([_final("Follow-up handled.")]))
    agent.messages = rebuilt
    log.user("anything else?", origin="human")
    agent.run("anything else?")
    assert any(e["text"] == "Follow-up handled."
               for e in store.transcript(sid) if e["type"] == "assistant_text")


def test_denied_tool_records_error_result(tmp_path, store):
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake/model")
    log = store.log_for(meta["id"])
    sink = LogSink(log, asker=lambda *a: "n")  # deny everything
    agent = Orchestrator(_cfg(project), sink=sink,
                         completer=_Seq([_write_call("nope.txt", "x"), _final("Stopped.")]))
    log.user("write nope.txt", origin="human")
    with _in_dir(project):
        agent.run("write nope.txt")
    assert not (project / "nope.txt").exists()
    # the denied tool still appears in the transcript with a non-ok outcome
    tool_events = [e for e in store.transcript(meta["id"]) if e["type"] == "tool"]
    assert tool_events and tool_events[0]["outcome"] != "ok"


def test_mirror_categories():
    assert mirror_for("read_file", {"path": "x"}, "ok", "")["category"] == "file_read"
    assert mirror_for("write_file", {"path": "x"}, "ok", "")["category"] == "file_write"
    assert mirror_for("grep", {"pattern": "p"}, "ok", "")["target"] == "p"
    assert mirror_for("run_bash", {"command": "ls"}, "ok", "")["category"] == "command"


def _cfg(project):
    cfg = Config()
    cfg.workdir = str(project)
    cfg.critic_enabled = False
    return cfg
