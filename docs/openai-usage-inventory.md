# AI Gateway Usage Inventory

## Central defaults
- Every workflow sends `bluesky/auto`, and Multra selects the provider/model behind it. There is no
  per-repository provider credential or direct OpenAI fallback in the normal coding path.
- Codex runs as the coding agent on the checked-out GitHub Actions workspace; Multra supplies exact-SHA graph/context and model routing; Verity owns orchestration, checkpoints, retries, PRs, and deploy gates.

## Gateway defaults
- Multra origin: `https://multra.ai`.
- Direct coding transport: Codex CLI custom Multra provider with `wire_api = "responses"`; no `openai/codex-action` Responses proxy is used.
- Exact coding turns are pinned to `/v1/context/responses` with repository, commit SHA, and context handle resolved by the shared `verity-codex-direct` action.
- Codex provider auth is supplied from `MULTRA_API_KEY` or `BLUESKY_API_KEY` through the custom provider `BLUESKY_API_KEY` environment variable; provider credentials never go to the runner.
- GitHub Actions secret names: `MULTRA_API_KEY` or `BLUESKY_API_KEY`.

## Workflow variables

Repository variables these workflows actually read. Anything not listed here has
no effect on this repository.

- `VERITY_CODEX_EFFORT` sets the Codex reasoning effort (`low`, `medium`, `high`); defaults to `medium`.
- The Multra origin defaults to `https://multra.ai` in the shared direct action. Workflows do not use the retired `MULTRA_RESPONSES_API_ENDPOINT` proxy override.

## Privacy and authority boundary
- Verity's authenticated workflow callback prepares/reconciles repository authorization and the exact-SHA graph server-side before the coding turn.
- The runner waits for application-facing Multra context, resolves a signed exact-SHA context handle, then runs the pinned Codex CLI directly against that context endpoint.
- Codex reads/writes only the checked-out workspace allowed by its sandbox. Trusted Verity workflow steps, not Codex, own Git credentials, checkpoint commit/push, PR, merge, and deployment actions.
- Multra/BlueSky keys remain in GitHub Secrets or backend secret storage; widget/frontend code receives only secret names and status metadata, never key values.
- Verity no longer ships a local Context Kernel compiler or `.verity/kernel` receipt artifact path.
