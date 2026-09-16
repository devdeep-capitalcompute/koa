# Verity Repo Context Builder (Reference)

This repo includes a workflow `.github/workflows/verity-repo-context-builder.yml` that can be run manually to:
- detects likely setup/test/build/deploy commands
- updates `docs/REPO_CONTEXT.md`
- fills empty `commands.*` arrays in `.verity/config.yml` (only when empty)
- uploads the suggested `docs/REPO_CONTEXT.md`/`.verity/config.yml` changes as a
  workflow artifact and reports completion to Verity — it does **not** open a PR
  itself (`permissions: contents: read` makes that structurally impossible). A
  human or Verity's own backend turns the artifact into a PR separately.

It may also include `.github/workflows/verity-auto-docs.yml` plus `scripts/sync_repo_docs.py` to keep:
- `docs/REPO_CONTEXT.md`
- `docs/AI_HANDOFF.md`
fresh as code evolves.

If you modify repository structure, keep this workflow working for noob developers.
