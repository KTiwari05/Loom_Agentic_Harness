# 03 — Roadmap

The guiding principle: **you always have a working tool.** Every phase ends with something you can run and demo. You layer impressiveness on top of a working core — you never spend three weeks on infrastructure with nothing to show.

Time estimates assume part-time effort (you're learning as you go). They're deliberately generous; moving faster is fine. **Do not start Phase N+1 until Phase N runs.**

---

## Phase 0 — Foundations (1–2 days)

**Goal:** environment ready, you can call an open model and get a tool-call back.

- [ ] Install Python 3.11+, set up a project with `uv` (fast, modern) — see [tech stack](04-tech-stack.md).
- [ ] Get a Moonshot (Kimi) API key **and** an OpenRouter key (OpenRouter gives you every other open model behind one OpenAI-compatible API — huge time-saver).
- [ ] Write a 20-line script: call Kimi K2 with one tool defined (`get_weather`), confirm you get a tool call back and can return a result. **This is the "hello world" of agents.**
- [ ] Set up git, a `LOOM.md`, and the folder layout from [tech stack](04-tech-stack.md).

**Milestone:** a script that does one round of model → tool call → tool result → model.

---

## Phase 1 — The minimal agent (3–5 days) → *first demoable tool*

**Goal:** an agent that can actually edit a real codebase from the terminal, on a single model.

- [ ] Implement the **agent loop** (Orchestrator) from [architecture](01-architecture.md).
- [ ] Implement the **read-only tools**: `read_file`, `list_dir`, `grep`.
- [ ] Implement the **write tools**: `edit_file` (string-replace), `write_file`, `run_bash`.
- [ ] Add a **permission prompt** before any write/bash ("allow once / always / deny").
- [ ] Basic CLI: type a goal, watch streamed output, see proposed diffs.
- [ ] Hard-code the model to Kimi K2 (no routing yet).

**Milestone:** "Add a `--version` flag to this Python CLI" works end-to-end on a real repo. **Record a screen capture now** — this is your baseline demo.

> ⚠️ Resist scope creep here. No routing, no critic, no RAG. Just a working loop with tools and permissions. This is 80% of what makes the thing feel real.

---

## Phase 2 — Make it trustworthy: the Critic + checkpoints (4–6 days)

**Goal:** the agent verifies its own work and you can undo it. (Features [F2](02-novel-features.md), [F5](02-novel-features.md).)

- [ ] **Scratch-copy workflow**: apply diffs to a throwaway copy, run tests there before touching real files.
- [ ] **Critic loop**: a second model call with a reviewer prompt + the test output; approve or send back with feedback; bounded retries.
- [ ] **Checkpointing**: snapshot the workspace before applying; add `/rewind` to roll back the last change.
- [ ] Handle the obvious failure modes: model returns a malformed edit, tests hang (timeout), file doesn't exist.

**Milestone:** give it a task with a subtle bug; watch the critic reject the first attempt and the second pass the tests. **This is your best interview-demo moment — record it.**

---

## Phase 3 — The Conductor: cost-aware routing (4–6 days) → *the signature feature*

**Goal:** route across multiple open models by difficulty; show dollars saved. (Feature [F1](02-novel-features.md).)

- [ ] Wire up **≥3 models** via OpenRouter (e.g. Kimi K2 cheap tier, GLM-5.1 mid, DeepSeek V4 strong).
- [ ] **v1 router = heuristic**: classify each step by signals (plan vs. edit vs. trivial rename; prompt length; whether the last attempt failed → escalate). Route accordingly.
- [ ] **Cost meter** in the UI: $ spent this task + $ saved vs. always-using-the-strong-model.
- [ ] Log every routing decision + outcome to the trace (sets up the flywheel).

**Milestone:** run the same task with routing on vs. off; quote a real cost-reduction number. That number goes on your resume.

*(Optional now or later: v2 learned router — train a small classifier on your trace logs. This is a great standalone "I trained a model" story but isn't required to ship.)*

---

## Phase 4 — Glass box: tracing, replay & evals (5–7 days) → *the AI-engineer proof*

**Goal:** full observability + a real benchmark number. (Feature [F3](02-novel-features.md); see [evals doc](06-evals-and-observability.md).)

- [ ] **Recorder**: structured event per step (model, tokens, cost, latency, tool calls) → SQLite + JSONL + OpenTelemetry.
- [ ] **Replay**: re-run a recorded session; **fork** from a chosen checkpoint.
- [ ] **Eval harness**: a set of ~20–50 tasks with automated pass/fail (tests pass? file changed correctly?). Score: task success rate, avg cost/task, avg steps/task.
- [ ] Run the harness on a couple of public mini-benchmarks or your own task set; **write down the numbers.**

**Milestone:** a one-page eval report ("Loom solves X% of N tasks at $Y avg/task; routing cuts cost Z%"). **This single artifact does more for your hireability than any feature.**

---

## Phase 5 — RAG & memory (5–7 days)

**Goal:** the agent retrieves the right code and remembers the repo. (Features [F4](02-novel-features.md), [F7](02-novel-features.md).)

- [ ] **AST indexing** with tree-sitter → symbol units → embeddings → local vector store (LanceDB).
- [ ] Retrieve relevant units per step instead of dumping files; measure the token reduction.
- [ ] **Repo memory**: maintain `LOOM.md`; extract "lessons learned" after runs and re-inject next session.

**Milestone:** show that Loom handles a *large* repo it couldn't fit in context before, and that the second run on a repo is faster/cheaper than the first.

---

## Phase 6 — Polish & make it a product (4–6 days)

**Goal:** it looks and feels like something a startup shipped.

- [ ] **Real TUI** with Textual: input box, streaming, live diff viewer, cost meter, checkpoint timeline.
- [ ] `LOOM.md`-driven config; slash commands (`/rewind`, `/branch`, `/cost`, `/model`).
- [ ] Great README with GIFs, an architecture diagram, and the eval numbers.
- [ ] **A 2-minute demo video.** Recruiters watch videos; they rarely clone repos.
- [ ] A short blog post: "What I learned building a coding agent on cheap open models." (Recruiters and HMs read these.)

**Milestone:** a stranger can install Loom, run it, and understand why it's special in 2 minutes.

---

## Phase 7 — Upside (open-ended)

Pick from: best-of-N with verifier ([F6](02-novel-features.md)), learned router v2, calibrated autonomy ([F8](02-novel-features.md)), sandbox/policy engine, background/ambient mode, MCP support (so Loom can use external tools like Claude Code does). Each is a fresh blog post and a fresh interview talking point. **Don't gate "done" on any of these.**

---

## Timeline at a glance

| Phase | Theme | ~Time | You can now say… |
|---|---|---|---|
| 0 | Foundations | 1–2 d | "I can drive open-model tool-calling." |
| 1 | Minimal agent | 3–5 d | "I built a working coding agent." |
| 2 | Critic + checkpoints | 4–6 d | "It verifies its own work and is reversible." |
| 3 | Cost routing | 4–6 d | "It costs ~10x less and I can prove the savings." |
| 4 | Glass box + evals | 5–7 d | "I have benchmark numbers and full observability." |
| 5 | RAG + memory | 5–7 d | "It does proper retrieval and learns the repo." |
| 6 | Product polish | 4–6 d | "It's a real, shippable product with a demo." |
| 7 | Upside | ∞ | "...and here are three research-flavored extensions." |

**Hire-ready checkpoint: end of Phase 4.** Phases 5–6 make it a *product*; Phase 7 makes it *impressive*. Realistic part-time total to hire-ready: **~5–7 weeks.** Full project: **~10–12 weeks.**

## Working rules to protect the project

1. **Demo-driven**: every phase ends with a recorded demo. If you can't demo it, it's not done.
2. **Commit daily, write a one-line changelog.** This becomes your project narrative.
3. **One language, one package manager, one vector DB.** No yak-shaving on tooling.
4. **When stuck >2 hours, simplify the feature, don't abandon the phase.** A heuristic router still ships; a perfect learned router that never ships is worthless.
5. **Keep the [anti-goals list](00-vision-and-positioning.md) visible.** Say no to the web UI.
