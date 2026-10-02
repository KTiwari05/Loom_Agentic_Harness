"""The event log — Loom v2's spine (docs/v2/01-data-schemas.md §1–§6, 02-module-specs.md §1).

One append-only JSONL file per session. Every piece of session state — user messages,
model responses, tool results, injected context, compaction boundaries — is a typed
record in this file. Everything else (UI transcript, stats, context assembly, resume,
replay) is DERIVED from it. Records form a parent-linked tree; compaction starts a new
root, so `mainline()` (newest root → leaf) is exactly "what the model can still see".
"""

from __future__ import annotations

import json
import os
import threading
import uuid as uuidlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__

# Tree-node record types: chained via uuid/parentUuid.
NODE_TYPES = {"user", "assistant", "attachment", "system"}
# Session-level metadata types: no tree linkage.
META_TYPES = {"queue-operation", "mode", "ai-title", "last-prompt"}

ATTACHMENT_TYPES = {
    "task_reminder", "file", "edited_text_file", "skill_listing",
    "deferred_tools_delta", "agent_listing_delta", "date_change",
    "goal_status", "hook_context",
}
SYSTEM_SUBTYPES = {"compact_boundary", "stop_hook", "error"}
ORIGIN_KINDS = {"human", "task-notification", "hook", "compaction"}


def project_slug(workdir: str | Path) -> str:
    """C:\\Users\\me\\proj -> C--Users-me-proj (schema doc, storage roots)."""
    s = str(workdir)
    for ch in ("\\", "/", ":"):
        s = s.replace(ch, "-")
    while "--" in s[2:]:  # keep the drive's double dash, collapse the rest
        s = s[:2] + s[2:].replace("--", "-")
    return s


def _now() -> str:
    now = datetime.now(timezone.utc)  # single clock read: seconds and millis must agree
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@dataclass
class LogReport:
    """Result of EventLog.verify() — integrity check over the whole file."""

    records: int = 0
    roots: int = 0
    orphan_parents: int = 0
    unmatched_tool_calls: list[str] = field(default_factory=list)
    schema_errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.orphan_parents or self.unmatched_tool_calls or self.schema_errors)


