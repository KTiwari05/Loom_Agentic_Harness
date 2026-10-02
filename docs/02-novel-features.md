# 02 — Novel Features (Beyond Claude Code / Codex)

You asked for features that go *beyond* what the closed tools ship. Below are eight, each tagged with what it proves to an employer and how hard it is. They're ranked so that the **most impressive-per-unit-effort** come first. Build the S-tier ones; treat the rest as upside.

A feature earns "novel" status here if Claude Code / Codex either **don't ship it**, **can't ship it** (vendor lock-in), or **hide it** (black box). Each one is also chosen to be a self-contained portfolio story.

---

## S-tier — these ARE the project. Build all three.

### F1 — The Conductor: cost-aware multi-model routing with a learning flywheel ⭐ signature feature

**What it is.** Before each step, a lightweight classifier estimates the step's difficulty and routes it to the *cheapest model that can handle it*: a tiny/cheap model for "rename a variable," a stronger model for "design the refactor." It tracks cumulative dollars spent **and dollars saved vs. always-frontier**, shown live in the TUI.

**Why it's beyond Claude/Codex.** They are welded to a single vendor model. They *structurally cannot* route to a cheaper model for cheap steps. This is a capability they can't copy without abandoning their business model.

**The flywheel (the part that screams "startup AI engineer").** Every run is traced (F3). Each trace is a labeled example: *(step context) → (which model was used) → (did it succeed: tests passed / critic approved / had to escalate)*. Periodically you train/refresh the difficulty classifier on **your own accumulated run logs**. The tool gets cheaper and smarter the more you use it. That's a data flywheel — the core mental model of a modern AI product.

**Proves:** cost optimization, evals, classification, the data-flywheel instinct.
**Effort:** Medium (heuristic router in a day; learned router is a weekend ML mini-project).
**Details:** [`05-models-and-cost-routing.md`](05-models-and-cost-routing.md).

### F2 — The Critic: a verifier loop that makes a stochastic model behave deterministically

**What it is.** No diff touches your real files until (a) a *separate* critic model reviews it against the goal and (b) the tests pass on a scratch copy. On failure, the critic's feedback goes back to the implementer for another round (bounded by N rounds / budget).

**Why it's beyond Claude/Codex.** They have implicit self-review at best; verification is offloaded to you. Loom makes "prove it works before applying" a *first-class, demoable* mechanism. In a demo you can intentionally hand it a tricky task and *show the critic catching a bad diff* — that's a memorable interview moment.

**Proves:** LLM-as-judge, agentic reliability, self-correction loops.
**Effort:** Low–Medium. It's mostly a second model with a reviewer prompt + a test runner + a loop.

### F3 — Glass-box trace, replay & fork ("time-travel for agents")

