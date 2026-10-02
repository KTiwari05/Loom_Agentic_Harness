# 00 — Vision & Positioning

## The one-sentence vision

> Loom is an open-source, terminal-based coding agent that achieves frontier-tool reliability at a fraction of the cost by **routing across cheap open models**, **verifying its own work**, and being **fully transparent and replayable**.

## The problem you're attacking

The 2026 coding-agent market is dominated by closed, single-vendor tools (Claude Code, OpenAI Codex, Cursor). They are excellent but share three structural weaknesses that an individual builder can credibly beat in a focused way:

1. **Cost is opaque and fixed to one expensive model.** You can't see, control, or reduce per-task spend. Every step — even "rename this variable" — runs on a frontier model.
2. **They are black boxes.** When an agent does something dumb, you can't replay the run, inspect the exact context it saw, or branch from a decision point to try again.
3. **Reliability is implicit.** The agent writes code and hopes; verification is bolted on by the user (you run the tests). There's no built-in, first-class "prove it works before applying it" loop.

Meanwhile, **open models got good enough in 2026.** Kimi K2.6/K2.7, GLM-5.1, DeepSeek V4, and Qwen 3.6 are competitive on agentic-coding benchmarks at 5–20x lower cost. ([sources](08-research-and-sources.md)) The infrastructure question — "how do you build a *reliable* agent on *cheap, swappable* models?" — is exactly the question startups are hiring AI engineers to answer.

## Who it's for (and why that matters for hiring)

Primary user: **a developer who wants Claude-Code-style help without Claude-Code-style bills**, and who wants to *see and trust* what the agent does. Think indie hackers, students, OSS maintainers, and cost-conscious startups.

Why this framing helps you get hired: it makes Loom a *product with a thesis*, not a toy clone. In interviews you talk about **a user, a cost model, and a reliability story** — the exact language of a startup engineer — instead of "I cloned Claude Code."

## How Loom is different (positioning table)

| Dimension | Claude Code | OpenAI Codex | Aider (OSS) | **Loom** |
|---|---|---|---|---|
| Model | Single vendor (Claude) | Single vendor (GPT) | Any (manual) | **Auto-routed across open models** |
| Cost control | None (fixed) | None (fixed) | Manual model pick | **Per-task budget + learned router** |
| Transparency | Black box | Black box | Partial | **Full trace + replay + fork** |
| Self-verification | Implicit | Implicit | Test-driven (opt-in) | **First-class critic + eval loop** |
| Memory of codebase | Session + files | Session | Repo map | **AST RAG + temporal repo memory** |
| Multi-agent | Sub-agents | Limited | No | **Planner/Implementer/Critic team** |
| License | Closed | Closed | Open | **Open** |

You are **not** trying to beat Claude Code on raw capability — you'd lose, and that's fine. You're beating it on **cost transparency, observability, and verifiability**, which are (a) genuinely valuable and (b) exactly the engineering competencies that get an AI engineer hired.

## The narrative arc (your "why I built this" story)

Every strong portfolio project has a story. Yours:

> "Open models got good enough in 2026, but everyone was still paying frontier prices and treating agents as magic black boxes. I wanted to know: *can a single engineer build a reliable coding agent on cheap, swappable models — and prove that it's reliable?* That's Loom. The interesting engineering wasn't the LLM calls; it was the router, the eval harness, and the replay system that make a cheap, stochastic model behave like a dependable tool."

That last sentence is the whole game. **The LLM is the easy part. The engineering around it is the job.** Loom is designed so that the engineering around the LLM is the visible, impressive part.

## Anti-goals (what you are deliberately *not* building)

Scope discipline is itself a senior signal. Loom is explicitly **not**:

- A web IDE or GUI (terminal-first; a TUI is the ceiling).
- A model training / fine-tuning project (you *use* models; you may train a tiny router classifier, nothing more).
- A general chatbot (it's a coding agent with a narrow, deep job).
- A multi-language rewrite (one language, done well — see [tech stack](04-tech-stack.md)).
- A hosted SaaS (local-first; cloud is a stretch goal, not the point).

Write these down. When you're tempted to add a web UI in month two, this list saves the project.

## Definition of done (what "finished enough to get hired" looks like)

You do **not** need to finish everything in this repo. You're hire-ready when you can demonstrate:

- ✅ A working agent that edits real multi-file codebases from the terminal.
- ✅ Cost routing across ≥2 open models with a measured $-savings number.
- ✅ A trace you can replay and a benchmark you can quote (even a small one).
- ✅ The critic loop catching at least one class of bad diff in a live demo.
- ✅ A clean README, a 2-minute demo video, and a short write-up of one hard problem you solved.

That's Phases 1–4 of the [roadmap](03-roadmap.md). Phases 5–7 are upside.
