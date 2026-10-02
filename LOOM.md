# LOOM.md — project memory

This file is re-injected into Loom's system prompt every session. Keep it short and high-signal:
durable facts about *this* repo that the agent should always know. (Later phases will update it
automatically with "lessons learned"; for now, edit it by hand.)

## What this repo is
Loom — an open, terminal coding agent on cheap open models. Python, src-layout (`src/loom`).

## How to run
- Install: `uv sync` (or `pip install -e ".[dev]"`)
- Run the agent: `loom "your task"`  or just `loom` for an interactive prompt
- Tests: `pytest`  (no API key needed — tests don't call models)

## Conventions
- Edits are exact string replacements (`old_string` → `new_string`), not line numbers.
- Read a file before editing it.
- Prefer minimal, surgical changes. Run the tests after changing code.
- Tools that write files or run shell commands require user approval unless `--yes` is passed.
