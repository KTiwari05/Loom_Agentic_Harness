"""The Recorder — full structured traces of every run.

One JSON line per event in `.loom/traces/<run>.jsonl`. Since v2 the trace captures the
*complete* model response (text + tool calls + usage), which makes every run:
- replayable (feed recorded responses back through the loop — see replay.py)
- forkable (replay N calls, then go live)
- scoreable (the eval harness and the router's training data read traces)
Keep the event shapes stable; add fields, don't rename them. See docs/06.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


class Recorder:
    def __init__(self, workdir: str):
        self.run_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.path = Path(workdir) / ".loom" / "traces" / f"{self.run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, event: dict[str, Any]) -> None:
        event = {"ts": time.time(), "run_id": self.run_id, **event}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def tagged(self, **tags: Any) -> "Recorder":
        """A view of this recorder that stamps every event with extra fields.

        Subagents record through a tagged view of the PARENT's recorder (e.g.
        subagent="explore", depth=1), so one trace holds every model call in exact
        call order — that ordering is what makes --replay work across `task` runs.
        """
        return _TaggedRecorder(self, tags)

    def log_meta(self, goal: str, model: str, workdir: str) -> None:
        self._write({"type": "meta", "goal": goal, "model": model, "workdir": workdir})

    def log_model(
        self,
        kind: str,  # "implementer" | "critic"
        step: int,
        model: str,
        text: str,
        tool_calls: list[dict[str, str]],
        usage: dict[str, int],
        cost: float,
    ) -> None:
        self._write(
            {
                "type": "model",
                "kind": kind,
                "step": step,
                "model": model,
                "text": text,
                "tool_calls": tool_calls,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "cost_usd": round(cost, 6),
            }
        )

    def log_tool(self, step: int, name: str, outcome: str) -> None:
        self._write({"type": "tool", "step": step, "tool": name, "outcome": outcome})

    def log_critic_verdict(self, approved: bool) -> None:
        self._write({"type": "critic_verdict", "approved": approved})


class _TaggedRecorder(Recorder):
    """Writes into the parent's trace file, adding the tag fields to every event."""

    def __init__(self, parent: Recorder, tags: dict[str, Any]):
        self._parent = parent
        self._tags = tags
        self.run_id = parent.run_id
        self.path = parent.path

    def _write(self, event: dict[str, Any]) -> None:
        self._parent._write({**event, **self._tags})
