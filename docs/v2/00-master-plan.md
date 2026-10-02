# Loom v2 — Master Plan: The Event-Sourced Coding Agent

**Status:** approved, in progress.
**Read this first.** This plan is self-contained: any engineer (human or LLM) can pick up any
phase with only these three documents and the existing codebase. Do not rely on chat history.

Companion documents:
- [01-data-schemas.md](01-data-schemas.md) — every record/file format, verbatim JSON shapes.
- [02-module-specs.md](02-module-specs.md) — module responsibilities, public APIs, dependency rules.

---

## 1. Why v2

Loom v1 (current `src/loom/`) is a working agent: orchestrator loop, tools, permissions,
critic, skills, subagents, FastAPI + SPA desktop app, PyInstaller exe, 42 offline tests.

v1's weakness: its persistence and context model are ad-hoc (a `SessionStore` with three
parallel lists: meta/events/messages). Production coding agents (verified by runtime
forensics of a real Claude Code session — see §2) are built around **one append-only,
typed, parent-linked event log** from which *everything else* is derived: UI rendering,
context assembly, compaction, activity stats, resume, replay.

v2 rebuilds Loom's spine around that model and adds the seven subsystems the forensics
proved matter in practice: attachments (async context deltas), compaction, a task ledger,
background jobs + prompt queue, deferred tools, hooks, and sidechain subagents.

## 2. Architecture target (the forensics findings, restated as requirements)

These are **observed behaviors of a real production coding agent** that v2 must reproduce:

1. **Event log**: one JSONL file per session. Every record has a `uuid`, `parentUuid`,
   `type`, `timestamp`, envelope metadata (cwd, sessionId, version). Records form a tree
   with roots at session start and at each compaction boundary. ~8 record types.
2. **Tool linkage**: assistant `tool_use` blocks carry IDs; results are separate records
   pointing back via `tool_use_id` + a structured mirror (`toolUseResult`) for the UI.
3. **Attachments**: the harness injects typed context deltas *between* model turns:
   task reminders, injected file contents, external-edit snippets, skill/tool/agent
   listings, date changes, goal status. These are records, not prompt-string hacks.
4. **Compaction**: manual + auto. An LLM writes a sectioned summary (~20× token
   reduction); a `compact_boundary` record stores metrics + preserved-tail UUIDs; recent
   hot files are re-injected; discovered deferred tools are re-advertised. Raw history
   stays on disk, never in context.
5. **Task ledger**: numbered JSON files per session (`tasks/<session>/<n>.json`),
   mutated ONLY by explicit tool calls (TaskCreate/Update/List), re-surfaced to the
   model via reminder attachments. Independent of compaction.
6. **Background jobs + prompt queue**: shell commands can run detached with an opaque ID
   and an `.output` file; completion enqueues a synthetic prompt (`origin:
   task-notification`) that starts a model turn WITHOUT user input. User messages typed
   while the agent is busy go into the same queue (enqueue/dequeue/remove records).
7. **Deferred tools**: most tool schemas are NOT in context; only names are advertised.
   A `tool_search` tool loads schemas on demand; loaded names survive compaction.
8. **Hooks**: PostToolUse (inject extra context after a tool runs) and Stop (evaluate a
   goal condition; can veto turn-end and force continuation with feedback).
9. **Sidechains**: subagent turns are recorded in the SAME log with `isSidechain: true`,
   so one file replays everything.
10. **Model agnosticism**: model identity is per-assistant-record; switching models
    mid-session changes nothing else. Transport errors become synthetic assistant records.

## 3. Non-goals for v2

- No vector database / embeddings. The real system shows zero evidence of them; retrieval
  is grep/glob/read + recency heuristics. Do not add one.
- No multi-user server. Single user, local machine.
- No cloud sync.
- Do not break the v1 CLI (`loom "goal"`) or the exe packaging; they are ports, not rewrites.

## 4. Ground rules for every implementer (any model: Fable, Opus, Sonnet, others)

- **Language/stack:** Python 3.11, no new heavy deps without a note in this file.
  Frontend stays vanilla JS/CSS (no build step).
- **Every phase ships with offline tests** (no network, no real model calls — use the
  scripted fake completer pattern from `tests/`). Suite must stay green: `python -m pytest -q`.
- **Lint:** `python -m ruff check src tests` must pass.
- **Schemas are law:** implement exactly what [01-data-schemas.md](01-data-schemas.md)
  specifies. If you must deviate, update the schema doc in the same commit.