class EventLog:
    """Single writer for a session's state. Thread-safe appends, crash-safe writes."""

    def __init__(self, root: Path | str, project_slug: str, session_id: str | None = None):
        self.root = Path(root)
        self.project_slug = project_slug
        self.session_id = session_id or str(uuidlib.uuid4())
        self.dir = self.root / "sessions" / project_slug
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{self.session_id}.jsonl"
        self._lock = threading.Lock()
        self._head: str | None = None
        self._sidechain_id: str | None = None
        if self.path.exists():  # resume: recover the MAINLINE leaf (skip sidechain trees)
            for rec in self._iter_file():
                if (rec.get("uuid") and rec.get("type") in NODE_TYPES
                        and not rec.get("isSidechain")):
                    self._head = rec["uuid"]

    # ── writing ────────────────────────────────────────────────────────────────
    @property
    def head_uuid(self) -> str | None:
        return self._head

    def append(self, type: str, *, parent: str | None = "HEAD", **fields) -> str:  # noqa: A002
        if type not in NODE_TYPES:
            raise ValueError(f"unknown node record type: {type!r}")
        rec_uuid = str(uuidlib.uuid4())
        with self._lock:
            parent_uuid = self._head if parent == "HEAD" else parent
            record = {
                "uuid": rec_uuid,
                "parentUuid": parent_uuid,
                "type": type,
                "timestamp": _now(),
                "sessionId": self.session_id,
                "cwd": os.getcwd(),
                "version": __version__,
                "isSidechain": bool(fields.pop("is_sidechain", False)) or (
                    self._sidechain_id is not None),
                **fields,
            }
            if self._sidechain_id is not None:
                record.setdefault("sidechainId", self._sidechain_id)
            self._validate(record)
            self._write(record)
            self._head = rec_uuid
        return rec_uuid

    @contextmanager
    def sidechain(self, sidechain_id: str):
        """Record a subagent's turns into THIS log as a separate sidechain tree.

        Inside the scope every appended node is stamped `isSidechain: true` +
        `sidechainId`, and chains off its own root (the first record has
        parentUuid=null). The mainline head is saved and restored, so the parent's
        next record chains exactly where it would have — one file replays everything.
        Not safe for concurrent node appends from other threads (turns are serialized
        by the runtime, so in practice only the turn thread appends nodes)."""
        with self._lock:
            prev_head, prev_side = self._head, self._sidechain_id
            self._head = None                  # the sidechain starts its own root
            self._sidechain_id = sidechain_id
        try:
            yield self
        finally:
            with self._lock:
                self._head, self._sidechain_id = prev_head, prev_side

    def meta(self, type: str, **fields) -> None:  # noqa: A002
        if type not in META_TYPES:
            raise ValueError(f"unknown meta record type: {type!r}")
        with self._lock:
            self._write({"type": type, "timestamp": _now(), "sessionId": self.session_id, **fields})

    # ── typed conveniences ─────────────────────────────────────────────────────
    def user(self, content, *, origin: str = "human", is_meta: bool = False, **kw) -> str:
        if origin not in ORIGIN_KINDS:
            raise ValueError(f"unknown origin kind: {origin!r}")
        return self.append(
            "user",
            message={"role": "user", "content": content},
            origin={"kind": origin},
            isMeta=is_meta,
            **kw,
        )

    def assistant(
        self, message: dict, *, request_id: str = "", cost_usd: float | None = None, **kw
    ) -> str:
        if message.get("role") != "assistant":
            raise ValueError("assistant record needs message.role == 'assistant'")
        if "model" not in message:
            raise ValueError("assistant message must carry the model that produced it")
        return self.append(
            "assistant",
            message=message,
            requestId=request_id or f"local_{uuidlib.uuid4().hex[:12]}",
            costUsd=cost_usd,
            isApiErrorMessage=bool(kw.pop("is_api_error", False)),
            **kw,
        )

    def tool_result(
        self,
        tool_use_id: str,
        text: str,
        mirror: dict,
        source_assistant_uuid: str,
        is_error: bool = False,
        **kw,
    ) -> str:
        block = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": text,
            "is_error": is_error,
        }
        return self.append(
            "user",
            message={"role": "user", "content": [block]},
            origin={"kind": "human"},
            isMeta=False,
            toolUseResult=mirror,
            sourceToolAssistantUUID=source_assistant_uuid,
            **kw,
        )

    def attachment(self, atype: str, payload: dict, **kw) -> str:
        if atype not in ATTACHMENT_TYPES:
            raise ValueError(f"unknown attachment type: {atype!r}")
        return self.append("attachment", attachment={"type": atype, **payload}, **kw)

    def system(self, subtype: str, content: str, level: str = "info", **kw) -> str:
        if subtype not in SYSTEM_SUBTYPES:
            raise ValueError(f"unknown system subtype: {subtype!r}")
        # compaction boundaries start a new tree root (schema §6)
        parent = kw.pop("parent", None if subtype == "compact_boundary" else "HEAD")
        return self.append(
            "system", parent=parent, subtype=subtype, content=content, level=level, **kw
        )

    # ── reading ────────────────────────────────────────────────────────────────
    def read_all(self) -> list[dict]:
        return list(self._iter_file())

    def records(self, *types: str) -> Iterator[dict]:
        for rec in self._iter_file():
            if not types or rec.get("type") in types:
                yield rec

    def mainline(self) -> list[dict]:
        """Tree nodes from the NEWEST root to the current leaf, in order.

        This is the post-compaction view: exactly the records that can still reach the
        model. Meta records are excluded; sidechain records are included (callers filter).
        """
        nodes = [r for r in self._iter_file() if r.get("uuid")]
        if not nodes:
            return []
        by_uuid = {r["uuid"]: r for r in nodes}
        # walk up from the leaf to its root (never start on a sidechain node)
        main_nodes = [r for r in nodes if not r.get("isSidechain")]
        if self._head:
            leaf = by_uuid.get(self._head)
        else:
            leaf = main_nodes[-1] if main_nodes else None
        chain: list[dict] = []
        seen: set[str] = set()
        cur: dict | None = leaf
        while cur is not None and cur["uuid"] not in seen:
            chain.append(cur)
            seen.add(cur["uuid"])
            parent = cur.get("parentUuid")
            cur = by_uuid.get(parent) if parent else None
        chain.reverse()
        return chain

    def verify(self) -> LogReport:
        report = LogReport()
        uuids: set[str] = set()
        tool_calls: dict[str, str] = {}  # tool_use_id -> record uuid
        results: set[str] = set()
        parents: list[tuple[str, str]] = []
        for rec in self._iter_file():
            report.records += 1
            t = rec.get("type")
            if t in NODE_TYPES:
                u = rec.get("uuid")
                if not u:
                    report.schema_errors.append(f"node record without uuid (type={t})")
                    continue
                uuids.add(u)
                if rec.get("parentUuid") is None:
                    report.roots += 1
                else:
                    parents.append((u, rec["parentUuid"]))
            if t == "assistant":
                for b in rec.get("message", {}).get("content", []) or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tool_calls[b.get("id", "")] = rec.get("uuid", "")
            if t == "user":
                content = rec.get("message", {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            results.add(b.get("tool_use_id", ""))
        report.orphan_parents = sum(1 for _, p in parents if p not in uuids)
        report.unmatched_tool_calls = [tc for tc in tool_calls if tc not in results]
        return report

    # ── internals ──────────────────────────────────────────────────────────────
    def _validate(self, record: dict) -> None:
        t = record["type"]
        if t in {"user", "assistant"} and "message" not in record:
            raise ValueError(f"{t} record requires a message")
        if t == "attachment" and "attachment" not in record:
            raise ValueError("attachment record requires an attachment payload")
        if t == "system" and record.get("subtype") not in SYSTEM_SUBTYPES:
            raise ValueError("system record requires a known subtype")

    def _write(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _iter_file(self) -> Iterator[dict]:
        if not self.path.exists():
            return
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)


def list_sessions(root: Path | str, slug: str) -> list[Path]:
    """All session logs for a project, newest first."""
    d = Path(root) / "sessions" / slug
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def new_sidechain_id() -> str:
    """sc_ + 10 hex chars — groups one subagent run's records (schema §1)."""
    return "sc_" + uuidlib.uuid4().hex[:10]


def new_tool_call_id() -> str:
    """tc_ + 24 base62 chars (schema §3) for providers that don't supply ids."""
    import secrets
    import string

    alphabet = string.ascii_letters + string.digits
    return "tc_" + "".join(secrets.choice(alphabet) for _ in range(24))
