#!/usr/bin/env bash
#
# Resolve and run one named stage of work in this repository.
#
# Which command runs the tests is knowledge that belongs here, in the checkout —
# `.verity/config.yml`, a package script, a Makefile target — and not in
# Verity's database, where it would go stale the moment someone renames a
# script. Verity's planner names the stage; this file decides what that means
# for this repository.
#
# The exit codes are the contract:
#
#   0   the stage ran and passed
#   78  there was nothing for this stage to do, so nothing ran
#   *   the stage ran and failed
#
# 78 exists because "no tests configured" and "tests passed" are different
# answers and the old workflows reported both as a green check. Verity records
# 78 as *skipped, with the reason on screen* — never as a pass.
#
# `commit-branch` uses the same code for "the agent changed nothing", which is
# the same shape of answer: there was work to do and nothing to do it to.
# Verity stops the repair loop there rather than re-running an unchanged tree.

set -uo pipefail

STAGE="${1:-}"
if [ -z "$STAGE" ]; then
  echo "usage: verity_stage.sh <install|test|build|e2e|verify-changed|diff|checkout-branch|commit-branch|push-branch>" >&2
  exit 2
fi

NOT_CONFIGURED=78

# One command list out of `.verity/config.yml`, if the file and the key exist.
# Python rather than a YAML tool because Python is already installed for the
# runner client and a second dependency is a second thing that can be missing.
config_commands() {
  local key="$1"
  [ -f .verity/config.yml ] || return 0
  python3 .github/verity/verity_config.py "$key" 2>/dev/null || true
}

# A package.json script, if one by this name exists.
package_script() {
  local name="$1"
  [ -f package.json ] || return 1
  python3 - "$name" <<'PY'
import json, sys
try:
    scripts = json.load(open("package.json", encoding="utf-8")).get("scripts") or {}
except Exception:
    sys.exit(1)
sys.exit(0 if sys.argv[1] in scripts else 1)
PY
}

# npm unless a different lockfile says otherwise. Running `npm ci` in a pnpm
# repository does not fail cleanly — it rewrites the lockfile — so this is
# checked before anything installs.
package_manager() {
  if [ -f pnpm-lock.yaml ]; then echo pnpm
  elif [ -f yarn.lock ]; then echo yarn
  elif [ -f package-lock.json ]; then echo npm
  elif [ -f package.json ]; then echo npm
  else echo none
  fi
}

# Verity's own run artifacts, which git must never see.
#
# Model steps write their output into the workspace — they have to, because the
# agent's sandbox may only write there — so these files sit in the checkout
# beside the change. Nothing else distinguishes them from the agent's work, and
# that cost us twice:
#
#   * `push-branch` committed them, so Verity pushed its own scratch files into
#     customers' repositories, where they landed on the default branch.
#   * once there, `verify-changed` — the gate whose whole job is stopping an
#     empty pull request — passed on a run whose only "change" was Verity
#     rewriting its own output file. A request that implemented nothing was
#     reported as succeeded, with a pull request to prove it.
#
# Defined once and used by every command below that touches git. A second copy
# is a second thing to forget, which is exactly how the first two got through.
verity_scratch() {
  printf '%s\n' 'verity-*.md'
}

# Files that are a tool's output, not a change.
#
# A model exploring a repository writes its search results somewhere, and
# everything left in the tree is committed. One feature pull request came back
# +1492 lines across four files, of which 1451 were `leads_search.json`,
# `export_search_results.json` and `export_csv_search.json` — ripgrep's own JSON
# stream, dumped to disk and never cleaned up. The feature itself was 41 lines.
# A reviewer then has to work out which of the four files is the change.
#
# Asking the model to tidy up does not work; it was asked, and did not. This is
# a signature rather than a guess: ripgrep's stream is a JSONL of objects whose
# first key is `type` with a value of `begin`, `match`, `context` or `end`, and
# no source file, fixture or config looks like that. Anything else a run leaves
# behind is committed as before — the rule is narrow on purpose, because
# deleting a file someone meant to add is worse than shipping one they didn't.
is_tool_dump() {
  local file="$1"
  case "$file" in *.json|*.jsonl|*.txt|*.log) ;; *) return 1 ;; esac
  [ -f "$file" ] || return 1
  head -c 200 -- "$file" 2>/dev/null | grep -qE '^\{"type":"(begin|match|context|end)","data":' || return 1
  return 0
}

# The working tree's changes, Verity's own files excluded.
repo_changes() {
  git status --porcelain -- . ':(exclude)verity-*.md'
}

run_all() {
  local status=0
  while IFS= read -r command; do
    [ -z "$command" ] && continue
    echo "+ $command"
    bash -lc "$command" || { status=$?; break; }
  done
  return $status
}