- **One writer:** all mutations of session state go through `EventLog.append()`. No module
  writes JSONL directly.
- **Derive, don't duplicate:** UI transcripts, stats, activity counts, resume pointers are
  computed FROM the event log. Never maintain a second source of truth.
- **Read the acceptance criteria** at the end of each phase before starting; write the
  tests first if unsure.
- **Windows first:** paths via `pathlib`, UTF-8 everywhere (`encoding="utf-8"`), no POSIX-only calls.

## 5. Phase plan

Build strictly in order; each phase is independently shippable and tested.
(Ledger task numbers in parentheses.)

### Phase 1 (task #10) — Event log core  `src/loom/eventlog.py`
The spine. Append-only JSONL, typed records, envelope, parent chaining, tool linkage,
readers. Standalone module + tests; integration comes later.
**Accept when:** schema-conformant records round-trip; tree integrity (0 orphans);
tool_use↔tool_result linkage check passes; resume pointer works; 15+ new tests green.

### Phase 2 (task #11) — Session runtime on the log
Rewrite `server/sessions.py` + `server/bridge.py` so SessionRunner writes ONLY via
EventLog; derive UI transcript + dashboard stats from the log; migrate old session JSONs
(one-shot converter) or start fresh (config flag).
**Accept when:** desktop app works end-to-end on the new store; stats screen derives from
log; old tests adapted; no writes bypass EventLog.

### Phase 3 (task #12) — Background jobs + prompt queue
`src/loom/jobs.py`: detached processes, opaque IDs (9-char base36), `.output` files under
`~/.loom/jobs/<session>/`, exit-code capture, completion → queue. `src/loom/queue.py`:
enqueue/dequeue/remove with `origin: human | task-notification`, recorded as
`queue-operation` events. The web UI gets "type while busy" queuing.
**Accept when:** a fake slow job completes → notification event → agent turn auto-starts
with the notification as input (verified with scripted completer); queue ops appear in log.

### Phase 4 (task #13) — Task ledger + tools
`src/loom/ledger.py`: `~/.loom/tasks/<session>/<n>.json` + `.lock`, fields exactly per
schema doc. New model-facing tools: `task_create`, `task_update`, `task_list`. Reminder
attachment injected when ledger changed externally or every N turns.
**Accept when:** ledger files match schema byte-for-byte on the example; tools callable by
the fake model; reminders appear as attachment records; survives process restart.

### Phase 5 (task #14) — Attachment channel + context assembler
`src/loom/context.py`: builds the model message list each turn from: system prompt +
memory index + history (or summary) + pending attachments. Attachment types (all in
schema doc): `task_reminder`, `file`, `edited_text_file`, `skill_listing`,
`deferred_tools_delta`, `agent_listing_delta`, `date_change`, `goal_status`.
External-edit detection: mtime/hash watch on files the agent has touched this session.
**Accept when:** each attachment type renders into the prompt deterministically (golden
tests); an external file edit between turns produces an `edited_text_file` attachment.

### Phase 6 (task #15) — Compaction
`src/loom/compaction.py`: manual (`/compact`) + auto (threshold: estimated tokens >
`LOOM_COMPACT_AT`, default 150k). Summarizer prompt with the 9-section rubric (schema
doc §7). Writes `compact_boundary` system record (+ metrics), summary user record
(`isCompactSummary`), preserves last K message-records verbatim (UUID list), re-injects
up to 5 recently-touched files as `file` attachments, re-advertises discovered tools.
**Accept when:** post-compaction context contains summary + tail + re-injected files and
NOT old messages; boundary metadata fields all populated; agent continues a scripted
task across the boundary in tests.

### Phase 7 (task #16) — Memory system
`~/.loom/memory/<project-slug>/`: `MEMORY.md` index + one markdown file per fact with
frontmatter (name/description/metadata.type/originSessionId). Index injected at session
start. Model writes memories via existing file tools; helper `memory.py` for slugging +
index maintenance + recall (recency, not embeddings).
**Accept when:** memory written in session A is present in session B's start context
(index) and injectable on demand; format matches schema doc §8.

