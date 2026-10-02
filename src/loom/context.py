"""ContextAssembler — builds the model's message list each turn (module spec §5, schema §5).

This is the keystone of the v2 context model. Everything the model sees is assembled here
from the event log: the system prompt, the conversation (mainline records), and — the new
part — *attachments* rendered inline as `<context>` notes. Attachments are the async
side-channel a real coding agent uses to keep the model current between turns: the todo-list
reminder, files re-injected after compaction, a snippet of a file the user edited outside
the agent, listings of newly available skills/tools/subagents, a date rollover.

The assembler also owns external-edit detection: it fingerprints (mtime+sha1) every file the
agent touches, and before a turn reports any that changed underneath it as `edited_text_file`
payloads — so the model is told "this file is different now" instead of acting on stale content.

Determinism is a hard requirement: the same log must produce the same messages, byte for byte
(golden-tested), so replay and debugging stay reliable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .eventlog import EventLog
from .prompts import system_prompt

_EDIT_SNIPPET_LIMIT = 8_000  # chars of a changed file to show (schema §13)


# ── attachment rendering (pure, deterministic) ───────────────────────────────────

def render_attachment(attachment: dict[str, Any]) -> str:
    """Render one attachment payload to its inner text (no wrapper). Empty string = skip."""
    atype = attachment.get("type", "")
    if atype == "task_reminder":
        items = attachment.get("content", [])
        if not items:
            return "Your todo list is empty."
        lines = ["Your task list (keep it updated with task_update):"]
        mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
        for it in items:
            lines.append(f"  {mark.get(it.get('status'), '[ ]')} #{it.get('id')} "
                         f"{it.get('subject', '')}")
        return "\n".join(lines)
    if atype == "file":
        return (f"<file path=\"{attachment.get('filename', '')}\">\n"
                f"{attachment.get('content', '')}\n</file>")
    if atype == "edited_text_file":
        return (f"The file {attachment.get('filename', '')} was changed outside the agent. "
                f"Current contents (excerpt):\n{attachment.get('snippet', '')}")
    if atype == "skill_listing":
        names = attachment.get("names", [])
        return "Available skills (invoke with /name): " + ", ".join(names) if names else ""
    if atype == "deferred_tools_delta":
        added = attachment.get("addedNames", [])
        if not added:
            return ""
        if attachment.get("loaded"):
            return "Tool schemas now loaded and callable: " + ", ".join(added)
        return ("More tools exist but their schemas are not loaded. Call tool_search "
                "with a query (or \"select:name\") before using: " + ", ".join(added))
    if atype == "agent_listing_delta":
        added = attachment.get("addedTypes", [])
        return "Subagents you can delegate to via `task`: " + ", ".join(added) if added else ""
    if atype == "date_change":
        return f"The current date is now {attachment.get('newDate', '')}."
    if atype == "goal_status":
        if attachment.get("met"):
            return "The stated goal has been met."
        return (f"The goal is NOT yet met: {attachment.get('condition', '')}\n"
                f"{attachment.get('feedback', '')}").strip()
    if atype == "hook_context":
        return str(attachment.get("content", ""))
    return ""


def context_block(atype: str, body: str) -> str:
    return f'<context type="{atype}">\n{body}\n</context>'


# ── the canonical message builder (shared by resume and live turns) ──────────────

def messages_from_records(
    records: list[dict[str, Any]], workdir: str, *,
    render_attachments: bool, extra_system: str = "",
) -> list[dict[str, Any]]:
    """Map mainline records to a chat message list.

    When render_attachments is True, attachment records are rendered as `<context>` notes
    and attached to the user message that follows them (so context always precedes the turn
    it informs, and we never emit dangling consecutive user messages). `extra_system` is
    appended to the system prompt (the memory index, Phase 7)."""
    system = system_prompt(workdir)
    if extra_system:
        system += "\n\n" + extra_system
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    pending: list[str] = []

    def flush_into(text: str) -> str:
        nonlocal pending
        if not pending:
            return text
        prefix = "\n\n".join(pending) + "\n\n"
        pending = []
        return prefix + text

    def push_user(text: str) -> None:
        # HARD invariant: never emit consecutive user messages (providers reject them).
        # E.g. post-compaction, the summary user record can directly precede a tail that
        # opens with a user record — merge instead.
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] += "\n\n" + text
        else:
            messages.append({"role": "user", "content": text})

    for r in records:
        t = r.get("type")
        if t == "attachment":
            if render_attachments:
                body = render_attachment(r.get("attachment", {}))
                if body:
                    pending.append(context_block(r["attachment"]["type"], body))
            continue
        if t == "assistant":
            if r.get("isApiErrorMessage"):
                continue
            msg = r.get("message", {})
            text = _first_text(msg)
            tool_uses = [b for b in msg.get("content", []) or []
                         if isinstance(b, dict) and b.get("type") == "tool_use"]
            entry: dict[str, Any] = {"role": "assistant", "content": text}
            if tool_uses:
                entry["tool_calls"] = [
                    {"id": b["id"], "type": "function",
                     "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                    for b in tool_uses
                ]
            messages.append(entry)
        elif t == "user":
            content = r.get("message", {}).get("content")
            if isinstance(content, str):
                push_user(flush_into(content))
            elif isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        messages.append({"role": "tool", "tool_call_id": b.get("tool_use_id", ""),
                                         "content": b.get("content", "")})
                    elif b.get("type") == "text":
                        push_user(flush_into(b.get("text", "")))
        # system records (compact_boundary etc.) carry no chat message

    if pending:  # attachments trailing after the last user turn
        push_user("\n\n".join(pending))
    return messages


class ContextAssembler:
    def __init__(self, log: EventLog, workdir: str, memory=None):
        self.log = log
        self.workdir = workdir
        self.memory = memory  # MemoryStore | None — its index rides in the system prompt
        self._fingerprints: dict[str, tuple[float, str]] = {}

    def system_extra(self) -> str:
        return self.memory.prompt_section() if self.memory is not None else ""

    # ── the model's view for a turn / resume ─────────────────────────────────────
    def build(self) -> list[dict[str, Any]]:
        return messages_from_records(self.log.mainline(), self.workdir,
                                     render_attachments=True,
                                     extra_system=self.system_extra())

    def pending_context(self) -> str:
        """Rendered `<context>` blocks for attachments recorded since the last turn started.

        Used to inject fresh context (reminders, external edits) into a live turn without
        rebuilding the whole conversation. The boundary is the last turn-OPENING user record
        (string content, any origin) — tool results carry list content and don't count, and a
        task-notification turn consumes pending attachments just like a human turn does."""
        records = self.log.read_all()
        last_opener = -1
        for i, r in enumerate(records):
            if (r.get("type") == "user"
                    and isinstance(r.get("message", {}).get("content"), str)
                    and not r.get("isMeta")):
                last_opener = i
        blocks = []
        for r in records[last_opener + 1:]:
            if r.get("type") == "attachment":
                body = render_attachment(r.get("attachment", {}))
                if body:
                    blocks.append(context_block(r["attachment"]["type"], body))
        return "\n\n".join(blocks)

    # ── external-edit detection ──────────────────────────────────────────────────
    def track(self, path: str) -> None:
        """Baseline a file's on-disk state (called after the agent touches it)."""
        fp = _fingerprint(self._abspath(path))
        if fp is not None:
            self._fingerprints[path] = fp

    def track_from_log(self) -> None:
        """Re-baseline every file the agent has read/written, from the log's tool mirrors."""
        for r in self.log.records("user"):
            mirror = r.get("toolUseResult")
            if isinstance(mirror, dict) and mirror.get("category") in (
                    "file_read", "file_write", "file_edit") and mirror.get("target"):
                self.track(mirror["target"])

    def hot_files(self, limit: int, max_bytes: int = 100_000) -> list[tuple[str, str]]:
        """The most recently modified files the agent has touched, with their contents —
        what compaction re-injects so work continues without re-reading (schema §13 cap)."""
        candidates = []
        for path in self._fingerprints:
            p = self._abspath(path)
            try:
                candidates.append((p.stat().st_mtime, path, p))
            except OSError:
                continue  # deleted since we tracked it
        out = []
        for _, path, p in sorted(candidates, reverse=True)[:limit]:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > max_bytes:
                text = text[:max_bytes] + "\n… (truncated)"
            out.append((str(p), text))
        return out

    def scan_external_edits(self) -> list[dict[str, Any]]:
        """Report files that changed on disk since we last baselined them (schema §5)."""
        payloads = []
        for path, old_fp in list(self._fingerprints.items()):
            new_fp = _fingerprint(self._abspath(path))
            if new_fp is None or new_fp == old_fp:
                continue
            self._fingerprints[path] = new_fp
            payloads.append({"filename": path, "snippet": self._excerpt(path)})
        return payloads

    def _abspath(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else Path(self.workdir) / p

    def _excerpt(self, path: str) -> str:
        try:
            text = self._abspath(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(could not read the file)"
        return text[:_EDIT_SNIPPET_LIMIT] + ("\n… (truncated)" if len(text) > _EDIT_SNIPPET_LIMIT else "")


# ── helpers ──────────────────────────────────────────────────────────────────────

def _first_text(message: dict[str, Any]) -> str:
    parts = [b.get("text", "") for b in message.get("content", []) or []
             if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


def _fingerprint(path: Path) -> tuple[float, str] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return (path.stat().st_mtime, hashlib.sha1(data).hexdigest())
