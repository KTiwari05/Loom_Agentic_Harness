# Verification report

**Review date:** October 2, 2026

**Environment:** Windows, Python 3.11.4, existing project virtual environment

**Assessment:** the local prototype passes its offline checks and starts successfully. Live provider task quality and production readiness have not been established.

## Executed checks

| Check | Result |
| --- | --- |
| `python -m pytest -q` | **132 passed**, one dependency deprecation warning, 49.05 seconds |
| `python -m ruff check src tests` | All checks passed |
| `python -m pip check` | No broken requirements found |
| `python -m loom --help` | CLI loads and lists supported commands |
| `python -X utf8 scripts/demo_v2.py` | Exit 0; queue, background job, ledger, sidechain, compaction, and event-log verification completed |
| `python -m pip wheel . --no-deps --wheel-dir .loom/review/wheels` | Built `loom_agent-0.1.0-py3-none-any.whl` using the declared Hatchling backend |
| Wheel contents | Includes HTML, CSS, JavaScript, and all three built-in skill files |
| Installed-wheel smoke check | Imported from a separate target directory with `python -I`; seven HTTP routes returned 200 and three built-in skills were discoverable |
| Live local server | `python -m loom ui --no-open --port 8879`; root, `/api/state`, `/app.js`, and `/openapi.json` returned HTTP 200 |
| Browser inspection | Home dashboard, no-key setup banner, Customize form, and empty Artifacts view rendered |

The installed-wheel check used the existing virtual environment's dependencies, rather than a completely fresh dependency environment. The UI inspection used an isolated local home directory so existing user sessions and settings were not involved.

## What the offline suite verifies

The suite exercises real tool execution and temporary file changes with scripted model responses. Coverage includes permission handling, checkpoints, critic revision, replay and fork plumbing, subagent replay, provider response normalization, evaluation scoring, skills, event-tree integrity, transcript reconstruction, compaction, prompt queues, background jobs, task tracking, project memory, deferred tools, hooks, REST endpoints, and a WebSocket user-message → permission → file-write flow.

This is functional test coverage, not a measured coverage percentage or an LLM capability benchmark.

## Warnings observed

- Starlette's TestClient emits a deprecation warning about its `httpx` integration. Tests pass with the current installed dependencies; future dependency compatibility should be checked.
- In the restricted offline environment, LiteLLM could not fetch its remote model pricing map and used its bundled backup. Startup and the demo still succeeded. Pricing displays remain estimates.
- An initial wheel check with `--no-build-isolation` failed because Hatchling was not installed in the existing virtual environment. The normal isolated build installed the backend declared in `pyproject.toml` and succeeded; no dependency declaration change was needed.

## Source-review limits

- Conductor routing selects configured model tiers and escalates after failures; learned difficulty routing is future work.
- Critic review runs after edits, has bounded revision rounds, and can fail or be skipped. It does not guarantee approval or execute tests itself.
- Permissions and workspace checks are not an OS sandbox. Shell side effects are not fully covered by file rewind.
- Replay re-executes tools and depends on matching workspace state; it is not a dry-run viewer.
- The UI serializes core agent turns because tools use process-wide cwd. This is a local single-user application without API authentication.
- Cost guards are checked at core tool-step boundaries and should not be interpreted as a hard global cap across every model call.
- Original design documents include proposed features and earlier research. Current implementation claims are in the root README and architecture overview.

## Not verified

- Live OpenRouter or other provider requests, model availability, credentials, task success rates, and measured cost reductions.
- A newly built `Loom.exe`, native pywebview window behavior, or execution on a separate Windows PC.
- macOS/Linux execution, a fresh dependency environment, long-running soak behavior, hosted deployment, or security isolation.

No application source changes were required for the checks above. Publication changes focus on accurate reviewer documentation and excluding local credentials, caches, traces, build products, and scratch experiments from Git.
