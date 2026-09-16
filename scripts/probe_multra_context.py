#!/usr/bin/env python3
"""Resolve and pin Multra repository context before a Codex model turn.

This is deliberately not a model probe. It asks Multra's explicit context-handle
endpoint for the exact checked-out repository+commit, verifies the attestation,
and returns an immutable graph-selection handle that the real Codex call must
reuse. If the task itself produces no selection, a bounded deterministic local
path ranking supplies likely source paths and tries again. A coding turn never
starts with an empty or wrong-commit repository context.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_SECONDS = 25
MAX_PROMPT_CHARS = 20_000
MAX_CANDIDATES = 36
CANDIDATE_BATCH = 12
MAX_SCAN_BYTES = 96 * 1024
SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
PATH_TOKEN = re.compile(r"(?:^|[\s`'\"(])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.@+-]+)(?=$|[\s`'\"),:])")
WORD = re.compile(r"[A-Za-z0-9_]{3,}")
STOP_WORDS = {
    "about", "after", "agent", "before", "build", "change", "code", "create",
    "current", "edit", "export", "feature", "file", "files", "find", "fix",
    "from", "function", "help", "into", "make", "method", "model", "please",
    "repo", "repository", "request", "should", "task", "test", "tests", "that",
    "this", "update", "using", "with", "work",
}
SKIP_PARTS = {
    ".git", ".next", ".turbo", "build", "coverage", "dist", "node_modules",
    "vendor", "venv", ".venv",
}
TEXT_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".json", ".kt", ".mjs", ".mts", ".php", ".py",
    ".rb", ".rs", ".scss", ".sh", ".sql", ".swift", ".toml", ".ts", ".tsx",
    ".vue", ".yaml", ".yml", ".md",
}
HIGH_SIGNAL_NAMES = {
    "README.md", "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "pom.xml", "requirements.txt", "Makefile",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def checked_out_sha() -> str:
    value = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout.strip().lower()
    if not SHA.fullmatch(value):
        raise ValueError("Checkout did not resolve to a full Git object ID")
    return value


def read_prompt() -> str:
    inline = str(os.environ.get("MULTRA_PROMPT") or "")
    prompt_file = str(os.environ.get("MULTRA_PROMPT_FILE") or "").strip()
    parts: list[str] = []
    if inline.strip():
        parts.append(inline.strip())
    if prompt_file:
        try:
            with open(prompt_file, encoding="utf-8", errors="replace") as handle:
                text = handle.read(MAX_PROMPT_CHARS + 1)
            if text.strip():
                parts.append(text[:MAX_PROMPT_CHARS].strip())
        except OSError as error:
            raise ValueError("The configured Codex prompt file could not be read") from error
    prompt = "\n\n".join(parts).strip()
    if not prompt:
        raise ValueError("Repository-aware Codex calls require a non-empty prompt")
    return prompt[:MAX_PROMPT_CHARS]


def endpoint_contract(endpoint: str) -> tuple[str, str, str]:
    url = urllib.parse.urlsplit(endpoint)
    if url.scheme != "https" or not url.netloc or url.username or url.password:
        raise ValueError("Multra context endpoint must be absolute HTTPS without embedded credentials")
    if not url.path.rstrip("/").endswith("/v1/context/responses"):
        raise ValueError("Multra endpoint must end in /v1/context/responses")
    query = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    repository = str(query.get("repository_full_name") or "").strip()
    commit = str(query.get("commit_sha") or "").strip().lower()
    if not repository or "/" not in repository:
        raise ValueError("Multra endpoint is missing repository_full_name")
    if not SHA.fullmatch(commit):
        raise ValueError("Multra endpoint is missing an exact commit_sha")
    base = urllib.parse.urlunsplit((url.scheme, url.netloc, "", "", "")).rstrip("/")
    return base, repository, commit


def request_json(url: str, key: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "verity-multra-context-guard/2",
        },
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.loads(response.read(1024 * 1024).decode() or "{}")
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read(1024 * 1024).decode("utf-8", "replace") or "{}")
        except ValueError:
            payload = {}
        return error.code, payload


def selection_unavailable(status: int, payload: dict) -> bool:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    return (
        status == 409
        and str(error.get("code") or "") == "required_code_context_unavailable"
        and str(details.get("context_status") or "") in {"not_selected", "needs_more_context"}
    )


def create_context_handle(
    base: str,
    key: str,
    repository: str,
    commit: str,
    task: str,
    paths: list[str] | None = None,
) -> tuple[str | None, dict]:
    status, payload = request_json(
        f"{base}/v1/context/handles",
        key,
        {
            "repository_full_name": repository,
            "commit_sha": commit,
            "code_context_required": True,
            "task": task,
            **({"paths": paths} if paths else {}),
        },
    )
    if selection_unavailable(status, payload):
        return None, {}
    if status != 201:
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        code = str(error.get("code") or f"http_{status}")
        raise RuntimeError(f"Multra context handle creation failed ({status}, {code})")
    attestation = payload.get("attestation")
    if not isinstance(attestation, dict):
        raise RuntimeError("Multra context handle creation returned no attestation")
    handle = str(payload.get("context_handle") or "").strip() or None
    if not handle:
        raise RuntimeError("Multra context handle creation returned no handle")
    return handle, attestation


def attestation_matches(attestation: dict, repository: str, commit: str) -> bool:
    attested_repo = str(attestation.get("repository_full_name") or "").strip().lower()
    attested_commit = str(attestation.get("commit_sha") or "").strip().lower()
    return attested_repo == repository.lower() and attested_commit == commit.lower()


def selected(attestation: dict) -> bool:
    try:
        return (
            str(attestation.get("status") or "") == "ready"
            and int(attestation.get("selected_file_count") or 0) > 0
            and int(attestation.get("selected_span_count") or 0) > 0
        )
    except (TypeError, ValueError):
        return False


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    return [item.decode("utf-8", "replace") for item in result.split(b"\0") if item]


def meaningful_terms(prompt: str) -> list[str]:
    terms: list[str] = []
    for match in WORD.finditer(prompt):
        term = match.group(0).lower().strip("_")
        if len(term) < 3 or term in STOP_WORDS or term.isdigit():
            continue
        if term not in terms:
            terms.append(term)
        if len(terms) >= 40:
            break
    return terms


def normalize_prompt_path(value: str) -> str:
    path = value.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def path_eligible(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    if any(part in SKIP_PARTS for part in parts):
        return False
    if path.endswith((".lock", ".min.js", ".map", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip")):
        return False
    suffix = os.path.splitext(path)[1].lower()
    return suffix in TEXT_SUFFIXES or os.path.basename(path) in HIGH_SIGNAL_NAMES


def candidate_paths(prompt: str) -> list[str]:
    tracked = tracked_files()
    tracked_set = set(tracked)
    explicit: list[str] = []
    for match in PATH_TOKEN.finditer(prompt):
        candidate = normalize_prompt_path(match.group(1))
        if candidate in tracked_set and candidate not in explicit:
            explicit.append(candidate)

    terms = meaningful_terms(prompt)
    ranked: list[tuple[int, int, str]] = []
    for path in tracked:
        if not path_eligible(path):
            continue
        lower_path = path.lower()
        path_hits = sum(1 for term in terms if term in lower_path)
        content_hits = 0
        try:
            if terms and os.path.getsize(path) <= MAX_SCAN_BYTES:
                with open(path, encoding="utf-8", errors="ignore") as handle:
                    content = handle.read(MAX_SCAN_BYTES).lower()
                content_hits = sum(1 for term in terms[:20] if term in content)
        except OSError:
            pass
        high_signal = 1 if os.path.basename(path) in HIGH_SIGNAL_NAMES else 0
        score = path_hits * 20 + content_hits * 2 + high_signal
        if score > 0:
            ranked.append((score, -path.count("/"), path))

    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    result = list(explicit)
    for _, _, path in ranked:
        if path not in result:
            result.append(path)
        if len(result) >= MAX_CANDIDATES:
            break

    if not result:
        fallback = sorted(
            (path for path in tracked if path_eligible(path)),
            key=lambda path: (
                0 if os.path.basename(path) in HIGH_SIGNAL_NAMES else 1,
                path.count("/"),
                path,
            ),
        )
        result.extend(fallback[:MAX_CANDIDATES])
    return result[:MAX_CANDIDATES]


def final_endpoint(endpoint: str, handle: str) -> str:
    url = urllib.parse.urlsplit(endpoint)
    query = dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
    query.pop("paths", None)
    query.pop("symbols", None)
    query["context_handle"] = handle
    query["code_context_required"] = "true"
    return urllib.parse.urlunsplit(
        (url.scheme, url.netloc, url.path, urllib.parse.urlencode(query), "")
    )


def write_outputs(endpoint: str, handle: str, attestation: dict, repository: str, commit: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(f"context_handle={handle}\n")
            output.write(f"endpoint={endpoint}\n")
            output.write(f"repository={repository}\n")
            output.write(f"commit_sha={commit}\n")
            output.write(f"selected_file_count={int(attestation.get('selected_file_count') or 0)}\n")
            output.write(f"selected_span_count={int(attestation.get('selected_span_count') or 0)}\n")
            output.write(f"context_index_hash={str(attestation.get('index_hash') or '')}\n")

    # Bounded, source-free evidence of what actually grounded this coding turn.
    # The raw handle remains in GITHUB_OUTPUT for the next action step but is not
    # copied into human-readable logs or summaries.
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        handle_id = hashlib.sha256(handle.encode("utf-8")).hexdigest()[:12]
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(
                "Multra context: "
                f"repository={repository} commit={commit[:12]} "
                f"handle_id={handle_id} index_hash={str(attestation.get('index_hash') or '')[:16]} "
                f"selected_files={int(attestation.get('selected_file_count') or 0)} "
                f"selected_spans={int(attestation.get('selected_span_count') or 0)}\n"
            )


def main() -> int:
    try:
        key = required_env("MULTRA_API_KEY")
        endpoint = required_env("MULTRA_ENDPOINT")
        prompt = read_prompt()
        base, repository, commit = endpoint_contract(endpoint)
        checkout = checked_out_sha()
        expected_repository = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
        if checkout != commit:
            raise RuntimeError(
                f"Multra context commit {commit[:12]} does not match checkout {checkout[:12]}"
            )
        if expected_repository and expected_repository.lower() != repository.lower():
            raise RuntimeError("Multra context repository does not match the checked-out repository")

        handle, attestation = create_context_handle(base, key, repository, commit, prompt)
        strategy = "task"
        if attestation and not attestation_matches(attestation, repository, commit):
            raise RuntimeError("Multra context attestation does not match the checked-out repository and commit")

        if not handle or not selected(attestation):
            candidates = candidate_paths(prompt)
            handle = None
            for offset in range(0, len(candidates), CANDIDATE_BATCH):
                batch = candidates[offset:offset + CANDIDATE_BATCH]
                candidate_handle, candidate_attestation = create_context_handle(
                    base, key, repository, commit, prompt, batch
                )
                if candidate_attestation and not attestation_matches(candidate_attestation, repository, commit):
                    raise RuntimeError(
                        "Multra context attestation changed repository or commit during fallback resolution"
                    )
                if candidate_handle and selected(candidate_attestation):
                    handle, attestation = candidate_handle, candidate_attestation
                    strategy = "local-path-fallback"
                    break

        if not handle or not selected(attestation):
            raise RuntimeError(
                "Multra has an exact repository graph but selected no source spans for this task; "
                "coding is stopped rather than running blind"
            )

        pinned = final_endpoint(endpoint, handle)
        write_outputs(pinned, handle, attestation, repository, commit)
        print(
            "Repository context pinned: "
            f"{repository}@{commit[:12]} "
            f"files={int(attestation.get('selected_file_count') or 0)} "
            f"spans={int(attestation.get('selected_span_count') or 0)} "
            f"strategy={strategy} "
            f"index={str(attestation.get('index_hash') or '')[:16]}"
        )
        return 0
    except Exception as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())