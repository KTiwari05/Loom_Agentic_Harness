"""The permission model — runtime gate for risky tools.

The harness enforces this, NOT the model. A risky tool (write/edit/bash) cannot run until
the user approves it (once / always-this-session / deny). This is the right pattern: never
trust the model to self-restrict. See docs/01-architecture.md (Tool Engine).

*Where* the question is asked is a frontend concern: the `asker` callable comes from the
active Sink (terminal prompt for the CLI, Allow/Always/Deny buttons in the desktop app).
"""

from __future__ import annotations

from typing import Callable

Asker = Callable[[str, str], str]  # (tool_name, preview) -> "y" | "a" | "n"


class Permissions:
    def __init__(self, auto_approve: bool, asker: Asker | None = None):
        self.auto_approve = auto_approve
        self.asker: Asker = asker or (lambda _tool, _preview: "n")
        self.always: set[str] = set()  # tool names approved for the rest of the session

    def check(self, tool_name: str, preview: str) -> bool:
        if self.auto_approve or tool_name in self.always:
            return True
        answer = (self.asker(tool_name, preview) or "n").strip().lower()
        if answer in {"a", "always"}:
            self.always.add(tool_name)
            return True
        return answer in {"y", "yes"}
