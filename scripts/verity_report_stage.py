#!/usr/bin/env python3
"""Tell Verity which part of a run is happening.

A dev-cycle run takes twenty to forty-five minutes and used to report exactly
twice: once when it started, once when it finished. Everything in between —
writing the code, two rounds of tests, two rounds of build, up to three rounds
of browser tests, pushing the branch — was a single spinning icon in the
product, held long enough that "is this stuck?" became a fair question with
nothing on screen able to answer it.

This posts one `workflow_stage` callback to the endpoint the run already uses
for its start and finish, so no new infrastructure and no new credentials are
involved.

Every failure mode here is silent on purpose. This is telemetry: a run must
never fail, or slow down, because Verity was unreachable while it was working.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# The vocabulary Verity maps to a phase. A stage outside this set is dropped by
# the handler rather than shown, so adding one here means adding it there too.
KNOWN_STAGES = {
    "setup",
    "implement",
    "verify.tests",
    "verify.build",
    "verify.e2e",
    "package",
    "push",
}


def normalize_status(value: str) -> str:
    """GitHub step outcomes, in the vocabulary Verity records.

    `steps.<id>.outcome` is one of success/failure/cancelled/skipped, and a
    stage that did not run is not the same thing as a stage that failed — a
    repository with no browser tests configured skips them every time.
    """
    text = (value or "").strip().lower()
    if text in {"success", "passed", "completed"}:
        return "passed"
    if text in {"failure", "failed", "error", "timed_out"}:
        return "failed"
    if text in {"skipped", "cancelled", "canceled"}:
        return "skipped"
    return "running"


def main() -> int:
    url = (os.environ.get("VERITY_CALLBACK_URL") or "").strip()
    # Bootstrap substitutes the callback-URL placeholder everywhere in a file,
    # including inside any equality check written against it. An unsubstituted
    # placeholder has no URL scheme, so test for that rather than comparing.
    if not url.startswith(("http://", "https://")):
        print("Skipping stage callback: callback URL not configured")
        return 0

    stage = (os.environ.get("STAGE") or "").strip().lower()
    if stage not in KNOWN_STAGES:
        print(f"Skipping stage callback: unknown stage {stage!r}")
        return 0

    payload = {
        "event": "workflow_stage",
        "project_id": os.environ.get("VERITY_PROJECT_ID"),
        "repo": os.environ.get("GITHUB_REPOSITORY"),
        "workflow": "codex-dev-cycle.yml",
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "run_url": (
            f"https://github.com/{os.environ.get('GITHUB_REPOSITORY')}"
            f"/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"
        ),
        "dispatch_id": os.environ.get("DISPATCH_ID") or None,
        "verity_delivery_id": os.environ.get("VERITY_DELIVERY_ID") or None,
        "issue_number": os.environ.get("ISSUE_NUMBER") or None,
        "stage": stage,
        "status": normalize_status(os.environ.get("STAGE_STATUS", "")),
        "detail": (os.environ.get("STAGE_DETAIL") or "")[:500] or None,
    }

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {os.environ.get('VERITY_PROJECT_TOKEN', '')}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(f"Reported stage {stage} ({payload['status']}): HTTP {response.status}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        # Deliberately not a failure. Losing one progress ping costs the product
        # a few minutes of precision; failing the step would throw away the work
        # the run has already done.
        print(f"Stage callback failed, continuing: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
