"""Phase 7 tests — long-term project memory (module spec §10, schema §8)."""

from __future__ import annotations

import os
import time

import pytest

import loom.models as models
from loom.eventlog import project_slug
from loom.memory import MemoryStore, slugify
from loom.models import ModelResponse, ToolCall
from loom.server.bridge import SessionRunner
from loom.server.eventstore import EventStore


# ── units ─────────────────────────────────────────────────────────────────────────

def test_slugify():
    assert slugify("Prefers Pytest, not unittest!") == "prefers-pytest-not-unittest"
    assert slugify("---") == "memory"


def test_write_matches_schema_and_indexes(tmp_path):
    mem = MemoryStore(tmp_path, "T--proj")
    path = mem.write("Deploy Notes", "How deploys work", "project",
                     "Deploy with `make ship`.\nNever on Fridays.", origin_session="sess-a")
    assert path.name == "deploy-notes.md"
    text = path.read_text(encoding="utf-8")
    # schema §8: frontmatter with name == filename minus .md
    assert text.startswith("---\n")
    assert "name: deploy-notes\n" in text
    assert "description: How deploys work\n" in text
    assert "metadata:\n  type: project\n  originSessionId: sess-a\n---\n" in text
    assert "Never on Fridays." in text
    assert "- [deploy-notes](deploy-notes.md) — How deploys work" in mem.index_markdown()


def test_index_upserts_not_duplicates(tmp_path):
    mem = MemoryStore(tmp_path, "T--proj")
    mem.write("fact-one", "first version", "project", "v1")
    mem.write("fact-one", "second version", "project", "v2")
    mem.write("fact-two", "another", "user", "x")
    index = mem.index_markdown()
    assert index.count("(fact-one.md)") == 1
    assert "second version" in index and "first version" not in index
    assert mem.read("fact-one").endswith("v2\n")

    assert mem.delete("fact-one")
    assert "(fact-one.md)" not in mem.index_markdown()
    assert mem.read("fact-one") is None


def test_invalid_type_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid memory type"):
        MemoryStore(tmp_path, "T--proj").write("n", "d", "vibes", "b")


def test_recall_is_recency_ordered_and_skips_index(tmp_path):
    mem = MemoryStore(tmp_path, "T--proj")
    for i, name in enumerate(("oldest", "middle", "newer", "newest")):
        p = mem.write(name, f"desc {name}", "project", name)
        os.utime(p, (time.time() - 300 + i * 60, time.time() - 300 + i * 60))
    recalled = [p.stem for p in mem.recall(3)]
    assert recalled == ["newest", "newer", "middle"]
    assert "MEMORY" not in recalled


# ── integration: session A writes, session B starts with it ────────────────────────

def _final(text):
    return ModelResponse(text=text, tool_calls=[],
                         assistant_message={"role": "assistant", "content": text},
                         usage={"prompt_tokens": 4, "completion_tokens": 2},
                         cost=0.0, model="fake")


def _tool_call(cid, name, args):
    import json
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


def test_memory_from_session_a_reaches_session_b(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()

    # "session A" saved a fact (via the same MemoryStore the runner uses)
    mem = MemoryStore(store.root, project_slug(str(project)))
    mem.write("prefers-tabs", "User prefers tabs over spaces", "user",
              "MEMORY-BODY: the user always wants tabs.", origin_session="sess-a")

    captured: list[list[dict]] = []

    def fake(model, messages, tools, temperature):
        captured.append([dict(m) for m in messages])
        return _final("hello")

    monkeypatch.setattr(models, "complete", fake)
    meta_b = store.create(workdir=str(project), model="fake")
    runner = SessionRunner(meta_b["id"], store)
    _run_and_wait(runner, "hi")

    first_turn = captured[0]
    # 1. the index line is in session B's system prompt
    assert first_turn[0]["role"] == "system"
    assert "prefers-tabs" in first_turn[0]["content"]
    assert "User prefers tabs over spaces" in first_turn[0]["content"]
    # 2. the full fact was recalled into the first turn as a `file` attachment
    user_text = "\n".join(m.get("content", "") for m in first_turn
                          if m["role"] == "user" and isinstance(m.get("content"), str))
    assert "MEMORY-BODY" in user_text
    # and the recall is recorded in the log as a file attachment with reason=recall
    log = store.log_for(meta_b["id"])
    recalls = [r for r in log.read_all() if r.get("type") == "attachment"
               and r["attachment"].get("reason") == "recall"]
    assert len(recalls) == 1 and recalls[0]["attachment"]["type"] == "file"


def test_model_saves_memory_via_tool_and_next_session_sees_it(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOM_CRITIC", "0")
    captured: list[list[dict]] = []
    script = iter([
        _tool_call("m1", "memory_write", {
            "name": "Deploy Ritual", "description": "How we deploy",
            "type": "project", "body": "Deploy with make ship."}),
        _final("Saved."),
        _final("hello from session two"),
    ])

    def fake(model, messages, tools, temperature):
        captured.append([dict(m) for m in messages])
        return next(script)

    monkeypatch.setattr(models, "complete", fake)
    store = EventStore(tmp_path / "store")
    project = tmp_path / "proj"
    project.mkdir()

    meta_a = store.create(workdir=str(project), model="fake")
    runner_a = SessionRunner(meta_a["id"], store)
    _run_and_wait(runner_a, "remember how we deploy")

    mem = MemoryStore(store.root, project_slug(str(project)))
    saved = mem.read("deploy-ritual")
    assert saved is not None and "make ship" in saved
    assert "(deploy-ritual.md)" in mem.index_markdown()

    # a NEW session's very first model call carries the index in its system prompt
    meta_b = store.create(workdir=str(project), model="fake")
    runner_b = SessionRunner(meta_b["id"], store)
    _run_and_wait(runner_b, "hi again")
    assert "deploy-ritual" in captured[-1][0]["content"]
