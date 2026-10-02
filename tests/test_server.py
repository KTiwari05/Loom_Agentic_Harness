"""Desktop-app server tests: REST endpoints + the full WebSocket chat flow.

The WS test drives a real agent turn (scripted model, real tools, real permission
bridge): user message -> permission prompt -> Allow -> file written -> done -> stats.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import loom.models as models
from loom.models import ModelResponse, ToolCall
from loom.server import settings as user_settings
from loom.server.app import create_app
from loom.server.eventstore import EventStore


def _write_call(path: str, content: str) -> ModelResponse:
    args = json.dumps({"path": path, "content": content})
    return ModelResponse(
        text="Creating the file now.",
        tool_calls=[ToolCall(id="c1", name="write_file", arguments=args)],
        assistant_message={
            "role": "assistant",
            "content": "Creating the file now.",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": args}}
            ],
        },
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        cost=0.0001,
        model="fake",
    )


def _final(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=[],
        assistant_message={"role": "assistant", "content": text},
        usage={"prompt_tokens": 12, "completion_tokens": 4},
        cost=0.0001,
        model="fake",
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # isolate settings + sessions from the real ~/.loom
    monkeypatch.setattr(user_settings, "LOOM_HOME", tmp_path / "loomhome")
    monkeypatch.setattr(user_settings, "ENV_FILE", tmp_path / "loomhome" / "loom.env")
    monkeypatch.setenv("LOOM_CRITIC", "0")
    monkeypatch.delenv("LOOM_AUTO_APPROVE", raising=False)
    store = EventStore(tmp_path / "store")
    app = create_app(store=store)
    with TestClient(app) as c:
        yield c


def test_state_sessions_and_browse(client, tmp_path):
    state = client.get("/api/state").json()
    assert "model" in state and "version" in state

    project = tmp_path / "proj"
    project.mkdir()
    meta = client.post("/api/sessions", json={"workdir": str(project)}).json()
    assert meta["title"] == "New session" and meta["workdir"] == str(project.resolve())

    listed = client.get("/api/sessions").json()
    assert [m["id"] for m in listed] == [meta["id"]]

    browse = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    assert "proj" in browse["dirs"]

    assert client.post("/api/sessions", json={"workdir": str(tmp_path / "nope")}).status_code == 400


def test_settings_roundtrip_masks_secrets(client):
    r = client.post(
        "/api/settings",
        json={"OPENROUTER_API_KEY": "sk-or-test-abcd1234", "LOOM_MODEL": "openrouter/x/y"},
    ).json()
    assert r["values"]["OPENROUTER_API_KEY"].startswith("••••")
    assert r["values"]["OPENROUTER_API_KEY"].endswith("1234")
    assert r["values"]["LOOM_MODEL"] == "openrouter/x/y"

    # posting the masked value back must NOT clobber the real key
    client.post("/api/settings", json={"OPENROUTER_API_KEY": r["values"]["OPENROUTER_API_KEY"]})
    saved = user_settings.load_file()
    assert saved["OPENROUTER_API_KEY"] == "sk-or-test-abcd1234"


def test_ws_chat_flow_with_permission_gate(client, tmp_path, monkeypatch):
    responses = iter([_write_call("hello.txt", "hi from the app"), _final("All done.")])
    monkeypatch.setattr(models, "complete", lambda *a, **k: next(responses))

    project = tmp_path / "wsproj"
    project.mkdir()
    meta = client.post("/api/sessions", json={"workdir": str(project)}).json()

    seen: list[dict] = []
    with client.websocket_connect(f"/ws/{meta['id']}") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "user_message", "text": "create hello.txt"})
        for _ in range(40):  # safety bound
            event = ws.receive_json()
            seen.append(event)
            if event["type"] == "permission_request":
                assert event["tool"] == "write_file"
                assert "hello.txt" in event["preview"]
                ws.send_json({"type": "permission_response", "id": event["id"], "answer": "y"})
            if event["type"] == "turn_end":
                break

        # slash commands + unknown skill answer over the same socket
        ws.send_json({"type": "command", "name": "cost"})
        assert "spent" in ws.receive_json()["text"]
        ws.send_json({"type": "skill", "name": "no-such-skill", "arg": ""})
        assert ws.receive_json()["text"] == "Unknown skill."

    types = [e["type"] for e in seen]
    assert "user" in types and "step" in types and "tool" in types
    assert "done" in types and "turn_end" in types
    assert (project / "hello.txt").read_text(encoding="utf-8") == "hi from the app"

    # transcript persisted — but not the ephemeral permission_request
    stored = client.get(f"/api/sessions/{meta['id']}").json()
    stored_types = [e["type"] for e in stored["events"]]
    assert "tool" in stored_types and "permission_request" not in stored_types
    assert stored["meta"]["title"] == "create hello.txt"

    # the dashboard sees the activity
    stats = client.get("/api/stats").json()
    assert stats["sessions"] >= 1 and stats["messages"] >= 2 and stats["tokens"] > 0

    # the file the agent wrote is an artifact
    artifacts = client.get("/api/artifacts").json()
    assert any(a["path"] == "hello.txt" for a in artifacts)

    preview = client.get(
        "/api/file", params={"workdir": str(project), "path": "hello.txt"}
    ).json()
    assert preview["content"] == "hi from the app"
    assert client.get(
        "/api/file", params={"workdir": str(project), "path": "../outside.txt"}
    ).status_code == 400


def test_denied_permission_blocks_the_write(client, tmp_path, monkeypatch):
    responses = iter([_write_call("secret.txt", "x"), _final("Understood, stopping.")])
    monkeypatch.setattr(models, "complete", lambda *a, **k: next(responses))

    project = tmp_path / "denyproj"
    project.mkdir()
    meta = client.post("/api/sessions", json={"workdir": str(project)}).json()

    with client.websocket_connect(f"/ws/{meta['id']}") as ws:
        ws.receive_json()
        ws.send_json({"type": "user_message", "text": "write a secret"})
        for _ in range(40):
            event = ws.receive_json()
            if event["type"] == "permission_request":
                ws.send_json({"type": "permission_response", "id": event["id"], "answer": "n"})
            if event["type"] == "turn_end":
                break
    assert not (project / "secret.txt").exists()


def test_skills_endpoint_lists_builtins(client):
    names = {s["name"] for s in client.get("/api/skills", params={"workdir": "."}).json()}
    assert {"commit", "review", "test"} <= names
