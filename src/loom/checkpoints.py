"""Checkpoints — snapshot files before Loom touches them, so any turn can be rewound.

Every chat turn is one checkpoint: the first time a file is written/edited in a turn, its
original content (or "didn't exist") is captured. `/rewind` restores the last turn's files.
The same before/after pairs feed the Critic's diff review. See docs/02-novel-features.md (F5).
"""

from __future__ import annotations

import difflib
from pathlib import Path


class Checkpoints:
    def __init__(self, workdir: str):
        self.workdir = Path(workdir)
        self._turn: dict[str, str | None] = {}  # path -> original content (None = new file)
        self._history: list[dict[str, str | None]] = []

    # ── turn lifecycle ─────────────────────────────────────────────────────────
    def begin_turn(self) -> None:
        self._turn = {}

    def snapshot(self, path: str) -> None:
        """Record a file's pre-change state (first write in this turn wins)."""
        if path in self._turn:
            return
        p = self.workdir / path
        self._turn[path] = (
            p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
        )

    def commit_turn(self) -> None:
        if self._turn:
            self._history.append(self._turn)
        self._turn = {}

    # ── queries ────────────────────────────────────────────────────────────────
    def turn_dirty(self) -> bool:
        return bool(self._turn)

    def modified_files(self) -> list[str]:
        return sorted(self._turn)

    def turn_diff(self, max_chars: int = 12_000) -> str:
        """Unified diff of everything changed this turn (for the Critic and the user)."""
        parts: list[str] = []
        for path, before in sorted(self._turn.items()):
            p = self.workdir / path
            after = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
            diff = difflib.unified_diff(
                (before or "").splitlines(),
                after.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
            parts.append("\n".join(diff))
        text = "\n\n".join(parts)
        return text[:max_chars] + ("\n... (diff truncated)" if len(text) > max_chars else "")

    # ── rewind ─────────────────────────────────────────────────────────────────
    def rewind(self) -> list[str]:
        """Undo the last committed turn. Returns the list of restored paths."""
        if not self._history:
            return []
        last = self._history.pop()
        restored: list[str] = []
        for path, before in last.items():
            p = self.workdir / path
            if before is None:
                if p.is_file():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(before, encoding="utf-8")
            restored.append(path)
        return sorted(restored)
