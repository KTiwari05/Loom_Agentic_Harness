"""Shell tool: run_bash — execute a shell command with a timeout.

Risky: gated by the permission model. Phase 7 moves this into a real sandbox
(container / microVM). For now it's a timed subprocess in the workspace dir.
"""

from __future__ import annotations

import subprocess

from .base import tool

_TIMEOUT_SECONDS = 120
_MAX_OUTPUT_CHARS = 20_000


@tool(risky=True)
def run_bash(command: str) -> str:
    """Run a shell command in the workspace and return its combined stdout+stderr.

    Args:
        command: The command line to execute (used for tests, build, git, etc.).
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {_TIMEOUT_SECONDS}s."
    except Exception as exc:  # noqa: BLE001
        return f"Error: failed to run command: {exc}"

    output = (proc.stdout or "") + (proc.stderr or "")
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
    return f"(exit code {proc.returncode})\n{output.strip()}" if output.strip() else f"(exit code {proc.returncode}, no output)"
