#!/usr/bin/env python3
"""Prepare exact repository context once; coordinated cold graphs resume by callback."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONTRACT = "verity.repository_graph.v1"
SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
# Standalone workflows do not have a Verity job id/callback to park on, so this
# is a heartbeat interval, not a deadline. A healthy cold graph keeps the
# request gated until the exact SHA is ready instead of converting indexing
# latency into a failed coding request.
STANDALONE_WAIT_SECONDS = 90
STANDALONE_POLL_SECONDS = 3


def required_env(name):
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a repository key or runner token to a redirect.


def request_json(base_url, api_key, path, payload):
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Graph and callback APIs must use HTTPS without embedded credentials')
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        "User-Agent": "verity-multra-graph-preflight/4"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=25) as response:
            return response.status, json.loads(response.read(1024 * 1024).decode() or '{}')
    except urllib.error.HTTPError as error:
        try:
            value = json.loads(error.read(1024 * 1024).decode() or '{}')
        except (ValueError, UnicodeError):
            value = {}
        return error.code, value


def access_hint(code):
    if code in ('repository_not_found', 'repository_access_revoked', 'repository_access_denied'):
        return "Reconnect the repository in Verity (Project -> GitHub & Secrets) to restore Multra's read access."
    return "No coding was started against a stale graph. Check repository context preparation in Verity."


def write_state(ready, sha=''):
    if os.environ.get('GITHUB_ENV'):
        with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as handle:
            if ready:
                handle.write(f'VERITY_GRAPH_READY_SHA={sha}\nVERITY_EXPECTED_COMMIT={sha}\n')
            else:
                handle.write('VERITY_FINISHED=true\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
            handle.write(f"ready={'true' if ready else 'false'}\n")


def wait_for_standalone_graph(base, api_key, repository, sha):
    """Bridge legacy workflows without weakening the callback-first Thread path.

    Root/bootstrap workflows do not have a Verity job id to park and resume. They
    request the exact SHA graph once, then keep the writable request gated until
    Multra reports that exact graph ready or reports an explicit terminal error.
    STANDALONE_WAIT_SECONDS is only a progress heartbeat; a healthy cold graph is
    never turned into a failed coding request just because indexing takes longer.
    """
    last_status = 'queued'
    next_heartbeat = time.monotonic() + STANDALONE_WAIT_SECONDS
    while True:
        time.sleep(STANDALONE_POLL_SECONDS)
        status, payload = request_json(base, api_key, '/v1/code/repositories/external/status', {
            'repository_full_name': repository,
            'commit_sha': sha,
        })
        if status != 200:
            code = str(payload.get('error', {}).get('code') or f'http_{status}')
            print(f'Graph status failed ({status}, {code}). {access_hint(code)}', file=sys.stderr)
            return False
        last_status = str(payload.get('status') or 'not_indexed')
        if last_status == 'ready' and str(payload.get('commit_sha') or '').lower() == sha:
            write_state(True, sha)
            print('Repository context became ready for the exact checked-out commit.')
            return True
        if last_status in ('failed', 'revoked', 'repository_access_revoked', 'repository_access_denied'):
            code = str(payload.get('error_code') or last_status)
            print(
                f'Graph preparation stopped ({code}). {access_hint(code)} '
                'No coding was started against a stale or missing graph.',
                file=sys.stderr,
            )
            return False
        if time.monotonic() >= next_heartbeat:
            print(
                f'Repository context is still {last_status} for {sha[:12]}. '
                'Multra is continuing to build the exact graph; Verity will keep waiting instead of failing a healthy cold request.'
            )
            next_heartbeat = time.monotonic() + STANDALONE_WAIT_SECONDS


def main():
    coordinated = '--prepare-job' in sys.argv

    if coordinated:
        job = required_env('VERITY_JOB_ID')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', job):
            raise ValueError('Invalid job id')
        run_id = required_env('GITHUB_RUN_ID')
        token, verity = required_env('VERITY_JOB_TOKEN'), required_env('VERITY_API_BASE_URL')
        path = f'/v1/runner/jobs/{job}/graph'
        code, result = request_json(verity, token, path + '/prepare', {'runId': run_id})
        if code != 200:
            # The backend owns both the durable waiter and the privileged Multra
            # graph request. Surface its product-safe explanation here; never
            # fall back to a runner-side control-plane call with the model key.
            reason = str((result.get('error') or {}).get('message') or result.get('message') or '').strip()
            print(reason or 'Verity refused repository preparation; check the request and repository connection', file=sys.stderr)
            return 1
        decision = result.get('data', {})
        if decision.get('status') == 'stopped':
            write_state(False)
            return 0
        sha = str(decision.get('commitSha') or '').lower()
        if not SHA.fullmatch(sha):
            raise ValueError('Verity did not provide a full code version')
        # Snapshot the exact code chosen by Verity. A callback-started retry gets
        # the same pinned SHA from the durable graph wait before any coding step.
        subprocess.run(['git', 'fetch', 'origin', sha], check=True)
        subprocess.run(['git', 'checkout', '--detach', sha], check=True)
        if decision.get('status') == 'ready':
            write_state(True, sha)
            return 0
        if decision.get('status') == 'waiting':
            write_state(False)
            print('Repository context is being prepared. This runner is exiting; Multra will resume the exact job by callback.')
            return 0
        print('Verity returned an invalid repository preparation state. No coding was started.', file=sys.stderr)
        return 1

    # Standalone/root workflows have no durable Verity waiter. Keep the existing
    # project-key refresh/status loop for that legacy path only.
    api_key = required_env('MULTRA_API_KEY')
    repository = required_env('GITHUB_REPOSITORY')
    endpoint = os.environ.get('MULTRA_RESPONSES_API_ENDPOINT') or 'https://multra.ai/v1/responses'
    parsed = urllib.parse.urlsplit(endpoint)
    base = (os.environ.get('MULTRA_BASE_URL') or urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))).rstrip('/')
    sha = str(os.environ.get('MULTRA_GRAPH_SHA') or '').lower()
    if not SHA.fullmatch(sha):
        raise ValueError('MULTRA_GRAPH_SHA must be a full Git commit object ID')
    if os.environ.get('VERITY_GRAPH_READY_SHA') == sha:
        return 0
    status, payload = request_json(base, api_key, '/v1/code/repositories/external/refresh', {
        'repository_full_name': repository, 'commit_sha': sha, 'reason': 'verity_codex_preflight',
        'notify_when_ready': False})
    if status not in (200, 202):
        code = str(payload.get('error', {}).get('code') or 'graph_request_failed')
        print(f'Graph preparation failed ({status}, {code}). {access_hint(code)}', file=sys.stderr)
        return 1
    if payload.get('status') == 'ready' and str(payload.get('commit_sha') or '').lower() == sha:
        write_state(True, sha)
        print('Repository context is ready.')
        return 0
    if payload.get('status') in ('queued', 'indexing', 'publishing', 'not_indexed'):
        print('Repository context is cold; waiting for Multra to finish the exact checked-out commit graph.')
        return 0 if wait_for_standalone_graph(base, api_key, repository, sha) else 1
    print('Repository context is not ready. No coding was started.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        # Avoid echoing raw request errors containing URLs or credentials.
        print('Repository preparation could not complete. Check the connection and the Verity job result.', file=sys.stderr)
        raise SystemExit(1)