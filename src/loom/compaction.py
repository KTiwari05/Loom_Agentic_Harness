"""Compactor — context compaction (docs/v2/02-module-specs.md §6, schema §6–§7).

When the conversation outgrows the model's useful window, an LLM writes a sectioned
summary (~20× smaller) and the log gets a new tree root: a `compact_boundary` system
record whose `parentUuid` is null. Because `mainline()` walks from the NEWEST root,
everything before the boundary instantly drops out of the model's view while staying
on disk for replay. After the boundary we chain: the summary (a user record with
`isCompactSummary`), verbatim copies of the last few message records (the "tail", so
the model keeps its immediate short-term memory), re-injected contents of recently
touched files, and re-advertisements of capabilities discovered pre-compaction.

The summarizer runs through the same completer interface as everything else, so tests
script it exactly like any other model call.
"""

from __future__ import annotations

import copy
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import ContextAssembler, messages_from_records
from .eventlog import EventLog
from .tools.agents import AGENT_TYPES

DEFAULT_COMPACT_AT = 150_000   # estimated tokens (env: LOOM_COMPACT_AT)
TAIL_KEEP = 6                  # message records preserved verbatim after the boundary
MAX_REINJECTED_FILES = 5
_FILE_ATTACH_LIMIT = 100_000   # 100 KB per re-injected file (schema §13)

SUMMARY_RUBRIC = """1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and Fixes
5. Problem Solving
6. All User Messages
7. Pending Tasks
8. Current Work
9. Next Step"""

