"""The Loom desktop app — FastAPI routes + the WebSocket chat endpoint.

REST is for state (sessions, stats, settings, skills, artifacts, dir browsing);
the WebSocket is the live wire: user messages go in, agent events stream out,
permission prompts round-trip. The frontend in static/ is a plain SPA — no build step.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .. import __version__, skills
from ..compaction import compact_threshold
from ..config import Config
from ..ledger import TaskLedger
from . import settings as user_settings
from .bridge import RunnerRegistry
from .eventstore import EventStore

STATIC_DIR = Path(__file__).parent / "static"
MAX_PREVIEW_BYTES = 100_000


def create_app(store: EventStore | None = None) -> FastAPI:
    user_settings.apply_to_env()
    store = store or EventStore()
    runners = RunnerRegistry(store)
    app = FastAPI(title="Loom", version=__version__)

    # ── app state ──────────────────────────────────────────────────────────────
    @app.get("/api/state")
    def state() -> dict[str, Any]:
        config = Config()
        return {
            "version": __version__,
            "display_name": os.getenv("LOOM_DISPLAY_NAME", ""),
            "model": config.model,
            "model_strong": config.model_strong,
            "model_critic": config.model_critic,
            "home": str(Path.home()),
            "has_key": any(os.getenv(k) for k in user_settings.PROVIDER_KEYS),
            "compact_at": compact_threshold(),  # the context ring's "full" point
        }

    @app.get("/api/stats")
    def stats() -> dict[str, Any]:
        return store.stats()

    # ── settings (bring-your-own-key) ─────────────────────────────────────────
    @app.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return {
            "values": user_settings.masked(),
            "provider_keys": user_settings.PROVIDER_KEYS,
            "plain_fields": user_settings.PLAIN_FIELDS,
        }

    @app.post("/api/settings")
    def post_settings(updates: dict[str, str] = Body(...)) -> dict[str, Any]:
        # masked values round-tripping from the UI must not overwrite real keys
        updates = {k: v for k, v in updates.items() if not str(v).startswith("••••")}
        user_settings.save(updates)
        return {"ok": True, "values": user_settings.masked()}

    # ── sessions ──────────────────────────────────────────────────────────────
    @app.get("/api/sessions")
    def list_sessions() -> list[dict[str, Any]]:
        return store.list()

    @app.post("/api/sessions")
    def create_session(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        workdir = str(payload.get("workdir") or "").strip() or str(Path.home())
        if not Path(workdir).is_dir():
            raise HTTPException(400, f"not a directory: {workdir}")
        model = str(payload.get("model") or "").strip() or Config().model
        return store.create(workdir=str(Path(workdir).resolve()), model=model)

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            data = store.get(session_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"meta": data["meta"], "events": data["events"]}

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        runners.drop(session_id)
        store.delete(session_id)
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/activity")
    def session_activity(session_id: str) -> list[dict[str, Any]]:
        try:
            return store.activity(session_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/records")
    def session_records(session_id: str, after: str = "") -> list[dict[str, Any]]:
        try:
            return store.records_after(session_id, after)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/sessions/{session_id}/tasks")
    def session_tasks(session_id: str) -> list[dict[str, Any]]:
        return TaskLedger(store.root, session_id).list()

    # ── skills (the "/" menu) ─────────────────────────────────────────────────
    @app.get("/api/skills")
    def list_skills(workdir: str = ".") -> list[dict[str, str]]:
        return [
            {"name": s.name, "description": s.description, "source": s.source}
            for s in skills.discover(workdir)
        ]

    # ── artifacts (files the agent wrote, across sessions) ────────────────────
    @app.get("/api/artifacts")
    def artifacts() -> list[dict[str, Any]]:
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for meta in store.list():
            try:
                data = store.get(meta["id"])
            except KeyError:
                continue
            for event in data["events"]:
                if (
                    event.get("type") == "tool"
                    and event.get("name") in {"write_file", "edit_file"}
                    and event.get("outcome") == "ok"
                ):
                    try:
                        path = json.loads(event.get("arguments") or "{}").get("path")
                    except json.JSONDecodeError:
                        path = None
                    if not path:
                        continue
                    key = (meta["workdir"], path)
                    item = {
                        "path": path,
                        "workdir": meta["workdir"],
                        "session_id": meta["id"],
                        "session_title": meta.get("title", ""),
                        "action": event["name"],
                        "ts": event.get("ts", 0),
                    }
                    if key not in seen or item["ts"] > seen[key]["ts"]:
                        seen[key] = item
        return sorted(seen.values(), key=lambda a: a["ts"], reverse=True)[:200]

    @app.get("/api/file")
    def file_preview(workdir: str, path: str) -> dict[str, Any]:
        root = Path(workdir).resolve()
        target = (root / path).resolve()
        if root != target and root not in target.parents:
            raise HTTPException(400, "path escapes the workdir")
        if not target.is_file():
            raise HTTPException(404, f"not a file: {path}")
        size = target.stat().st_size
        text = target.read_text(encoding="utf-8", errors="replace")[:MAX_PREVIEW_BYTES]
        return {"path": path, "size": size, "truncated": size > MAX_PREVIEW_BYTES, "content": text}

    # ── directory browser (for the project picker) ────────────────────────────
    @app.get("/api/browse")
    def browse(path: str = "") -> dict[str, Any]:
        p = Path(path).expanduser() if path else Path.home()
        try:
            p = p.resolve()
            if not p.is_dir():
                raise HTTPException(400, f"not a directory: {path}")
            dirs = sorted(
                e.name
                for e in p.iterdir()
                if e.is_dir() and not e.name.startswith(".") and e.name not in {"node_modules", "__pycache__"}
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        parent = str(p.parent) if p.parent != p else None
        return {"path": str(p), "parent": parent, "dirs": dirs}

    # ── the live wire ─────────────────────────────────────────────────────────
    @app.websocket("/ws/{session_id}")
    async def ws_chat(ws: WebSocket, session_id: str) -> None:
        await ws.accept()
        try:
            runner = runners.get(session_id)
        except KeyError:
            await ws.close(code=4004)
            return
        runner.drain_stale()
        await ws.send_json({"type": "hello", "busy": runner.busy})

        stop = asyncio.Event()

        async def pump() -> None:
            while not stop.is_set():
                try:
                    event = await asyncio.to_thread(runner.queue.get, True, 0.5)
                except queue.Empty:
                    continue
                await ws.send_json(event)

        pump_task = asyncio.create_task(pump())
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                if kind == "user_message":
                    if not runner.send(str(msg.get("text", ""))):
                        await ws.send_json({"type": "busy"})
                elif kind == "skill":
                    skill = skills.find(runner.agent.config.workdir, str(msg.get("name", "")))
                    if skill is None:
                        await ws.send_json(
                            {"type": "command_result", "command": msg.get("name"), "text": "Unknown skill."}
                        )
                    else:
                        arg = str(msg.get("arg", ""))
                        display = f"/{skill.name} {arg}".strip()
                        if not runner.send(skills.render(skill, arg), display=display):
                            await ws.send_json({"type": "busy"})
                elif kind == "permission_response":
                    runner.permissions.resolve(str(msg.get("id", "")), str(msg.get("answer", "n")))
                elif kind == "command":
                    result = runner.command(str(msg.get("name", "")), str(msg.get("arg", "")))
                    await ws.send_json(result)
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()
            pump_task.cancel()

    # static SPA last, so /api/* and /ws/* win the routing race
    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
