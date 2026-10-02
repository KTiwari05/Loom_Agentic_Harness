# Implemented architecture

This document describes the source reviewed on October 2, 2026. The older [architecture plan](01-architecture.md) contains broader future scope.

## Entry points and core loop

`src/loom/__main__.py` implements one-shot and interactive terminal tasks, replay/fork options, `loom ui`, and the optional `loom desktop` wrapper. `pyproject.toml` exposes the `loom` command.

`Orchestrator.run()` in `orchestrator.py` maintains the conversation, selects a model, requests a response, executes tool calls, returns tool results to the model, and repeats until a final response or a guard stops the turn. Model completion is injectable, so tests execute the real loop with scripted responses.

`models.py` adapts LiteLLM responses to a common representation including assistant content, tool calls, usage, model identity, and estimated cost. `conductor.py` selects the configured workhorse, escalation, or critic tier. It does not train or use a difficulty classifier.

## Tools, permissions, and review

`tools/base.py` creates model-visible tool schemas from Python signatures. `tools/engine.py` validates arguments and runs tools through permission handling. File tools use workspace-relative path checks; shell commands execute with the user's OS privileges. `permissions.py` handles approval prompts, while `checkpoints.py` tracks file snapshots for `/rewind`.

`critic.py` asks a separate model to review the goal and accumulated diff. Rejection feeds revision instructions back into the core loop, with bounded rounds. The critic reviews after edits, has no execution tools in its review call, and is not a guarantee that tests ran or changes are correct. An unavailable reviewer or exhausted revision limit can still allow the turn to end.

`tools/agents.py` provides read-only exploration and general subagent work. Child calls and file changes are covered by the replay/checkpoint integration; nested delegation is limited by agent depth.

## UI session runner

`server/app.py` serves the static UI, REST endpoints, and a session WebSocket. `server/bridge.py` owns the per-session orchestrator and permission bridge, executes turns on a worker thread, streams events, and adds the session-specific tools.

`eventlog.py` writes append-only session JSONL records. `server/logsink.py` translates execution events into records, and `server/eventstore.py` derives transcript, activity, and statistics from stored records. Context reconstruction uses the canonical `context.messages_from_records()` builder.

The session runner integrates:

| Component | Responsibility |
| --- | --- |
| `context.py` | Context assembly, attachments, and external-edit tracking |
| `compaction.py` | Summary plus preserved tail with a recorded compaction boundary |
| `queue.py`, `jobs.py` | Queued prompts and background command notifications |
| `ledger.py` | Task creation, state changes, and reminders |
| `memory.py` | Durable project facts and subsequent context injection |
| `toolreg.py` | Deferred schema discovery |
| `hooks.py` | Post-tool hooks and bounded goal-condition vetoes |

These session integrations primarily belong to the UI runner. The terminal CLI uses the same core orchestrator and tools without all the UI's session lifecycle features.

## Recording, replay, and evaluation

`recorder.py` stores execution traces in `.loom/traces/`. `replay.py` supplies recorded model responses back through the loop. Tools execute again; replay is reproducible only when the workspace and external command effects match. A fork switches to a live completer after N recorded calls.

`evalrun.py` copies each task fixture into a temporary workspace, runs the agent with auto-approved tools, executes the task's check, and aggregates results. `evals/harness.py` is the CLI wrapper. The shipped suite has three starter tasks; offline harness tests verify its plumbing rather than real-model task quality.

## Tradeoffs and limits

- Vanilla HTML/CSS/JavaScript keeps the UI in the Python package without a frontend build step.
- An append-only local log makes sessions inspectable and recoverable without a database service.
- Tools use process-wide current-directory state. The UI runner serializes agent turns with a lock; it is not a parallel multi-user execution service.
- Permissions, path checks, and checkpoints improve control but do not isolate shell commands or reverse all their effects.
- The cost guard is checked after tool steps. Reviewer, compaction, title, hook, and background activity must not be assumed to share a strict global provider spending cap.
- The API has no authentication and is intended to remain bound to `127.0.0.1`. User settings include plaintext provider keys in a local file, masked in API responses.

See [VERIFICATION.md](VERIFICATION.md) for what was executed and what remains untested.
