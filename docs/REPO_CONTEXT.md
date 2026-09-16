# Repo Context

This file helps Verity/Codex understand how to work in this repository.

## What This Project Does
_(Not auto-generated — the deterministic doc sync script never writes here, so this survives
every refresh. The first Codex run in this repo fills this in from the actual codebase before
generating anything else. If it still says this after that run, something upstream skipped
Step 0 of use-case generation.)_

## What Verity detected
- Detected at: 2026-09-16T18:33:23+05:30
- Repo: devdeep-capitalcompute/koa
- Default branch: master

## Suggested commands (review before enabling automation)
These are written into `.verity/config.yml` (in a PR) if empty.

### Setup
_(none configured)_

### Tests
_(none configured)_

### Build
_(none configured)_

### Deploy
_(none configured)_

## Notes for humans
- If you change commands here, also update `.verity/config.yml`.
- No secrets should be committed. Use GitHub Secrets.
- Verity-managed runtime files (`.github/workflows`, `.github/codex`, `.verity/bootstrap.json`, and `scripts/`) should be updated by bootstrap/runtime rollout PRs, not direct default-branch pushes.
- Keep project-owned `.verity/config.yml` values intact when refreshing runtime templates.
- Full-auto behavior is opt-in and bounded by policy, dispatch ledger, duplicate checks, tests, deploy gates, and rollout state.

## Repository context
- Multra owns the code graph and supplies repository context to the coding agent.
- Workflows send the rendered prompt straight to Multra; Codex reads the checked-out repository directly for anything else it needs. The responses endpoint requires `MULTRA_API_KEY` or `BLUESKY_API_KEY`.
- Verity no longer ships a local context compiler.
- Raw source, raw prompts, raw logs, and model outputs remain local to GitHub Actions by default.
- Multra/BlueSky secrets remain in GitHub Secrets or backend secret storage; widget/frontend code receives only secret names and status metadata, never key values.

## Auto Documentation Snapshot
<!-- verity:auto-doc:start -->
- Commit: `282fadd82d7f84a8179f931c911108b1afc7d683`
- Commit date: `2026-09-16T18:33:23+05:30`
- Repository: `devdeep-capitalcompute/koa`
- Default branch: `master`

### Configured Commands
Setup:
_(none configured)_
Tests:
_(none configured)_
Build:
_(none configured)_
Deploy:
_(none configured)_

### Top-level Directories
- `__tests__`
- `docs`
- `lib`
- `scripts`
- `test-helpers`

### Workflow Files
- `codex-deploy-setup.yml`
- `codex-deploy.yml`
- `codex-dev-cycle.yml`
- `codex-pr-review.yml`
- `codex-test-generation.yml`
- `codex-test-to-issue.yml`
- `codex-usecase-generation.yml`
- `node.js.yml`
- `npm-publish.yml`
- `verity-agent.yml`
- `verity-auto-docs.yml`
- `verity-builder-plan.yml`
- `verity-command-router.yml`
- `verity-explore.yml`
- `verity-guardrails.yml`
- `verity-monitor.yml`
- `verity-pr-auto-fix.yml`
- `verity-repo-context-builder.yml`
- `verity-validation.yml`

### Enabled Policy Flags
- `- `deploy.enabled`: `False``
- `- `openai_guardrail.enabled`: `True``
- `- `pr_review.enabled`: `True``
<!-- verity:auto-doc:end -->