_SUMMARIZER_SYSTEM = (
    "You compress a coding-agent conversation so a fresh model instance can continue the "
    "work seamlessly. Write sectioned markdown with EXACTLY these numbered headings:\n"
    f"{SUMMARY_RUBRIC}\n"
    "Section 3 lists each file path with why it matters and key snippets only if critical. "
    "Section 4 is one line per error: error → root cause → fix. Section 6 lists every "
    "human input verbatim, each truncated at 200 characters. Section 9 is the single "
    "concrete next action. Target: under 4,000 tokens. No preamble, start at heading 1."
)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Crude but monotonic; only the auto-compaction trigger uses it (module spec §6)."""
    return len(json.dumps(messages, ensure_ascii=False)) // 4


def compact_threshold() -> int:
    try:
        return int(os.getenv("LOOM_COMPACT_AT", DEFAULT_COMPACT_AT))
    except ValueError:
        return DEFAULT_COMPACT_AT


@dataclass
class CompactReport:
    boundary_uuid: str = ""
    summary_uuid: str = ""
    pre_tokens: int = 0
    post_tokens: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    preserved_uuids: list[str] = field(default_factory=list)
    reinjected_files: list[str] = field(default_factory=list)
    discovered_tools: list[str] = field(default_factory=list)


class Compactor:
    def __init__(
        self,
        log: EventLog,
        assembler: ContextAssembler,
        completer: Callable[..., Any],  # models.complete signature
        model: str,
    ):
        self.log = log
        self.assembler = assembler
        self.completer = completer
        self.model = model

    def should_compact(self, est_tokens: int) -> bool:
        return est_tokens > compact_threshold()

    # ── the operation ────────────────────────────────────────────────────────────
    def compact(self, trigger: str) -> CompactReport:
        t0 = time.time()
        mainline = self.log.mainline()
        pre_messages = messages_from_records(
            mainline, self.assembler.workdir, render_attachments=True)
        report = CompactReport(pre_tokens=estimate_tokens(pre_messages))

        # 1. the summary, written by the summarizer model
        resp = self.completer(
            self.model,
            [{"role": "system", "content": _SUMMARIZER_SYSTEM},
             {"role": "user", "content": _transcript_text(pre_messages)}],
            None, 0.2,
        )
        report.cost_usd = getattr(resp, "cost", 0.0) or 0.0
        summary = (
            "This session is being continued from a previous conversation that ran out "
            f"of context. Raw transcript: {self.log.path}.\n\n{resp.text}"
        )

        # 2. the tail: last message records, never starting on an orphan tool_result
        tail = _pick_tail(mainline, TAIL_KEEP)
        report.preserved_uuids = [r["uuid"] for r in tail]

        # 3. hot files: most recently modified of the files touched this session
        hot = self.assembler.hot_files(MAX_REINJECTED_FILES)
        report.reinjected_files = [p for p, _ in hot]

        # capabilities discovered before the boundary must survive it
        report.discovered_tools = _discovered_tools(mainline)

        # Post-size is computed BEFORE the boundary is written (the log is append-only, so
        # the metadata can't be patched in later): simulate the post-compaction records.
        summary_rec = {"type": "user", "message": {"role": "user", "content": summary}}
        file_recs = [{"type": "attachment", "attachment":
                      {"type": "file", "filename": p, "content": c, "reason": "compaction"}}
                     for p, c in hot]
        report.post_tokens = estimate_tokens(messages_from_records(
            [summary_rec, *tail, *file_recs], self.assembler.workdir,
            render_attachments=True))
        report.duration_ms = int((time.time() - t0) * 1000)  # dominated by the model call

        # 4. boundary — a NEW TREE ROOT (parentUuid=null, handled by EventLog.system)
        report.boundary_uuid = self.log.system(
            "compact_boundary",
            f"Context compacted ({trigger}).",
            compactMetadata={
                "trigger": trigger,
                "preTokens": report.pre_tokens,
                "postTokens": report.post_tokens,
                "durationMs": report.duration_ms,
                "preservedUuids": report.preserved_uuids,
                "discoveredTools": report.discovered_tools,
                "reinjectedFiles": report.reinjected_files,
            },
        )

        # 5. the summary chains off the boundary
        report.summary_uuid = self.log.user(
            summary, origin="compaction", isCompactSummary=True)

        # 6. re-append the tail verbatim (new uuids, same content, chained in order).
        #    Usage/cost are zeroed on the copies so stats don't double-count the originals.
        for rec in tail:
            self._reappend(rec)

        # 7. re-inject hot files
        for path, content in hot:
            self.log.attachment("file", {
                "filename": path, "content": content, "reason": "compaction"})

        # 8. re-advertise discovered deferred tools + the agent listing
        if report.discovered_tools:
            self.log.attachment("deferred_tools_delta",
                                {"addedNames": report.discovered_tools, "removedNames": [],
                                 "loaded": True})
        self.log.attachment("agent_listing_delta",
                            {"addedTypes": sorted(AGENT_TYPES), "isInitial": False})

        return report

    def _reappend(self, rec: dict[str, Any]) -> None:
        t = rec["type"]
        if t == "assistant":
            message = copy.deepcopy(rec.get("message", {}))
            message["usage"] = {k: 0 if isinstance(v, int) else None
                                for k, v in (message.get("usage") or {}).items()}
            self.log.append(
                "assistant", message=message,
                requestId=rec.get("requestId", ""), costUsd=None,
                isApiErrorMessage=bool(rec.get("isApiErrorMessage")),
                isCompactTail=True,
            )
        else:  # user (incl. tool_result records)
            fields: dict[str, Any] = {
                "message": copy.deepcopy(rec.get("message", {})),
                "origin": rec.get("origin", {"kind": "human"}),
                "isMeta": bool(rec.get("isMeta")),
            }
            for key in ("toolUseResult", "sourceToolAssistantUUID", "display",
                        "isCompactSummary"):
                if key in rec:
                    fields[key] = rec[key]
            self.log.append("user", isCompactTail=True, **fields)


# ── helpers ──────────────────────────────────────────────────────────────────────

def _pick_tail(mainline: list[dict[str, Any]], keep: int) -> list[dict[str, Any]]:
    """The last `keep` user/assistant records — extended backwards if that slice would
    open on a tool_result, so no tool message is ever orphaned from its assistant call."""
    msgs = [r for r in mainline if r.get("type") in ("user", "assistant")]
    start = max(0, len(msgs) - keep)
    while start > 0 and _is_tool_result(msgs[start]):
        start -= 1  # stops once the slice opens on the assistant that made the calls
    return msgs[start:]


def _is_tool_result(rec: dict[str, Any]) -> bool:
    content = rec.get("message", {}).get("content")
    return (rec.get("type") == "user" and isinstance(content, list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content))


def _discovered_tools(mainline: list[dict[str, Any]]) -> list[str]:
    """Deferred tools LOADED before the boundary (deltas with loaded=true only —
    name-only advertisements don't count as discovered)."""
    names: list[str] = []
    for r in mainline:
        if r.get("type") != "attachment":
            continue
        att = r.get("attachment", {})
        if att.get("type") == "deferred_tools_delta" and att.get("loaded"):
            for n in att.get("addedNames", []):
                if n not in names:
                    names.append(n)
            for n in att.get("removedNames", []):
                if n in names:
                    names.remove(n)
    return names


def _transcript_text(messages: list[dict[str, Any]]) -> str:
    """Flatten the conversation to plain text for the summarizer (no tool-call structures —
    some providers reject histories that reference tools they weren't given)."""
    lines = ["Summarize this conversation per your instructions.", ""]
    for m in messages:
        role = m.get("role", "")
        if role == "system":
            continue
        content = m.get("content") or ""
        if role == "assistant" and m.get("tool_calls"):
            calls = ", ".join(
                f"{c['function']['name']}({c['function']['arguments']})"
                for c in m["tool_calls"])
            content = f"{content}\n[tool calls: {calls}]".strip()
        elif role == "tool":
            role, content = "tool result", str(content)[:2_000]
        lines.append(f"## {role}\n{content}\n")
    return "\n".join(lines)
