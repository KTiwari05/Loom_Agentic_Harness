"""Hooks — PostToolUse context injection + the Stop-hook goal gate (module spec §8).

Two hook points, mirroring what the forensics showed a production coding agent doing:

- PostToolUse: after a matching tool runs, a provider function may emit extra context
  (e.g. "you edited a .py file — remember the lint gate"). It lands as a `hook_context`
  attachment, so the NEXT model turn sees it through the normal context assembler.

- Stop: when the agent believes it's done, a configured goal condition is evaluated by a
  (cheap) model call. "Not met" vetoes completion: a `goal_status` attachment records the
  verdict and a synthetic user record (origin "hook") with the feedback forces another
  turn — at most 3 vetoes, so a stuck goal can't loop forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Callable

from .eventlog import EventLog

MAX_VETOES = 3

_JUDGE_SYSTEM = (
    "You judge whether a coding agent met its goal. Reply with EXACTLY one line: "
    "'MET' if the goal condition is satisfied by the work shown, otherwise "
    "'NOT MET: <one concrete sentence of what is missing>'."
)


@dataclass
class PostToolUseHook:
    tool_pattern: str                                 # fnmatch, e.g. "write_file" or "*_file"
    provider: Callable[[str, dict, str], str | None]  # (tool, args, result) -> context | None


@dataclass
class StopVerdict:
    met: bool
    feedback: str
    cost: float = 0.0


class HookEngine:
    def __init__(
        self,
        log: EventLog,
        completer: Callable[..., Any] | None = None,  # models.complete signature
        model: str = "",
        post_hooks: list[PostToolUseHook] | None = None,
        stop_condition: str = "",
    ):
        self.log = log
        self.completer = completer
        self.model = model
        self.post_hooks = post_hooks or []
        self.stop_condition = stop_condition.strip()
        self.vetoes = 0

    # ── PostToolUse ──────────────────────────────────────────────────────────────
    def after_tool(self, tool_use_id: str, name: str, args: dict, result: str) -> None:
        for hook in self.post_hooks:
            if not fnmatch(name, hook.tool_pattern):
                continue
            try:
                content = hook.provider(name, args, result)
            except Exception:  # noqa: BLE001 — a broken hook must never kill a turn
                continue
            if content:
                self.log.attachment("hook_context", {
                    "hookName": f"PostToolUse:{name}",
                    "toolUseId": tool_use_id,
                    "content": content,
                })

    # ── Stop ─────────────────────────────────────────────────────────────────────
    def on_stop(self, transcript_tail: str) -> StopVerdict | None:
        """Evaluate the goal condition; None when no condition or veto budget spent."""
        if not self.stop_condition or self.completer is None:
            return None
        if self.vetoes >= MAX_VETOES:
            return None
        resp = self.completer(
            self.model,
            [{"role": "system", "content": _JUDGE_SYSTEM},
             {"role": "user", "content":
              f"Goal condition: {self.stop_condition}\n\n"
              f"The agent's recent output:\n{transcript_tail}"}],
            None, 0.0,
        )
        verdict = _parse_verdict(resp.text)
        verdict.cost = getattr(resp, "cost", 0.0) or 0.0
        self.log.attachment("goal_status", {
            "met": verdict.met,
            "condition": self.stop_condition,
            "feedback": verdict.feedback,
        })
        if not verdict.met:
            self.vetoes += 1
        return verdict


def _parse_verdict(text: str) -> StopVerdict:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    upper = line.upper()
    if upper.startswith("NOT MET"):
        feedback = line.partition(":")[2].strip() or "The goal is not met yet."
        return StopVerdict(met=False, feedback=feedback)
    if upper.startswith("MET"):
        return StopVerdict(met=True, feedback="")
    # Unparseable judge output: fail OPEN (treat as met) — a flaky judge must not
    # trap the agent in forced continuations.
    return StopVerdict(met=True, feedback="")
