"""The Orchestrator — Loom's agent loop. The beating heart of the whole system.

    ask the model what to do -> run the tools it asks for -> feed results back -> repeat
    until the model returns a final answer with no tool calls (or a guard trips).

Layers on top of the bare loop:
- persistent chat: the conversation carries across turns, like a chat app (`run` per message)
- checkpoints: files are snapshotted before writes; `/rewind` undoes the last turn
- the Critic: when files changed, a reviewer model must approve the diff before "done"
- the Conductor: escalates to a stronger model after failures; prices the $-saved baseline
- sinks: the loop emits structured events; the CLI and the desktop app render them
See docs/01-architecture.md.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console

from . import critic, models
from .checkpoints import Checkpoints
from .conductor import Conductor
from .config import Config
from .permissions import Permissions
from .prompts import system_prompt
from .recorder import Recorder
from .sinks import ConsoleSink, Sink
from .tools.engine import ToolEngine


class Orchestrator:
    def __init__(
        self,
        config: Config,
        console: Console | None = None,
        completer=None,
        sink: Sink | None = None,
        recorder: Recorder | None = None,
    ):
        self.config = config
        self.sink = sink or ConsoleSink(console)
        self.completer = completer  # None = live model; replay injects recorded responses
        self.conductor = Conductor(config)
        self.permissions = Permissions(config.auto_approve, asker=self.sink.ask_permission)
        self.checkpoints = Checkpoints(config.workdir)
        self.engine = ToolEngine(self.permissions, self.checkpoints)
        # Subagents pass a tagged view of the parent's recorder so the whole run —
        # parent and child model calls, in exact call order — lands in ONE trace.
        self.recorder = recorder or Recorder(config.workdir)
        self.total_cost = 0.0
        self.baseline_cost = 0.0
        self.last_turn_steps = 0
        self.messages: list[dict[str, Any]] = []  # persists across turns = the chat

        # Subagents: the `task` tool needs to spawn bounded child orchestrators, so it
        # must be bound to this instance (config/sink/completer) rather than global.
        from .tools import agents

        agents.register(self)

    def _complete(self, model: str, messages, tools):
        fn = self.completer or models.complete
        return fn(model, messages, tools, self.config.temperature)

    # ── the loop ───────────────────────────────────────────────────────────────
    def run(self, goal: str) -> None:
        """One chat turn. History is kept, so follow-up messages continue the conversation."""
        if not self.messages:
            self.messages.append({"role": "system", "content": system_prompt(self.config.workdir)})
            self.recorder.log_meta(goal, self.config.model, self.config.workdir)
        self.messages.append({"role": "user", "content": goal})

        self.checkpoints.begin_turn()
        turn_cost_start = self.total_cost
        critic_rounds = 0
        escalate = False
        self.last_turn_steps = 0

        for step in range(1, self.config.max_steps + 1):
            self.last_turn_steps = step
            model = self.conductor.pick_model(role="implementer", escalate=escalate)
            # Schemas refresh every step: tool_search can load a deferred tool mid-turn.
            tools = self.engine.schemas()
            try:
                resp = self._complete(model, self.messages, tools)
            except Exception as exc:  # noqa: BLE001
                self._model_error(exc)
                return

            self._account(resp)
            self.recorder.log_model(
                "implementer",
                step,
                resp.model,
                resp.text,
                [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in resp.tool_calls],
                resp.usage,
                resp.cost,
            )
            self.sink.emit(
                "step",
                step=step,
                model=resp.model,
                request_id=getattr(resp, "request_id", ""),
                escalated=escalate,
                tokens=resp.usage.get("prompt_tokens", 0) + resp.usage.get("completion_tokens", 0),
                usage=resp.usage,
                cost=resp.cost,
                tool_calls=len(resp.tool_calls),
            )
            self.messages.append(resp.assistant_message)

            if resp.text.strip():
                self.sink.emit("assistant_text", text=resp.text, model=resp.model)

            if resp.tool_calls:
                escalate = False
                for call in resp.tool_calls:
                    result, outcome = self.engine.execute(call.name, call.arguments)
                    self.recorder.log_tool(step, call.name, outcome)
                    self.sink.emit(
                        "tool",
                        step=step,
                        id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                        outcome=outcome,
                        result=result[:4000],
                    )
                    self.messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
                    if outcome not in ("ok", "denied"):
                        escalate = True  # a failed step routes the next call to the strong tier

                if self.total_cost > self.config.max_cost_usd:
                    self.sink.emit(
                        "budget_stop", total_cost=self.total_cost, max_cost=self.config.max_cost_usd
                    )
                    self.checkpoints.commit_turn()
                    return
                continue

            # Final answer (no tool calls). If files changed, the Critic gets a veto.
            if (
                self.config.critic_enabled
                and self.checkpoints.turn_dirty()
                and critic_rounds < self.config.max_critic_rounds
            ):
                verdict = self._run_critic(goal)
                if verdict is not None and not verdict.approved:
                    critic_rounds += 1
                    escalate = True
                    self.messages.append(
                        {
                            "role": "user",
                            "content": (
                                "[critic review — the change was NOT accepted]\n"
                                f"{verdict.feedback}\n"
                                "Revise your changes to address this, then verify again."
                            ),
                        }
                    )
                    continue

            self.checkpoints.commit_turn()
            self._done(turn_cost_start)
            return

        self.checkpoints.commit_turn()
        self.sink.emit("max_steps", max_steps=self.config.max_steps)

    # ── critic ─────────────────────────────────────────────────────────────────
    def _run_critic(self, goal: str) -> critic.Verdict | None:
        diff = self.checkpoints.turn_diff()
        model = self.conductor.pick_model(role="critic")
        self.sink.emit("critic_start", model=model)
        try:
            verdict = critic.review(goal, diff, model, completer=self.completer)
        except Exception as exc:  # noqa: BLE001
            self.sink.emit("critic_error", error=str(exc))
            return None
        self._account_raw(verdict.usage, verdict.cost)
        self.recorder.log_model(
            "critic", self.last_turn_steps, verdict.model, verdict.raw, [], verdict.usage, verdict.cost
        )
        self.recorder.log_critic_verdict(verdict.approved)
        self.sink.emit("critic_verdict", approved=verdict.approved, feedback=verdict.feedback)
        return verdict

    # ── chat/session controls (used by slash commands) ─────────────────────────
    def rewind(self) -> list[str]:
        restored = self.checkpoints.rewind()
        if restored:
            self.messages.append(
                {
                    "role": "user",
                    "content": "[system note] The user rewound your last file changes: "
                    + ", ".join(restored)
                    + ". Those files are back to their previous state.",
                }
            )
        return restored

    def clear(self) -> None:
        self.messages = []

    def cost_summary(self) -> str:
        saved = self.baseline_cost - self.total_cost
        pct = (saved / self.baseline_cost * 100) if self.baseline_cost > 0 else 0.0
        return (
            f"spent ${self.total_cost:.4f} · frontier baseline ${self.baseline_cost:.4f} · "
            f"saved ${saved:.4f} ({pct:.0f}%)"
        )

    # ── accounting ─────────────────────────────────────────────────────────────
    def _account(self, resp: models.ModelResponse) -> None:
        self._account_raw(resp.usage, resp.cost)

    def _account_raw(self, usage: dict[str, int], cost: float) -> None:
        self.total_cost += cost
        self.baseline_cost += self.conductor.baseline_cost(usage)

    def _done(self, turn_cost_start: float) -> None:
        self.sink.emit(
            "done",
            turn_cost=self.total_cost - turn_cost_start,
            total_cost=self.total_cost,
            baseline_cost=self.baseline_cost,
            summary=self.cost_summary(),
        )

    def _model_error(self, exc: Exception) -> None:
        msg = str(exc)
        self.sink.emit(
            "model_error",
            error=msg,
            key_hint="api_key" in msg.lower() or "auth" in msg.lower(),
        )