configured="$(config_commands "$STAGE")"
if [ -n "$configured" ]; then
  printf '%s\n' "$configured" | run_all
  exit $?
fi

case "$STAGE" in
  install)
    case "$(package_manager)" in
      pnpm) corepack enable >/dev/null 2>&1 || true; pnpm install --frozen-lockfile ;;
      yarn) corepack enable >/dev/null 2>&1 || true; yarn install --frozen-lockfile ;;
      npm)  if [ -f package-lock.json ]; then npm ci; else npm install; fi ;;
      *)
        if [ -f requirements.txt ]; then python3 -m pip install -r requirements.txt
        elif [ -f pyproject.toml ]; then python3 -m pip install -e .
        elif [ -f go.mod ]; then go mod download
        else exit $NOT_CONFIGURED
        fi
        ;;
    esac
    ;;

  e2e)
    # Browser tests only where the repository says how to run them.
    #
    # No fallback to `npx playwright test` or anything like it, deliberately:
    # a guessed command that starts no server fails for a reason that has
    # nothing to do with the change, and a guessed command that finds no specs
    # exits zero and reports a browser suite that never ran. Either is worse
    # than saying we did not run one. `.verity/config.yml`'s `e2e:` list is the
    # place to say how, and it can start servers before it runs anything.
    if package_script e2e; then
      case "$(package_manager)" in
        pnpm) pnpm run e2e ;;
        yarn) yarn e2e ;;
        *)    npm run e2e ;;
      esac
    elif package_script "test:e2e"; then
      case "$(package_manager)" in
        pnpm) pnpm run test:e2e ;;
        yarn) yarn test:e2e ;;
        *)    npm run test:e2e ;;
      esac
    else
      echo "No end-to-end test command is configured for this repository." >&2
      exit $NOT_CONFIGURED
    fi
    ;;

  test|build)
    if package_script "$STAGE"; then
      case "$(package_manager)" in
        pnpm) pnpm run "$STAGE" ;;
        yarn) yarn "$STAGE" ;;
        *)    npm run "$STAGE" --if-present ;;
      esac
    elif [ "$STAGE" = "test" ] && [ -f pytest.ini -o -d tests ] && command -v pytest >/dev/null 2>&1; then
      pytest
    elif [ -f Makefile ] && grep -qE "^${STAGE}:" Makefile; then
      make "$STAGE"
    elif [ -f go.mod ]; then
      go "$STAGE" ./...
    else
      # Nothing to run. Said out loud, because a silent zero here is the exact
      # green check over an empty suite that this whole design exists to remove.
      echo "No ${STAGE} command is configured for this repository." >&2
      exit $NOT_CONFIGURED
    fi
    ;;

  verify-changed)
    # The gate that stops an empty pull request being reported as a feature.
    #
    # Verity's own run artifacts are excluded, or the gate passes on this
    # runtime rewriting its own output file — which is how a request that
    # implemented nothing came back green with a pull request attached.
    if [ -z "$(repo_changes)" ]; then
      # Distinguish "decided to change nothing" from "could not write".
      #
      # They look identical from the tree and they are not the same problem.
      # `*** End Patch` in the agent's own output means it produced the whole
      # change and something stopped it reaching the filesystem — the v2 dev
      # cycle caught the same thing and said so, and this runtime lost that
      # check when it replaced the workflow.
      #
      # The usual cause is the write permission: `sandbox: workspace-write`
      # grants nothing on its own, and without `permission-profile: verity-ci`
      # (or on a runner with no bubblewrap) the model has no way to edit files
      # and writes the patch out as text instead.
      if ls verity-*.md >/dev/null 2>&1 && grep -qs -- '\*\*\* End Patch' verity-*.md; then
        echo "The agent wrote a complete patch but it never reached the working tree." >&2
        echo "Its output contains an apply_patch block, so it produced the change and could" >&2
        echo "not apply it. Check the run log for 'CODEX_PERMISSION_PROFILE' being empty or" >&2
        echo "'could not find bubblewrap' — the model step needs a profile that grants" >&2
        echo "workspace writes, not just sandbox: workspace-write." >&2
        exit 1
      fi
      echo "The working tree is unchanged: the agent made no edits." >&2
      exit 1
    fi
    # The tree changed - but changed how?
    #
    # A model that cannot edit files writes the diff out as text. The check
    # above catches that when the text stays in the agent's own output. It does
    # not catch the same failure landing *inside* the checkout: on staging a run
    # came back green with a pull request containing exactly one new file,
    # `backend/src/http/routes/leads.ts.patch` — 51 lines of unified diff sitting
    # beside the source file it was meant to modify. The code in it was
    # reasonable. Nothing imports a `.patch`, so merging it would have shipped
    # nothing, and the requirement would have read as delivered.
    #
    # `git status` cannot tell the difference: one file was added, so the tree
    # changed. This can, because a change made only of patch artefacts is never
    # an implementation.
    if [ -z "$(git status --porcelain -- . ':(exclude)verity-*.md' ':(exclude)*.patch' ':(exclude)*.diff' ':(exclude)*.rej' ':(exclude)*.orig')" ]; then
      echo "The only changes are patch files, not edits:" >&2
      git status --porcelain -- . ':(exclude)verity-*.md' >&2
      echo "" >&2
      echo "A .patch or .diff beside the source is the change written out as text, not" >&2
      echo "applied to it. Nothing reads those files, so a pull request built from this" >&2
      echo "would claim a feature and contain none. Apply the patch to the file it names" >&2
      echo "(apply_patch, or edit the file directly) and leave no .patch behind." >&2
      exit 1
    fi
    git --no-pager diff --stat -- . ':(exclude)verity-*.md'
    ;;

  diff)
    base="${VERITY_BASE_REF:-origin/${GITHUB_BASE_REF:-main}}"
    git --no-pager diff "$base"...HEAD 2>/dev/null || git --no-pager diff HEAD
    ;;

  checkout-branch)
    # Move onto an existing branch this thread already pushed.
    #
    # `workflow_dispatch` only accepts a branch or tag as its `ref`, and Verity
    # dispatches on the default branch, so a stage that works on the thread's
    # own branch has to get there itself. Fetched explicitly rather than relying
    # on the checkout's refspec, which for a dispatch run covers only the ref it
    # was started on.
    branch="${VERITY_BRANCH:-}"
    if [ -z "$branch" ]; then
      echo "No branch was given to check out." >&2
      exit 2
    fi
    pinned="${VERITY_EXPECTED_COMMIT:-}"
    if [ -n "$pinned" ]; then
      [[ "$pinned" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]] || { echo "Invalid prepared commit" >&2; exit 2; }
      git fetch origin "$pinned"
      git checkout -B "$branch" "$pinned"
    else
      git fetch origin "$branch"
      git checkout -B "$branch" "origin/$branch"
    fi
    git --no-pager log --oneline -1
    ;;

  commit-branch)
    # Add this stage's work to a branch that already exists.
    #
    # Onto the same branch, so tests and repairs land in the pull request they
    # belong to rather than opening a second one beside it. Verity's own
    # scratch files are excluded: they are the run's evidence, uploaded as an
    # artifact, and committing them into the customer's repository would put
    # prompts and model output in their history.
    branch="${VERITY_BRANCH:-}"
    if [ -z "$branch" ]; then
      echo "No branch was given to commit to." >&2
      exit 2
    fi
    git config user.name "verity-agent[bot]"
    git config user.email "verity-agent[bot]@users.noreply.github.com"
    git add -A
    git reset -- $(verity_scratch) 2>/dev/null || true
    # Search output the model left behind, dropped before the commit rather
    # than deleted: the file stays on the runner for the log, and simply does
    # not become part of the change.
    while IFS= read -r candidate; do
      [ -n "$candidate" ] || continue
      if is_tool_dump "$candidate"; then
        echo "Leaving out $candidate: it is tool output, not part of the change." >&2
        git reset -- "$candidate" >/dev/null 2>&1 || true
      fi
    done <<< "$(git diff --cached --name-only --diff-filter=A)"
    if git diff --cached --quiet; then
      # Nothing was changed. Said with 78 so Verity stops rather than running
      # the same suite against the same tree again.
      echo "Nothing was changed, so there is nothing to commit." >&2
      exit $NOT_CONFIGURED
    fi
    git commit -m "${VERITY_COMMIT_MESSAGE:-Verity: update this change}"
    git push origin "HEAD:${branch}"
    ;;

  push-branch)
    # Push only. The pull request is opened by Verity with its own installation
    # token, because a PR opened by GITHUB_TOKEN needs "Allow GitHub Actions to
    # create and approve pull requests" — off by default, and neither readable
    # nor settable by Verity. This is the same rescue pattern the older
    # workflows already use, and it is why a failed PR loses no work: the branch
    # is already on the remote.
    branch="${VERITY_BRANCH:-verity/agent-${VERITY_JOB_ID:-run}}"
    git config user.name "verity-agent[bot]"
    git config user.email "verity-agent[bot]@users.noreply.github.com"
    git checkout -b "$branch"
    git add -A
    # Verity's own scratch files are not the customer's change. Without this the
    # branch carried `verity-implement-output.md` and `verity-pr-body.md` into
    # their repository, and every later run then saw those as work.
    git reset -- $(verity_scratch) 2>/dev/null || true
    git commit -m "${VERITY_COMMIT_MESSAGE:-Verity: apply requested change}"
    git push --set-upstream origin "$branch"
    echo "$branch"
    ;;

  *)
    echo "Unknown stage: $STAGE" >&2
    exit 2
    ;;
esac
