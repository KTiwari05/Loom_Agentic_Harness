"""Replay & fork — time-travel for agent runs (F3 in docs/02-novel-features.md).

A trace records every model response verbatim (implementer AND critic, in call order).
Replay feeds those responses back through the real loop:
- tools re-execute for real, so the run is reproduced in your workspace;
- `fork_at=N` replays the first N model calls, then hands control to the live model —
  branch a past run at any decision point and let it take a different path.

    loom --replay .loom/traces/<run>.jsonl [--fork-at N]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import models
from .models import ModelResponse, ToolCall


class ReplayExhausted(RuntimeError):
    """The trace ran out of recorded responses (and no live fallback was allowed)."""


def load_trace(path: str | Path) -> tuple[str, list[ModelResponse]]:
    """Returns (goal, recorded model responses in call order)."""
    goal = ""
    responses: list[ModelResponse] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event: dict[str, Any] = json.loads(line)
        if event.get("type") == "meta":
            goal = goal or event.get("goal", "")
        elif event.get("type") == "model":
            responses.append(_response_from_event(event))
    if not goal:
        raise ValueError(f"No meta/goal event in trace {path} — is it a v2 trace?")
    return goal, responses


def _response_from_event(event: dict[str, Any]) -> ModelResponse:
    tool_calls = [
        ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
        for tc in event.get("tool_calls", [])
    ]
    assistant_message: dict[str, Any] = {"role": "assistant", "content": event.get("text", "")}
    if tool_calls:
        assistant_message["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments},
            }
            for c in tool_calls
        ]
    return ModelResponse(
        text=event.get("text", ""),
        tool_calls=tool_calls,
        assistant_message=assistant_message,
        usage={
            "prompt_tokens": event.get("prompt_tokens", 0),
            "completion_tokens": event.get("completion_tokens", 0),
        },
        cost=float(event.get("cost_usd", 0.0)),
        model=event.get("model", "replay"),
    )


class SequenceCompleter:
    """A completer that returns recorded responses in order, then (optionally) goes live.

    Drop-in for models.complete — pass to Orchestrator(completer=...).
    """

    def __init__(self, responses: list[ModelResponse], live_after: bool = False):
        self._responses = list(responses)
        self._index = 0
        self.live_after = live_after
        self.went_live = False

    def __call__(self, model: str, messages, tools, temperature: float = 0.2) -> ModelResponse:
        if self._index < len(self._responses):
            resp = self._responses[self._index]
            self._index += 1
            return resp
        if self.live_after:
            self.went_live = True
            return models.complete(model, messages, tools, temperature)
        raise ReplayExhausted(
            "Recorded trace exhausted. Use --fork-at to continue live from this point."
        )
