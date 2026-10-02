# Loom v2 — Module Specifications (normative)

Public APIs, responsibilities, and dependency rules. Names are binding: implementers keep
these signatures so phases integrate cleanly. All schemas referenced are in
[01-data-schemas.md](01-data-schemas.md).

**Dependency rule (strict):** modules may only import DOWN this list, never up.

```
eventlog → (nothing loom-internal)
queue, jobs, ledger, memory → eventlog
context, compaction, hooks, toolreg → eventlog, ledger, memory, queue
orchestrator → all of the above
server/*, __main__ → orchestrator
```

---

## 1. `loom/eventlog.py` — EventLog (Phase 1)

Single writer for all session state. Append-only.

```python
class EventLog:
    def __init__(self, root: Path, project_slug: str, session_id: str | None = None): ...
    # creates/opens ~/.loom/sessions/<slug>/<sessionId>.jsonl

    @property
    def session_id(self) -> str: ...
    @property
    def head_uuid(self) -> str | None: ...     # newest tree-node uuid (the leaf)

    def append(self, type: str, *, parent: str | None = "HEAD", **fields) -> str:
        """Write one record. parent="HEAD" chains to current leaf; None makes a root.
        Fills envelope (uuid, timestamp, sessionId, cwd, version, isSidechain=False).
        Returns the new uuid. Raises on unknown type or schema violation."""

    # typed conveniences (thin wrappers over append, enforcing §2–§6 shapes):
    def user(self, content, *, origin="human", is_meta=False, **kw) -> str: ...
    def assistant(self, message: dict, *, request_id: str, cost_usd: float | None, **kw) -> str: ...
    def tool_result(self, tool_use_id: str, text: str, mirror: dict,
                    source_assistant_uuid: str, is_error=False) -> str: ...
    def attachment(self, atype: str, payload: dict) -> str: ...
    def system(self, subtype: str, content: str, level="info", **kw) -> str: ...
    def meta(self, type: str, **fields) -> None: ...   # queue-operation/mode/ai-title/last-prompt

    # readers (used by UI, context assembler, stats):
    def read_all(self) -> list[dict]: ...
    def records(self, *types: str) -> Iterator[dict]: ...
    def mainline(self) -> list[dict]:
        """Tree nodes from the NEWEST root to the leaf, in order (post-compaction view)."""
    def verify(self) -> LogReport:
        """Integrity: orphan parents, unmatched tool_use ids, schema violations."""
```

Concurrency: one process, many threads → a `threading.Lock` around append; file opened
in append mode per write (crash-safe).

## 2. `loom/queue.py` — PromptQueue (Phase 3)

```python
class PromptQueue:
    def __init__(self, log: EventLog): ...
    def enqueue(self, content: str, origin: str = "human") -> str: ...  # returns item id
    def remove(self, item_id: str) -> bool: ...
    def pop(self) -> QueueItem | None: ...   # records dequeue op; FIFO, human before notifications? NO — strict FIFO
    def peek_all(self) -> list[QueueItem]: ...
```
Every operation writes a `queue-operation` record. The agent worker loop drains the
queue whenever it finishes a turn; if the queue is non-empty, the next turn starts
immediately with the popped item as the user message (`origin` preserved on the record).

## 3. `loom/jobs.py` — JobManager (Phase 3)

```python
class JobManager:
    def __init__(self, log: EventLog, queue: PromptQueue, root: Path): ...
    def start(self, command: str, description: str, cwd: str, timeout_s: int = 3600) -> str:
        """Detached subprocess; stdout+stderr → jobs/<session>/<id>.output.
        Returns job id (9-char base36). Non-blocking."""
    def status(self, job_id: str) -> JobStatus: ...   # running|completed|failed + exitCode
    def stop(self, job_id: str) -> bool: ...
```
A watcher thread per job; on exit it formats the notification (schema §11) and calls
`queue.enqueue(notification, origin="task-notification")`. Jobs are OS processes: they
keep running through compaction (nothing to do — compaction is a context operation).
The shell tool gains `run_in_background: bool` which routes to JobManager and returns
the id + output path to the model immediately.

