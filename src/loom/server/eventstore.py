"""EventStore — sessions, transcript, and the home dashboard, all DERIVED from event logs.

v1 kept three parallel lists per session (meta / events / messages). v2 keeps ONE
append-only event log per session (loom.eventlog.EventLog) and derives everything else:

- the session list + per-session meta (title, counts, tokens, cost)  ← scanned from records
- the UI transcript                                                   ← EventLog.mainline()
- the dashboard (streaks / heatmap / favorite model)                 ← record timestamps + usage
- the raw model conversation for resume                              ← rebuild_messages()

The only thing NOT in the log is per-session *config* (workdir + default model + created
time): those are constants, not conversation state, so they live in a tiny `<id>.meta.json`
sidecar. The log remains the single source of truth for everything the user sees.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..compaction import estimate_tokens
from ..context import messages_from_records
from ..eventlog import EventLog, project_slug

LOOM_HOME = Path.home() / ".loom"
_HOBBIT_TOKENS = 125_000


class EventStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else LOOM_HOME
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    # ── locating logs ───────────────────────────────────────────────────────────
    def _all_logs(self) -> list[Path]:
        return list(self.sessions_dir.glob("*/*.jsonl"))

    def _locate(self, session_id: str) -> Path:
        for p in self._all_logs():
            if p.stem == session_id:
                return p
        raise KeyError(f"unknown session {session_id}")

    def _sidecar(self, log_path: Path) -> Path:
        return log_path.with_suffix(".meta.json")

    def _config(self, session_id: str) -> dict[str, Any]:
        sc = self._sidecar(self._locate(session_id))
        if sc.is_file():
            return json.loads(sc.read_text(encoding="utf-8"))
        return {}

    def log_for(self, session_id: str) -> EventLog:
        path = self._locate(session_id)
        slug = path.parent.name
        return EventLog(self.root, slug, session_id)

    # ── lifecycle ───────────────────────────────────────────────────────────────
    def create(self, workdir: str, model: str) -> dict[str, Any]:
        slug = project_slug(workdir)
        log = EventLog(self.root, slug)
        log.path.touch()  # make the empty session visible to list()/glob immediately
        cfg = {
            "id": log.session_id, "slug": slug, "workdir": workdir,
            "model": model, "created": time.time(), "title": "New session",
        }
        self._sidecar(log.path).write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        return self._meta(log.session_id)

    def delete(self, session_id: str) -> None:
        try:
            path = self._locate(session_id)
        except KeyError:
            return
        self._sidecar(path).unlink(missing_ok=True)
        path.unlink(missing_ok=True)

    def set_model(self, session_id: str, model: str) -> None:
        with self.lock:
            path = self._locate(session_id)
            sc = self._sidecar(path)
            cfg = json.loads(sc.read_text(encoding="utf-8")) if sc.is_file() else {}
            cfg["model"] = model
            sc.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    # ── reads for the API ───────────────────────────────────────────────────────
    def list(self) -> list[dict[str, Any]]:
        metas = []
        for path in self._all_logs():
            try:
                metas.append(self._meta(path.stem))
            except (KeyError, json.JSONDecodeError):
                continue
        return sorted(metas, key=lambda m: m.get("updated", 0), reverse=True)

    def get(self, session_id: str) -> dict[str, Any]:
        meta = self._meta(session_id)
        log = self.log_for(session_id)
        # what the model's context currently holds, estimated (drives the UI ring)
        meta["context_tokens"] = estimate_tokens(
            messages_from_records(log.mainline(), meta.get("workdir", "."),
                                  render_attachments=False))
        return {"meta": meta, "events": self.transcript(session_id)}

    def activity(self, session_id: str) -> list[dict[str, Any]]:
        """Per-turn deterministic activity summaries (module spec §12)."""
        log = self.log_for(session_id)
        return activity_from_records(
            [r for r in log.read_all() if not r.get("isSidechain")])

    def records_after(self, session_id: str, after: str = "") -> list[dict[str, Any]]:
        """Incremental record fetch: everything appended after the given uuid."""
        records = self.log_for(session_id).read_all()
        if not after:
            return records
        for i, r in enumerate(records):
            if r.get("uuid") == after:
                return records[i + 1:]
        return records

    # ── meta derivation ─────────────────────────────────────────────────────────
    def _meta(self, session_id: str) -> dict[str, Any]:
        cfg = self._config(session_id)
        log = self.log_for(session_id)
        records = log.read_all()

        title = cfg.get("title") or "New session"
        message_count = 0
        tokens = 0
        cost = 0.0
        latest_model = cfg.get("model", "")
        last_ts = cfg.get("created", 0.0)
        last_prompt = ""

        for r in records:
            t = r.get("type")
            if r.get("timestamp"):
                last_ts = max(last_ts, _epoch(r["timestamp"]))
            if t == "last-prompt":
                last_prompt = r.get("content") or last_prompt
            elif t == "ai-title":
                title = r.get("aiTitle") or title
            elif t == "user" and r.get("origin", {}).get("kind") == "human":
                content = r.get("message", {}).get("content")
                if isinstance(content, str) and not r.get("isSidechain"):
                    message_count += 1
                    if title in ("New session", "") and content.strip():
                        title = _title_from(r.get("display") or content)
            elif t == "assistant" and not r.get("isApiErrorMessage"):
                msg = r.get("message", {})
                # sidechain turns don't count as messages, but their usage/cost is
                # real spend and stays in the totals below
                if not r.get("isSidechain") and any(
                        b.get("type") == "text" and b.get("text", "").strip()
                        for b in msg.get("content", []) if isinstance(b, dict)):
                    message_count += 1
                latest_model = msg.get("model") or latest_model
                usage = msg.get("usage", {})
                tokens += int(usage.get("input_tokens", 0) or 0)
                tokens += int(usage.get("output_tokens", 0) or 0)
                if r.get("costUsd"):
                    cost += float(r["costUsd"])

        return {
            "id": session_id,
            "title": title,
            "workdir": cfg.get("workdir", ""),
            "model": latest_model,
            "created": cfg.get("created", 0.0),
            "updated": last_ts,
            "message_count": message_count,
            "tokens": tokens,
            "total_cost": round(cost, 6),
            "last_prompt": last_prompt,
        }

    # ── transcript derivation (records -> the UI event shape the SPA renders) ─────
    def transcript(self, session_id: str) -> list[dict[str, Any]]:
        log = self.log_for(session_id)
        events = _events_from(log.mainline())

        # Sidechains: a subagent run's records live in the same file, grouped by
        # sidechainId. Its events are tagged `subagent` so the UI renders them nested
        # (the goal user record is skipped — the parent's `task` tool row covers it).
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in log.read_all():
            if r.get("isSidechain") and r.get("sidechainId"):
                groups.setdefault(r["sidechainId"], []).append(r)
        for sid, recs in groups.items():
            atype = next((r["sidechainAgentType"] for r in recs
                          if r.get("sidechainAgentType")), "agent")
            for ev in _events_from(recs):
                if ev["type"] == "user":
                    continue
                ev["subagent"] = atype
                ev["sidechain_id"] = sid
                events.append(ev)

        events.sort(key=lambda e: e.get("ts") or 0)  # stable: mainline order preserved
        return events

    # ── the dashboard ───────────────────────────────────────────────────────────
    def stats(self) -> dict[str, Any]:
        sessions = messages = tokens = 0
        models: Counter[str] = Counter()
        hours: Counter[int] = Counter()
        per_day: Counter[str] = Counter()

        for path in self._all_logs():
            sessions += 1
            try:
                records = EventLog(self.root, path.parent.name, path.stem).read_all()
            except (OSError, json.JSONDecodeError):
                continue
            for r in records:
                t = r.get("type")
                if t == "user" and r.get("origin", {}).get("kind") == "human":
                    content = r.get("message", {}).get("content")
                    if isinstance(content, str) and not r.get("isSidechain"):
                        messages += 1
                        dt = datetime.fromtimestamp(_epoch(r.get("timestamp", "")))
                        hours[dt.hour] += 1
                        per_day[dt.date().isoformat()] += 1
                elif t == "assistant" and not r.get("isApiErrorMessage"):
                    msg = r.get("message", {})
                    if not r.get("isSidechain") and any(
                            b.get("type") == "text" and b.get("text", "").strip()
                            for b in msg.get("content", []) if isinstance(b, dict)):
                        messages += 1
                    if msg.get("model"):
                        models[msg["model"]] += 1
                    usage = msg.get("usage", {})
                    tokens += int(usage.get("input_tokens", 0) or 0)
                    tokens += int(usage.get("output_tokens", 0) or 0)

        current, longest = _streaks({date.fromisoformat(d) for d in per_day})
        return {
            "sessions": sessions,
            "messages": messages,
            "tokens": tokens,
            "active_days": len(per_day),
            "current_streak": current,
            "longest_streak": longest,
            "peak_hour": hours.most_common(1)[0][0] if hours else None,
            "favorite_model": models.most_common(1)[0][0] if models else None,
            "heatmap": _heatmap(per_day),
            "fun_line": _fun_line(tokens),
        }


# ── resume: rebuild the raw model conversation from the log ──────────────────────

def rebuild_messages(records: list[dict[str, Any]], workdir: str,
                     extra_system: str = "") -> list[dict[str, Any]]:
    """Reconstruct the chat message list the Orchestrator continues from, so a reopened
    session picks up where it left off — WITHOUT a separate `messages` store. Delegates to
    the single canonical builder in loom.context (no attachment rendering for plain resume)."""
    return messages_from_records(records, workdir, render_attachments=False,
                                 extra_system=extra_system)


# ── activity aggregation (module spec §12 — deterministic, NO model call) ─────────

def activity_from_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group records into turns (human user record → next) and summarize tool mirrors."""
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for r in records:
        if r.get("isCompactTail"):
            continue  # verbatim copies of already-counted records (compaction §6)
        t = r.get("type")
        if t == "user":
            content = r.get("message", {}).get("content")
            origin = r.get("origin", {}).get("kind")
            if (isinstance(content, str) and origin == "human"
                    and not r.get("isMeta") and not r.get("isCompactSummary")):
                current = {"prompt": (r.get("display") or content)[:120], "mirrors": []}
                turns.append(current)
                continue
            mirror = r.get("toolUseResult")
            if current is not None and isinstance(mirror, dict):
                current["mirrors"].append(mirror)
    out = []
    for i, turn in enumerate(turns, 1):
        counts = _activity_counts(turn["mirrors"])
        out.append({"turn": i, "prompt": turn["prompt"],
                    "summary": summarize_activity(counts), **counts})
    return out


