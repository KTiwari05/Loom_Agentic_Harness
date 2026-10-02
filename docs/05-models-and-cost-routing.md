# 05 — Models & Cost Routing (The Conductor)

This is the doc behind Loom's signature feature ([F1](02-novel-features.md)). Two parts: (1) which open models to use and what they cost, and (2) the routing algorithm.

## The open-model lineup (2026)

Open models crossed the "good enough for agentic coding" line in 2026. The leaders, per current benchmarks ([sources](08-research-and-sources.md)):

| Model | Strength | Notes |
|---|---|---|
| **Kimi K2.6 / K2.7 Code** (Moonshot) | Leads open models on coding & agentic-coding benchmarks; built for sub-agent parallelism and long autonomous runs. K2.7 Code uses ~30% fewer thinking tokens. | Your **default workhorse**. Great agentic + tool-use profile. |
| **GLM-5.1** (Zhipu) | Strong agentic engineering; **MIT license** (matters if you ever self-host/commercialize). | Excellent **mid-tier** router target. |
| **DeepSeek V4 / V4-Pro** | Best performance-to-cost for self-hosting; leads LiveCodeBench & long-context. | Your **strong-tier** escalation target. |
| **Qwen 3.6 Plus** | 1M-token context, frontier-competitive. | Use when a step needs **huge context**. |

> Prices move monthly and vary by provider. **Treat the numbers below as illustrative as of mid-2026** and re-check before you quote them. Pull live prices from OpenRouter's model pages or each provider's pricing page. ([sources](08-research-and-sources.md))

### Illustrative pricing (USD per 1M tokens, mid-2026)

| Model | Input | Output | Use as |
|---|---|---|---|
| Kimi K2.5 | ~$0.60 | ~$2.50 | cheap workhorse |
| Kimi K2.6 | ~$0.95 (cache-miss) / ~$0.16 (cache-hit) | ~$4.00 | default |
| Kimi K2.7 Code | ~$0.95 | ~$4.00 | agentic default |
| GLM-5.1 | (check OpenRouter) | (check) | mid |
| DeepSeek V4 | (check OpenRouter) | (check) | strong/cheap self-host |

**Contrast for your pitch:** frontier closed models run materially higher per token. The cost story isn't "open is free" — it's "**route cheap work to cheap models and you cut the bill several-fold without losing much success rate**." Your eval report ([Phase 4](03-roadmap.md)) turns that claim into a number.

**Two practical tips:**
- **Cache hits are huge.** Kimi K2.6 cache-hit input is ~6x cheaper than cache-miss. Structure your prompts so the stable prefix (system prompt + repo memory) is cacheable. This alone is a real cost win and a great thing to mention.
- **Start through OpenRouter** (one key, all models) for development; switch hot paths to **Moonshot-direct** for the best Kimi pricing once you optimize.

## The routing algorithm

### Tiers

Define 3 tiers (keep it simple):

```
TIER_CHEAP    = "moonshotai/kimi-k2.5"      # trivial / mechanical steps
TIER_MID      = "z-ai/glm-5.1"              # normal implementation steps
TIER_STRONG   = "deepseek/deepseek-v4"      # planning, hard bugs, after a failure
TIER_LONGCTX  = "qwen/qwen-3.6-plus"        # when context is very large
```

### v1 — Heuristic router (build this first, Phase 3)

A function `pick_model(step) -> model` using cheap signals. No ML needed:

```python
def pick_model(step) -> str:
    # 1) Escalate after failure: if the last attempt failed tests/critic, go stronger.
    if step.previous_attempt_failed:
        return TIER_STRONG

    # 2) Big context → long-context model.
    if step.estimated_tokens > 200_000:
        return TIER_LONGCTX

    # 3) Role-based: planning & review are high-leverage → don't cheap out.
    if step.role in ("planner", "critic"):
        return TIER_MID  # or STRONG for the critic on risky changes

    # 4) Task-type heuristic for implementer steps:
    if step.kind in ("rename", "format", "trivial_edit", "read_only"):
        return TIER_CHEAP
    if step.kind in ("multi_file_refactor", "new_feature", "debug"):
        return TIER_STRONG
    return TIER_MID  # default
```

How do you know `step.kind`? Cheap options, in order: (a) a quick classifier prompt to the cheap model ("classify this step: trivial/normal/hard"), (b) keyword/heuristic on the plan text, (c) signals like number of files touched, diff size, presence of "refactor/architecture/bug" in the goal. Start with (b)+(c); it's free and surprisingly good.

### v2 — Learned router (Phase 7 upside, the flywheel)

Once you've logged hundreds of `(step features) → (model used) → (succeeded?)` rows in your traces, you have a labeled dataset. Train a tiny classifier (logistic regression / small gradient-boosted tree — **not** a neural net; keep it explainable) to predict **the cheapest tier likely to succeed** for a step. Features: goal/step embeddings, files-touched count, diff size, recent failure flag, repo size.

This is the part that makes interviewers lean in:

> "The agent generates its own training data. Every run labels examples of which model was sufficient for which kind of step. I retrain the router on that, so it gets cheaper over time without getting worse. That's a closed-loop data flywheel."

### The cost meter (make the value visible)

Maintain two running totals per task:
- `actual_cost` — what you actually spent with routing.
- `baseline_cost` — what it *would* have cost if every step used `TIER_STRONG`.

Show `saved = baseline_cost - actual_cost` and `saved_pct` live in the TUI. **This number is the product.** Log both to the trace so your eval report can aggregate "routing cut cost by X% at Y% of the success rate."

## Quality guardrails for cheap routing

Routing cheap is only smart if quality holds. Two safety nets, both free because you already built them:
1. **The Critic ([F2](02-novel-features.md))** catches when a cheap model produced a bad diff → triggers escalation to a stronger tier on retry.
2. **The evals ([Phase 4](03-roadmap.md))** tell you the *aggregate* success-rate cost of routing, so you can tune the tiers with data, not vibes.

That loop — cheap-by-default, verified, measured, escalate-on-failure — *is* the engineering story. It's far more impressive than "I called an LLM."