## 4. `loom/ledger.py` — TaskLedger (Phase 4)

```python
class TaskLedger:
    def __init__(self, root: Path, session_id: str): ...
    def create(self, subject: str, description: str, active_form: str = "") -> str: ...
    def update(self, task_id: str, **changes) -> dict: ...   # status/subject/description/blocks/blockedBy
    def get(self, task_id: str) -> dict: ...
    def list(self) -> list[dict]: ...
    def delete(self, task_id: str) -> bool: ...
```
File-per-task per schema §9, exclusive lock for writes. Exposed to the model as three
tools (`task_create`, `task_update`, `task_list`) registered in the global tool registry
with `risky=False`. The context assembler injects a `task_reminder` attachment when
(a) the ledger changed since last injection, or (b) 8 turns passed without one.

## 5. `loom/context.py` — ContextAssembler (Phase 5)

```python
class ContextAssembler:
    def __init__(self, log: EventLog, ledger: TaskLedger, memory: MemoryStore,
                 toolreg: ToolRegistry, workdir: str): ...
    def build(self) -> list[dict]:
        """Model message list for the next turn:
        [system prompt (incl. memory index + skill/agent listing on first turn)]
        + mainline() records mapped to chat messages
        + pending attachments rendered as user-role text blocks tagged
          <context type="<atype>">…</context>, appended before the newest user msg."""
    def notice_external_edit(self, path: str) -> None: ...  # → edited_text_file attachment
    def pending_attachments(self) -> list[dict]: ...
```
Rendering rules (golden-tested): task_reminder → markdown checklist; file →
`<file path=…>` fenced content; edited_text_file → `<external-edit path=…>` + snippet;
listings → bullet lists; date_change/goal_status → one-line notes. Deterministic:
same log ⇒ same prompt, byte for byte.
File watch: keep `{path: (mtime, sha1)}` for every file the agent read/wrote this
session; check before each turn; changed ⇒ `edited_text_file` attachment (max 8 KB snippet).

## 6. `loom/compaction.py` — Compactor (Phase 6)

```python
class Compactor:
    def __init__(self, log: EventLog, assembler: ContextAssembler, completer, model: str): ...
    def should_compact(self, est_tokens: int) -> bool: ...   # est > LOOM_COMPACT_AT (150k)
    def compact(self, trigger: str) -> CompactReport:
        """1. summary = summarizer model call (rubric schema §7, ≤4k tokens)
           2. tail = last 6 mainline records' uuids
           3. hot = up to 5 most-recently touched files (from assembler watch table)
           4. append system/compact_boundary (parent=None!) with full metadata
           5. append user record isCompactSummary=True (parent=boundary)
           6. re-append tail records (new uuids, same content, parent-chained)
           7. append file attachments for hot files
           8. re-advertise discovered deferred tools + agent listing (deltas)"""
```
Token estimation: `len(json.dumps(messages)) // 4` — crude is fine, only the trigger uses it.
The summarizer runs with the SAME completer interface as everything else (fake-able in tests).

## 7. `loom/toolreg.py` — ToolRegistry with deferral (Phase 8)

```python
class ToolRegistry:
    def __init__(self, core: list[ToolMeta], deferred: list[ToolMeta]): ...
    def schemas_for_context(self) -> list[dict]: ...   # core + LOADED deferred only
    def deferred_names(self) -> list[str]: ...          # advertised via attachment
    def load(self, query: str) -> list[ToolMeta]: ...   # "select:name,name" or keyword match
    def discovered(self) -> list[str]: ...              # survives compaction (re-advertised)
    def lookup(self, name: str) -> ToolMeta | None: ...  # None if deferred & not loaded → error result
```
`tool_search` is itself a core tool. Calling a deferred, unloaded tool returns an error
tool result: `"Tool '<name>' is deferred; call tool_search first."` (the model recovers).

## 8. `loom/hooks.py` — Hooks (Phase 8)

