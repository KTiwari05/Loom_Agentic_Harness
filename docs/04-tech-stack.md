# 04 — Tech Stack & Code Layout

Every choice here optimizes for three things, in order: **(1) demonstrating AI-engineering skill**, **(2) shipping fast as a solo learner**, **(3) running well on your Windows machine**.

## Language: Python 3.11+

**Why not TypeScript** (what Claude Code uses)? Because the *job you want* is AI engineer, and the AI-engineering ecosystem — model SDKs, evals, RAG, tree-sitter bindings, observability, the tiny ML you'll do for the router — is richest and most idiomatic in Python. Most AI-eng job descriptions list Python first. ([source](08-research-and-sources.md)) You get a beautiful terminal UI in Python too (Textual), so you lose nothing.

> A reasonable alternative: TS + Bun + Ink if you specifically want frontend-leaning roles. For AI-engineer roles, **Python wins.** Pick one and don't look back.

## The stack

| Concern | Choice | Why |
|---|---|---|
| Package/venv manager | **uv** | Fastest, modern, one tool for venv + deps + lockfile. |
| Model calls | **LiteLLM** (+ raw `httpx` where needed) | One OpenAI-compatible interface to Kimi, GLM, DeepSeek, Qwen, etc. Swap models with a string. Essential for the router. |
| Model gateway | **OpenRouter** | One API key → every open model. Removes 5 signups. Use Moonshot direct for Kimi once you care about price. |
| Structured outputs | **Pydantic** (+ `instructor` if useful) | Typed tool args, validated model JSON, fewer parsing bugs. |
| TUI | **Textual** | Gorgeous async terminal UIs in pure Python (input box, diff view, live cost meter). Start with `rich` for plain output in Phase 1. |
| Code parsing (RAG) | **tree-sitter** (`tree-sitter-languages`) | AST-aware chunking for real code retrieval. |
| Embeddings + vector store | **fastembed** or a small API model + **LanceDB** | Local, fast, zero infra. LanceDB is an embedded vector DB — no server. |
| Tracing | **OpenTelemetry** + **SQLite** (JSONL mirror) | Industry-standard observability; SQLite for queryable trace history. |
| Eval runner | **pytest** + a thin custom harness | You already use pytest as a tool; reuse it for evals. |
| Sandbox (later) | **subprocess + a temp dir** → **Docker** (Phase 7) | Start with timeouts + scratch dirs; graduate to containers when you do autonomous runs. |
| Config | `LOOM.md` + `pydantic-settings` + `.env` | Project memory in Markdown (re-injected each turn), secrets in `.env`. |

**Deliberately avoided:** LangChain/LangGraph as the *core*. Reason: for a *portfolio* project, hand-rolling the agent loop teaches you (and signals) far more than wiring a framework. You'll *understand* every token. Mention in your README that you evaluated LangGraph and chose to build the loop yourself for control and transparency — that's a senior-sounding decision. (You can still borrow ideas from Deep Agents / Open SWE — see [research](08-research-and-sources.md).)

## Proposed code layout

```
loom/
├─ pyproject.toml            # uv-managed
├─ LOOM.md                   # project memory, re-injected into the agent each session
├─ README.md
├─ src/loom/
│  ├─ __main__.py            # entrypoint: `python -m loom` / `loom`
│  ├─ orchestrator.py        # THE agent loop
│  ├─ conductor.py           # model router (F1)
│  ├─ context/
│  │  ├─ engine.py           # assemble / compact / isolate
│  │  └─ retrieval.py        # AST RAG (F4)
│  ├─ tools/
│  │  ├─ base.py             # Tool ABC + JSON schema generation from type hints
│  │  ├─ fs.py               # read_file, write_file, edit_file, list_dir
│  │  ├─ shell.py            # run_bash (with timeout + sandbox)
│  │  └─ search.py           # grep
│  ├─ agents/
│  │  ├─ planner.py
│  │  ├─ implementer.py
│  │  └─ critic.py           # the verifier (F2)
│  ├─ permissions.py         # allow/deny + interactive prompt
│  ├─ checkpoints.py         # content-addressed snapshots (F5)
│  ├─ recorder.py            # tracing + replay (F3)
│  ├─ models.py              # LiteLLM wrappers, pricing table, cost accounting
│  └─ tui/                   # Textual app (Phase 6)
├─ evals/
│  ├─ tasks/                 # task fixtures (mini repos + expected outcomes)
│  ├─ harness.py             # run tasks, score success/cost/steps
│  └─ report.py              # generate the eval report
└─ tests/                    # your own unit tests (eat your own dog food)
```

## Key implementation notes (save yourself pain)

- **Generate tool JSON schemas from Python type hints + docstrings** (a `@tool` decorator that introspects the signature). Don't hand-write JSON schemas — it's error-prone and looks amateur.
- **Stream model output** from day one; it makes the tool feel alive and is trivial with LiteLLM.
- **Centralize cost accounting** in `models.py` with a pricing table (see [models doc](05-models-and-cost-routing.md)). Every model call returns `(text, tool_calls, usage, cost)`.
- **Make the trace the source of truth.** If it's not in the trace, it didn't happen. Evals, the cost meter, replay, and the router's training data all read from the trace — build it early (Phase 4, but design for it in Phase 1 by emitting events even if you only print them).
- **Edits via exact string-replace, not line numbers.** Line numbers drift; `(old_string, new_string)` patches are robust and are how real agents do it.
- **Windows note:** `run_bash` should shell out via `bash` if available (Git Bash/WSL) or fall back to PowerShell; normalize paths. Test on your actual machine early.

## Minimal dependency list to start (Phase 0–1)

```
uv add litellm pydantic rich python-dotenv
# later: textual tree-sitter-languages lancedb fastembed opentelemetry-sdk
```

That's enough to build a real agent. Add the rest per phase — don't install everything on day one.
