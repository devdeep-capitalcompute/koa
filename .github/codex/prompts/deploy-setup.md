# Verity Deploy Setup

Verity could not confidently detect how to deploy this repository on its own (no
`package.json` "deploy" script, no Vercel config, no recognizable IaC template with a saved
deploy config). Your job is to write a **one-time deploy script**, not to deploy anything —
this run has no cloud credentials, and must not need any to complete.

## What "done" looks like

1. Read the repo. Look specifically for: existing CI/CD workflows under `.github/workflows/`
   that already deploy something (even to a different provider or environment), a `Makefile`
   with a `deploy`/`release`/`publish` target, a `Procfile`, `fly.toml`, `render.yaml`,
   `railway.json`, a Dockerfile plus any hint of where the image goes (an ECR/GCR/registry
   reference, a Kubernetes manifest, a `docker-compose.prod.yml`), or comments/docs
   (`DEPLOYMENT.md`, `docs/deploy*.md`, README sections) that describe the process a human
   currently follows by hand.
2. From what you find, write the **smallest correct set of shell commands** that would deploy
   this project, and put them in `.verity/config.yml` under `commands.deploy:` (a YAML list of
   strings, each run in order). Prefer reusing something that already exists (a Makefile
   target, an npm script, a CI step you can lift verbatim) over inventing a new build process.
3. If the deploy genuinely needs more than a few one-line commands (e.g. building an image,
   tagging it, pushing it, then triggering a rollout), write a `scripts/verity-deploy.sh`
   instead, executable, with a comment at the top explaining what it assumes (which secrets/
   env vars it reads, which registry/target it pushes to), and set
   `commands.deploy: ["bash scripts/verity-deploy.sh"]`.
4. Add a short `## Deploying` section to `docs/DEPLOYMENT.md` (create it if it doesn't exist)
   explaining, in plain language, what the script does and which secrets it needs — this is
   what a non-technical founder will read if a deploy ever fails and they want to understand
   why, not just an engineer.

## Hard constraints — read before writing anything

- **Never write a credential, token, or secret value anywhere in the repo.** Reference secrets
  only by the environment variable name a deploy runner (GitHub Actions) would already provide
  — e.g. `$AWS_REGION`, `$VERCEL_TOKEN` — never invent a value or ask the user to hardcode one.
- **Do not touch `policies.deploy.enabled`** in `.verity/config.yml`. That is a separate,
  human-approved opt-in Verity's own dashboard handles; changing it here would make this PR
  both draft the script and silently turn deploys on, which is not your call to make.
- **Do not attempt to actually run a deploy, install cloud CLIs beyond checking they're
  referenced correctly, or contact any external service.** This run has no credentials for a
  reason — verify the script by reading it, not by executing it.
- **If you cannot find enough signal to propose anything you'd trust,** do not guess. Write
  `docs/DEPLOYMENT.md` explaining what you looked for and did not find, and leave
  `commands.deploy` empty. An honest "I don't know" is worth more than a plausible-looking
  script that deploys the wrong thing the first time someone runs it for real.
- This PR will be reviewed by a human before it can affect anything — nothing you write here
  runs until someone reads it and merges it. Write it as if they will actually read it: explain
  *why* each command is there, not just what it is.
