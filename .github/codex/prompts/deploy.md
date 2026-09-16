# Verity Deploy Guidance

Deploy uses `.verity/config.yml` -> `commands.deploy`.

- Backend deploy should use AWS credentials from GitHub Secrets.
- Frontend deploy uses Vercel secrets and Vercel CLI.
- Always report started/completed status back to Verity via callback URL.

## Your deploy command must report where it landed

Verity records what is running at a URL: it health-checks that URL, runs the
project's journey tests against it, and uses it as the rollback target. A deploy
that finishes without naming its result gives Verity nothing to verify, so the
change can never be confirmed as live.

`commands.deploy` therefore has one output contract. Write the deployed base URL
either way:

```bash
# From the deploy step itself:
echo "verity_url=https://api.example.com" >> "$GITHUB_OUTPUT"

# From inside a script the step calls (usually easier):
echo "https://api.example.com" >> "$VERITY_URL_FILE"
```

Two environment variables are available to the command:

| Variable | Meaning |
|---|---|
| `VERITY_ENVIRONMENT` | `staging` or `production` — the environment being deployed |
| `VERITY_COMMIT_SHA` | The exact commit being deployed |

**When the backend is the only deploy target, a run that reports no URL fails.**
This is deliberate: Verity will not record an environment it cannot name and
cannot test. A project that also deploys a frontend already reports that URL as
the environment's address, so the backend URL is optional there.
