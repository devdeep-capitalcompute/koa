# Verity Codex Dev Cycle

Follow `AGENTS.md` and `.verity/config.yml`.

Do NOT commit/push/open PR yourself. Leave changes in the working tree; the workflow creates the PR.

## CRITICAL: You must EXECUTE file writes — do not just describe them

The sandbox environment may not preserve heredoc or shell-redirect writes reliably.
**Always write files using Python so content is guaranteed to be on disk:**

```python
from pathlib import Path
Path("path/to/file").parent.mkdir(parents=True, exist_ok=True)
Path("path/to/file").write_text("""
<full file content here>
""".lstrip(), encoding="utf-8")
```

After every file write, **verify** the file exists and has non-trivial content.

**CRITICAL: Always read a file before overwriting it.** If you are modifying an existing file, read its current content first, then write the updated version.

## Required steps

1) Read `AGENTS.md` and `.verity/config.yml` to understand the project layout, commands, and policies.

2) Implement the request with minimal, production-safe changes.

3) If `policies.documentation.auto_mode` is enabled, run `python scripts/sync_repo_docs.py`.

4) Run the resolved Verity test/build suite, including grouped test commands when present. Fix code failures until green.

5) Update existing tests when the intended behavior changed, and add new tests when new logic needs coverage. Never weaken assertions just to make CI pass.

6) **Write a Playwright browser test** for the feature or fix you just implemented:
   - Look for an existing `e2e/` directory or `playwright.config.ts` in the project. Create the test file there (e.g., `e2e/<feature-name>.spec.ts` or `tests/e2e/<feature-name>.spec.ts`).
   - Import from `@playwright/test`: `import { test, expect } from '@playwright/test';`
   - **Before finishing, confirm `@playwright/test` is actually installable**: check that it appears in `package.json` (`dependencies` or `devDependencies`) and in the lockfile. If the project has no prior Playwright setup, add `@playwright/test` as a devDependency and a matching `playwright.config.ts` yourself, in the same change — do not leave a spec importing a package the project doesn't declare. A spec that can't resolve its own import is not test coverage; it is a broken build, and review will reject it every time.
   - Test the SPECIFIC feature or fix you built — not the entire application.
   - Verify the complete user flow: navigation, interactions, expected outcomes.
   - Use relative paths for navigation (e.g., `await page.goto('/dashboard')`) — the base URL is set by the workflow via `PLAYWRIGHT_BASE_URL`.
   - If the feature requires authentication, check for `VERITY_E2E_EMAIL` and `VERITY_E2E_PASSWORD` env vars and skip if not set:
     ```typescript
     test.beforeEach(async () => {
       if (!process.env.VERITY_E2E_EMAIL) test.skip(true, 'E2E credentials not configured');
     });
     ```
   - The workflow will start local servers and run your test automatically after your code changes.

7) Final message must include:
   - Which source files were changed and a brief rationale for each.
   - Test and build commands run and their outcomes.
   - If no code change was possible, explain specifically why.


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
