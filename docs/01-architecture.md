# 01 — Architecture

This is the system design. Read it once for shape; you'll implement it incrementally per the [roadmap](03-roadmap.md). Nothing here is exotic — modern agents like Claude Code reduce to a surprisingly simple core loop plus disciplined engineering around it. ([source](08-research-and-sources.md))

## The mental model

An agentic coding tool is, at its heart, one loop:

> **Ask the model what to do → do it → show the model the result → repeat, until the model says it's done.**

Everything else — tools, routing, memory, verification, UI — is scaffolding around that loop. Build the loop first (Phase 1). Add scaffolding later.

## High-level diagram

```
┌──────────────────────────────────────────────────────────────────────────┐
│                                  TUI / CLI                                 │
│              (input box, streaming output, diff viewer, cost meter)        │
└───────────────┬────────────────────────────────────────────┬─────────────┘
                │ user goal                                   │ stream of events
                ▼                                             ▲
┌──────────────────────────────────────────────────────────────────────────┐
│                              ORCHESTRATOR                                   │
│   owns the agent loop, the message history, checkpoints, and the budget    │
│                                                                            │
│   ┌────────────┐   ┌──────────────┐   ┌───────────────┐   ┌─────────────┐  │
│   │ CONDUCTOR  │   │   CONTEXT    │   │   TOOL ENGINE │   │  RECORDER   │  │
│   │ (router)   │   │   ENGINE     │   │ (dispatch +   │   │ (tracing +  │  │
│   │ picks model│   │ (build the   │   │  permission + │   │  replay)    │  │
│   │ per step   │   │  prompt)     │   │  sandbox)     │   │             │  │
│   └─────┬──────┘   └──────┬───────┘   └───────┬───────┘   └──────┬──────┘  │
└─────────┼─────────────────┼──────────────────┼──────────────────┼─────────┘
          │                 │                   │                  │
          ▼                 ▼                   ▼                  ▼
   ┌────────────┐   ┌───────────────┐   ┌──────────────┐   ┌──────────────┐
   │ Model APIs │   │  RAG / Memory │   │  Tools:      │   │  Trace store │
   │ Kimi / GLM │   │  (AST index,  │   │  read,write, │   │  (SQLite +   │
   │ DeepSeek...│   │   repo graph) │   │  bash,grep…  │   │   JSONL)     │
   └────────────┘   └───────────────┘   └──────────────┘   └──────────────┘
```

## The core components

### 1. Orchestrator (the agent loop)

The beating heart. Pseudocode for the minimal version (Phase 1):

```python
def run(goal: str):
    messages = [system_prompt(), user(goal)]
    while True:
        model = conductor.pick_model(messages)          # Phase 3: routing
        response = model.complete(messages, tools=TOOLS) # streamed
        recorder.log(response)                           # Phase 5: tracing
        messages.append(response.as_message())

        if response.tool_calls:
            for call in response.tool_calls:
                result = tool_engine.execute(call)       # with permission/sandbox
                messages.append(tool_result(call, result))
            continue                                     # loop again with results

        if response.is_final():                          # model produced an answer, no tools
            return response.text
```

Key design rules (learned from how real agents work):
- **Tool results are just appended to history**; there is no separate "handle tool output" phase. The next loop iteration sees them naturally.
- **The loop is single-threaded and synchronous** at the top level. Parallelism (best-of-N, sub-agents) happens *inside* a step, not in the main loop. This keeps it debuggable.
- **The model decides when it's done** by returning text with no tool calls. You don't hard-code stopping logic beyond a max-iteration / max-budget guard.

### 2. The Conductor (cost-aware model router)

The signature component. Before each model call, the Conductor decides *which* model to use based on the difficulty of the current step. See [`05-models-and-cost-routing.md`](05-models-and-cost-routing.md) for the full algorithm. In Phase 1 it's a constant ("always Kimi K2"); it becomes intelligent in Phase 3.

### 3. Context Engine

Decides *what goes into the prompt*. This is where "context engineering" lives — the discipline that separates a toy from a tool. ([source](08-research-and-sources.md)) Four jobs:

- **Assemble**: system prompt + `LOOM.md` project memory + relevant code (from RAG) + recent history.
- **Retrieve**: pull only the code the step needs via AST-aware retrieval (not the whole repo).
- **Compact**: when history approaches the context window, summarize old turns and offload full content to references. ("Reduction" + "offloading".)
- **Isolate**: hand sub-agents a fresh, scoped context so exploration doesn't pollute the main thread.

