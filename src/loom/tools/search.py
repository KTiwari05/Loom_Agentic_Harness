"""Search tool: grep — a pure-Python regex search over the workspace.

No external dependency (works the same on Windows). Phase 5 replaces naive search with
AST-aware semantic retrieval; this is the Phase 1 baseline.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import tool

_IGNORE_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".loom", "dist", "build"}
_MAX_MATCHES = 200
_MAX_FILE_BYTES = 1_000_000


@tool()
def grep(pattern: str, path: str = ".", glob: str | None = None) -> str:
    """Search file contents for a regular expression. Returns `path:line: text` matches.

    Args:
        pattern: A Python regular expression.
        path: Directory (or file) to search under. Defaults to ".".
        glob: Optional filename glob filter, e.g. "*.py".
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"Error: invalid regex: {exc}"

    root = Path(path)
    matches: list[str] = []
    files = [root] if root.is_file() else root.rglob(glob or "*")

    for file in files:
        if not file.is_file():
            continue
        if any(part in _IGNORE_DIRS for part in file.parts):
            continue
        try:
            if file.stat().st_size > _MAX_FILE_BYTES:
                continue
            for lineno, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{file}:{lineno}: {line.strip()}")
                    if len(matches) >= _MAX_MATCHES:
                        matches.append(f"... (stopped at {_MAX_MATCHES} matches)")
                        return "\n".join(matches)
        except OSError:
            continue

    return "\n".join(matches) if matches else "(no matches)"
