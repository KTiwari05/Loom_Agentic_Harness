---
name: commit
description: Stage the current changes and write a good commit message
---
Create a git commit for the current work.

Steps:
1. Run `git status` and `git diff` to see what changed.
2. Review the changes and group them into ONE coherent commit (if the changes are clearly
   unrelated, say so and commit only the dominant group).
3. Stage the relevant files explicitly (`git add <paths>` — avoid `git add -A` unless
   everything belongs together).
4. Write a conventional, imperative commit message: a short subject line (<= 72 chars)
   that says WHY, plus a body if the change needs context.
5. Commit and show the result with `git log -1 --stat`.

$ARGUMENTS