A useful framing of the four context techniques (offload, reduce, retrieve, isolate) is in the [research doc](08-research-and-sources.md).

### 4. Tool Engine

Tools are how the agent touches the world. Each tool is a typed function with a JSON schema the model sees. The engine handles **dispatch → permission check → sandbox → execute → format result**.

Minimum viable toolset (Phase 1–2):

| Tool | Purpose | Risk |
|---|---|---|
| `read_file(path, range?)` | Read source | low |
| `list_dir(path)` | Explore structure | low |
| `grep(pattern, glob?)` | Search code | low |
| `write_file(path, content)` | Create/overwrite | **high** |
| `edit_file(path, old, new)` | Precise patch | **high** |
| `run_bash(cmd)` | Tests, build, git | **high** |
| `apply_patch(diff)` | Multi-file edit | **high** |

High-risk tools go through the **permission model**: allowlist/denylist + an interactive "allow once / allow always / deny" prompt, enforced at *runtime in the harness*, never by trusting the model to behave. ([source](08-research-and-sources.md)) This is the right pattern and is cheap to implement well.

### 5. Sub-agents & the multi-agent team

For exploration or role separation, the orchestrator can spawn **sub-agents** with isolated context that return a short, distilled summary (often 1–2k tokens) instead of their whole transcript. This is the key token-efficiency trick: a sub-agent may burn 30k tokens exploring and return 1k. ([source](08-research-and-sources.md))

Loom's optional **role team** (Phase 4):

```
        ┌──────────┐     plan      ┌─────────────┐    diff     ┌──────────┐
 goal ─▶│ PLANNER  │ ────────────▶ │ IMPLEMENTER │ ──────────▶ │  CRITIC  │
        │ (cheap)  │               │ (mid model) │             │(mid model)│
        └──────────┘               └─────────────┘             └────┬─────┘
              ▲                                                      │
              │                  reject + feedback                   │
              └──────────────────────────────────────────────────────┘
                                   (loop until critic approves
                                    AND tests pass, max N rounds)
```

- **Planner** (cheap model): turns the goal into a TODO list / step plan.
- **Implementer** (mid model): does one step, proposes a diff.
- **Critic** (mid model, *different* role/prompt): reviews the diff against the goal, runs the tests, approves or sends it back with feedback.

The critic is a form of **LLM-as-judge**, an explicit, demoable reliability mechanism — and a skill employers name directly.

### 6. Recorder (observability + replay)

Every loop iteration emits a structured event (model used, prompt tokens, completion tokens, cost, tool calls, latency). Events are written to a trace store and to OpenTelemetry. Because the trace contains the full input to every model call, **any run can be deterministically replayed**, and you can **fork** from any checkpoint. See [`06-evals-and-observability.md`](06-evals-and-observability.md).

## Data & control flow for one user request

```
user: "add input validation to the signup endpoint"
  │
  ├─ Context Engine: retrieve signup handler + related schema via AST RAG
  ├─ Conductor: classify difficulty → "medium" → route to GLM-5.1
  ├─ Planner: [1) find handler 2) add validation 3) add test 4) run tests]
  ├─ loop:
  │    Implementer: edit_file(...) → proposes diff
  │    Tool Engine: permission prompt → user allows → applies to scratch copy
  │    run_bash("pytest tests/test_signup.py") → 1 failed
  │    Critic: "validation rejects valid emails, regex too strict" → reject
  │    Implementer: edit_file(...) → fixes regex
  │    run_bash("pytest ...") → passed
  │    Critic: approve
  ├─ apply diff to real working tree (checkpoint created)
  └─ Recorder: full trace saved; cost = $0.004; saved vs frontier = $0.06
```

## State & persistence

| What | Where | Why |
|---|---|---|
| Conversation history | in-memory, snapshotted per checkpoint | the agent's working memory |
| Checkpoints (workspace + history) | content-addressed store on disk | instant rollback / branching |
| Traces | SQLite + JSONL | replay, evals, router training data |
| Repo memory (`LOOM.md`) | file in repo root | persistent project knowledge, re-injected each turn |
| AST/RAG index | local vector store (e.g. LanceDB) + SQLite | fast semantic code retrieval |
| Router training data | derived from traces | the data flywheel |

## Why this architecture is "advanced" but achievable

Each box is independently buildable and testable. You are never blocked on a hard component to have a working tool: Phase 1 is just Orchestrator + Tool Engine + a single model. The "advanced" parts (Conductor, Critic, Recorder/replay, RAG) are **additive layers**, each one a self-contained, resume-worthy mini-project. That decomposition is itself the senior-engineering signal.
