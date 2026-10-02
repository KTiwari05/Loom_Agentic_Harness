# 07 — Career: Resume, Interview & What to Study

This doc connects the build to the goal: **landing an AI Engineer role at a startup** with no prior senior AI experience.

## The reframe you need

You said you don't have senior AI experience. Here's the truth for 2026: **startups hire for demonstrated ability over credentials.** Hiring managers say they prioritize 3–5 complete, end-to-end projects over degrees, and they're tired of "completed the course" portfolios. ([sources](08-research-and-sources.md)) Loom is engineered to *be* the demonstrated ability. One deep project that touches routing, evals, RAG, agents, observability, and cost beats ten shallow ones.

You are not "a junior with no experience." After this project you are **"the person who built a multi-model coding agent with its own eval harness and cost router."** That's a sentence that gets interviews.

## Resume bullets (fill in your real numbers)

Put these under a "Projects" section, above or instead of weak work experience:

> **Loom — open-source multi-model coding agent** · *Python, LiteLLM, tree-sitter, LanceDB, OpenTelemetry*
> - Built a terminal coding agent (agent loop + typed tool system + permission model) that autonomously edits multi-file repos; **~X%** task success on a **N-task** eval suite.
> - Designed a **cost-aware model router** across 3 open LLMs (Kimi K2, GLM-5.1, DeepSeek V4) that cut per-task cost **~Xx** vs. single-frontier-model baseline at near-equal success rate.
> - Implemented an **LLM-as-judge critic + test-in-sandbox loop** that verifies diffs before applying, catching **X%** of faulty changes pre-commit.
> - Added **full OpenTelemetry tracing with deterministic replay/fork** ("time-travel debugging") and an **automated eval harness** reporting success, cost, and steps per task.
> - Built **AST-aware RAG** (tree-sitter symbol graph + embeddings) reducing per-step context tokens **~X%** on large repos.

Every bracketed number comes from your [eval report](06-evals-and-observability.md). **Numbers are what make bullets believable.**

## The portfolio package (what to actually publish)

1. **GitHub repo** — clean README, architecture diagram, the eval table + plots, a "design decisions" section (why you hand-rolled the loop, why routing, why you measured X).
2. **A 2-minute demo video** — the critic catching a bad diff + the cost meter ticking. Recruiters watch; they rarely clone.
3. **One blog post** — "Building a reliable coding agent on cheap open models." Pick the *hardest problem you solved* (e.g., deterministic replay, or tuning the router with evals) and go deep. This ranks, gets shared, and doubles as interview prep.
4. **A pinned GitHub profile + a one-line bio**: "I build agentic AI systems. Latest: Loom, a multi-model coding agent."

## The interview talking points (rehearse these)

When they say "tell me about a project," you have a *layered* story — go as deep as they want:

- **30-second version**: the [pitch in the README](../README.md).
- **System-design version**: walk the [architecture diagram](01-architecture.md) — agent loop, router, critic, recorder.
- **"Tell me about a hard bug"**: deterministic replay (state/nondeterminism), or a malformed-edit failure mode you handled, or context-window compaction.
- **"How do you know it works?"**: the eval harness, the metrics, the honest tradeoff you found and engineered around. *This question is where most candidates die and you shine.*
- **"How would you scale / productionize it?"**: sandboxing (microVMs/containers, egress allowlists), budget governors, the policy engine, multi-tenancy. You've read about these in [research](08-research-and-sources.md); speak to them even if unbuilt.

## Skills this project forces you to learn (and where)

Map each gap to the phase that closes it:

| Skill (employer-named) | You'll learn it in | Quick study resource |
|---|---|---|
| LLM API / tool use | Phase 1 | Provider tool-calling docs; build, don't read |
| Prompt & context engineering | Phase 1, 5 | Anthropic "Effective context engineering" ([research](08-research-and-sources.md)) |
| Agents & multi-agent | Phase 2, 4 | Deep Agents / Open SWE write-ups ([research](08-research-and-sources.md)) |
| RAG | Phase 5 | Any solid RAG tutorial + tree-sitter docs |
| Evals | Phase 4 | Write your own harness — that *is* the learning |
| Observability | Phase 4 | OpenTelemetry Python quickstart |
| Cost optimization | Phase 3 | This repo's [models doc](05-models-and-cost-routing.md) |
| A bit of classic ML | Phase 7 (router) | scikit-learn logistic regression / gradient boosting |
| MLOps/deployment basics | Phase 6–7 | Docker fundamentals |

You don't need to study these *first*. **Learn them by hitting the phase that needs them.** Project-driven learning sticks; tutorial-driven learning evaporates.

## Where to apply (and how to use Loom there)

- **Target startups** building dev tools, agents, or LLM apps — they value exactly these skills and care less about pedigree.
- In applications, **lead with Loom**, not your background. Link the repo + video in the first two lines.
- For each role, write one sentence connecting Loom to *their* product ("I built a cost-routing agent; your inference-cost problem is the same problem").
- Contribute a small PR to an OSS agent project (Aider, Cline, a Deep Agents-style repo) — it's a warm intro and more resume surface.

## Managing the "no senior experience" worry

Three things to internalize:
1. **Depth beats breadth and beats credentials** for this specific role in this specific market. Loom is depth.
2. **You'll have genuinely done the hard things** — evals, routing, observability — that most *employed* engineers haven't. That's real, not a trick.
3. **Ship something runnable.** A working demo collapses the experience question. "Here, run it" ends the debate.

Build Phase 1–4. Record the demos as you go. Write the post. Then start applying *while* you build Phase 5–7 — the project keeps getting stronger and gives you fresh things to talk about every week.