def _activity_counts(mirrors: list[dict[str, Any]]) -> dict[str, int]:
    reads: set[str] = set()
    edits: set[str] = set()
    searches = commands = subagents = 0
    for m in mirrors:
        cat, target = m.get("category"), m.get("target")
        if cat == "file_read" and target:
            reads.add(target)
        elif cat in ("file_write", "file_edit") and target:
            edits.add(target)
        elif cat == "search":
            searches += 1
        elif cat == "command":
            commands += 1
        elif cat == "agent":
            subagents += 1
    return {"files_read": len(reads), "files_edited": len(edits),
            "searches": searches, "commands": commands, "subagents": subagents}


def summarize_activity(c: dict[str, int]) -> str:
    """'Read 4 files · Edited 2 files · Ran 3 commands' — zero groups omitted."""
    def n(count: int, noun: str) -> str:
        return f"{count} {noun}{'' if count == 1 else 's'}"
    parts = []
    if c["files_read"]:
        parts.append(f"Read {n(c['files_read'], 'file')}")
    if c["files_edited"]:
        parts.append(f"Edited {n(c['files_edited'], 'file')}")
    if c["searches"]:
        parts.append(f"Searched {n(c['searches'], 'time')}")
    if c["commands"]:
        parts.append(f"Ran {n(c['commands'], 'command')}")
    if c["subagents"]:
        parts.append(f"Delegated {n(c['subagents'], 'subagent')}")
    return " · ".join(parts) if parts else "No tool activity"


