---
name: test
description: Run the project's tests and fix any failures
---
Run this project's test suite and make it green.

Steps:
1. Detect the test runner: look for pytest config / package.json test script / a
   Makefile target / cargo, etc. Prefer what LOOM.md says if it exists.
2. Run the tests and read the failures carefully.
3. Fix the ROOT CAUSE in the code under test — do not weaken or delete assertions just
   to make tests pass. If a test itself is wrong, explain why before changing it.
4. Re-run until green (or explain precisely what still fails and why).
5. Summarize: what failed, what you changed, final pass count.

$ARGUMENTS
