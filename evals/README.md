# Evaluation harness

The harness runs small coding tasks in temporary workspace copies and checks each result with a command. Implementation: `src/loom/evalrun.py`; CLI: `evals/harness.py`.

## Run from the repository root

After following [the setup guide](../QUICKSTART.md) and configuring a provider:

```powershell
.\.venv\Scripts\python.exe evals\harness.py
.\.venv\Scripts\python.exe evals\harness.py -v
.\.venv\Scripts\python.exe evals\harness.py --no-critic --model openrouter/moonshotai/kimi-k2
```

With an activated environment, `python evals/harness.py` also works. `--tasks` selects another task directory, and `--report` selects the Markdown output file (default: `evals/report.md`).

**These commands make live provider calls.** The agent auto-approves tools inside temporary task workspaces. Approved shell commands are not OS-sandboxed. The offline test suite does not need credentials and does not run these live evaluations.

## Task format

```text
evals/tasks/001-add-slugify/
  task.json       # goal and check command
  repo/           # starting workspace
  check.py        # optional checker copied beside workspace files
```

Example specification:

```json
{
  "goal": "Add a slugify(text) function to utils.py.",
  "check": "python check.py"
}
```

Each task's `repo/` and supplementary files are copied to a fresh temporary directory. The agent receives the goal. After the turn, the check command's exit code determines success: zero means pass. The harness reports task name, pass/fail, core steps, elapsed time, estimated model cost, and comparison with the configured token-price baseline.

| Task | Objective |
| --- | --- |
| `001-add-slugify` | Add a function that satisfies the checker |
| `002-fix-bug` | Correct the planted calculation bug and pass tests |
| `003-write-readme` | Explain the sample program in a README |

## Interpreting results

No live benchmark results were produced in [the October 2, 2026 review](../docs/VERIFICATION.md). Passing offline harness tests verifies the evaluation plumbing, not model quality.

These three tasks are a starter suite, not a representative benchmark. Success only covers the checker's assertions. Cost savings are a token-price comparison, not proof of equal quality across models. Temporary workspaces are retained for inspection; the harness does not remove them automatically.

To expand the suite, add a task directory with starting files, a precise goal, and a meaningful checker. When reporting live results, include the model IDs, configuration, task set, date, and repeated-run results so reviewers can reproduce the comparison.
