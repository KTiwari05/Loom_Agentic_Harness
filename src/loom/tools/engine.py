"""The Tool Engine: dispatch -> permission check -> execute -> format result.

Knows how to render a human-readable preview (incl. a unified diff for file writes) so the
permission prompt shows exactly what will change before it happens.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

from ..checkpoints import Checkpoints
from ..permissions import Permissions
from .base import REGISTRY, ToolMeta

_WRITE_TOOLS = {"write_file", "edit_file"}


class ToolEngine:
    def __init__(
        self,
        permissions: Permissions,
        checkpoints: Checkpoints | None = None,
        allowed: set[str] | None = None,
    ):
        self.permissions = permissions
        self.checkpoints = checkpoints
        self.registry = REGISTRY
        self.allowed = allowed  # None = every registered tool; a set restricts (subagents)
        self.extra: dict[str, ToolMeta] = {}  # per-engine tools (e.g. `task`), not global
        # Deferred tools (Phase 8): hidden from schemas until tool_search loads them.
        self.deferred_hidden: set[str] = set()

    def add(self, meta: ToolMeta) -> None:
        """Register a tool on THIS engine only (bound tools like the subagent `task`)."""
        self.extra[meta.name] = meta

    def _lookup(self, name: str) -> ToolMeta | None:
        if name in self.extra:
            return self.extra[name]
        if name in self.deferred_hidden:
            return None
        if self.allowed is not None and name not in self.allowed:
            return None
        return self.registry.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        metas = [m for n, m in self.registry.items()
                 if (self.allowed is None or n in self.allowed)
                 and n not in self.deferred_hidden]
        return [meta.schema for meta in metas] + [meta.schema for meta in self.extra.values()]

    def execute(self, name: str, arguments_json: str) -> tuple[str, str]:
        """Run a tool call. Returns (result_text, outcome) where outcome is for the trace."""
        meta = self._lookup(name)
        if meta is None:
            if name in self.deferred_hidden:
                return (f"Tool '{name}' is deferred; call tool_search first.", "deferred")
            return f"Error: unknown tool '{name}'.", "unknown_tool"

        try:
            args = json.loads(arguments_json or "{}")
        except json.JSONDecodeError:
            return "Error: tool arguments were not valid JSON.", "bad_args"

        if meta.risky:
            preview = self._preview(name, args)
            if not self.permissions.check(name, preview):
                return "Denied by the user. Try a different approach or ask why.", "denied"

        if self.checkpoints is not None and name in _WRITE_TOOLS and "path" in args:
            self.checkpoints.snapshot(str(args["path"]))

        try:
            result = meta.fn(**args)
            return result, "ok"
        except TypeError as exc:
            return f"Error: bad arguments for '{name}': {exc}", "bad_args"
        except Exception as exc:  # noqa: BLE001
            return f"Error while running '{name}': {exc}", "error"

    # ── preview rendering ──────────────────────────────────────────────────────
    def _preview(self, name: str, args: dict[str, Any]) -> str:
        if name == "run_bash":
            return f"  $ {args.get('command', '')}"
        if name == "write_file":
            return self._diff_preview(args.get("path", ""), args.get("content", ""))
        if name == "edit_file":
            path = args.get("path", "")
            current = self._read(path)
            new = current.replace(args.get("old_string", ""), args.get("new_string", ""), 1)
            return self._diff_preview(path, new, current=current)
        return "  " + json.dumps(args)[:500]

    @staticmethod
    def _read(path: str) -> str:
        p = Path(path)
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""

    def _diff_preview(self, path: str, new: str, current: str | None = None) -> str:
        if current is None:
            current = self._read(path)
        diff = difflib.unified_diff(
            current.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
        lines = list(diff)[:60]
        body = "\n".join(lines) if lines else "  (new file)"
        return body
