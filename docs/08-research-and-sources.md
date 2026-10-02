# 08 — Research & Sources

This plan is grounded in live research into the 2026 agentic-coding landscape (conducted June 2026). Below is the distilled intelligence and the sources, so you can verify, go deeper, and keep the plan current. **Prices and benchmark numbers move monthly — re-check before quoting.**

---

## Finding 1 — Open models are good enough, and cheap

- Kimi K2.6/K2.7 (Moonshot) currently lead *open* models on coding and agentic-coding benchmarks; K2.6 is built for sub-agent parallelism and long autonomous runs; K2.7 Code uses ~30% fewer thinking tokens.
- GLM-5.1 (Zhipu) — strong agentic engineering, **MIT-licensed** (matters for self-hosting/commercial use).
- DeepSeek V4 / V4-Pro — best performance-to-cost for self-hosting; leads LiveCodeBench and long-context.
- Qwen 3.6 Plus — 1M-token context, frontier-competitive.
- Illustrative pricing seen mid-2026: Kimi K2.5 ~$0.60 in / ~$2.50 out; K2.6 ~$0.95 cache-miss (~$0.16 cache-hit) in / ~$4.00 out; K2.7 Code ~$0.95 / ~$4.00 (per 1M tokens).

Sources:
- [Best Open-Source LLMs for Agentic Coding in 2026 — MindStudio](https://www.mindstudio.ai/blog/best-open-source-llms-agentic-coding-2026)
- [Best Chinese LLMs in 2026 (DeepSeek V4, Kimi K2.6, GLM-5, Qwen) — BenchLM](https://benchlm.ai/blog/posts/best-chinese-llm)
- [Best Open-Source & Open-Weight Coding Models (2026) — Kilo](https://kilo.ai/open-source-models)
- [Kimi K2.7 Code Complete Guide 2026 — Codersera](https://codersera.com/blog/kimi-k2-7-complete-guide-2026/)
- [Kimi API Pricing 2026 — TokenMix](https://tokenmix.ai/blog/kimi-k2-api-pricing)
- [Kimi K2.6 — OpenRouter](https://openrouter.ai/moonshotai/kimi-k2.6) · [Kimi K2.5 — OpenRouter](https://openrouter.ai/moonshotai/kimi-k2.5)
- [Kimi K2.6 Pricing & Deployment — DeepInfra](https://deepinfra.com/blog/kimi-k2-6-pricing-guide-deployment-tradeoffs)

## Finding 2 — How agentic coding tools actually work

- The core is one **agent loop**: ask the model → execute tool calls → append results to history → repeat until the model returns a final answer with no tool calls. There is no separate "tool-result handling" phase.
- Claude Code = a single-threaded master loop, a steering queue, a tool toolkit, TODO-list planning, controlled sub-agent spawning, and safety/permission measures. Built on Bun + CommanderJS + React Ink.
- Sub-agents run with **depth limits** (can't recursively spawn) and **isolated context**, returning distilled summaries.
- Permission model: writes, risky bash, and external/MCP/web use require explicit allow/deny, enforced by the harness at runtime — not by trusting the model.

Sources:
- [Claude Code: Behind-the-scenes of the master agent loop — PromptLayer](https://blog.promptlayer.com/claude-code-behind-the-scenes-of-the-master-agent-loop/)
- [Claude Code Internals, Part 2: The Agent Loop — Kotrotsos](https://kotrotsos.medium.com/claude-code-internals-part-2-the-agent-loop-5b3977640894)
- [Inside Claude Code: A Deep Dive — John Ding](https://medium.com/@dingzhanjun/inside-claude-code-a-deep-dive-into-anthropics-agentic-cli-assistant-a4bedf3e6f08)
- [Claude Code Architecture Explained (Rust Rewrite Analysis) — DEV](https://dev.to/brooks_wilson_36fbefbbae4/claude-code-architecture-explained-agent-loop-tool-system-and-permission-model-rust-rewrite-41b2)
- [claude-code-from-source — ch01 architecture (GitHub)](https://github.com/alejandrobalderas/claude-code-from-source/blob/main/book/ch01-architecture.md)

## Finding 3 — The 2026 frontier features (what "advanced" means now)

- **Long-running autonomous workflows** with **checkpointing + self-healing/recovery** (resume after transient failures); enables time-travel debugging and human-in-the-loop pause/resume.
- **Advanced memory**: vector recall, temporal knowledge graphs, checkpoint persistence, self-editing memory blocks (Mem0, Zep/Graphiti, LangMem, Letta/MemGPT). **Background memory extraction** runs after conversations.
- **Multi-agent teams**: Planner → Architect → Implementer → Tester → Reviewer, each role-specialized.
- **Persistent personal agents** with identity/memory/tools across sessions.

Sources:
- [The State of AI Coding Agents (2026) — Dave Patten](https://medium.com/@dave-patten/the-state-of-ai-coding-agents-2026-from-pair-programming-to-autonomous-ai-teams-b11f2b39232a)
- [State of AI Agent Memory 2026 — Mem0](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [Best AI Agent Memory Frameworks 2026 — Atlan](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- [Agentic Coding in 2026 — Webfuse](https://www.webfuse.com/blog/agentic-coding-in-2026)
- [awesome-harness-engineering (GitHub)](https://github.com/ai-boost/awesome-harness-engineering)

## Finding 4 — Context engineering (the discipline that separates toys from tools)

- Four techniques: **Offload** (summarize tool outputs, keep full data by reference), **Reduce** (compact conversation history), **Retrieve** (RAG at runtime), **Isolate** (sub-agents with scoped context).
- A sub-agent may burn tens of thousands of tokens exploring and return a 1–2k-token distilled summary — the key efficiency lever.
- More tokens ≠ better; a context compactor watches utilization and applies reduction under pressure.

Sources:
- [Effective context engineering for AI agents — Anthropic](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Context Engineering 101 (Claude Code) — Victor Dibia](https://newsletter.victordibia.com/p/context-engineering-101-how-agents)
- [Context Engineering: Why More Tokens Makes Agents Worse — Morph](https://www.morphllm.com/context-engineering)
- [Context compaction in agent frameworks — DEV](https://dev.to/crabtalk/context-compaction-in-agent-frameworks-4ckk)
- [Building AI Coding Agents for the Terminal (arXiv)](https://arxiv.org/html/2603.05344v1)

## Finding 5 — Security & sandboxing for autonomous agents

- **Execution-time governance**: allowlists/denylists, tool permissions, environment constraints, policy engines with runtime checks *before* each action — enforced by the harness, not the model.
- **Sandboxing tiers**: microVMs (Firecracker/Kata, strongest), gVisor (user-space kernel), hardened containers.
- **Resource limits at three levels**: per tool call (~30s), per task loop (~20min), per sandbox lifetime.
- **Zero-trust networking**: egress blocked by default, only required endpoints allowlisted.
- Open Agent Passport (OAP, March 2026): intercepts tool calls and evaluates declarative policy (~53ms median).

Sources:
- [How to sandbox AI agents in 2026 — Northflank](https://northflank.com/blog/how-to-sandbox-ai-agents)
- [AI Agent Sandbox — Firecrawl](https://www.firecrawl.dev/blog/ai-agent-sandbox)
- [Practical Security Guidance for Sandboxing Agentic Workflows — NVIDIA](https://developer.nvidia.com/blog/practical-security-guidance-for-sandboxing-agentic-workflows-and-managing-execution-risk/)
- [AI Agent Best Practices: Production-Ready Harness Engineering (2026) — Medium](https://medium.com/@tort_mario/ai-agent-best-practices-production-ready-harness-engineering-2026-guide-c1236d713fac)

## Finding 6 — Open-source agents to learn from (don't reinvent everything)

- **Aider** — strongest all-around OSS coding agent; multi-file edits, git integration, any LLM.
- **Cline** — polished VS Code-integrated agent.
- **Deep Agents (LangChain)** — planning + filesystem + sub-agents + context compression out of the box; child agents via a task tool, each with isolated middleware/todo/files.
- **Open SWE (LangChain)** — framework for internal coding agents.

Read their docs for ideas; Loom deliberately hand-rolls its core for learning + control (see [tech stack](04-tech-stack.md)).

Sources:
- [Best Open-Source AI Coding Agents in 2026 — Agentic.ai](https://agentic.ai/best/open-source-coding-agents)
- [9 Open-Source Agent Orchestrators (2026) — Augment Code](https://www.augmentcode.com/tools/open-source-agent-orchestrators)
- [Open SWE — LangChain](https://www.langchain.com/blog/open-swe-an-open-source-framework-for-internal-coding-agents)
- [Everything About Codex (2026) — Bhavishya Pandit](https://bhavishyapandit9.substack.com/p/everything-about-codex-the-complete)

## Finding 7 — What the AI-engineer job market wants (2026)

- **RAG is the most in-demand AI-engineering skill**; LLM + RAG + agents command salary premiums.
- Hiring managers prioritize **3–5 complete end-to-end projects** over degrees; they're saturated with "completed the course" portfolios and hire for **production instincts**: error handling, evaluation, deployment, structured thinking.
- Core stack: Python 3.10+, LLM APIs, RAG, agents, vector DBs, plus Docker/MLOps basics.
- Reported market: thousands of open roles; high average comp. (Verify current figures before citing.)

Sources:
- [5 AI Portfolio Projects That Actually Get You Hired in 2026 — DEV](https://dev.to/klement_gunndu/5-ai-portfolio-projects-that-actually-get-you-hired-in-2026-5bpl)
- [AI Engineer Roadmap: 5 Production Projects — The AI Corner](https://www.the-ai-corner.com/p/ai-engineer-roadmap-production-projects-2026)
- [AI Engineering Career Path 2026 — DataExpert](https://www.dataexpert.io/blog/ai-engineering-career-path-complete-guide-2026)
- [How to Become an AI Engineer in 2026 — Dataquest](https://www.dataquest.io/blog/ai-engineer-roadmap/)

---

## How to keep this plan fresh

This is a fast-moving space. Every few weeks: re-check model prices (OpenRouter), skim the awesome-harness-engineering list for new patterns, and re-run your evals against any newly-released open model — "I swapped in the new Kimi and re-ran my eval suite" is itself a great thing to be able to say in an interview.
