"""LogSink — turns the Orchestrator's event stream into v2 event-log records.

The Orchestrator emits sink events (step / assistant_text / tool / done / …). LogSink is
the bridge to persistence: it groups each step into ONE schema-valid assistant record plus
its tool_result records (docs/v2/01-data-schemas.md §3–§4), and — if given a `forward`
callback — also passes every raw event straight to the live WebSocket queue for rendering.

This is the ONLY writer of assistant/tool records for a live session: the event log stays
the single source of truth, while the UI still gets the same live stream it had in v1.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Callable

from ..eventlog import EventLog, new_tool_call_id

# tool name -> activity category (schema §4; drives the UI activity aggregation in Phase 10)
_CATEGORY = {
    "read_file": "file_read", "list_dir": "file_read",
    "write_file": "file_write", "edit_file": "file_edit",
    "grep": "search", "glob": "search",
    "run_bash": "command", "run_shell": "command",
    "task": "agent",
    "task_create": "task", "task_update": "task", "task_list": "task",
}


def _target(name: str, args: dict[str, Any]) -> str | None:
    if name in ("read_file", "list_dir", "write_file", "edit_file"):
        return args.get("path")
    if name in ("grep", "glob"):
        return args.get("pattern") or args.get("glob")
    if name in ("run_bash", "run_shell"):
        return args.get("command")
    return None


def mirror_for(name: str, args: dict[str, Any], outcome: str, result: str) -> dict[str, Any]:
    """The structured tool-result mirror (schema §4) — never truncated, used by the UI."""
    return {
        "tool": name,
        "ok": outcome == "ok",
        "target": _target(name, args),
        "category": _CATEGORY.get(name, "other"),
        "detail": {"outcome": outcome},
    }


def normalize_usage(usage: dict[str, int]) -> dict[str, Any]:
    """Provider-neutral usage superset (schema §3). Cache fields stay null unless reported."""
    cache_read = usage.get("cache_read_input_tokens")
    cache_creation = usage.get("cache_creation_input_tokens")
    return {
        "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        "cache_read_input_tokens": None if cache_read is None else int(cache_read),
        "cache_creation_input_tokens": None if cache_creation is None else int(cache_creation),
    }


class LogSink:
    """Sink implementation that persists to an EventLog and forwards live to the UI."""

    def __init__(
        self,
        log: EventLog,
        asker: Callable[[str, str], str],
        forward: Callable[[dict[str, Any]], None] | None = None,
        hooks=None,  # HookEngine | None — PostToolUse fires after each persisted result
    ):
        self.log = log
        self._asker = asker
        self._forward = forward
        self.hooks = hooks
        self._reset_step()

    def _reset_step(self) -> None:
        self._open = False
        self._model: str | None = None
        self._request_id = ""
        self._text = ""
        self._usage: dict[str, int] = {}
        self._cost: float | None = None
        self._calls: list[dict[str, Any]] = []          # tool_use content blocks
        self._results: list[dict[str, Any]] = []          # pending tool_result rows

    # ── Sink protocol ───────────────────────────────────────────────────────────
    def emit(self, type: str, **data: Any) -> None:  # noqa: A002
        if self._forward is not None:
            self._forward({"type": type, **data})
        handler = getattr(self, f"_on_{type}", None)
        if handler is not None:
            handler(**data)

    def ask_permission(self, tool_name: str, preview: str) -> str:
        return self._asker(tool_name, preview)

    # ── sidechains (Phase 9) ─────────────────────────────────────────────────────
    @contextmanager
    def sidechain(self, sidechain_id: str, agent_type: str, goal: str):
        """Scope for recording one subagent run into the SAME event log.

        Yields a fresh child LogSink (own step-grouping state — the parent's half-built
        step is untouched) whose records are stamped isSidechain/sidechainId by the log,
        and whose live events are tagged for the UI's nested rendering. The child's goal
        is recorded as the sidechain tree's root user record."""
        def tagged(event: dict[str, Any]) -> None:
            if self._forward is not None:
                self._forward({**event, "subagent": agent_type, "sidechainId": sidechain_id})

        with self.log.sidechain(sidechain_id):
            child = LogSink(self.log, asker=self._asker, forward=tagged, hooks=None)
            self.log.user(goal, origin="human", sidechainAgentType=agent_type)
            try:
                yield child
            finally:
                child._flush()  # a child that died mid-step still lands its record

    # ── event handlers ──────────────────────────────────────────────────────────
    def _on_step(self, model: str | None = None, usage: dict[str, int] | None = None,
                 cost: float | None = None, request_id: str = "", **_: Any) -> None:
        self._flush()                       # close the previous step's assistant record
        self._model = model
        self._request_id = request_id or ""
        self._usage = usage or {}
        self._cost = cost
        self._open = True

    def _on_assistant_text(self, text: str = "", model: str | None = None, **_: Any) -> None:
        self._text = text
        if model:
            self._model = model

    def _on_tool(self, id: str | None = None, name: str = "", arguments: Any = None,  # noqa: A002
                 outcome: str = "ok", result: str = "", **_: Any) -> None:
        tid = id or new_tool_call_id()
        args = _parse_args(arguments)
        self._calls.append({"type": "tool_use", "id": tid, "name": name, "input": args})
        self._results.append({
            "id": tid,
            "name": name,
            "args": args,
            "text": result,
            "is_error": outcome not in ("ok", "denied", "deferred"),
            "mirror": mirror_for(name, args, outcome, result),
        })

    def _on_done(self, **_: Any) -> None:
        self._flush()

    def _on_max_steps(self, **_: Any) -> None:
        self._flush()

    def _on_budget_stop(self, **_: Any) -> None:
        self._flush()

    def _on_critic_verdict(self, approved: bool = True, feedback: str = "", **_: Any) -> None:
        # A rejection becomes a real model-visible message (the orchestrator appends it to
        # its own history); persist it so resume reconstructs the same conversation.
        if not approved and feedback:
            self._flush()
            self.log.user(f"[critic review — not accepted]\n{feedback}",
                          origin="hook", is_meta=True)

    def _on_model_error(self, error: str = "", **_: Any) -> None:
        self._flush()
        self.log.assistant(
            {"role": "assistant", "model": "<synthetic>",
             "content": [{"type": "text", "text": error}],
             "stop_reason": "error", "usage": normalize_usage({})},
            is_api_error=True,
        )

    # ── the core: one step -> one assistant record + its tool results ────────────
    def _flush(self) -> None:
        if not self._open:
            return
        content: list[dict[str, Any]] = []
        if self._text.strip():
            content.append({"type": "text", "text": self._text})
        content.extend(self._calls)
        if not content:
            content = [{"type": "text", "text": ""}]
        message = {
            "role": "assistant",
            "model": self._model or "unknown",
            "content": content,
            "stop_reason": "tool_use" if self._calls else "end_turn",
            "usage": normalize_usage(self._usage),
        }
        assistant_uuid = self.log.assistant(
            message, request_id=self._request_id, cost_usd=self._cost
        )
        for row in self._results:
            self.log.tool_result(
                row["id"], row["text"], row["mirror"],
                source_assistant_uuid=assistant_uuid, is_error=row["is_error"],
            )
            if self.hooks is not None:
                self.hooks.after_tool(row["id"], row["name"], row["args"], row["text"])
        self._reset_step()


def _parse_args(arguments: Any) -> dict[str, Any]:
    """Orchestrator passes tool arguments as a raw JSON string; store the parsed object
    (schema §3 says tool_use.input is an object). Keep unparseable input recoverable."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
            return parsed if isinstance(parsed, dict) else {"_value": parsed}
        except json.JSONDecodeError:
            return {"_raw": arguments}
    return {}