# ── helpers ──────────────────────────────────────────────────────────────────────

def _events_from(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map a record list (mainline or one sidechain group) to UI transcript events."""
    # index tool results by their tool_use id
    results: dict[str, dict[str, Any]] = {}
    for r in records:
        if r.get("type") == "user":
            for b in r.get("message", {}).get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b.get("tool_use_id", "")] = {
                        "text": b.get("content", ""),
                        "is_error": b.get("is_error", False),
                        "mirror": r.get("toolUseResult", {}),
                        "ts": _epoch(r.get("timestamp", "")),
                    }

    events: list[dict[str, Any]] = []
    for r in records:
        t = r.get("type")
        ts = _epoch(r.get("timestamp", ""))
        if t == "user":
            content = r.get("message", {}).get("content")
            origin = r.get("origin", {}).get("kind")
            # Show real user input and background-job notifications; hide internal
            # hook/compaction user records (critic feedback, summaries).
            if isinstance(content, str) and origin in ("human", "task-notification"):
                events.append({"type": "user", "text": r.get("display") or content,
                               "origin": origin, "ts": ts})
        elif t == "assistant":
            msg = r.get("message", {})
            if r.get("isApiErrorMessage"):
                err = _first_text(msg)
                events.append({"type": "model_error", "error": err, "ts": ts})
                continue
            text = _first_text(msg)
            if text.strip():
                events.append({"type": "assistant_text", "text": text,
                               "model": msg.get("model", ""), "ts": ts})
            for b in msg.get("content", []) or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    res = results.get(b.get("id", ""), {})
                    mirror = res.get("mirror", {})
                    events.append({
                        "type": "tool",
                        "name": b.get("name", ""),
                        "arguments": json.dumps(b.get("input", {})),
                        "outcome": mirror.get("detail", {}).get(
                            "outcome", "error" if res.get("is_error") else "ok"),
                        "result": res.get("text", ""),
                        "ts": res.get("ts", ts),
                    })
        elif t == "system" and r.get("subtype") == "compact_boundary":
            events.append({"type": "compaction", "ts": ts,
                           "meta": r.get("compactMetadata", {})})
    return events


def _first_text(message: dict[str, Any]) -> str:
    parts = [b.get("text", "") for b in message.get("content", []) or []
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _epoch(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _title_from(text: str) -> str:
    title = " ".join(text.split())
    return (title[:60] + ("…" if len(title) > 60 else "")) if title else "New session"


def _streaks(days: set[date]) -> tuple[int, int]:
    if not days:
        return 0, 0
    longest = run = 1
    ordered = sorted(days)
    for prev, cur in zip(ordered, ordered[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        longest = max(longest, run)
    current = 0
    day = date.today()
    if day not in days and (day - timedelta(days=1)) in days:
        day -= timedelta(days=1)
    while day in days:
        current += 1
        day -= timedelta(days=1)
    return current, longest


def _heatmap(per_day: Counter[str], weeks: int = 26) -> list[dict[str, Any]]:
    today = date.today()
    start = today - timedelta(days=weeks * 7 - 1)
    start -= timedelta(days=start.weekday())
    cells, day = [], start
    while day <= today:
        cells.append({"date": day.isoformat(), "count": per_day.get(day.isoformat(), 0)})
        day += timedelta(days=1)
    return cells


def _fun_line(tokens: int) -> str:
    if tokens <= 0:
        return "Your token history starts with your first session."
    ratio = tokens / _HOBBIT_TOKENS
    if ratio < 1:
        return f"You've used ~{tokens:,} tokens — closing in on The Hobbit."
    return f"You've used ~{ratio:.0f}× more tokens than The Hobbit."
