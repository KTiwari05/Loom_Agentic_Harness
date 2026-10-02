"""TaskLedger — the agent's own todo list (docs/v2/02-module-specs.md §4, schema §9).

One JSON file per task under ~/.loom/tasks/<session>/<n>.json, mutated ONLY by explicit
tool calls (task_create / task_update / task_list). This mirrors how a real coding agent
tracks a multi-step plan: the list survives compaction and process restart because it lives
on disk, independent of the conversation, and is resurfaced to the model as a `task_reminder`
attachment when it changes.

IDs are sequential integers (max existing + 1). Writes are guarded by an advisory `.lock`
file in the directory plus an in-process lock, so concurrent worker threads can't corrupt it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

STATUSES = {"pending", "in_progress", "completed"}

# Fields a caller may change, and the aliases the tools accept (snake_case → schema camelCase).
_ALLOWED = {"status", "subject", "description", "activeForm", "blocks", "blockedBy"}
_ALIASES = {"active_form": "activeForm", "blocked_by": "blockedBy"}


@contextmanager
def _dir_lock(lock_path: Path, timeout: float = 5.0, poll: float = 0.02):
    """Advisory cross-process lock: exclusive-create a .lock file, retry, remove on exit."""
    deadline = time.time() + timeout
    acquired = False
    while time.time() < deadline:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            time.sleep(poll)
    try:
        yield acquired  # if contention outlasts the timeout we proceed best-effort
    finally:
        if acquired:
            try:
                os.unlink(str(lock_path))
            except OSError:
                pass


class TaskLedger:
    def __init__(self, root: Path | str, session_id: str):
        self.dir = Path(root) / "tasks" / session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.dir / ".lock"
        self._mem = threading.Lock()

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _ids(self) -> list[int]:
        return sorted(int(p.stem) for p in self.dir.glob("*.json") if p.stem.isdigit())

    # ── operations ───────────────────────────────────────────────────────────────
    def create(self, subject: str, description: str = "", active_form: str = "") -> str:
        with self._mem, _dir_lock(self.lock_path):
            ids = self._ids()
            task_id = str((ids[-1] + 1) if ids else 1)
            data = {
                "id": task_id,
                "subject": subject,
                "description": description,
                "activeForm": active_form or subject,
                "status": "pending",
                "blocks": [],
                "blockedBy": [],
            }
            self._write(data)
        return task_id

    def update(self, task_id: str, **changes: Any) -> dict[str, Any]:
        with self._mem, _dir_lock(self.lock_path):
            data = self._read(task_id)  # KeyError if missing
            for raw_key, value in changes.items():
                if value is None:
                    continue
                key = _ALIASES.get(raw_key, raw_key)
                if key not in _ALLOWED:
                    continue
                if key == "status" and value not in STATUSES:
                    raise ValueError(f"invalid status {value!r}; use one of {sorted(STATUSES)}")
                data[key] = value
            self._write(data)
            return data

    def get(self, task_id: str) -> dict[str, Any]:
        return self._read(task_id)

    def list(self) -> list[dict[str, Any]]:
        return [self._read(str(i)) for i in self._ids()]

    def delete(self, task_id: str) -> bool:
        with self._mem, _dir_lock(self.lock_path):
            path = self._path(task_id)
            if path.is_file():
                path.unlink()
                return True
            return False

    def reminder_payload(self) -> dict[str, Any]:
        """The `task_reminder` attachment payload (schema §5)."""
        items = self.list()
        return {"content": items, "itemCount": len(items)}

    # ── file io ──────────────────────────────────────────────────────────────────
    def _read(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        if not path.is_file():
            raise KeyError(task_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        self._path(data["id"]).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def render(items: list[dict[str, Any]]) -> str:
    """A compact human/model-readable rendering of the ledger (used by the task_list tool)."""
    if not items:
        return "No tasks yet."
    lines = []
    for it in items:
        mark = {"pending": "○", "in_progress": "◐", "completed": "●"}.get(it["status"], "○")
        lines.append(f"{mark} #{it['id']} [{it['status']}] {it['subject']}")
    return "\n".join(lines)