### Phase 8 (task #17) — Deferred tools + hooks
Tool registry split: core schemas always in context; the rest advertised name-only via
`deferred_tools_delta`; new `tool_search` tool returns full schemas; discovered set
persisted per session and re-advertised after compaction. Hooks: `post_tool_use`
(pattern → extra context attachment) and `stop` (goal condition text → evaluated by a
cheap model call → can veto completion, emitting `goal_status` + feedback).
**Accept when:** fake model must call tool_search before using a deferred tool (enforced);
stop-hook veto forces a continuation turn in tests.

### Phase 9 (task #18) — Sidechain subagents
Refactor `tools/agents.py`: child turns recorded in the parent's EventLog with
`isSidechain: true` + `sidechainId`; UI renders them nested/collapsed; replay
reconstructs parent+child from one file.
**Accept when:** one JSONL contains both mainline and sidechain records; UI shows nested
card; replay test passes across a task-tool call.

### Phase 10 (task #19) — UI v2
Derive from the log: activity summaries per turn ("Read 4 files · Edited 2 files · Ran
3 commands" — deterministic aggregation, rules in module spec §12), session auto-titles
(cheap model call, `ai-title` record, throttled), resume (last-prompt pointer), task
ledger panel, queue indicator, compaction marker in transcript, sidechain nesting.
**Accept when:** aggregation matches fixture log exactly in tests; title regenerates at
most once per 5 turns; UI renders all v2 record types without console errors.

### Phase 11 (task #20) — Provider abstraction hardening + repackage
`models.py`: per-response model recorded; transport errors → synthetic assistant records
(`isApiErrorMessage`, model `"<synthetic>"`); usage normalized to superset (nullable cache
fields); mid-session model switch (`/model`) is just config + next record differs.
Re-run exe packaging with all new modules; smoke test script `scripts/smoke_exe.ps1`
(curl-based — NEVER Invoke-WebRequest, it false-fails in non-interactive shells).
**Accept when:** switching model mid-scripted-session yields records with two model ids
and identical behavior; exe boots and serves `/api/state` + `/api/skills`.

## 6. Definition of done for v2

- All 11 phases accepted; full suite green (target ≥ 120 tests); ruff clean.
- One demo script `scripts/demo_v2.py` that replays a fixture log demonstrating: queue,
  background job notification, ledger updates, compaction boundary, sidechain, and prints
  derived activity stats.
- README + QUICKSTART updated; exe rebuilt and smoke-tested.

## 7. Progress log (append entries here)

- 2026-07-06: Plan written. Phase 1 started.
- 2026-07-06: Phase 1 DONE — `src/loom/eventlog.py` + 19 tests (`tests/test_eventlog.py`).
  Suite: 61 passed, ruff clean.
- 2026-07-06: Phase 2 DONE — session runtime on the log. New: `server/eventstore.py`
  (derives session list + meta + transcript + dashboard stats from logs; config-only
  `<id>.meta.json` sidecar; `rebuild_messages()` reconstructs the model conversation for
  resume) and `server/logsink.py` (Orchestrator events → schema-valid assistant/tool
  records, forwards live events to the WS). Rewired `bridge.py` + `app.py`; deleted the
  old `sessions.py` (three-list store). Minimal orchestrator emit additions (tool-call id,
  per-msg model, raw usage). Tests: `tests/test_eventstore.py` (7) + adapted `test_server.py`.
  Suite: 67 passed, ruff clean; desktop server boots on the new store and serves the
  session API end-to-end. Known small gaps (deferred to later phases): /rewind and /clear
  notes aren't persisted to the log yet; baseline "$ saved" meter resets on resume.
  Next: Phase 3 (background jobs + prompt queue, task #12).
- 2026-07-06: Phase 3 DONE — background jobs + prompt queue. New: `src/loom/queue.py`
  (FIFO PromptQueue; every op is a `queue-operation` record; rebuilds pending items from
  the log on resume; `on_enqueue` hook) and `src/loom/jobs.py` (JobManager: detached
  subprocess → `~/.loom/jobs/<session>/<id>.output`, 9-char base36 ids, watcher thread
  turns exit into a `<task-notification>` enqueued on the queue). Wired into SessionRunner:
  unified turn engine that starts a turn if idle or queues if busy, auto-drains on turn end,
  and a background job finishing while idle auto-starts a turn with the notification as
  input. Added a per-session `run_shell_background` tool (bound like the `task` tool).
  Transcript now shows both human and task-notification turns. Tests: `tests/test_jobs.py`
  (11, incl. a real subprocess job → notification → auto-started scripted turn). Suite: 78
  passed, ruff clean. Next: Phase 4 (task ledger + task tools, task #13).
- 2026-07-06: Phase 4 DONE — task ledger + task tools. New: `src/loom/ledger.py`
  (TaskLedger: `~/.loom/tasks/<session>/<n>.json` per schema §9, sequential ids, advisory
  `.lock` + in-process lock, status/alias validation, `reminder_payload()`, `render()`).
  Three model tools bound per session in SessionRunner — `task_create` / `task_update` /
  `task_list` (non-risky, no permission prompt). A turn that changes the ledger emits a
  `task_reminder` attachment for the next turn (full assembler wiring is Phase 5). Tests:
  `tests/test_ledger.py` (9, incl. schema-shape check, restart persistence, and a scripted
  model driving the tools end-to-end with a reminder attachment recorded). Suite: 87 passed,
  ruff clean. Next: Phase 5 (attachment channel + context assembler, task #14) — this is
  where task_reminder / injected files / listings actually get rendered into the prompt.
- 2026-07-06: Phase 5 DONE — attachment channel + context assembler. New:
  `src/loom/context.py` — `render_attachment()` for all 9 types (deterministic),
  `messages_from_records()` (the ONE canonical record→chat-message builder; coalesces
  attachment `<context>` notes into the following user message so no dangling consecutive
  user messages), and `ContextAssembler` (`build()`, `pending_context()`, plus mtime+sha1
  external-edit detection: `track`/`track_from_log`/`scan_external_edits`). Removed the
  duplicate builder — `eventstore.rebuild_messages` now delegates to `messages_from_records`.
  Wired into SessionRunner: each turn scans for files the user changed underneath the agent
  (→ `edited_text_file` attachments) and prepends all pending context (reminders + edits) to
  the model input, while the log still stores the clean user message. Tests:
  `tests/test_context.py` (10) incl. two LIVE integration tests capturing the model's actual
  messages — proving a task reminder and an external file edit both reach the model. Suite:
  97 passed, ruff clean. Next: Phase 6 (compaction manager, task #15).
- 2026-07-09: Independent code review of Phases 1–5 (fresh model, full re-read of all v2
  modules + tests, suite re-run from the venv). Verdict: architecture sound (single writer,
  derived views, schema validation at append, integration tests capture real model input).
  Three defects found and fixed: (1) `eventlog._now()` read the clock twice → occasional
  ~1s-wrong timestamps; (2) `PromptQueue.enqueue` wrote its queue-operation record outside
  the lock → resume could rebuild a different order than live under concurrency; (3)
  `pending_context()` bounded by last HUMAN turn → a no-tool-call task-notification turn
  caused reminder double-injection; boundary is now the last turn-opening user record of any
  origin (string content, not isMeta). Suite: 97 passed, ruff clean.
- 2026-07-09: Phase 6 DONE — compaction. New: `src/loom/compaction.py` (Compactor:
  summarizer call through the standard completer interface with the 9-heading rubric;
  `compact_boundary` new-root record with ALL metadata populated — post-size is simulated
  from the parts before the boundary is written since the log is append-only; summary user
  record `isCompactSummary` prefixed with the raw-transcript path; tail = last 6 message
  records re-appended verbatim with usage/cost zeroed so stats don't double-count, never
  opening on an orphan tool_result; up to 5 hot files re-injected as `file` attachments via
  new `ContextAssembler.hot_files()`; discovered deferred tools + agent listing
  re-advertised as deltas). Wired into SessionRunner: `/compact` command + auto-trigger
  after each turn when `estimate_tokens(agent.messages) > LOOM_COMPACT_AT` (default 150k);
  post-compaction the live conversation is rebuilt from the new root and re-injected files
  reach the model via pending_context() on the next turn. Hardened
  `messages_from_records` with a global no-consecutive-user-messages invariant (merge) —
  a live test caught summary(user)+tail-opening-user producing an invalid history.
  Tests: `tests/test_compaction.py` (5, incl. a live 6-response scripted session proving
  the model sees summary + tail but NOT pre-boundary history, and an auto-trigger test).
  Suite: 102 passed, ruff clean. Next: Phase 7 (memory system, task #16).
- 2026-07-10: Phase 7 DONE — long-term memory. New: `src/loom/memory.py` (MemoryStore per
  module spec §10: one fact file per schema §8 with frontmatter, MEMORY.md index upserted
  in lockstep, recency-based `recall(k)`, `prompt_section()` for the system prompt).
  `messages_from_records` gained `extra_system` (threaded through resume, live turns, and
  post-compaction rebuild); a brand-new session records `file` attachments
  (reason "recall") for the 3 most recent facts. DEVIATION (documented in module spec
  §10): model saves via namespaced `memory_write`/`memory_read` session tools, not raw
  file tools — fs tools are workspace-sandboxed and ~/.loom is outside. Also fixed: fresh
  sessions previously fell back to the Orchestrator's own system prompt, which would have
  omitted the memory index; bridge now always seeds agent.messages from the canonical
  builder. Tests: `tests/test_memory.py` (7, incl. session A→B index + recall-attachment
  proof and model-writes-via-tool). Suite: 109 passed, ruff clean.
- 2026-07-10: Live provider smoke on DeepSeek (user-provided key, `deepseek-v4-flash`):
  single completion through models.complete OK (usage + cost computed), and a full
  Orchestrator turn OK — write_file → read_file self-check → done, proof file verified,
  $0.000258/turn. Windows note for Phase 11: force UTF-8 stdout (PYTHONUTF8) in CLI/exe —
  Rich box-drawing crashes cp1252 consoles when output is piped. Next: Phase 8.
- 2026-07-10: Phase 8 DONE — deferred tools + hooks. New: `src/loom/toolreg.py`
  (ToolRegistry: core/deferred split, "select:" + keyword loading, discovered set;
  `loaded_from_records` replays `deferred_tools_delta(loaded=true)` attachments — schema
  §5 row updated with the `loaded` flag) and `src/loom/hooks.py` (HookEngine: PostToolUse
  pattern hooks → `hook_context` attachments, broken hooks swallowed; Stop hook: judge
  model call → `goal_status` attachment; NOT-MET vetoes completion, ≤3/turn, unparseable
  judge output fails OPEN). Engine: `deferred_hidden` set — hidden from schemas, calling
  one returns "Tool 'x' is deferred; call tool_search first." (outcome `deferred`, not an
  error). Orchestrator: schemas refresh EVERY step so a mid-turn tool_search makes the
  tool immediately callable. Bridge: `tool_search` session tool, session-start name-only
  advertisement, discovered-set restore on resume, stop-hook veto loop in the turn engine
  (`LOOM_STOP_CONDITION`), LogSink fires PostToolUse after each persisted tool_result.
  Default deferred set: {grep} (env `LOOM_DEFERRED`). Tests: `tests/test_toolreg.py` (4)
  + `tests/test_hooks.py` (5), incl. live blocked→search→retry discovery and a scripted
  veto→continue→accept goal loop. Suite: 118 passed, ruff clean. NOTE: suite executed via
  a preview-runner workaround (harness Bash approvals flaky today); output verified.
  Next: Phase 9 (sidechain subagents, task #18).
- 2026-07-10: Phase 9 DONE — sidechain subagents. Fixed a LIVE BUG found during design:
  child subagent events flowed through the parent's LogSink (v1 _TaggedSink), where a
  child `step` event prematurely flushed the parent's half-built assistant record and
  child turns were written as mainline records. Now: `EventLog.sidechain(id)` context
  manager (separate tree root, every node stamped isSidechain+sidechainId, mainline head
  saved/restored; resume head-recovery and the mainline-leaf fallback skip sidechain
  nodes) + `LogSink.sidechain(id, agent_type, goal)` yielding a FRESH child sink over the
  same log (own step grouping; goal recorded as the sidechain root user record carrying
  `sidechainAgentType`; live events tagged subagent+sidechainId for nested UI rendering —
  the SPA's existing `.sub` styling covers it). `tools/agents.py` uses the scope when the
  sink provides it (duck-typed; CLI ConsoleSink keeps v1 tagged-events behavior).
  `eventstore`: transcript extracts a shared `_events_from()` mapper and appends
  sidechain groups as nested events merged by timestamp; _meta/stats exclude sidechain
  records from message counts but keep their real usage/cost. Schema §1 envelope
  documents `sidechainId` + `sidechainAgentType`. Tests: `tests/test_sidechain.py` (4):
  scope isolation, resume-head recovery, a live task-tool run proving one JSONL holds
  both trees (clean mainline, report in the tool result, nested transcript, verify() ok),
  and restart-after-sidechain. Suite: 122 passed, ruff clean.
  Next: Phase 10 (UI v2, task #19).
- 2026-07-10: Phase 10 DONE — UI v2. Backend: `activity_from_records`/`summarize_activity`
  in eventstore (deterministic per-turn aggregation per module spec §12, golden fixture
  `tests/fixtures/activity_log.jsonl` + generator in scratchpad); `EventStore.get()` meta
  gains `context_tokens` (estimated model-context size) and `last_prompt` (from the new
  `last-prompt` meta record written per human turn); `records_after()` incremental fetch;
  new REST: /activity, /records?after=, /tasks (ledger), and /api/state exposes
  `compact_at`. Bridge: `turn_end` now carries queued/context_tokens/activity;
  `_maybe_title()` — AI session titles via a cheap model call, ≥5 human turns between
  regenerations, opt-in LOOM_AI_TITLES=1 (set by the `loom ui`/desktop entrypoints so
  scripted tests never trigger it), failures swallowed. Frontend: removed the disabled
  Chat tab (user request — Code only); circular CONTEXT RING next to send showing the
  session's live token usage filling toward the auto-compaction point (green→orange→red,
  tooltip with %); queue badge; task-ledger panel above the transcript; per-turn activity
  notes; compaction markers (live + stored shapes); goal-veto notes. Tests:
  `tests/test_ui2.py` (5: golden activity, plural rules, ai-title throttle, last-prompt +
  turn_end payload, REST endpoints). Suite: 127 passed, ruff clean. VERIFIED LIVE in the
  preview on deepseek-v4-flash: turn ran through the SPA, ring updated 520→702 tokens in
  real time, DeepSeek titled the session "Loom UI OK", zero console errors.
  Next: Phase 11 (provider hardening + exe repackage, task #20).
- 2026-07-17: Phase 11 DONE — provider abstraction hardening + repackage. Much of the
  record plumbing already existed (synthetic `<synthetic>`/isApiErrorMessage records in
  `LogSink._on_model_error`, usage superset in `normalize_usage`, per-record model via the
  step event, `/model` switch in bridge+CLI). Closed the real gaps: `models.complete` now
  returns the ACTUAL model from the provider response (not the requested string), the
  provider request id (`ModelResponse.request_id` → assistant `requestId`; "" still mints
  a `local_…` id), and cache usage when reported (OpenAI `prompt_tokens_details.cached_tokens`
  or Anthropic-style fields → nullable `cache_read/creation_input_tokens` pass through
  `normalize_usage` instead of being forced null). Orchestrator threads `request_id`
  through the step event; LogSink persists it. New `scripts/smoke_exe.ps1` (curl.exe only)
  boots `Loom.exe ui --no-open` and asserts /api/state + /api/skills return 200. Tests:
  `tests/test_provider.py` (4: actual-model/request-id/cache capture, usage superset,
  mid-session /model switch → two model ids + two request ids in one log with identical
  behavior, transport error → synthetic record and the session stays usable). Suite: 131
  passed, ruff clean. Exe rebuilt via `scripts/build_exe.ps1` and smoke-tested.
  ALL 11 PHASES COMPLETE. Machine gotcha: keep `.ps1` files ASCII-only — PowerShell 5.1
  parses BOM-less UTF-8 as ANSI, and an em-dash in build_exe.ps1 was a parse error.
- 2026-07-17: v2 DEFINITION OF DONE (§6) closed. `scripts/demo_v2.py` — offline one-command
  demo: queue ops, a real background job → task-notification, ledger create/update,
  sidechain records, a compaction boundary, then activity stats + record census + verify()
  all DERIVED back out of the one JSONL. Writing the demo exposed a real bug: re-appended
  compaction-tail copies double-counted in activity aggregation (usage was zeroed but tool
  mirrors weren't skipped — a phantom duplicate turn per compaction in /activity and
  turn_end). Fix at the source: `Compactor._reappend` stamps `isCompactTail: true` (schema
  §6 updated) and `activity_from_records` skips flagged records; regression test in
  test_compaction. README + QUICKSTART refreshed (v2 status, 132 tests, demo pointer).
  Suite: 132 passed, ruff clean. v2 IS DONE. Natural next steps beyond v2: grow the eval
  suite + headline benchmark numbers, per-step difficulty routing on the Conductor,
  record subagent model calls in the parent trace (v1 replay limitation).