**What it is.** Every step is recorded with its exact inputs, so you can:
- **Replay** any past run deterministically (re-feed recorded model outputs, or re-run live).
- **Inspect** the *exact* context the model saw at any step (the #1 thing you can't do in a black box).
- **Fork** from any checkpoint: rewind to step 7 and let it try a different path, branching like git.

**Why it's beyond Claude/Codex.** They're black boxes. You cannot see the model's exact context, replay a run, or branch from a decision point. This is arguably Loom's most *technically* impressive piece and the one that makes debugging the agent possible at all.

**Proves:** observability, production debugging instincts, systems design.
**Effort:** Medium. Recording is easy; deterministic replay + a checkpoint store takes care. (Checkpointing/time-travel is an explicit 2026 frontier trend — [source](08-research-and-sources.md).)

---

## A-tier — strong additions once S-tier works.

### F4 — AST-aware codebase RAG (semantic code retrieval)

**What it is.** Instead of dumping files or naively chunking by line count, Loom parses code with **tree-sitter** into a symbol graph (functions, classes, imports, call edges), embeds those units, and retrieves *whole semantic units* relevant to a step. "Find where validation happens" returns the right functions, not 40 lines that straddle two functions.

**Why it matters for hiring.** RAG is repeatedly cited as the single most in-demand AI-engineering skill in 2026. ([source](08-research-and-sources.md)) Doing it *well* (AST-aware, not naive chunking) is the difference between "did a tutorial" and "understands retrieval."

**Proves:** RAG done properly, embeddings, vector DBs, tree-sitter.
**Effort:** Medium.

### F5 — Checkpoint & branch agent state ("git for the agent")

**What it is.** Every applied change snapshots *both* the workspace (content-addressed, like git objects) *and* the conversation state. Instant rollback of a whole multi-step session; branch to explore alternatives without losing the original. Closely related to F3 but user-facing: a `/checkpoint`, `/rewind`, `/branch` UX.

**Why it's beyond Claude/Codex.** They have limited undo; nobody ships first-class *branching of an agent session* as a feature.
**Proves:** content-addressed storage, systems design, thoughtful UX.
**Effort:** Medium.

### F6 — Speculative best-of-N with a verifier (inference-time scaling)

**What it is.** For a hard step, spin up N implementer attempts **in parallel sandboxes** with different temperatures/models, run the tests on each, and let the critic pick the winner. Cheap open models make N=3–5 affordable — you're trading a little money for reliability, *deliberately and measurably*.

**Why it's interesting.** "Inference-time scaling" / best-of-N with verifiers is a frontier research idea most tools don't expose. Pairs beautifully with F1's cost tracking: "I spent 4x the tokens to go from 70% → 92% task success — here's the curve."
**Proves:** inference-time scaling, sandboxing, parallel orchestration, eval-driven tradeoffs.
**Effort:** Medium–High (needs the sandbox + parallel execution).

### F7 — Self-improving repo memory (temporal knowledge graph + lessons learned)

**What it is.** Loom maintains a `LOOM.md` plus a small knowledge store of facts it learns about *this* repo ("tests live in `/tests`, run with `make test`, the auth module is fragile"). It's re-injected each session and updated *after* runs via background memory extraction, so the agent gets faster and avoids repeating mistakes.

**Why it matters.** Persistent agent memory is a defining 2026 theme. ([source](08-research-and-sources.md)) It also visibly improves the demo: the second time you run Loom on a repo, it's noticeably smarter.
**Proves:** memory architectures, knowledge graphs, background processing.
**Effort:** Medium. Start with a flat `LOOM.md`; graduate to a graph only if you want the depth.

---

## B-tier — stretch goals / strong-finish flourishes.

### F8 — Calibrated autonomy ("ask only when genuinely uncertain")

**What it is.** The agent self-reports a confidence score per step. It runs autonomously when confident and **interrupts you only when calibrated-uncertain** (cross-checked against whether it was actually right, using your trace history). This is the antidote to agents that either steamroll or nag.
**Proves:** calibration, human-in-the-loop design, uncertainty quantification.
**Effort:** Medium-High (the calibration measurement is the hard, interesting part).

### Also-rans worth a sentence in your README's "future work"
- **Policy-engine sandbox**: microVM/container isolation + egress allowlist (zero-trust network) for safe autonomous runs. ([source](08-research-and-sources.md))
- **Hard budget governor**: "do this task for under $0.10" — the agent plans within a dollar budget and refuses to exceed it.
- **Ambient/background mode**: long-running agent that watches the repo and proposes fixes, with checkpointing + recovery. (A defining 2026 trend, but operationally heavy — list it, don't build it first.)

---

## How to choose what to actually build

> Ship **F1 + F2 + F3** and you have a project that is genuinely differentiated from Claude Code *and* hits cost-optimization, evals, LLM-as-judge, and observability — four named hiring skills. Add **F4** to claim RAG. Everything else is for the version of you that's having fun and wants the project to keep paying off.

Don't build them in this order, though — build them in **[roadmap](03-roadmap.md) order**, which interleaves them so you always have a working tool.
