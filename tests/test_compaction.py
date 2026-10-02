"""Phase 6 tests — compaction (module spec §6, schema §6–§7)."""

from __future__ import annotations

import json
import time

import loom.models as models
from loom.compaction import Compactor, _pick_tail, estimate_tokens
from loom.context import ContextAssembler, messages_from_records
from loom.eventlog import EventLog
from loom.models import ModelResponse
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore

SLUG = "T--compact"


def _final(text, cost=0.001):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 4, "completion_tokens": 2},
                         cost=cost, model="fake")


def _assistant_msg(text):
    return {"role": "assistant", "model": "fake", "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}


def _fake_completer(summary_text="1. Primary Request and Intent\nBuild the thing."):
    calls = []

    def completer(model, messages, tools, temperature):
        calls.append(messages)
        return _final(summary_text)

    completer.calls = calls
    return completer


# ── units ─────────────────────────────────────────────────────────────────────────

def test_estimate_and_threshold(monkeypatch):
    small = [{"role": "user", "content": "hi"}]
    big = [{"role": "user", "content": "x" * 4000}]
    assert estimate_tokens(big) > estimate_tokens(small) > 0

    monkeypatch.setenv("LOOM_COMPACT_AT", "500")
    c = Compactor(None, None, None, "fake")  # threshold check touches nothing else
    assert c.should_compact(501) and not c.should_compact(500)


def test_pick_tail_never_opens_on_a_tool_result(tmp_path):
    log = EventLog(tmp_path, SLUG)
    for i in range(4):
        log.user(f"msg {i}", origin="human")
        a = log.assistant({"role": "assistant", "model": "fake", "content": [
            {"type": "text", "text": f"reply {i}"},
            {"type": "tool_use", "id": f"tc{i}", "name": "grep", "input": {}}]})
        log.tool_result(f"tc{i}", "hit", {"tool": "grep", "ok": True, "target": None,
                                          "category": "search", "detail": {}}, a)
    tail = _pick_tail(log.mainline(), 2)  # naive slice would open on tc3's result
    first = tail[0]
    content = first.get("message", {}).get("content")
    opens_on_result = isinstance(content, list) and any(
        b.get("type") == "tool_result" for b in content if isinstance(b, dict))
    assert not opens_on_result
    # every tool_result in the tail has its assistant in the tail too
    tail_tool_ids = {b["id"] for r in tail if r["type"] == "assistant"
                     for b in r["message"]["content"] if b.get("type") == "tool_use"}
    for r in tail:
        c = r.get("message", {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    assert b["tool_use_id"] in tail_tool_ids


def test_compact_boundary_summary_tail_and_files(tmp_path):
    log = EventLog(tmp_path, SLUG)
    # 5 turns: the first must fall OUT of the post-compaction view (tail keeps 6 records)
    for i in range(5):
        log.user(f"user turn {i} SENTINEL-{i}", origin="human")
        log.assistant(_assistant_msg(f"assistant reply {i}"))
    # a touched file to re-inject
    hot = tmp_path / "hot.py"
    hot.write_text("HOT FILE CONTENT", encoding="utf-8")
    asm = ContextAssembler(log, str(tmp_path))
    asm.track("hot.py")
    # a deferred tool discovered (loaded) pre-compaction
    log.attachment("deferred_tools_delta", {"addedNames": ["grep_ultra"], "removedNames": [],
                                            "loaded": True})

    original_tail_uuids = [r["uuid"] for r in log.mainline()
                           if r["type"] in ("user", "assistant")][-6:]
    completer = _fake_completer("1. Primary Request and Intent\nSUMMARY-BODY")
    report = Compactor(log, asm, completer, "fake").compact("manual")

    # boundary: a new root with every metadata field populated
    boundary = next(r for r in log.read_all()
                    if r.get("type") == "system" and r.get("subtype") == "compact_boundary")
    assert boundary["parentUuid"] is None
    meta = boundary["compactMetadata"]
    assert meta["trigger"] == "manual"
    assert meta["preTokens"] > 0 and meta["postTokens"] > 0
    assert meta["durationMs"] >= 0
    assert meta["preservedUuids"] == original_tail_uuids == report.preserved_uuids
    assert meta["reinjectedFiles"] == [str(tmp_path / "hot.py")]
    assert meta["discoveredTools"] == ["grep_ultra"]

    # summary record: chained off the boundary, flagged, prefixed with the transcript path
    summary = next(r for r in log.read_all() if r.get("isCompactSummary"))
    assert summary["parentUuid"] == boundary["uuid"]
    assert summary["origin"]["kind"] == "compaction"
    assert str(log.path) in summary["message"]["content"]
    assert "SUMMARY-BODY" in summary["message"]["content"]

    # the post-compaction model view: summary + tail + file, and NOT the oldest turn
    mainline = log.mainline()
    assert mainline[0]["uuid"] == boundary["uuid"]
    msgs = messages_from_records(mainline, str(tmp_path), render_attachments=True)
    joined = "\n".join(str(m.get("content", "")) for m in msgs)
    assert "SUMMARY-BODY" in joined
    assert "assistant reply 4" in joined          # tail preserved verbatim
    assert "HOT FILE CONTENT" in joined            # file re-injected
    assert "grep_ultra" in joined                  # tools re-advertised
    assert "explore" in joined                     # agent listing re-advertised
    assert "SENTINEL-0" not in joined              # old history is out of context

    # re-appended assistant copies must not double-count usage
    tail_assistants = [r for r in mainline if r["type"] == "assistant"]
    assert all(not any(v for v in r["message"]["usage"].values() if v)
               for r in tail_assistants)
    assert log.verify().ok


def test_compaction_tail_does_not_double_count_activity(tmp_path):
    from loom.server.eventstore import activity_from_records

    log = EventLog(tmp_path, SLUG)
    log.user("do the work", origin="human")
    a = log.assistant({"role": "assistant", "model": "fake", "content": [
        {"type": "text", "text": "on it"},
        {"type": "tool_use", "id": "tc_act1", "name": "write_file",
         "input": {"path": "x.py"}}]})
    log.tool_result("tc_act1", "Created 'x.py'",
                    {"tool": "write_file", "ok": True, "target": "x.py",
                     "category": "file_write", "detail": {}}, a)

    before = activity_from_records(log.read_all())
    asm = ContextAssembler(log, str(tmp_path))
    Compactor(log, asm, _fake_completer(), "fake").compact("manual")
    after = activity_from_records(log.read_all())

    # the re-appended tail copies (isCompactTail) must not create a phantom second turn
    assert after == before
    assert len(after) == 1 and after[0]["files_edited"] == 1


# ── live: the agent continues across the boundary ──────────────────────────────────

def _run_and_wait(runner, text):
    runner.send(text)
    deadline = time.time() + 8
    while time.time() < deadline and runner.busy:
        time.sleep(0.05)
    while runner.busy:
        time.sleep(0.05)


def test_manual_compact_then_continue(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.delenv("LOOM_COMPACT_AT", raising=False)
    captured: list[list[dict]] = []
    script = iter([
        _final("noted FIRST-TURN-MARK"),
        _final("noted second"),
        _final("noted third"),
        _final("noted LAST-TURN-MARK"),
        _final("1. Primary Request and Intent\nCOMPACT-SUMMARY-MARK"),  # the summarizer
        _final("continuing after compaction"),
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

    for text in ("first", "second", "third", "fourth"):
        _run_and_wait(runner, text)

    result = runner.command("compact")
    assert "Compacted" in result["text"]

    _run_and_wait(runner, "keep going")
    last_turn = captured[-1]
    joined = "\n".join(str(m.get("content", "")) for m in last_turn)
    assert "COMPACT-SUMMARY-MARK" in joined      # summary reached the model
    assert "LAST-TURN-MARK" in joined            # tail reached the model
    assert "FIRST-TURN-MARK" not in joined       # old history did not
    roles = [m["role"] for m in last_turn]
    assert not any(roles[i] == roles[i + 1] == "user" for i in range(len(roles) - 1))


def test_auto_compaction_fires_when_over_threshold(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.setenv("LOOM_COMPACT_AT", "10")  # anything triggers
    script = iter([
        _final("a normal reply"),
        _final("1. Primary Request and Intent\nAUTO-SUMMARY"),  # the summarizer
    ])
    monkeypatch.setattr(models, "complete",
                        lambda model, messages, tools, temperature: next(script))
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)

    _run_and_wait(runner, "hello")
    log = store.log_for(meta["id"])
    boundary = next((r for r in log.read_all()
                     if r.get("type") == "system"
                     and r.get("subtype") == "compact_boundary"), None)
    assert boundary is not None
    assert boundary["compactMetadata"]["trigger"] == "auto"
    # the transcript shows the compaction marker for the UI
    events = store.transcript(meta["id"])
    assert any(e["type"] == "compaction" for e in events)
    # a queued follow-up still works on the new root
    assert json.loads(json.dumps(events))  # transcript remains JSON-serializable
