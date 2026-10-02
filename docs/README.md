# Documentation index

Start with the [repository README](../README.md) and [quickstart](../QUICKSTART.md).

## Current implementation

| Document | Purpose |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Implemented components, data flow, and engineering tradeoffs |
| [VERIFICATION.md](VERIFICATION.md) | Checks performed on October 2, 2026 and verification boundaries |
| [Evaluation guide](../evals/README.md) | Task format, scoring, and live evaluation commands |

## Original design and planning documents

The numbered documents below are retained as design history. They mix implemented features, proposed work, positioning, and earlier research. They are **not evidence that every described feature exists or that quoted pricing, model availability, benchmark results, or career statistics are current**. Use the root README and verification report for current claims.

| Document | Topic |
| --- | --- |
| [00 — Vision](00-vision-and-positioning.md) | Original product positioning |
| [01 — Architecture](01-architecture.md) | Proposed system design |
| [02 — Novel features](02-novel-features.md) | Feature candidates and priorities |
| [03 — Roadmap](03-roadmap.md) | Build phases and future work |
| [04 — Technology](04-tech-stack.md) | Proposed stack choices |
| [05 — Models and routing](05-models-and-cost-routing.md) | Earlier model research and routing plans |
| [06 — Evaluation and observability](06-evals-and-observability.md) | Measurement plans |
| [07 — Career notes](07-career-resume-interview.md) | Original presentation and interview planning |
| [08 — Research](08-research-and-sources.md) | Earlier research references |
| [v2 master plan](v2/00-master-plan.md) | Event-sourcing design |
| [v2 schemas](v2/01-data-schemas.md) | Session record schema specification |
| [v2 module specs](v2/02-module-specs.md) | Session subsystem specifications |

Learned difficulty routing, AST-aware retrieval, and OpenTelemetry integration remain roadmap ideas rather than claims about this snapshot. The implemented Conductor selects configured tiers using simple escalation rules.
