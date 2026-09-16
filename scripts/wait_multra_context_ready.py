#!/usr/bin/env python3
"""Wait for application-facing Multra context at the exact checked-out SHA.

Direct GitHub/Codex workflows do not call Verity-only `/v1/code/repositories/external/*`
control routes with a project key. The authenticated workflow_started callback asks
Verity to prepare the graph server-side; this runner only waits until the bound
application key can mint exact-SHA repository context.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
POLL_SECONDS = 3
HEARTBEAT_SECONDS = 90
MAX_WAIT_SECONDS = 12 * 60
RETRYABLE_GRAPH_CODES = {
    "repository_graph_refresh_queued",
    "repository_graph_refresh_in_progress",
    "repository_graph_not_ready",
    "graph_not_ready",
}
RETRYABLE_CONTEXT_STATUSES = {
    "graph_not_ready",
    "repository_graph_refresh_queued",
    "repository_graph_refresh_in_progress",
    "queued",
    "indexing",
    "building",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def required(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def candidate_paths() -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True, timeout=20
    ).stdout.splitlines()
    preferred = [".verity/config.yml", "README.md", "package.json", "pyproject.toml"]
    selected = [path for path in preferred if path in tracked]
    if selected:
        return selected[:4]
    for path in tracked:
        if path and not path.startswith(".git/"):
            selected.append(path)
        if len(selected) >= 4:
            break
    return selected


def request_context(base: str, key: str, repository: str, sha: str, paths: list[str]):
    url = f"{base.rstrip('/')}/v1/context/handles"
    payload = {
        "repository_full_name": repository,
        "commit_sha": sha,
        "code_context_required": True,
        "task": "Verify exact repository context is ready for the pending Codex coding workflow.",
        **({"paths": paths} if paths else {}),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "verity-direct-codex-context-ready/2",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=25) as response:
            return response.status, json.loads(response.read(1024 * 1024).decode() or "{}")
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(1024 * 1024).decode("utf-8", "replace") or "{}")
        except ValueError:
            body = {}
        return error.code, body


def retryable_graph_state(status: int, error: dict) -> bool:
    if status != 409:
        return False
    code = str(error.get("code") or "")
    if code in RETRYABLE_GRAPH_CODES:
        return True
    if code != "required_code_context_unavailable":
        return False
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    context_status = str(details.get("context_status") or details.get("graph_status") or "")
    return context_status in RETRYABLE_CONTEXT_STATUSES


def retry_delay(error: dict) -> float:
    value = error.get("retry_after")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(POLL_SECONDS)
    return max(1.0, min(seconds, 15.0))


def main() -> int:
    key = required("MULTRA_API_KEY")
    repository = required("GITHUB_REPOSITORY")
    sha = required("MULTRA_GRAPH_SHA").lower()
    if not SHA.fullmatch(sha):
        raise ValueError("MULTRA_GRAPH_SHA must be a full Git commit object ID")

    raw_base = required("MULTRA_BASE_URL")
    parsed = urllib.parse.urlsplit(raw_base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("MULTRA_BASE_URL must be an absolute HTTPS origin")
    base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
    paths = candidate_paths()
    started = time.monotonic()
    deadline = started + MAX_WAIT_SECONDS
    next_heartbeat = started + HEARTBEAT_SECONDS

    while True:
        status, payload = request_context(base, key, repository, sha, paths)
        if status == 201:
            attestation = payload.get("attestation") if isinstance(payload, dict) else None
            if not isinstance(attestation, dict):
                print("Multra context readiness returned no attestation.", file=sys.stderr)
                return 1
            attested_repo = str(attestation.get("repository_full_name") or "").strip().lower()
            attested_sha = str(attestation.get("commit_sha") or "").strip().lower()
            if attested_repo != repository.lower() or attested_sha != sha:
                print("Multra returned repository context for the wrong repository or commit.", file=sys.stderr)
                return 1
            print(
                f"Repository context is ready for {repository}@{sha[:12]} using the application-facing context contract."
            )
            return 0

        error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or f"http_{status}")
        if retryable_graph_state(status, error):
            now = time.monotonic()
            if now >= deadline:
                print(
                    f"Repository context did not become ready for {repository}@{sha[:12]} within {MAX_WAIT_SECONDS // 60} minutes; "
                    "stopping instead of looping indefinitely.",
                    file=sys.stderr,
                )
                return 1
            if now >= next_heartbeat:
                print(f"Repository graph is still preparing for {sha[:12]} ({code}); continuing to wait for the exact graph.")
                next_heartbeat = now + HEARTBEAT_SECONDS
            time.sleep(retry_delay(error))
            continue

        message = str(error.get("message") or "Multra context readiness failed")
        print(f"Repository context readiness failed ({status}, {code}): {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Repository context readiness could not complete: {error}", file=sys.stderr)
        raise SystemExit(1)
