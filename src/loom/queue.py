"""PromptQueue — the single inbox for turns (docs/v2/02-module-specs.md §2, schema §10).

Two kinds of things become turns: messages the user types while the agent is busy, and
notifications from background jobs that finish. Both go through this FIFO queue. Every
operation is recorded as a `queue-operation` meta record in the event log, so the queue is
reconstructable on resume and visible in the transcript.

The agent runtime drains the queue whenever a turn ends; if a job finishes while the agent
is idle, the `on_enqueue` hook lets the runtime start a turn immediately.
"""

from __future__ import annotations

import secrets
import string
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

from .eventlog import EventLog

_ALPHABET = string.digits + string.ascii_lowercase  # base36


def new_item_id() -> str:
    """9 chars, base36, first char alphabetic (same shape as job ids, schema §11)."""
    first = secrets.choice(string.ascii_lowercase)
    rest = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    return first + rest


@dataclass
class QueueItem:
    id: str
    content: str
    origin: str  # "human" | "task-notification"
    display: str | None = None  # what the UI shows, when it differs from content (skills)


class PromptQueue:
    def __init__(self, log: EventLog, on_enqueue: Callable[[QueueItem], None] | None = None):
        self.log = log
        self.on_enqueue = on_enqueue
        self._items: deque[QueueItem] = deque()
        self._lock = threading.Lock()
        self._rebuild()

    def _rebuild(self) -> None:
        """Reconstruct pending items by replaying queue-operation records (resume-safe)."""
        pending: dict[str, QueueItem] = {}
        order: list[str] = []
        for r in self.log.records("queue-operation"):
            op, iid = r.get("operation"), r.get("itemId")
            if not iid:
                continue
            if op == "enqueue":
                pending[iid] = QueueItem(iid, r.get("content", ""), r.get("origin", "human"),
                                         r.get("display"))
                order.append(iid)
            elif op in ("dequeue", "remove"):
                pending.pop(iid, None)
        self._items = deque(pending[i] for i in order if i in pending)

    # ── operations (each writes a queue-operation record) ────────────────────────
    def enqueue(self, content: str, origin: str = "human", display: str | None = None) -> str:
        item = QueueItem(new_item_id(), content, origin, display)
        fields = {"operation": "enqueue", "itemId": item.id, "content": content, "origin": origin}
        if display and display != content:
            fields["display"] = display
        with self._lock:
            # the record is written under the same lock so log order == queue order
            self._items.append(item)
            self.log.meta("queue-operation", **fields)
        if self.on_enqueue is not None:
            self.on_enqueue(item)  # fired OUTSIDE the lock, so a drain can re-enter safely
        return item.id

    def pop(self) -> QueueItem | None:
        with self._lock:
            if not self._items:
                return None
            item = self._items.popleft()
        self.log.meta("queue-operation", operation="dequeue", itemId=item.id, origin=item.origin)
        return item

    def remove(self, item_id: str) -> bool:
        with self._lock:
            for i, it in enumerate(self._items):
                if it.id == item_id:
                    del self._items[i]
                    break
            else:
                return False
        self.log.meta("queue-operation", operation="remove", itemId=item_id)
        return True

    def peek_all(self) -> list[QueueItem]:
        with self._lock:
            return list(self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
