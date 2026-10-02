# Copilot instructions — Loom

Loom is an **open, glass-box coding agent** (Claude Code / Codex style) built on cheap open models,
with a terminal CLI *and* a local browser app. Its three differentiators: configured model tiers
("the Conductor"), a Critic that reviews diffs after edits, and recorded replay/fork.

**Verified October 2, 2026: 132 offline tests pass, Ruff passes, Python wheel builds, browser UI starts.**
Live providers and a fresh executable build remain unverified; see `docs/VERIFICATION.md`.
Original roadmap ideas are in `docs/03-roadmap.md`. The v2 architecture is specified in
`docs/v2/00-master-plan.md` (+ `01-data-schemas.md`, `02-module-specs.md`) — **those schemas are law**.

## Stack (do not add to it)

Python 3.11 · LiteLLM (any provider) · FastAPI + WebSocket for the desktop app · vanilla-JS SPA
(no build step) · PyInstaller for the exe. Setup instructions use stock `venv` + `pip`.

## Commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q                 # offline, no keys, must stay green
.\.venv\Scripts\python.exe -m ruff check src tests      # must stay clean
.\.venv\Scripts\python.exe evals\harness.py             # the eval suite (needs a key)
loom ui                                                  # desktop app
powershell -File scripts\build_exe.ps1                   # -> dist\Loom.exe
powershell -File scripts\smoke_exe.ps1                   # boots the exe, asserts the API answers
```

## Invariants — never break these

1. **The event log is the single source of truth.** One append-only JSONL per session;
   `eventlog.py` is the ONLY writer. UI transcript, stats, activity summaries, resume and replay are
   all **derived** from it. Never maintain a second copy of session state.
2. **Schemas are law.** Implement exactly what `docs/v2/01-data-schemas.md` says. If you must
   deviate, update that doc in the same commit.
3. **Compaction tail records carry `isCompactTail: true`.** They are verbatim copies of already-
   logged records, so anything that *counts* (activity aggregation, stats) must skip them or it
   double-counts. This was a real bug; don't reintroduce it.
4. **One canonical record→message builder**: `context.py::messages_from_records`. It enforces the
   no-consecutive-user-messages invariant. Do not write a second one.
5. **Model-agnostic.** Model identity is per-assistant-record; a mid-session `/model` switch changes
   nothing else. Transport errors become synthetic assistant records (`model: "<synthetic>"`,
   `isApiErrorMessage: true`), never crashes.
6. **Everything offline in tests**: scripted fake completers (`SequenceCompleter`, or monkeypatch
   `models.complete`), `tmp_path`, no network, no real home directory.

## Windows gotchas

- Keep `.ps1` files **ASCII-only** — PowerShell 5.1 parses BOM-less UTF-8 as ANSI and an em-dash is
  a parse error. This actually broke `build_exe.ps1`.
- Use `curl.exe`, never `Invoke-WebRequest`, in smoke scripts.
- Force UTF-8 stdout (`PYTHONUTF8=1`) or Rich's box-drawing crashes a cp1252 console when piped.

## Verification boundaries

Subagent replay is covered by `tests/test_replay.py`. Replay executes tools again and depends
on workspace state. Permission prompts are not an OS sandbox; shell side effects are not
fully reversible. The UI serializes agent turns because tools use process-wide cwd.
