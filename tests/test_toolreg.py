"""Phase 8 tests — deferred tools (module spec §7, schema §5)."""

from __future__ import annotations

import json
import time

import loom.models as models
from loom.models import ModelResponse, ToolCall
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore
from loom.toolreg import ToolRegistry, loaded_from_records
from loom.tools.base import ToolMeta, _build_schema


def _meta(fn) -> ToolMeta:
    return ToolMeta(name=fn.__name__, fn=fn, schema=_build_schema(fn), risky=False)


def _hammer(nail: str) -> str:
    """Drive a nail into a board."""
    return f"drove {nail}"


def _wrench(bolt: str) -> str:
    """Turn a bolt until tight."""
    return f"turned {bolt}"


# ── units ─────────────────────────────────────────────────────────────────────────

def test_registry_hides_deferred_until_loaded():
    reg = ToolRegistry(core=[_meta(_hammer)], deferred=[_meta(_wrench)])
    names = [s["function"]["name"] for s in reg.schemas_for_context()]
    assert names == ["_hammer"]
    assert reg.deferred_names() == ["_wrench"]
    assert reg.lookup("_wrench") is None          # deferred + unloaded
    assert reg.lookup("_hammer") is not None

    hits = reg.load("select:_wrench")
    assert [m.name for m in hits] == ["_wrench"]
    assert reg.lookup("_wrench") is not None
    assert reg.discovered() == ["_wrench"]
    names = [s["function"]["name"] for s in reg.schemas_for_context()]
    assert names == ["_hammer", "_wrench"]
    assert reg.deferred_names() == []
    # loading again doesn't duplicate
    reg.load("select:_wrench")
    assert reg.discovered() == ["_wrench"]


def test_registry_keyword_match_and_restore():
    reg = ToolRegistry(core=[], deferred=[_meta(_hammer), _meta(_wrench)])
    hits = reg.load("bolt tighten")               # matches _wrench's description
    assert [m.name for m in hits] == ["_wrench"]
    reg2 = ToolRegistry(core=[], deferred=[_meta(_hammer), _meta(_wrench)])
    reg2.restore(["_wrench", "nonexistent"])
    assert reg2.discovered() == ["_wrench"]


def test_loaded_from_records_replays_only_loaded_deltas():
    records = [
        {"type": "attachment", "attachment": {"type": "deferred_tools_delta",
                                              "addedNames": ["a"], "removedNames": []}},
        {"type": "attachment", "attachment": {"type": "deferred_tools_delta",
                                              "addedNames": ["b"], "removedNames": [],
                                              "loaded": True}},
        {"type": "user"},
    ]
    assert loaded_from_records(records) == ["b"]


# ── live: the model must discover grep through tool_search ─────────────────────────

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


def test_deferred_tool_enforced_then_discovered(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.delenv("LOOM_DEFERRED", raising=False)   # default: grep is deferred
    monkeypatch.delenv("LOOM_STOP_CONDITION", raising=False)
    captured_tools: list[list[str]] = []
    tool_results: list[str] = []
    script = iter([
        _tool_call("t1", "grep", {"pattern": "needle"}),             # blocked: not loaded
        _tool_call("t2", "tool_search", {"query": "select:grep"}),   # discover it
        _tool_call("t3", "grep", {"pattern": "needle"}),             # now it works
        _final("found it"),
    ])

    def fake(model, messages, tools, temperature):
        captured_tools.append(sorted(t["function"]["name"] for t in tools or []))
        for m in messages:
            if m.get("role") == "tool" and m.get("content"):
                if m["content"] not in tool_results:
                    tool_results.append(m["content"])
        return next(script)

    monkeypatch.setattr(models, "complete", fake)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "hay.txt").write_text("a needle in a haystack", encoding="utf-8")
    meta = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta["id"], store)
    _run_and_wait(runner, "find the needle")

    # grep's schema was absent until after tool_search, then present
    assert "grep" not in captured_tools[0] and "tool_search" in captured_tools[0]
    assert "grep" in captured_tools[-1]
    # the blocked call produced the recoverable error, and the retry actually ran
    assert any("deferred; call tool_search first" in r for r in tool_results)
    assert any("needle" in r and "hay.txt" in r for r in tool_results)
    # the discovery was recorded as a loaded delta (survives resume + compaction)
    log = store.log_for(meta["id"])
    assert loaded_from_records(log.read_all()) == ["grep"]

    # the session-start advertisement (name-only, not loaded) was recorded
    adverts = [r for r in log.read_all() if r.get("type") == "attachment"
               and r["attachment"].get("type") == "deferred_tools_delta"
               and not r["attachment"].get("loaded")]
    assert adverts and adverts[0]["attachment"]["addedNames"] == ["grep"]

    # resume: a fresh runner restores the discovered set
    runner2 = SessionRunner(meta["id"], store)
    assert runner2.toolreg.discovered() == ["grep"]
    assert "grep" not in runner2.agent.engine.deferred_hidden
