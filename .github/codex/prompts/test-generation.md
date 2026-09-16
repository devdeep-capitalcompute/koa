# Verity Codex Test Generation

Generate/update tests to reflect intended behavior and prevent regressions.

Rules:
- Follow `AGENTS.md`.
- Prefer adding tests over changing production code.
- Keep scope minimal.
- Update existing tests when intended behavior changed.
- Do not weaken assertions just to make CI pass.

Steps:
1) Read `.verity/config.yml` to learn test/build commands.
2) Add/update tests, including existing tests that no longer match intended behavior.
3) If `policies.documentation.auto_mode` is enabled, run `python scripts/sync_repo_docs.py`.
4) Run tests/build until green.
5) Final message: Summary, tests added, commands run.


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
