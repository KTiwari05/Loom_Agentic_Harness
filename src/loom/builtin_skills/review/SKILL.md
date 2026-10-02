---
name: review
description: Review the current uncommitted changes like a strict senior engineer
---
Do a code review of the current uncommitted changes.

Steps:
1. Run `git diff` (and `git diff --staged`) to collect everything that changed. If the
   repo has no changes, review the most recent commit instead (`git show`).
2. For each changed file, read enough surrounding code to judge the change in context.
3. Report findings ranked by severity:
   - **Bugs** — concrete inputs/state that produce wrong behavior.
   - **Risks** — missing error handling, edge cases, security issues.
   - **Style** — only where it hurts readability; skip nitpicks.
4. For every finding, give the file, the line, why it's a problem, and a suggested fix.
5. End with a verdict: APPROVE or REQUEST CHANGES, with a one-line justification.

Do NOT modify any files — this is a read-only review.

$ARGUMENTS
