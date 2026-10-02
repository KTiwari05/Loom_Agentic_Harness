"""Subagents — the `task` tool. The main agent can delegate work to a bounded child agent.

This is the Claude Code "Task/agents" pattern: the parent describes a job, a child
orchestrator runs it in its own conversation (so the parent's context stays small), and
only the child's final report comes back. Agent types:

- explore: READ-ONLY tools (read_file/list_dir/grep). For codebase questions —
  "where is X defined", "how does the auth flow work". Cheap and safe.
- general: full tool access. For self-contained side jobs — the child's file writes
  still go through the SAME permission gate and are snapshotted into the PARENT's
  current turn, so /rewind undoes subagent changes too.

The tool is registered per-orchestrator (ToolEngine.add), not globally, because it must
bind to the parent's config, sink, permissions, completer and recorder. Depth is capped at 1:
subagents don't get a `task` tool, so no infinite nesting.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..checkpoints import Checkpoints
from .base import ToolMeta, _build_schema
from .engine import ToolEngine

READ_ONLY = {"read_file", "list_dir", "grep"}

AGENT_TYPES: dict[str, dict[str, Any]] = {
    "explore": {
        "tools": READ_ONLY,
        "prompt": (
            "You are a read-only exploration subagent. Investigate the codebase to answer "
            "the request below. Do NOT modify anything. Finish with a concise, complete "
            "report of what you found (paths, line numbers, how things connect):\n\n"
        ),
        "max_steps": 12,
    },
    "general": {
        "tools": None,  # full access
        "prompt": (
            "You are a subagent handling a delegated job. Complete it end to end, then "
            "finish with a short report of exactly what you did and any follow-ups:\n\n"
        ),
        "max_steps": 20,
    },
}


class _SharedTurnCheckpoints(Checkpoints):
    """Routes a child's snapshots into the parent's open turn; begin/commit are no-ops.

    The subagent runs in the middle of a parent turn, so its file changes belong to that
    turn — this is what makes /rewind undo subagent writes as well.
    """

    def __init__(self, parent: Checkpoints):
        super().__init__(str(parent.workdir))
        self._parent = parent

    def begin_turn(self) -> None:
        pass

    def commit_turn(self) -> None:
        pass

    def snapshot(self, path: str) -> None:
        self._parent.snapshot(path)

    def turn_dirty(self) -> bool:
        return False  # the parent's critic reviews the whole turn; the child has none


class _TaggedSink:
    """Wraps the parent sink, stamping every child event with `subagent=<type>` so the
    UI can render nested agent activity indented under the task card."""

    def __init__(self, inner, tag: str):
        self._inner = inner
        self._tag = tag

    def emit(self, type: str, **data: Any) -> None:  # noqa: A002
        self._inner.emit(type, subagent=self._tag, **data)

    def ask_permission(self, tool_name: str, preview: str) -> str:
        return self._inner.ask_permission(tool_name, preview)


def register(parent) -> None:
    """Give `parent` (an Orchestrator) the `task` tool, unless it is itself a subagent."""
    if parent.config.agent_depth >= 1:
        return

    def task(description: str, agent_type: str = "explore") -> str:
        """Delegate a self-contained job to a subagent with its own context window.
        Use `explore` to investigate the codebase (read-only, cheap) and `general` for
        side jobs that may change files. The subagent only returns its final report, so
        give it a complete, standalone description.

        Args:
            description: The full job description. Must stand alone — the subagent cannot
                see this conversation.
            agent_type: One of: explore (read-only investigation), general (full tools).
        """
        return _run_subagent(parent, description, agent_type)

    parent.engine.add(ToolMeta(name="task", fn=task, schema=_build_schema(task), risky=False))


def _run_subagent(parent, description: str, agent_type: str) -> str:
    from ..eventlog import new_sidechain_id  # late imports: avoid module cycles
    from ..orchestrator import Orchestrator

    spec = AGENT_TYPES.get(agent_type)
    if spec is None:
        return f"Error: unknown agent_type '{agent_type}'. Use one of: {', '.join(AGENT_TYPES)}."

    goal = spec["prompt"] + description
    # Sidechain recording (Phase 9): a log-backed sink records the child's turns into the
    # parent's event log as a separate sidechain tree (isSidechain + sidechainId), so ONE
    # file replays parent and child. Sinks without sidechain support (the CLI's
    # ConsoleSink) fall back to tagged live events only.
    scope = getattr(parent.sink, "sidechain", None)
    sidechain_id = new_sidechain_id() if scope is not None else None

    parent.sink.emit("subagent_start", agent_type=agent_type, description=description,
                     sidechainId=sidechain_id)

    config = replace(
        parent.config,
        agent_depth=parent.config.agent_depth + 1,
        critic_enabled=False,  # the parent's critic reviews the merged turn diff
        max_steps=min(spec["max_steps"], parent.config.max_steps),
    )

    def build_child(sink) -> Orchestrator:
        child = Orchestrator(
            config,
            completer=parent.completer,
            sink=sink,
            # One trace per run: the child records into the parent's trace (tagged), so the
            # model-call order in the file matches execution and --replay works across tasks.
            recorder=parent.recorder.tagged(subagent=agent_type, depth=config.agent_depth),
        )
        child.permissions = parent.permissions  # shared gate: "always" approvals carry over
        child.engine = ToolEngine(
            parent.permissions,
            _SharedTurnCheckpoints(parent.checkpoints),
            allowed=spec["tools"],
        )
        return child

    if scope is not None:
        with scope(sidechain_id, agent_type, goal) as child_sink:
            child = build_child(child_sink)
            child.run(goal)
    else:
        child = build_child(_TaggedSink(parent.sink, agent_type))
        child.run(goal)

    # The parent pays the child's bill (it shows up in /cost and the $-saved meter).
    parent.total_cost += child.total_cost
    parent.baseline_cost += child.baseline_cost

    parent.sink.emit(
        "subagent_done", agent_type=agent_type, steps=child.last_turn_steps,
        cost=child.total_cost, sidechainId=sidechain_id,
    )

    report = _final_text(child.messages)
    if not report:
        return "The subagent finished without a final report (it may have hit a limit)."
    return f"[subagent:{agent_type} report]\n{report}"


def _final_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and str(msg.get("content") or "").strip():
            return str(msg["content"]).strip()
    return ""
