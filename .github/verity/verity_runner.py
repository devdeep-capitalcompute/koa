#!/usr/bin/env python3
"""Verity runner client.

Speaks the runner protocol and nothing else. It holds no product logic: it does
not choose a prompt, decide whether to retry, classify a failure or decide when
to stop. Every one of those answers arrives from `GET /v1/runner/jobs/{id}/next`.

See docs/adr/0005-thread-model-and-runner-protocol.md in the Verity repository.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 30
RETRIES = 4
HEARTBEAT_SECONDS = 30

# What fits in one event. The 256KB protocol ceiling is a hard limit; this is
# well under it so a long command's tail always arrives. The *whole* log is
# written to a file and uploaded with the run, and the event says it was cut —
# evidence is never dropped to make a request fit.
EVENT_SUMMARY_LIMIT = 100_000

# Commands are drained inside one workflow step until a model step is reached,
# because a model step needs a composite action and an action cannot be called
# from a loop. This bounds that drain so a planner bug cannot spin here.
MAX_COMMANDS_PER_SLOT = 40


def _env(name: str, required: bool = True) -> str:
    value = str(os.environ.get(name) or "").strip()
    if required and not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def call(method: str, path: str, payload: dict | None = None) -> dict:
    """One protocol call, with retries on transport and 5xx failures.

    A 4xx is never retried: it is Verity telling the runner it asked for
    something it may not have, and asking again cannot change that.
    """
    base = _env("VERITY_API_BASE_URL").rstrip("/")
    token = _env("VERITY_JOB_TOKEN")
    url = f"{base}/v1/runner/jobs/{_env('VERITY_JOB_ID')}{path}"

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    last = "no attempt made"
    for attempt in range(RETRIES):
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8") or "{}"
                return json.loads(raw).get("data", {})
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            if error.code < 500:
                raise SystemExit(f"Verity refused {method} {path}: {error.code} {detail}")
            last = f"{error.code} {detail}"
        except Exception as error:  # noqa: BLE001 - transport errors of every shape
            last = str(error)
        time.sleep(2 ** attempt)
    raise SystemExit(f"Could not reach Verity for {method} {path}: {last}")


def write_output(pairs: dict) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in pairs.items():
                handle.write(f"{key}={value}\n")

    # One job-wide flag, so the workflow's later slots can switch themselves off
    # with a single condition instead of each one testing every slot before it.
    if pairs.get("finished") == "true":
        env_file = os.environ.get("GITHUB_ENV")
        if env_file:
            with open(env_file, "a", encoding="utf-8") as handle:
                handle.write("VERITY_FINISHED=true\n")


def extract_token_usage(codex_home: str) -> dict | None:
    """Token counts for the model call that just ran.

    The Codex action publishes exactly one output, `final-message` — no usage.
    The counts do exist, in the CLI's own session rollout under CODEX_HOME,
    which is why the workflow pins that directory. This parser is deliberately
    shape-tolerant: it scans every JSON object in every rollout file for
    input/output token fields and keeps the largest totals it finds, so a change
    to the rollout schema degrades to "not found" rather than to a wrong number.

    Returning None is a real answer and the caller treats it as a hard failure.
    A model call recorded as zero cost is how a budget guard silently switches
    itself off, which is the one outcome that is worse than stopping.
    """
    if not codex_home or not os.path.isdir(codex_home):
        return None

    best = None
    patterns = ("**/*.jsonl", "**/*.json")
    for pattern in patterns:
        for path in glob.glob(os.path.join(codex_home, pattern), recursive=True):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line or line[0] not in "{[":
                            continue
                        try:
                            found = _scan_for_usage(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                        if found and (best is None or found["total"] > best["total"]):
                            best = found
            except OSError:
                continue
    if not best:
        return None
    return {
        "inputTokens": best["input"],
        "outputTokens": best["output"],
        "cachedInputTokens": best.get("cached", 0),
    }


def extract_served_model(codex_home: str) -> str | None:
    """Which model actually answered, as opposed to which one we asked for.

    Verity dispatches `bluesky/auto`, and that is a router: what sits behind it
    is Multra's choice and has changed under us before. Probing the API shows it
    resolving to `gpt-4.1-mini` today, but a probe uses a different key than the
    runner does, so it is evidence about the probe and not about this run.

    The run itself knows. The provider echoes the model it served in its own
    response, and Codex writes that into the session rollout the token counts
    already come from. Reading it here makes the most important variable in a
    delivery a recorded fact instead of an inference: a requirement that came
    back empty can say which model produced it.

    Same shape-tolerance as the usage parser, and the same consequence for
    failure: `None` means "not recorded", never a guess. It is not part of the
    step's success — a run that delivers must not fail because a provider
    stopped echoing its own name.
    """
    if not codex_home or not os.path.isdir(codex_home):
        return None

    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        value = node.get("model")
        # A response object, not the request we sent: the request carries the
        # slug Verity asked for, which is the thing this exists to look past.
        if isinstance(value, str) and value.strip() and node.get("object") in (
            "response",
            "chat.completion",
            None,
        ):
            found.append(value.strip())
        for child in node.values():
            walk(child)

    for pattern in ("**/*.jsonl", "**/*.json"):
        for path in sorted(glob.glob(os.path.join(codex_home, pattern), recursive=True)):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line or line[0] not in "{[":
                            continue
                        try:
                            walk(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

    requested = str(os.environ.get("CODEX_MODEL") or "").strip()

    def is_router(name: str) -> bool:
        # The slug we dispatch is a router, and reporting it back as the model
        # that did the work says nothing. Belt and braces: the comparison with
        # `requested` is the real check, and this holds when the environment
        # does not carry it — which is how a run once recorded
        # `servedModel: bluesky/auto` and answered its own question with the
        # question.
        return name == requested or name.endswith("/auto")

    served = [name for name in found if not is_router(name)]
    if not served:
        return None
    # The last distinct answer wins: a turn that switched models mid-run should
    # report what finished it, and reporting the router's own slug back would
    # say nothing at all.
    return served[-1]


def extract_last_error(codex_home: str) -> str | None:
    """The last error the model CLI recorded, for the run to show.

    A failed model step used to reach the thread as "the model step did not run
    to completion" and nothing else, so the actual cause — a provider that
    dropped the stream, a model name it did not recognise, a refused key — was
    only ever in a GitHub Actions log four steps away. Evidence the product
    holds is worth more than evidence it points at.

    Same rollout files the usage parser already reads, and deliberately just as
    shape-tolerant: anything that looks like an error message is a candidate and
    the last one wins, because a schema change should cost us the detail rather
    than the run. Best-effort by construction — `None` simply means the caller
    falls back to the generic sentence it used before.
    """
    if not codex_home or not os.path.isdir(codex_home):
        return None

    found: list[str] = []
    for pattern in ("**/*.jsonl", "**/*.json"):
        for path in sorted(glob.glob(os.path.join(codex_home, pattern), recursive=True)):
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line or line[0] not in "{[":
                            continue
                        try:
                            _collect_errors(json.loads(line), found)
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

    if not found:
        return None
    # Trimmed: this becomes a sentence on a page, not a log file.
    return found[-1].strip()[:2000] or None


# Multra's own generic top-level error sentences, keyed to the typed code
# that names them server-side. `code` and `details.context_status` ride
# alongside `message` in Multra's actual JSON response, but Codex's CLI
# flattens that structured body into one string
# ("unexpected status 409 Conflict: <message>, url: ..., request id: ...")
# before it ever writes a rollout file — confirmed by reading a real trace:
# the code and status were gone, only this sentence survived. Matching on the
# sentence itself is what is actually observable from here; it cannot tell a
# cold graph from an empty selection from revoked access, since Multra uses
# this one message for all three (`context_status` is what would have told
# them apart, and that never arrives). `_context_status_headline` says so
# honestly rather than guessing which.
_KNOWN_MULTRA_MESSAGES = {
    'Required repository context is unavailable for this exact request.': 'required_code_context_unavailable',
}


def _tag_known_multra_message(text: str) -> str:
    """Append a `[code]` tag when `text` contains one of Multra's own known
    error sentences verbatim, so `_context_status_headline` can recognize it
    even though Codex discarded the structured code/status that named it.
    """
    for message, code in _KNOWN_MULTRA_MESSAGES.items():
        if message in text:
            return f"{text} [{code}]"
    return text


def _collect_errors(node, out: list[str]) -> None:
    """Every error-shaped string in a rollout entry, in the order they appear."""
    if isinstance(node, list):
        for item in node:
            _collect_errors(item, out)
        return
    if not isinstance(node, dict):
        return

    kind = str(node.get("type") or node.get("level") or "").lower()
    if "error" in kind:
        for key in ("message", "error", "text", "detail"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                out.append(_tag_known_multra_message(value))
                break

    error = node.get("error")
    if isinstance(error, str) and error.strip():
        out.append(_tag_known_multra_message(error))
    elif isinstance(error, dict):
        message = error.get("message") or error.get("detail")
        if isinstance(message, str) and message.strip():
            # The rare case where a structured error dict *does* survive to
            # here: prefer its own code/status over guessing from the
            # sentence, since those are the exact fields Multra sent.
            code = error.get("code")
            details = error.get("details")
            status = details.get("context_status") if isinstance(details, dict) else None
            tag = "/".join(str(value) for value in (code, status) if value)
            out.append(f"{message} [{tag}]" if tag else _tag_known_multra_message(message))

    for value in node.values():
        if isinstance(value, (dict, list)):
            _collect_errors(value, out)


def _scan_for_usage(node) -> dict | None:
    """Depth-first search for an object carrying input and output token counts."""
    if isinstance(node, list):
        best = None
        for item in node:
            found = _scan_for_usage(item)
            if found and (best is None or found["total"] > best["total"]):
                best = found
        return best

    if not isinstance(node, dict):
        return None

    input_keys = ("input_tokens", "inputTokens", "prompt_tokens")
    output_keys = ("output_tokens", "outputTokens", "completion_tokens")
    # How much of the input was a cache hit. Reported beside the totals by the
    # Responses API, and nested under `input_tokens_details` by the chat one.
    cached_keys = ("cached_input_tokens", "cachedInputTokens", "cached_tokens")
    input_value = next((node[k] for k in input_keys if isinstance(node.get(k), (int, float))), None)
    output_value = next((node[k] for k in output_keys if isinstance(node.get(k), (int, float))), None)
    cached_value = next((node[k] for k in cached_keys if isinstance(node.get(k), (int, float))), None)
    if cached_value is None:
        details = node.get("input_tokens_details") or node.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached_value = next(
                (details[k] for k in cached_keys if isinstance(details.get(k), (int, float))), None
            )

    best = None
    if input_value is not None and output_value is not None:
        best = {
            "input": int(input_value),
            "output": int(output_value),
            "cached": int(cached_value) if cached_value is not None else 0,
            "total": int(input_value) + int(output_value),
        }

    for value in node.values():
        found = _scan_for_usage(value)
        if found and (best is None or found["total"] > best["total"]):
            best = found
    return best


def price(tokens: int, usd_per_million: str) -> float:
    try:
        rate = float(usd_per_million)
    except (TypeError, ValueError):
        # No pricing configured for this repository. Tokens are still reported
        # truthfully; the thread's token ceiling is what enforces the stop.
        return 0.0
    return (tokens / 1_000_000.0) * rate


def cmd_claim(_args) -> int:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    call("POST", "/claim", {"runId": run_id, "runUrl": f"{server}/{repo}/actions/runs/{run_id}"})
    print("Claimed.")
    return 0


def log_dir() -> str:
    """Where full command output is kept for the run artifact."""
    path = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "verity-logs")
    os.makedirs(path, exist_ok=True)
    return path


def run_command(step: dict) -> dict:
    """Execute one command step and report exactly what happened.

    Every part of this is about not losing the output. The full stream goes to a
    file that the workflow uploads with the run; the tail goes into the event so
    it is readable in Verity without leaving; and the event is marked truncated
    when the two differ. A command step whose output vanished is a top-severity
    defect in this product, not a rough edge — see PRODUCT.md on evidence.
    """
    step_id = step.get("stepId", "")
    command = step.get("run", "")
    timeout = int(step.get("timeoutSec") or 900)

    print(f"::group::{step_id}")
    print(f"+ {command}")

    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as expired:
        # A timeout is a real answer with real evidence: whatever the command
        # printed before it hung is usually the most useful thing in the run.
        partial = ""
        for chunk in (expired.stdout, expired.stderr):
            if isinstance(chunk, bytes):
                partial += chunk.decode("utf-8", errors="replace")
            elif chunk:
                partial += chunk
        output = partial + f"\n\nThis command was stopped after {timeout} seconds without finishing."
        exit_code = 124
    except Exception as error:  # noqa: BLE001 - the command could not be started at all
        output = f"This command could not be started: {error}"
        exit_code = 127

    print(output[-8000:])
    print("::endgroup::")

    path = os.path.join(log_dir(), f"{step_id.replace('/', '_').replace(':', '_')}.log")
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(output)
    except OSError as error:
        print(f"::warning::Could not write the full log for {step_id}: {error}")

    truncated = len(output) > EVENT_SUMMARY_LIMIT
    call(
        "POST",
        "/events",
        {
            "stepId": step_id,
            "kind": "command",
            "exitCode": exit_code,
            # The tail, not the head: a failure's cause is at the end of a log.
            "summary": output[-EVENT_SUMMARY_LIMIT:] if truncated else output,
            "truncated": truncated,
        },
    )
    return {"exitCode": exit_code, "truncated": truncated}


def cmd_step(args) -> int:
    """Ask for work and do it, until the next thing needs the workflow.

    The runner decides nothing here. It runs command steps itself — a shell is
    all they need — and hands back to the workflow the moment a model step comes
    up, because a model step runs through a composite action and an action
    cannot be invoked from inside a loop. So the workflow is a short line of
    identical slots, and this drains everything between them.
    """
    for _ in range(MAX_COMMANDS_PER_SLOT):
        step = call("GET", "/next")
        kind = step.get("kind", "")

        if kind == "command":
            run_command(step)
            # Deliberately not checked for success: whether a failed command
            # ends the job, gets handed to the model, or is retried is Verity's
            # decision, and asking again is how the runner finds out.
            continue

        if kind == "model":
            # No slot left to run it in.
            #
            # The workflow's model slots are a fixed list, and the verdict step
            # at the end calls this without `--write-prompt`. Reaching here
            # means the stage asked for more model calls than the workflow has
            # slots — and until this branch existed the runner printed a line,
            # returned 0, and let the workflow end green with the job still
            # live. The heartbeat stopped, the stall reaper settled it three
            # minutes later, and the person was told "we lost contact with it":
            # a wrong answer to a question the runner could answer exactly.
            if not args.write_prompt:
                message = (
                    f"This run needed another model step ({step.get('stepId')}) and the "
                    "workflow has no slot left to run it in. Nothing was lost — the work so "
                    "far is in the run log — but the repository's verity-agent.yml needs more "
                    "model slots for a stage this long."
                )
                print(f"::error::{message}")
                call(
                    "POST",
                    "/events",
                    {
                        "stepId": f"runner-no-slot:{os.environ.get('GITHUB_RUN_ID', '0')}",
                        "kind": "terminal",
                        "exitCode": 1,
                        "summary": message,
                    },
                )
                write_output({"kind": "abort", "finished": "true"})
                return 0

            with open(args.write_prompt, "w", encoding="utf-8") as handle:
                handle.write(step.get("prompt", ""))
            write_output(
                {
                    "kind": "model",
                    "step_id": step.get("stepId", ""),
                    "output_file": step.get("outputFile", ""),
                    "sandbox": step.get("sandbox", "read-only"),
                    "model": step.get("model") or "",
                }
            )
            print(f"Next step: model ({step.get('stepId')})")
            return 0

        if kind == "abort":
            write_output({"kind": "abort", "finished": "true"})
            # The reason is written for a person, by the planner. Surfacing it
            # here is the difference between a run that stopped and a run that
            # stopped for a reason you can act on.
            print(f"::warning::Verity stopped this run: {step.get('message', '')}")
            return 0

        write_output({"kind": kind or "done", "finished": "true"})
        print(f"Finished: {step.get('summary', kind)}")
        return 0

    # Reached only if the planner keeps issuing commands forever, which is a bug
    # in Verity rather than in this repository. Said out loud rather than
    # quietly returning, so it cannot be mistaken for an ordinary hand-back.
    print("::error::Verity issued more command steps in a row than this runner will run.")
    call(
        "POST",
        "/events",
        {
            "stepId": f"runner-drain:{os.environ.get('GITHUB_RUN_ID', '0')}",
            "kind": "note",
            "exitCode": 1,
            "summary": (
                f"The runner ran {MAX_COMMANDS_PER_SLOT} commands in a row without being asked "
                "for a model step, and stopped rather than continuing indefinitely."
            ),
        },
    )
    return 1


def _hit_the_time_limit() -> int | None:
    """Minutes the model step was given, if it was cancelled for using them all.

    GitHub cancels a step that exceeds `timeout-minutes` without saying so
    anywhere the workflow can read: the step is simply `failure`, identical to
    a crash. So the workspace step stamps a start time and the report step is
    told the cap, and the two together answer the question. Anything short of
    the cap is not a timeout and this returns None rather than guessing.
    """
    started_at = str(os.environ.get("MODEL_START_FILE") or "").strip()
    limit = str(os.environ.get("MODEL_TIMEOUT_MINUTES") or "").strip()
    if not started_at or not limit or not os.path.exists(started_at):
        return None
    try:
        with open(started_at, encoding="utf-8") as handle:
            began = int(handle.read().strip())
        minutes = int(limit)
    except (OSError, ValueError):
        return None
    if minutes <= 0:
        return None
    # A minute of slack: the stamp is written before the action starts, so the
    # measured span runs a little long, never short.
    return minutes if time.time() - began >= (minutes * 60) - 60 else None


# Multra's own typed refusal codes/context-status values, translated into the
# plain-English headline a person approving a plan can act on. Keyed on
# whichever of the two `_collect_errors` tagged, most specific first: a
# `context_status` (e.g. `repository_access_revoked`) names the actual cause
# behind the generic `required_code_context_unavailable` code, so it wins when
# both are present. Wording matches CONTEXT_REFUSAL_MESSAGES in
# backend/services/ai/multraCodeContext.ts so the same cause reads the same
# way whether it surfaced there or here.
_CONTEXT_STATUS_HEADLINES = {
    "repository_access_revoked": (
        "Multra no longer has read access to this repository, so the model step stopped "
        "instead of guessing at your code. Verity will try to re-establish access "
        "automatically before the next run."
    ),
    "graph_not_ready": (
        "Multra has not finished indexing this repository at the exact commit this run "
        "checked out, so the model step stopped instead of coding blind. This is usually "
        "temporary — retry once indexing catches up."
    ),
    "not_selected": (
        "Multra could not find code relevant to this task in the repository, so the model "
        "step stopped instead of guessing. Try naming the file or area to look at first."
    ),
    "needs_more_context": (
        "Multra could not find code relevant to this task in the repository, so the model "
        "step stopped instead of guessing. Try naming the file or area to look at first."
    ),
    # The one Codex's CLI actually leaves recoverable in practice: it flattens
    # Multra's error into one string before writing it anywhere this reads,
    # discarding the `context_status` that would have said which of the above
    # three this was. Honest about not knowing, rather than guessing one.
    "required_code_context_unavailable": (
        "Multra stopped this model step because it could not read your repository for this "
        "request — the graph may not be ready yet, it may not have found code relevant to the "
        "task, or access may have been revoked. Verity will try to reconnect before the next "
        "run; retrying after a few minutes often resolves a cold graph on its own."
    ),
}
_CONTEXT_TAG_PATTERN = re.compile(r"\[([a-z_]+)(?:/([a-z_]+))?\]\s*$")


def _context_status_headline(recorded: str) -> str | None:
    """A plain-English headline for a recognized Multra refusal, or None.

    Reads the `[code/status]` tag `_collect_errors` appended; adds nothing to
    `recorded` itself, so an unrecognized or absent tag falls through to the
    existing raw-evidence sentence exactly as before.

    This tag grammar is the other half of a two-language contract: Verity's
    own `backend/services/threads/runnerFailureClassifier.ts` reads this same
    `[code]` / `[code/status]` shape off the event summary this function's
    caller (`_failure_summary`) produces, to decide whether to re-register
    Multra repository delegation. Changing this pattern without updating that
    module (and its tests) reopens the gap `runnerFailureClassifier.ts`'s own
    docstring describes: a literal-string match on the TS side once missed
    exactly this function's combined `[code/status]` form.
    """
    match = _CONTEXT_TAG_PATTERN.search(recorded)
    if not match:
        return None
    code, status = match.group(1), match.group(2)
    return _CONTEXT_STATUS_HEADLINES.get(status) or _CONTEXT_STATUS_HEADLINES.get(code)


def _failure_summary(codex_home: str) -> str:
    """What to put on the run when the model step failed and wrote no output."""
    timed_out = _hit_the_time_limit()
    if timed_out is not None:
        return (
            f"The model step ran for its full {timed_out}-minute limit and was cut off before "
            "it finished, so any work it was part-way through was discarded. This is a time "
            "limit, not a crash: the same step may well succeed on a retry, and a step that "
            "keeps hitting it needs either a smaller slice of work or a larger limit "
            "(`timeout-minutes` on the model steps in .github/workflows/verity-agent.yml).\n\n"
            "What it had done up to that point is in the GitHub Actions log for this run."
        )

    recorded = extract_last_error(codex_home)
    if recorded:
        headline = _context_status_headline(recorded)
        prefix = f"{headline}\n\n" if headline else ""
        return (
            f"{prefix}The model step did not run to completion: {recorded}\n\n"
            "The full output is in the GitHub Actions log for this run."
        )
    return (
        "The model step did not run to completion. Its output is in the GitHub "
        "Actions log for this run — the step that failed is the one to read."
    )


def cmd_report_model_step(_args) -> int:
    step_id = _env("STEP_ID")
    outcome = str(os.environ.get("STEP_OUTCOME") or "").strip()
    output_file = str(os.environ.get("OUTPUT_FILE") or "").strip()

    summary = ""
    if output_file and os.path.exists(output_file):
        with open(output_file, encoding="utf-8", errors="replace") as handle:
            summary = handle.read()

    usage = extract_token_usage(os.environ.get("CODEX_HOME", ""))
    served_model = extract_served_model(os.environ.get("CODEX_HOME", ""))

    # The step's own outcome is read before its usage, and the order is the
    # whole of this fix.
    #
    # A model step that failed *before Codex ran* — a bad provider endpoint, a
    # missing key, a preflight that exits non-zero — has no session to read
    # usage from, so the missing-usage branch below used to catch it first and
    # report "Model step completed but its spend could not be measured". Every
    # word of that is wrong: the step did not complete, and metering is not what
    # went wrong. A person reading the run went looking at budgets while the
    # real cause sat in the Actions log four steps away.
    #
    # Missing usage is only the interesting anomaly when the step *succeeded*.
    if outcome != "success":
        call(
            "POST",
            "/events",
            {
                "stepId": step_id,
                "kind": "command",
                "exitCode": 1,
                "summary": (
                    summary[:100_000]
                    or _failure_summary(os.environ.get("CODEX_HOME", ""))
                ),
                "truncated": len(summary) > 100_000,
            },
        )
        print("::error::The model step failed; reported to Verity.")
        return 1

    if usage is None:
        # Loud, not silent. Verity rejects a model event with no usage, so
        # inventing a zero here would only move the lie one layer down.
        call(
            "POST",
            "/events",
            {
                "stepId": f"{step_id}:unmeasured",
                "kind": "note",
                "exitCode": 1,
                "summary": (
                    "The model step ran but its token usage could not be read from the Codex "
                    "session, so this run's spend cannot be counted against the project budget."
                ),
            },
        )
        print("::error::Could not measure model token usage; reporting the step as failed.")
        call(
            "POST",
            "/events",
            {
                "stepId": step_id,
                "kind": "command",
                "exitCode": 1,
                "summary": "Model step completed but its spend could not be measured.",
            },
        )
        return 1

    cost = price(usage["inputTokens"], os.environ.get("CODEX_INPUT_USD_PER_1M", "")) + price(
        usage["outputTokens"], os.environ.get("CODEX_OUTPUT_USD_PER_1M", "")
    )
    # Summaries are capped well under the 256KB event limit; the full document
    # travels as a workflow artifact, and the event says so.
    result = call(
        "POST",
        "/events",
        {
            "stepId": step_id,
            "kind": "model",
            "exitCode": 0 if outcome == "success" else 1,
            "summary": summary[:100_000],
            "truncated": len(summary) > 100_000,
            "usage": {
                "inputTokens": usage["inputTokens"],
                # How much of that input the provider served from its prompt
                # cache. Verity prices it at the cached rate; without this every
                # re-sent turn of a long agentic run is billed as if it were
                # read fresh, which on one staging run made $5.39 of a job that
                # had spent a fraction of it and stopped the run.
                "cachedInputTokens": usage.get("cachedInputTokens", 0),
                "outputTokens": usage["outputTokens"],
                "costUsd": round(cost, 6),
            },
            # Recorded, never required. See `extract_served_model`.
            **({"servedModel": served_model} if served_model else {}),
        },
    )
    print(f"Reported step {step_id} as seq {result.get('seq')}.")
    return 0


def cmd_report_abort(args) -> int:
    """The backstop event for a run that stopped without finishing.

    It runs on every unfinished exit, including the one where an earlier step
    has *already* told Verity exactly what went wrong. In that case the job is
    finished on Verity's side and this event is refused with a 409 — correct,
    and not a failure of this step: the run has its explanation, and turning
    the last line of the log red only points a reader away from it. So the
    "already finished" refusal is a no-op; every other refusal still fails.
    """
    try:
        call(
            "POST",
            "/events",
            {
                "stepId": f"runner-abort:{os.environ.get('GITHUB_RUN_ID', '0')}",
                "kind": "terminal",
                "exitCode": 1,
                "summary": args.message,
            },
        )
    except SystemExit as refusal:
        detail = str(refusal)
        if "409" in detail and "does not accept new events" in detail:
            print("::notice::The run was already reported as finished; nothing more to send.")
            return 0
        raise
    return 0


def cmd_heartbeat_loop(_args) -> int:
    while True:
        try:
            call("POST", "/heartbeat", {})
        except SystemExit:
            # A dead heartbeat must never fail the run it is reporting on. The
            # reaper handles the silence.
            pass
        time.sleep(HEARTBEAT_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verity runner client")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("claim").set_defaults(func=cmd_claim)

    step_parser = sub.add_parser("step")
    step_parser.add_argument("--write-prompt", default="")
    step_parser.set_defaults(func=cmd_step)

    sub.add_parser("report-model-step").set_defaults(func=cmd_report_model_step)

    abort_parser = sub.add_parser("report-abort")
    abort_parser.add_argument("--message", required=True)
    abort_parser.set_defaults(func=cmd_report_abort)

    sub.add_parser("heartbeat-loop").set_defaults(func=cmd_heartbeat_loop)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
