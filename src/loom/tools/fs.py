"""Filesystem tools: read_file, list_dir, write_file, edit_file.

Paths are resolved relative to the current working directory and confined to it (no escaping
the workspace). Edits are exact string replacements — robust against line drift.
"""

from __future__ import annotations

from pathlib import Path

from .base import tool

MAX_READ_BYTES = 200_000


class _OutsideWorkspace(ValueError):
    """Raised when a tool path would escape the workspace root."""


def _safe_path(path: str) -> Path:
    root = Path.cwd().resolve()
    resolved = (root / path).resolve()
    if root != resolved and root not in resolved.parents:
        raise _OutsideWorkspace(f"Error: path '{path}' is outside the workspace.")
    return resolved


@tool()
def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
    """Read a UTF-8 text file. Optionally restrict to a 1-indexed line range.

    Args:
        path: File path relative to the workspace root.
        start_line: First line to include (1-indexed). Optional.
        end_line: Last line to include (inclusive). Optional.
    """
    try:
        p = _safe_path(path)
    except _OutsideWorkspace as exc:
        return str(exc)
    if not p.is_file():
        return f"Error: '{path}' is not a file."
    if p.stat().st_size > MAX_READ_BYTES:
        return f"Error: '{path}' is too large ({p.stat().st_size} bytes); read a line range instead."

    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    if start_line is not None or end_line is not None:
        s = (start_line or 1) - 1
        e = end_line if end_line is not None else len(lines)
        lines = lines[s:e]
        offset = s + 1
    else:
        offset = 1
    return "\n".join(f"{i + offset}\t{line}" for i, line in enumerate(lines))


@tool()
def list_dir(path: str = ".") -> str:
    """List the entries of a directory (dirs first, then files).

    Args:
        path: Directory path relative to the workspace root. Defaults to ".".
    """
    try:
        p = _safe_path(path)
    except _OutsideWorkspace as exc:
        return str(exc)
    if not p.is_dir():
        return f"Error: '{path}' is not a directory."
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    out = []
    for e in entries:
        if e.name in {".git", "__pycache__", ".venv", "node_modules"}:
            continue
        out.append(f"{e.name}/" if e.is_dir() else e.name)
    return "\n".join(out) if out else "(empty)"


@tool(risky=True)
def write_file(path: str, content: str) -> str:
    """Create or overwrite a text file with the given content.

    Args:
        path: File path relative to the workspace root.
        content: The full new contents of the file.
    """
    try:
        p = _safe_path(path)
    except _OutsideWorkspace as exc:
        return str(exc)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.is_file()
    p.write_text(content, encoding="utf-8")
    verb = "Overwrote" if existed else "Created"
    return f"{verb} '{path}' ({len(content)} bytes)."


@tool(risky=True)
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace an exact substring in a file. `old_string` must occur exactly once.

    Args:
        path: File path relative to the workspace root.
        old_string: The exact text to replace (must be unique in the file).
        new_string: The replacement text.
    """
    try:
        p = _safe_path(path)
    except _OutsideWorkspace as exc:
        return str(exc)
    if not p.is_file():
        return f"Error: '{path}' is not a file."
    text = p.read_text(encoding="utf-8", errors="replace")
    count = text.count(old_string)
    if count == 0:
        return "Error: old_string not found. Read the file and copy the exact text."
    if count > 1:
        return f"Error: old_string occurs {count} times; add surrounding context to make it unique."
    p.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
    return f"Edited '{path}'."