```python
@dataclass
class StopHook:  condition: str; model: str  # evaluated by cheap model: met? feedback?
@dataclass
class PostToolUseHook:  tool_pattern: str; provider: Callable[[ToolCall, str], str | None]

class HookEngine:
    def after_tool(self, call, result) -> None: ...     # may append hook_context attachment
    def on_stop(self, transcript_tail: str) -> StopVerdict: ...
        # met=False → orchestrator appends goal_status attachment + synthetic user
        # record (origin="hook") with feedback, and runs another turn (max 3 vetoes).
```

## 9. `loom/orchestrator.py` changes (Phases 2–9, incremental)

The loop per turn becomes:

```
item = queue.pop() or wait-for-user
log.user(item)
loop:
    msgs = assembler.build()
    if compactor.should_compact(est(msgs)): compactor.compact("auto"); msgs = assembler.build()
    resp = completer(model, msgs, toolreg.schemas_for_context())
    log.assistant(resp)
    if resp.tool_calls: execute each → permission gate → hooks.after_tool → log.tool_result; continue
    verdict = hooks.on_stop(...)   # if configured
    if not verdict.met and vetoes < 3: inject feedback; continue
    break
drain queue (next item starts immediately)
```

Subagents (Phase 9): child orchestrator gets the SAME EventLog with a
`sidechain(sidechain_id)` context manager that stamps `isSidechain: true` on every
record it appends; parent records the `task` tool_use/result as usual.

## 10. `loom/memory.py` — MemoryStore (Phase 7)

```python
class MemoryStore:
    def __init__(self, root: Path, project_slug: str): ...
    def index_markdown(self) -> str: ...        # contents of MEMORY.md ("" if none)
    def write(self, name, description, mtype, body, origin_session) -> Path: ...  # + index line
    def read(self, name) -> str | None: ...
    def recall(self, k=3) -> list[Path]: ...    # most recently modified k files
```

**Deviation from the master plan (documented 2026-07-09):** the model saves memories via
dedicated per-session tools `memory_write` / `memory_read` (bound in `server/bridge.py`,
non-risky) rather than raw file tools. Reason: the fs tools are sandboxed to the workspace
(`_safe_path` rejects any path outside CWD) and `~/.loom/memory/` is outside it; weakening
that sandbox is worse than adding a namespaced tool, and the helper owning the write is
what keeps `MEMORY.md` in lockstep with the fact files on every model. Injection points:
the index rides in the system prompt via `messages_from_records(extra_system=…)`; a
brand-new session additionally records `file` attachments (`reason: "recall"`) for the
`recall(3)` most recent facts, delivered by `pending_context()` on the first turn.

## 11. `server/` changes (Phases 2, 10)

- `sessions.py` → thin wrapper: list sessions = scan JSONL dir; stats/heatmap/streaks
  computed from event logs (same outputs as v1 dashboard, new source).
- `bridge.py` → SessionRunner owns {EventLog, PromptQueue, JobManager, Orchestrator};
  WebSocket messages: `user_message` → queue.enqueue; `permission_response` unchanged;
  server → client messages are v2 records verbatim (the SPA renders record types directly).
- New REST: `GET /api/sessions/{id}/records?after=<uuid>` (incremental fetch),
  `GET /api/sessions/{id}/activity` (Phase-10 aggregation).

## 12. Activity aggregation (Phase 10, deterministic — NO model call)

Group mainline records by turn (a turn = user record → next user record with
origin=human). Within a turn, over tool_result mirrors:

```
files_read    = distinct targets where category == file_read
files_edited  = distinct targets where category in (file_write, file_edit)
searches      = count where category == search
commands      = count where category == command
subagents     = count where category == agent
```
Render: "Read 4 files · Edited 2 files · Ran 3 commands" (omit zero groups).
Golden test: fixture log in `tests/fixtures/activity_log.jsonl` with expected strings.

## 13. Testing conventions (all phases)

- Fake completer: `SequenceCompleter([...scripted ModelResponse...])` (exists in v1 replay).
- Every phase adds `tests/test_<module>.py`; fixtures under `tests/fixtures/`.
- No test touches the network, the real home dir (use `tmp_path`), or wall-clock sleeps > 0.2 s.
