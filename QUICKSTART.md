# Loom quickstart

## Install

Use Python 3.11 or newer. From a clone of this repository:

```powershell
python -m venv .venv
# Activation is optional when using the interpreter explicitly.
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -X utf8 scripts\demo_v2.py
```

For macOS/Linux, activate with `source .venv/bin/activate`, then use `python` in place of `.\.venv\Scripts\python.exe`. Windows is the platform verified in [the current review](docs/VERIFICATION.md).

The suite currently contains 132 offline tests. It uses scripted model responses and temporary workspaces. The demo prints queue, background-job, task-ledger, sidechain, and compaction activity, then verifies its event log. Neither command needs an API key.

## Configure a live model

Copy the template on Windows:

```powershell
Copy-Item .env.example .env
```

On macOS/Linux: `cp .env.example .env`.

Set a provider key and matching LiteLLM model ID. For the template's OpenRouter configuration:

```dotenv
OPENROUTER_API_KEY=your-key-here
LOOM_MODEL=openrouter/moonshotai/kimi-k2
```

Use a model available to your account that supports tool calling. Provider availability and live calls have not been checked in this review.

Alternatively, start the UI and use **Customize**. Settings are saved in `~/.loom/loom.env`; keys are masked in settings responses but stored as plaintext locally. A project `.env` takes precedence over that user file. Keep both files private.

| Setting | Default / behavior |
| --- | --- |
| `LOOM_MODEL` | Workhorse model |
| `LOOM_MODEL_STRONG` | Escalation after tool failure or critic rejection; defaults to workhorse |
| `LOOM_MODEL_CRITIC` | Diff reviewer; defaults to workhorse |
| `LOOM_TEMPERATURE` | `0.2` |
| `LOOM_MAX_STEPS` | `30` steps per core turn |
| `LOOM_MAX_COST_USD` | `1.0`; checked after tool steps, not a hard global spending cap |
| `LOOM_AUTO_APPROVE` | `0`; approval prompts enabled |
| `LOOM_CRITIC` | `1`; critic enabled |
| `LOOM_MAX_CRITIC_ROUNDS` | `2` revision rounds |
| `LOOM_BASELINE_INPUT_PER_M`, `LOOM_BASELINE_OUTPUT_PER_M` | Comparison rates: `3.0` and `15.0` USD per million tokens by default, not verified market prices |

## Run

With the environment activated:

```powershell
loom --help
loom --dir C:\code\example
loom --dir C:\code\example "add a slugify function and tests, then run pytest"
loom ui
loom ui --no-open --port 8763
```

Without activation, replace `loom` with `.\.venv\Scripts\python.exe -m loom`.

The UI opens at http://127.0.0.1:8763. Choose a project folder, configure the model, and send a task. Edits and shell commands pause for **Allow once / Always this session / Deny**. The agent works on actual files; begin with a disposable example project. Stop the server with Ctrl+C.

`loom -y "task"` skips tool approval prompts. Use it only when you intend to authorize all tools in that workspace.

### Interactive CLI commands

| Command | Behavior |
| --- | --- |
| `/cost` | Usage cost and comparison with the configured baseline |
| `/rewind` | Restore the last turn's tracked file changes |
| `/model <id>` | Change the workhorse model |
| `/skills` | List prompt skills |
| `/commit`, `/review`, `/test` | Run a built-in prompt skill |
| `/clear` | Clear conversation history; files remain |
| `/help` | Show commands |
| `exit` | Quit the chat |

The UI session runner also exposes compaction and session tools for tasks, memory, background jobs, and deferred tool discovery. These integrations are not all available in the terminal CLI.

## Replay and fork

CLI traces are stored in `.loom/traces/`. Replay consumes recorded responses but executes tools again:

```powershell
loom --dir C:\code\fresh-copy --replay C:\code\original\.loom\traces\RUN.jsonl
loom --dir C:\code\fresh-copy --replay C:\code\original\.loom\traces\RUN.jsonl --fork-at 4
```

`--fork-at N` uses the first N recorded model calls, then calls the live model. Model calls include reviewer/subagent calls recorded in the trace. Full replay needs no live provider; a fork does. File operations and shell effects depend on workspace state, so use a fresh copy matching the original input.

## Live evaluations

```powershell
.\.venv\Scripts\python.exe evals\harness.py
```

This calls a real model, auto-approves tools in temporary task copies, and writes `evals/report.md`. See [evals/README.md](evals/README.md) for options and scoring. No live evaluation results are included in the current verification report.

## Optional Windows desktop window

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
.\.venv\Scripts\python.exe -m loom desktop
```

The native wrapper uses pywebview and falls back to the browser when it cannot create a native window. To build the standalone Windows executable:

```powershell
powershell -File scripts\build_exe.ps1
powershell -File scripts\smoke_exe.ps1
```

The build helper installs the existing `desktop` and `build` extras and uses PyInstaller. Output is `dist/Loom.exe`, which is excluded from Git. These executable/native-window commands were not rerun in the current review; a Python wheel was built and tested instead.

## Troubleshooting

- **`loom` not found:** activate the environment or use `python -m loom` with its interpreter.
- **No key / provider errors:** configure the correct key for the model prefix and verify account access. Offline tests should still run.
- **Port in use:** select another port with `--port`.
- **Unicode output on Windows:** use `python -X utf8` for the demo, or set `PYTHONUTF8=1`.
- **Model pricing metadata warning offline:** LiteLLM can fall back to bundled pricing metadata. Treat displayed costs as estimates.

See [docs/VERIFICATION.md](docs/VERIFICATION.md) for the checked environment and remaining limitations.
