# 06 — Evals & Observability (The Part That Gets You Hired)

If you do only one thing in this project *unusually well*, make it this. Hiring managers in 2026 explicitly screen for **evaluation and observability** because they separate people who *build demos* from people who *ship reliable systems*. ([sources](08-research-and-sources.md)) Most portfolio projects have zero evals. Yours will have a benchmark number and a trace you can replay. That alone puts you in a different pile.

---

## Part A — Observability ("the glass box")

### The trace is the source of truth

Every step of every run emits a structured event. Design the event once and route it everywhere (stdout, SQLite, JSONL, OpenTelemetry):

```python
@dataclass
class StepEvent:
    run_id: str
    step_index: int
    role: str                 # planner | implementer | critic
    model: str                # which model the Conductor picked
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float
    baseline_cost_usd: float   # cost if we'd used the strong model
    latency_ms: int
    tool_calls: list[str]
    outcome: str               # ok | tool_error | tests_failed | critic_rejected
    context_digest: str        # hash of the exact prompt → enables replay
```

Why each field matters:
- `model` + `cost_usd` + `baseline_cost_usd` → the **cost-savings story** (F1).
- `outcome` + `model` + step features → the **router's training data** (the flywheel).
- `context_digest` (and the stored full context) → **deterministic replay** (F3).
- `latency_ms`, `cached_tokens` → performance tuning + the "I optimized prompt caching" talking point.

### OpenTelemetry (do this; it's a resume keyword and it's easy)

Wrap each model call and tool call in an OTel span. Export to a local viewer (Jaeger via Docker, or just the console exporter to start). Now you can *see* a run as a waterfall: which steps were slow, which model ran where, where the tokens went. Screenshot this for your README — a trace waterfall of an agent run is a striking, credible image.

### Replay & fork (F3)

Because you stored the exact context per step (or its digest + the inputs to rebuild it), you can:
- **Replay deterministically**: feed recorded model outputs back through the loop to reproduce a run exactly (great for debugging your *harness*).
- **Replay live**: re-run from step K with the *real* models to see if a fix to your prompt changes the outcome.
- **Fork**: branch at step K, change one thing (model, prompt, temperature), and compare the two branches. This is "time-travel debugging" for agents — a genuinely advanced capability the closed tools don't expose.

---

## Part B — Evals ("prove it works")

### Why evals are the whole point

An agent is a stochastic system. "It worked when I tried it" is not evidence. An **eval harness** turns reliability into a measurable, improvable number — and gives you a graph to show. This is the single most senior-coded thing in the project.

### Build a task suite

Start with **20–50 tasks**. Each task = a small repo fixture + a goal + an automatic success check. Three sources:
1. **Hand-made fixtures**: tiny repos with a planted bug or a missing feature; success = "the included pytest passes after the agent runs."
2. **Your own real bugs**: every time you fix something by hand, add it as a task. Free, realistic, growing.
3. **Public benchmarks** (stretch): SWE-bench-style task sets exist for exactly this. Even running a *small slice* and quoting it is credible. (Adapt, don't boil the ocean.)

```
evals/tasks/001-add-validation/
  repo/                 # the starting code
  goal.txt              # "Add email validation to signup; existing tests must pass."
  check.sh              # exit 0 == success (usually: run the tests)
```

### Metrics to report

| Metric | What it tells employers |
|---|---|
| **Task success rate** (% of tasks where `check.sh` passes) | core capability |
| **Avg cost / task** | cost discipline (pair with routing on/off) |
| **Avg steps / task** & **avg wall-clock** | efficiency |
| **Critic catch rate** (bad diffs caught before apply) | reliability mechanism works |
| **Escalation rate** (how often cheap→strong) | router calibration |
| **Cost saved vs. always-strong** (%) | the headline number |

### The killer comparison plots

Run the suite under different configs and chart the results. Two plots win interviews:
1. **Cost vs. success**: routing-off (always strong) vs. routing-on. Show you kept most of the success rate for a fraction of the cost.
2. **Best-of-N curve** (if you build [F6](02-novel-features.md)): success rate vs. N candidates vs. cost. The classic inference-time-scaling tradeoff curve, *measured on your own tool.*

### The one-page eval report

Generate `evals/report.md` automatically from the trace DB after a run. Put the table + the two plots in your README. **This artifact is worth more than three more features.** It's the difference between "I built a thing" and "I built a thing and here's how well it works and what it costs."

---

## A note on honesty (it's a strength)

Report the numbers straight, including the unflattering ones. "Routing cut cost 6x but dropped success from 84% to 79%, so I escalate on the critic's first rejection to recover it" is *more* impressive than a suspiciously perfect result. Showing you measured a tradeoff and engineered around it is exactly the judgment startups are buying. Never quote a number you can't reproduce from the trace.
