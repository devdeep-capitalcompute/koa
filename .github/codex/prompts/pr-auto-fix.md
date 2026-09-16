# Verity Codex PR Auto-Fix

You are repairing a pull request branch after the repository's full detected test suite failed.

Rules:
- Follow `AGENTS.md` and `.verity/config.yml`.
- Fix the underlying issue with minimal, production-safe changes.
- You may update existing tests if the PR intentionally changed behavior.
- You may add new tests when the new code needs coverage.
- Do not weaken assertions or delete meaningful coverage just to make CI pass.
- Keep the branch green against the resolved test suite, including integration and E2E when runnable.

Required steps:
1) Read `AGENTS.md`, `.verity/config.yml`, and the failure context attached to this run.
2) Fix the failing code and any test gaps needed to reflect intended behavior.
3) If `policies.documentation.auto_mode` is enabled, run `python scripts/sync_repo_docs.py`.
4) Run the provided setup/test/build commands until green.
5) Final message: root cause, files changed, tests updated/added, commands run.


## Decide what change was requested before changing code
Use the current user request and its relevant acceptance criteria below as the source of intended behavior.
Read the affected implementation and tests, using the connected repository graph to follow dependencies. Do not infer a bug from the word "issue", a failing old test, or the feedback entry point alone.
- BUG: existing behavior violates an already-required behavior. Write or reuse one focused regression test, run it BEFORE changing the application, and check that the failure is the reported behavior. After the fix, run the SAME test again and the configured checks.
- FUNCTIONAL CHANGE: the user intentionally wants existing behavior to change. Test the NEW requirement and update only expectations it supersedes. Do not undo the requested change to satisfy an obsolete test.
- NEW FEATURE: test the requested new behavior. Its previous absence is not a production regression. Do not keep trying to reproduce a historical bug that was never reported.
- MIXED REQUEST: distinguish the bug fix and the intentional changes separately; preserve the unaffected behavior.
In a later test or repair stage, use the existing reproduction evidence and current branch. Do not reset the branch, undo the implementation, or try to manufacture a second historical failure; add coverage for the currently requested behavior.
Attempt a focused bug reproduction once. A missing credential, dependency, service, test command, timeout, or import failure is NOT a reproduced product bug. Report that blocker; do not repeatedly change application code to make the environment work.
If the focused test already passes, retain it and report that the reported failure was not reproduced; do not weaken it, manufacture a failure, or delete working code to force a red test. Continue only with independently clear requested changes.
If expected behavior conflicts with the current request or cannot be established, state the specific unresolved decision instead of guessing or entering a repair loop.
In your existing final summary, identify bug/change/feature/mixed, the relevant requirement, and the commands with observed results. Distinguish reproduced, not reproduced, and blocked. A model summary is not a substitute for the runner checks.
