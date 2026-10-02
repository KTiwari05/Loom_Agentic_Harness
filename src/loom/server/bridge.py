"""The bridge between the synchronous agent core and the async web frontend (v2).

- SessionRunner: one live chat session. An Orchestrator runs turns on a worker thread; a
  LogSink persists every turn into the session's event log AND forwards the live event
  stream to a queue the WebSocket drains. There is no separate transcript/messages store —
  the event log is the single source of truth, and resume rebuilds the model conversation
  from it (eventstore.rebuild_messages).
- PermissionBridge: turns the blocking "may I run this tool?" question into a WebSocket
  round-trip — the agent thread parks on a threading.Event until the user answers.

Turns across ALL sessions share one lock: the fs/shell tools resolve paths against the
process CWD, so exactly one turn may own the CWD at a time.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from typing import Any

from .. import models
from ..compaction import Compactor, estimate_tokens
from ..config import Config
from ..context import ContextAssembler
from ..eventlog import project_slug
from ..hooks import HookEngine
from ..jobs import JobManager
from ..ledger import TaskLedger, render as render_ledger
from ..memory import MemoryStore
from ..toolreg import ToolRegistry, deferred_names_from_env, loaded_from_records
from ..orchestrator import Orchestrator
from ..queue import PromptQueue, QueueItem
from ..tools.base import ToolMeta, _build_schema
from .eventstore import EventStore, activity_from_records, rebuild_messages
from .logsink import LogSink

_RUN_LOCK = threading.Lock()

PERMISSION_TIMEOUT_S = 600  # an unanswered prompt denies after 10 minutes


class PermissionBridge:
    def __init__(self, push):
        self._push = push
        self._pending: dict[str, dict[str, Any]] = {}

    def ask(self, tool_name: str, preview: str) -> str:
        request_id = uuid.uuid4().hex[:8]
        entry = {"event": threading.Event(), "answer": "n"}
        self._pending[request_id] = entry
        self._push(
            {"type": "permission_request", "id": request_id, "tool": tool_name, "preview": preview}
        )
        entry["event"].wait(timeout=PERMISSION_TIMEOUT_S)
        self._pending.pop(request_id, None)
        return str(entry["answer"])

    def resolve(self, request_id: str, answer: str) -> bool:
        entry = self._pending.get(request_id)
        if entry is None:
            return False
        entry["answer"] = answer
        entry["event"].set()
        return True


class SessionRunner:
    """Owns the Orchestrator + event log for one session, running turns on a worker thread."""

    def __init__(self, session_id: str, store: EventStore):
        self.session_id = session_id
        self.store = store
        self.log = store.log_for(session_id)
        meta = store.get(session_id)["meta"]

        self.queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.permissions = PermissionBridge(self._forward)

        config = Config()  # picks up saved settings (loaded into os.environ at startup)
        config.workdir = meta["workdir"]
        if meta.get("model"):
            config.model = meta["model"]
            if not os.getenv("LOOM_MODEL_STRONG"):
                config.model_strong = config.model
            if not os.getenv("LOOM_MODEL_CRITIC"):
                config.model_critic = config.model

        # Hooks: PostToolUse context injection + the Stop-hook goal gate (late-bound
        # completer so tests that monkeypatch models.complete drive the judge too).
        self.hooks = HookEngine(
            self.log,
            completer=lambda *a, **k: models.complete(*a, **k),
            model=config.model,
            stop_condition=os.getenv("LOOM_STOP_CONDITION", ""),
        )

        # LogSink persists to the event log and forwards live events to the WS queue.
        self.sink = LogSink(self.log, asker=self.permissions.ask, forward=self._forward,
                            hooks=self.hooks)
        self.agent = Orchestrator(config, sink=self.sink)

        # Deferred tools: hide configured schemas until tool_search loads them; the
        # discovered set replays from the log so it survives restart and compaction.
        deferred = deferred_names_from_env() & set(self.agent.engine.registry)
        core = [m for n, m in self.agent.engine.registry.items() if n not in deferred]
        self.toolreg = ToolRegistry(core, [self.agent.engine.registry[n] for n in deferred])
        self.agent.engine.deferred_hidden = set(deferred)
        restored = loaded_from_records(self.log.read_all())
        self.toolreg.restore(restored)
        self.agent.engine.deferred_hidden -= set(restored)

        # Long-term memory: per-project facts whose index rides in the system prompt.
        self.memory = MemoryStore(store.root, project_slug(meta["workdir"]))

        # Resume: rebuild the model conversation from the log (no separate messages store).
        # Set unconditionally — even a fresh session's system prompt must carry the memory
        # index, which the Orchestrator's own fallback prompt would not.
        self.agent.messages = rebuild_messages(self.log.mainline(), meta["workdir"],
                                               extra_system=self.memory.prompt_section())
        self.agent.total_cost = float(meta.get("total_cost", 0.0))
        self.busy = False
        self._turn_lock = threading.Lock()

        # The prompt queue is the single inbox: messages typed while busy AND background-job
        # completions both flow through it and become turns.
        self.prompts = PromptQueue(self.log, on_enqueue=self._on_enqueue)
        self.jobs = JobManager(self.prompts, store.root, session_id)

        # The agent's own todo list (survives compaction + restart).
        self.ledger = TaskLedger(store.root, session_id)
        self._ledger_dirty = False

        # Assembles the model's context: renders attachments + watches for external edits.
        self.assembler = ContextAssembler(self.log, meta["workdir"], memory=self.memory)
        self.assembler.track_from_log()  # baseline files already touched this session

        # A brand-new session gets its opening context deltas: recent memories recalled as
        # `file` attachments, and the name-only advertisement of deferred tools. The first
        # turn's pending_context() carries both to the model.
        if self.log.head_uuid is None:
            for path in self.memory.recall(3):
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                self.log.attachment("file", {"filename": str(path), "content": content,
                                             "reason": "recall"})
            if self.toolreg.deferred_names():
                self.log.attachment("deferred_tools_delta", {
                    "addedNames": self.toolreg.deferred_names(), "removedNames": []})

        # Compaction: late-bound completer so tests that monkeypatch models.complete work.
        self.compactor = Compactor(
            self.log, self.assembler,
            lambda *a, **k: models.complete(*a, **k),
            config.model_strong or config.model,
        )

        self._register_session_tools()

    def _register_session_tools(self) -> None:
        """Bind per-session tools (background shell + task ledger) to THIS runner."""
        def run_shell_background(command: str, description: str = "") -> str:
            """Run a shell command in the background and return immediately. Use for long
            jobs (builds, test suites, servers). You'll receive a task-notification with the
            exit code when it finishes; read its output file to see what happened.

            Args:
                command: The command line to run detached.
                description: A short label for the job, shown in the notification.
            """
            status = self.jobs.start(command, description or command,
                                     cwd=self.agent.config.workdir)
            return (f"Started background job {status.id}. Output streams to "
                    f"{status.output_path}. A task-notification will arrive when it ends.")

        def task_create(subject: str, description: str = "", active_form: str = "") -> str:
            """Add a task to your todo list for a multi-step job. Returns the new task id.

            Args:
                subject: Short imperative title (e.g. "Add retry to the API client").
                description: What needs to be done, in enough detail to act on later.
                active_form: Present-continuous label shown while in progress (e.g. "Adding retry").
            """
            task_id = self.ledger.create(subject, description, active_form)
            self._ledger_dirty = True
            return f"Created task #{task_id}: {subject}"

        def task_update(task_id: str, status: str = "", subject: str = "",
                        description: str = "", active_form: str = "") -> str:
            """Update a task. Set status to 'pending', 'in_progress', or 'completed'.

            Args:
                task_id: The id returned by task_create.
                status: New status (pending | in_progress | completed).
                subject: New title, if changing it.
                description: New description, if changing it.
                active_form: New present-continuous label, if changing it.
            """
            changes = {k: v for k, v in {
                "status": status, "subject": subject,
                "description": description, "active_form": active_form}.items() if v}
            try:
                data = self.ledger.update(str(task_id), **changes)
            except KeyError:
                return f"No task #{task_id} exists."
            except ValueError as exc:
                return f"Error: {exc}"
            self._ledger_dirty = True
            return f"Updated task #{task_id} (status: {data['status']})."

        def task_list() -> str:
            """List all tasks with their ids and statuses."""
            return render_ledger(self.ledger.list())

        def memory_write(name: str, description: str, type: str = "project",  # noqa: A002
                         body: str = "") -> str:
            """Save a durable fact to long-term project memory. It survives sessions and
            compaction; its index line appears in every future session's system prompt.
            Save user preferences, decisions, and constraints — never things the code or
            git history already records.

            Args:
                name: Short kebab-case slug (e.g. "prefers-pytest-over-unittest").
                description: One-line summary shown in the memory index.
                type: One of user | project | feedback | reference.
                body: The fact itself, self-contained, with absolute dates.
            """
            try:
                path = self.memory.write(name, description, type, body,
                                         origin_session=self.session_id)
            except ValueError as exc:
                return f"Error: {exc}"
            return f"Saved memory '{path.stem}' to {path}."

        def memory_read(name: str) -> str:
            """Load the full text of one saved memory by its name (see the memory index
            in your context).

            Args:
                name: The memory's name/slug from the index.
            """
            text = self.memory.read(name)
            return text if text is not None else f"No memory named '{name}'."

        def tool_search(query: str) -> str:
            """Load the schemas of deferred tools so you can call them. Deferred tool
            NAMES are listed in your context; their schemas are not loaded until you ask.

            Args:
                query: "select:name1,name2" for exact names, or keywords matched against
                    tool names and descriptions.
            """
            metas = self.toolreg.load(query)
            if not metas:
                unloaded = ", ".join(self.toolreg.deferred_names()) or "(none left)"
                return f"No deferred tools matched {query!r}. Unloaded tools: {unloaded}."
            for m in metas:
                self.agent.engine.deferred_hidden.discard(m.name)
            self.log.attachment("deferred_tools_delta", {
                "addedNames": [m.name for m in metas], "removedNames": [], "loaded": True})
            lines = [f"Loaded {len(metas)} tool(s) — callable from your next step:"]
            for m in metas:
                fn_schema = m.schema["function"]
                lines.append(f"- {fn_schema['name']}: {fn_schema.get('description', '')}")
                lines.append(f"  parameters: {json.dumps(fn_schema.get('parameters', {}))}")
            return "\n".join(lines)

        for fn, risky in ((run_shell_background, True), (task_create, False),
                          (task_update, False), (task_list, False),
                          (memory_write, False), (memory_read, False),
                          (tool_search, False)):
            self.agent.engine.add(ToolMeta(name=fn.__name__, fn=fn,
                                           schema=_build_schema(fn), risky=risky))

    # forward a live event to the WebSocket (persistence is LogSink's job)
    def _forward(self, event: dict[str, Any]) -> None:
        self.queue.put(event)

    def drain_stale(self) -> None:
        """Drop queued events on (re)connect — the client re-renders from the stored
        transcript, so anything still in the queue would be shown twice."""
        try:
            while True:
                self.queue.get_nowait()
        except queue.Empty:
            pass

    # ── chat ──────────────────────────────────────────────────────────────────
    def send(self, text: str, display: str | None = None) -> bool:
        """Accept a user message. Starts a turn if idle, else queues it. Always accepted.

        `display` is what the transcript shows (e.g. "/commit fix bug") when the actual
        prompt is a rendered skill body the user never typed."""
        if not self._try_begin(text, display, "human"):
            self.prompts.enqueue(text, origin="human", display=display)
            self._forward({"type": "queued", "text": display or text})
        return True

    # ── the turn engine (shared by user messages and queued items) ─────────────
    def _try_begin(self, text: str, display: str | None, origin: str) -> bool:
        with self._turn_lock:
            if self.busy:
                return False
            self.busy = True
        self._launch_turn(text, display, origin)
        return True

    def _on_enqueue(self, item: QueueItem) -> None:
        """A background job (or a queued message) landed — start it if the agent is idle."""
        self._drain_next()

    def _drain_next(self) -> None:
        with self._turn_lock:
            if self.busy:
                return
            item = self.prompts.pop()
            if item is None:
                return
            self.busy = True
        self._launch_turn(item.content, item.display, item.origin)

    def _launch_turn(self, text: str, display: str | None, origin: str) -> None:
        # Record any files the user changed underneath the agent since the last turn, then
        # gather all pending context (external edits + task reminders + listings) to prepend.
        for payload in self.assembler.scan_external_edits():
            self.log.attachment("edited_text_file", payload)
        context = self.assembler.pending_context()

        kw = {"display": display} if display and display != text else {}
        self.log.user(text, origin=origin, **kw)  # the clean user message is what's persisted
        if origin == "human":
            self.log.meta("last-prompt", content=display or text)  # the resume pointer
        self._forward({"type": "user", "text": display or text, "origin": origin})

        model_text = f"{context}\n\n{text}" if context else text  # the model additionally sees context

        def _work() -> None:
            with _RUN_LOCK:
                try:
                    os.chdir(self.agent.config.workdir)
                except OSError as exc:
                    self.sink.emit("model_error", error=f"cannot open workdir: {exc}")
                else:
                    try:
                        self.hooks.vetoes = 0  # the veto budget is per turn
                        self.agent.run(model_text)
                        # Stop hook: a configured goal condition can veto completion and
                        # force continuation with feedback (≤ 3 vetoes per turn).
                        while True:
                            verdict = self.hooks.on_stop(self._tail_text())
                            if verdict is None or verdict.met:
                                break
                            self.agent.total_cost += verdict.cost
                            feedback = (
                                f"[stop hook] The goal is not met yet: "
                                f"{self.hooks.stop_condition}\n{verdict.feedback}\n"
                                "Continue working until the condition is satisfied."
                            )
                            self.log.user(feedback, origin="hook")
                            self._forward({"type": "goal_veto", "feedback": verdict.feedback})
                            self.agent.run(feedback)
                    except Exception as exc:  # noqa: BLE001 — never kill the server thread
                        self.sink.emit("model_error", error=str(exc))
            # If the todo list changed this turn, resurface it as a reminder attachment that
            # the NEXT turn's pending_context() will render for the model.
            if self._ledger_dirty:
                self.log.attachment("task_reminder", self.ledger.reminder_payload())
                self._ledger_dirty = False
            self.assembler.track_from_log()  # re-baseline files (incl. ones the agent just wrote)
            if self.compactor.should_compact(estimate_tokens(self.agent.messages)):
                self._compact("auto")
            self._maybe_title()
            with self._turn_lock:
                self.busy = False
            self._forward(self._turn_end())
            self._drain_next()  # a job may have finished, or a message was queued, while busy

        threading.Thread(target=_work, daemon=True, name=f"loom-{self.session_id}").start()

    def _tail_text(self, limit: int = 2000) -> str:
        """The last stretch of assistant prose — what the stop-hook judge evaluates."""
        texts = []
        for r in self.log.mainline():
            if r.get("type") == "assistant" and not r.get("isApiErrorMessage"):
                parts = [b.get("text", "") for b in r.get("message", {}).get("content", []) or []
                         if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(p for p in parts if p)
                if text.strip():
                    texts.append(text)
        return "\n\n".join(texts)[-limit:]

    def _compact(self, trigger: str) -> dict[str, Any]:
        """Run compaction and swing the live conversation onto the new tree root.

        The rebuilt messages hold the summary + preserved tail; the re-injected file
        attachments reach the model via pending_context() at the start of the next turn."""
        report = self.compactor.compact(trigger)
        self.agent.messages = rebuild_messages(self.log.mainline(),
                                               self.agent.config.workdir,
                                               extra_system=self.memory.prompt_section())
        self.agent.total_cost += report.cost_usd
        event = {
            "type": "compaction", "trigger": trigger,
            "pre_tokens": report.pre_tokens, "post_tokens": report.post_tokens,
            "duration_ms": report.duration_ms,
        }
        self._forward(event)
        return event

    def _turn_end(self) -> dict[str, Any]:
        turns = activity_from_records(
            [r for r in self.log.read_all() if not r.get("isSidechain")])
        return {
            "type": "turn_end",
            "total_cost": self.agent.total_cost,
            "baseline_cost": self.agent.baseline_cost,
            "summary": self.agent.cost_summary(),
            "queued": len(self.prompts),
            "context_tokens": estimate_tokens(self.agent.messages),
            "activity": turns[-1]["summary"] if turns else "",
        }

    def _maybe_title(self) -> None:
        """AI session title: a cheap model call, at most once per 5 human turns, opt-in
        (LOOM_AI_TITLES=1 — the `loom ui` entrypoint enables it; scripted tests don't)."""
        if os.getenv("LOOM_AI_TITLES") != "1":
            return
        human_turns = sum(
            1 for r in self.log.records("user")
            if isinstance(r.get("message", {}).get("content"), str)
            and r.get("origin", {}).get("kind") == "human"
            and not r.get("isSidechain") and not r.get("isMeta")
            and not r.get("isCompactSummary"))
        last_at = 0
        for r in self.log.records("ai-title"):
            last_at = int(r.get("atTurn", 0) or 0)
        if human_turns < 1 or (last_at and human_turns - last_at < 5):
            return
        try:
            resp = models.complete(
                self.agent.config.model,
                [{"role": "system", "content":
                  "Title this coding session in at most 6 words. Reply with the title "
                  "only — no quotes, no punctuation at the end."},
                 {"role": "user", "content": self._tail_text(1200) or "(no output yet)"}],
                None, 0.2)
            title = " ".join(resp.text.strip().splitlines()[0].split())[:60]
            if title:
                self.log.meta("ai-title", aiTitle=title, atTurn=human_turns)
                self.agent.total_cost += getattr(resp, "cost", 0.0) or 0.0
        except Exception:  # noqa: BLE001 — a failed title must never hurt the session
            pass

    # ── slash commands (the built-ins the UI offers) ──────────────────────────
    def command(self, name: str, arg: str = "") -> dict[str, Any]:
        if self.busy and name != "cost":
            return {"type": "command_result", "command": name, "text": "A turn is still running."}
        if name == "cost":
            return {"type": "command_result", "command": name, "text": self.agent.cost_summary()}
        if name == "rewind":
            restored = self.agent.rewind()
            text = "Rewound: " + ", ".join(restored) if restored else "Nothing to rewind."
            return {"type": "command_result", "command": name, "text": text, "files": restored}
        if name == "compact":
            event = self._compact("manual")
            text = (f"Compacted: ~{event['pre_tokens']:,} → ~{event['post_tokens']:,} "
                    f"estimated tokens in {event['duration_ms']} ms.")
            return {"type": "command_result", "command": name, "text": text}
        if name == "clear":
            self.agent.clear()
            return {"type": "command_result", "command": name, "text": "Conversation cleared."}
        if name == "model":
            if arg.strip():
                self.agent.config.model = arg.strip()
                self.store.set_model(self.session_id, arg.strip())
            text = f"Model: {self.agent.config.model} (strong: {self.agent.config.model_strong})"
            return {"type": "command_result", "command": name, "text": text}
        return {"type": "command_result", "command": name, "text": f"Unknown command /{name}."}


class RunnerRegistry:
    """One SessionRunner per open session, created lazily and reused across reconnects."""

    def __init__(self, store: EventStore):
        self.store = store
        self._runners: dict[str, SessionRunner] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> SessionRunner:
        with self._lock:
            if session_id not in self._runners:
                self._runners[session_id] = SessionRunner(session_id, self.store)
            return self._runners[session_id]

    def drop(self, session_id: str) -> None:
        with self._lock:
            self._runners.pop(session_id, None)
