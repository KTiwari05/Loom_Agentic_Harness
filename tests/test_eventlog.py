"""Phase 1 tests — the event log core (docs/v2 schemas §1–§6, module spec §1)."""

from __future__ import annotations

import json
import threading

import pytest

from loom.eventlog import (
    ATTACHMENT_TYPES,
    EventLog,
    list_sessions,
    new_tool_call_id,
    project_slug,
)

SLUG = "T--test-proj"


@pytest.fixture
def log(tmp_path):
    return EventLog(tmp_path, SLUG)


def _assistant_msg(model="test/model", content=None):
    return {"role": "assistant", "model": model, "content": content or [{"type": "text", "text": "hi"}]}


# ── envelope & chaining ─────────────────────────────────────────────────────────

def test_first_record_is_root(log):
    u = log.user("hello")
    rec = log.read_all()[0]
    assert rec["uuid"] == u
    assert rec["parentUuid"] is None
    assert rec["type"] == "user"
    assert rec["sessionId"] == log.session_id
    assert rec["isSidechain"] is False
    assert rec["timestamp"].endswith("Z")


def test_records_chain_to_head(log):
    u1 = log.user("one")
    u2 = log.assistant(_assistant_msg())
    recs = log.read_all()
    assert recs[1]["parentUuid"] == u1
    assert log.head_uuid == u2


def test_explicit_parent_and_root(log):
    log.user("one")
    root2 = log.append("user", parent=None, message={"role": "user", "content": "x"},
                       origin={"kind": "human"}, isMeta=False)
    rec = [r for r in log.read_all() if r["uuid"] == root2][0]
    assert rec["parentUuid"] is None


def test_meta_records_have_no_tree_linkage(log):
    log.meta("ai-title", aiTitle="My session")
    log.meta("last-prompt", lastPrompt="do it", leafUuid="abc")
    log.meta("mode", mode="normal")
    log.meta("queue-operation", operation="enqueue", content="next", origin="human")
    recs = log.read_all()
    assert all("uuid" not in r for r in recs)
    assert log.head_uuid is None  # meta records never move the head


def test_unknown_types_rejected(log):
    with pytest.raises(ValueError):
        log.append("banana", message={})
    with pytest.raises(ValueError):
        log.meta("user")  # node type via meta
    with pytest.raises(ValueError):
        log.attachment("nonsense", {})
    with pytest.raises(ValueError):
        log.system("nonsense", "x")
    with pytest.raises(ValueError):
        log.user("x", origin="alien")


# ── typed records ───────────────────────────────────────────────────────────────

def test_assistant_requires_model_and_role(log):
    with pytest.raises(ValueError):
        log.assistant({"role": "assistant", "content": []})  # no model
    with pytest.raises(ValueError):
        log.assistant({"role": "user", "model": "m", "content": []})


def test_assistant_synthetic_error_record(log):
    log.assistant(_assistant_msg(model="<synthetic>",
                                 content=[{"type": "text", "text": "boom"}]),
                  is_api_error=True)
    rec = log.read_all()[0]
    assert rec["isApiErrorMessage"] is True
    assert rec["message"]["model"] == "<synthetic>"


def test_tool_result_record_shape(log):
    a = log.assistant(_assistant_msg(content=[
        {"type": "tool_use", "id": "tc_abc", "name": "read_file", "input": {"path": "x.py"}}]))
    log.tool_result("tc_abc", "line1\nline2",
                    {"tool": "read_file", "ok": True, "target": "x.py",
                     "category": "file_read", "detail": {"lines": 2}},
                    source_assistant_uuid=a)
    rec = log.read_all()[-1]
    block = rec["message"]["content"][0]
    assert block["type"] == "tool_result" and block["tool_use_id"] == "tc_abc"
    assert rec["toolUseResult"]["category"] == "file_read"
    assert rec["sourceToolAssistantUUID"] == a


def test_all_attachment_types_accepted(log):
    for atype in sorted(ATTACHMENT_TYPES):
        log.attachment(atype, {"probe": atype})
    recs = log.read_all()
    assert {r["attachment"]["type"] for r in recs} == ATTACHMENT_TYPES


