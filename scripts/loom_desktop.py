"""PyInstaller entry point for the packaged desktop app (dist/Loom.exe).

Double-clicking the exe opens the native Loom window. Passing CLI args still works
(`Loom.exe ui`, `Loom.exe "fix the tests" --dir C:\\proj`), so the one exe is both
the desktop app and the terminal agent.
"""

import sys
from pathlib import Path


def _wire_streams() -> None:
    """In a --windowed exe there is no console: sys.stdout/stderr are None, which
    breaks uvicorn's log handlers and hides every traceback. Point them at a log
    file so the app runs and problems are diagnosable (~/.loom/desktop.log)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = Path.home() / ".loom"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "desktop.log", "a", buffering=1, encoding="utf-8", errors="replace")
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


_wire_streams()

from loom.__main__ import main  # noqa: E402  (streams must be wired before rich/uvicorn load)

if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.append("desktop")
    main()
