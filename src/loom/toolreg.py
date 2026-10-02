"""ToolRegistry — deferred tool schemas (docs/v2/02-module-specs.md §7, schema §5).

Most tool schemas don't need to sit in the model's context burning tokens; only their
NAMES are advertised (a `deferred_tools_delta` attachment at session start). When the
model wants one, it calls the core `tool_search` tool with a query; the matching schemas
load into context from then on. The loaded ("discovered") set is itself recorded as
`deferred_tools_delta` attachments with `loaded: true`, so it survives resume and is
re-advertised after compaction. Calling a deferred, unloaded tool returns a recoverable
error result telling the model to call tool_search first.
"""

from __future__ import annotations

import os

from .tools.base import ToolMeta

# Names deferred by default when nothing is configured. Env: LOOM_DEFERRED="a,b,c".
DEFAULT_DEFERRED = {"grep"}


def deferred_names_from_env() -> set[str]:
    raw = os.getenv("LOOM_DEFERRED")
    if raw is None:
        return set(DEFAULT_DEFERRED)
    return {n.strip() for n in raw.split(",") if n.strip()}


class ToolRegistry:
    def __init__(self, core: list[ToolMeta], deferred: list[ToolMeta]):
        self._core = {m.name: m for m in core}
        self._deferred = {m.name: m for m in deferred}
        self._loaded: list[str] = []  # insertion order = discovery order

    # ── context view ─────────────────────────────────────────────────────────────
    def schemas_for_context(self) -> list[dict]:
        metas = list(self._core.values()) + [self._deferred[n] for n in self._loaded]
        return [m.schema for m in metas]

    def deferred_names(self) -> list[str]:
        """Names advertised but not yet loaded."""
        return sorted(n for n in self._deferred if n not in self._loaded)

    def discovered(self) -> list[str]:
        """Loaded deferred names, in discovery order (survives compaction, re-advertised)."""
        return list(self._loaded)

    # ── loading ──────────────────────────────────────────────────────────────────
    def load(self, query: str) -> list[ToolMeta]:
        """`"select:name,name"` for exact names, anything else is a keyword match against
        name + description. Matches move to the loaded set; already-loaded ones are
        returned again without duplicating."""
        query = query.strip()
        if query.startswith("select:"):
            wanted = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
            hits = [self._deferred[n] for n in wanted if n in self._deferred]
        else:
            tokens = [t for t in query.lower().split() if t]
            hits = []
            for meta in self._deferred.values():
                haystack = (meta.name + " "
                            + meta.schema["function"].get("description", "")).lower()
                if any(t in haystack for t in tokens):
                    hits.append(meta)
        for meta in hits:
            if meta.name not in self._loaded:
                self._loaded.append(meta.name)
        return hits

    def restore(self, names: list[str]) -> None:
        """Re-mark names as loaded (resume: replayed from the log's loaded-deltas)."""
        for n in names:
            if n in self._deferred and n not in self._loaded:
                self._loaded.append(n)

    def lookup(self, name: str) -> ToolMeta | None:
        """Core or loaded-deferred meta; None means deferred-but-unloaded (or unknown)."""
        if name in self._core:
            return self._core[name]
        if name in self._loaded:
            return self._deferred[name]
        return None


def loaded_from_records(records: list[dict]) -> list[str]:
    """Replay `deferred_tools_delta(loaded=true)` attachments → the discovered set."""
    names: list[str] = []
    for r in records:
        att = r.get("attachment", {}) if r.get("type") == "attachment" else {}
        if att.get("type") == "deferred_tools_delta" and att.get("loaded"):
            for n in att.get("addedNames", []):
                if n not in names:
                    names.append(n)
    return names