def test_notification_origin(log):
    log.user("<task-notification>…</task-notification>", origin="task-notification")
    assert log.read_all()[0]["origin"] == {"kind": "task-notification"}


# ── compaction boundary semantics ───────────────────────────────────────────────

def test_compact_boundary_is_new_root_and_mainline_follows_it(log):
    log.user("old work")
    log.assistant(_assistant_msg())
    log.system("compact_boundary", "Conversation compacted",
               compactMetadata={"trigger": "manual", "preTokens": 100, "postTokens": 10,
                                "durationMs": 5, "preservedUuids": [], "discoveredTools": [],
                                "reinjectedFiles": []})
    log.user("summary text", origin="compaction", isCompactSummary=True)
    log.assistant(_assistant_msg())

    boundary = [r for r in log.read_all() if r.get("subtype") == "compact_boundary"][0]
    assert boundary["parentUuid"] is None

    main = log.mainline()
    assert [r["type"] for r in main] == ["system", "user", "assistant"]
    assert "old work" not in json.dumps(main)  # pre-compaction records not in mainline


def test_mainline_without_compaction_is_everything(log):
    log.user("a")
    log.assistant(_assistant_msg())
    log.attachment("date_change", {"newDate": "2026-07-07"})
    assert [r["type"] for r in log.mainline()] == ["user", "assistant", "attachment"]


# ── integrity ───────────────────────────────────────────────────────────────────

def test_verify_clean_log(log):
    a = log.assistant(_assistant_msg(content=[
        {"type": "tool_use", "id": "tc_1", "name": "grep", "input": {}}]))
    log.tool_result("tc_1", "ok", {"tool": "grep", "ok": True, "target": None,
                                   "category": "search", "detail": {}}, a)
    rep = log.verify()
    assert rep.ok and rep.records == 2 and rep.roots == 1
    assert rep.orphan_parents == 0 and rep.unmatched_tool_calls == []


def test_verify_flags_unmatched_tool_call(log):
    log.assistant(_assistant_msg(content=[
        {"type": "tool_use", "id": "tc_lost", "name": "grep", "input": {}}]))
    rep = log.verify()
    assert not rep.ok and rep.unmatched_tool_calls == ["tc_lost"]


# ── persistence & resume ────────────────────────────────────────────────────────

def test_reopen_recovers_head_and_continues_chain(tmp_path):
    log1 = EventLog(tmp_path, SLUG)
    u1 = log1.user("first")
    sid = log1.session_id

    log2 = EventLog(tmp_path, SLUG, session_id=sid)
    assert log2.head_uuid == u1
    log2.assistant(_assistant_msg())
    assert log2.read_all()[-1]["parentUuid"] == u1


def test_list_sessions_newest_first(tmp_path):
    import os
    import time

    a = EventLog(tmp_path, SLUG)
    a.user("x")
    b = EventLog(tmp_path, SLUG)
    b.user("y")
    now = time.time()
    os.utime(a.path, (now - 60, now - 60))  # deterministic: 'a' is older
    os.utime(b.path, (now, now))
    paths = list_sessions(tmp_path, SLUG)
    assert len(paths) == 2
    assert paths[0].stem == b.session_id  # newest first


def test_concurrent_appends_are_safe(log):
    def worker(n):
        for _ in range(25):
            log.user(f"msg-{n}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    recs = log.read_all()
    assert len(recs) == 100
    assert log.verify().orphan_parents == 0
    assert len({r["uuid"] for r in recs}) == 100


# ── helpers ─────────────────────────────────────────────────────────────────────

def test_project_slug():
    assert project_slug("C:\\Users\\me\\proj") == "C--Users-me-proj"
    assert project_slug("/home/me/proj") == "-home-me-proj"


def test_new_tool_call_id_format():
    tid = new_tool_call_id()
    assert tid.startswith("tc_") and len(tid) == 27 and tid[3:].isalnum()
