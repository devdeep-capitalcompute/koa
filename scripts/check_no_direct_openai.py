#!/usr/bin/env python3
"""Regression guard for the direct Codex CLI -> Multra architecture."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BAD_SUBSTRINGS = ["import openai", "from openai", "api.openai.com"]
REQUIRED_WORKFLOWS = [
    "codex-dev-cycle.yml", "codex-pr-review.yml", "codex-test-generation.yml",
    "codex-usecase-generation.yml", "codex-test-to-issue.yml", "codex-deploy.yml",
    "verity-builder-plan.yml", "verity-pr-auto-fix.yml", "verity-validation.yml",
    "verity-auto-docs.yml", "verity-repo-context-builder.yml", "verity-guardrails.yml",
    "verity-monitor.yml", "verity-command-router.yml",
]
PLACEHOLDERS = ["__VERITY_CALLBACK_URL__", "__VERITY_PROJECT_ID__", "__BOOTSTRAP_VERSION__"]
SKIP_DIRS = {".git", "node_modules", "dist", "build", "coverage", ".venv", ".aws-sam", "test-results", "__pycache__"}
DIRECT_ACTION = "./.github/actions/verity-codex-direct"
OLD_ACTION = "./.github/actions/verity-codex"


def files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith((".cache", ".next", ".turbo"))]
        base = Path(dirpath)
        for name in filenames:
            yield base / name


def check_action(root: Path) -> list[str]:
    errors: list[str] = []
    new = root / ".github" / "actions" / "verity-codex-direct" / "action.yml"
    old = root / ".github" / "actions" / "verity-codex" / "action.yml"
    if old.exists():
        errors.append("retired .github/actions/verity-codex/action.yml still exists")
    if not new.exists():
        return errors + ["missing .github/actions/verity-codex-direct/action.yml"]
    text = new.read_text(encoding="utf-8", errors="ignore")
    for token in [
        "cmd=(\"$codex_bin\" exec", "model_provider = \"multra\"", "env_key = \"BLUESKY_API_KEY\"",
        "wire_api = \"responses\"", "repository_full_name", "commit_sha", "context_handle",
        "code_context_required", "X-Multra-Code-Context-Required", "wait_multra_context_ready.py",
        "probe_multra_context.py", "restore_git_auth", "git_auth_file",
    ]:
        if token not in text:
            errors.append(f"{new.relative_to(root)} missing direct-Multra token: {token}")
    for token in ["openai/codex-action@", "codex-responses-api-proxy"]:
        if token in text:
            errors.append(f"{new.relative_to(root)} still contains retired proxy/action token: {token}")
    return errors


def check_workflows(root: Path, directory: Path) -> list[str]:
    errors: list[str] = []
    if not directory.exists():
        return []
    direct_re = re.compile(r"^\s*(?:-\s*)?uses:\s*\./\.github/actions/verity-codex-direct\s*$", re.M)
    old_re = re.compile(r"^\s*(?:-\s*)?uses:\s*\./\.github/actions/verity-codex\s*$", re.M)
    upstream_old_re = re.compile(r"^\s*(?:-\s*)?uses:\s*openai/codex-action@", re.M)
    key_re = re.compile(r"^\s*multra-api-key:\s*\$\{\{\s*secrets\.MULTRA_API_KEY\s*\|\|\s*secrets\.BLUESKY_API_KEY\s*\}\}\s*$", re.M)
    for path in sorted(directory.glob("*.yml")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(root)
        if upstream_old_re.search(text):
            errors.append(f"{rel} still invokes openai/codex-action")
        for token in ["codex-responses-api-proxy", "responses-api-endpoint:", "safety-strategy:"]:
            if token in text:
                errors.append(f"{rel} contains retired Codex token: {token}")
        if old_re.search(text):
            errors.append(f"{rel} still invokes {OLD_ACTION}")
        if "OPENAI_API_KEY" in text:
            errors.append(f"{rel} references OPENAI_API_KEY")
        if re.search(r"^\s*codex-args:\s*", text, re.M):
            errors.append(f"{rel} still passes codex-args; translate them to explicit direct-runner inputs")
        calls = len(direct_re.findall(text))
        keys = len(key_re.findall(text))
        if calls and keys < calls:
            errors.append(f"{rel} has {calls} direct Codex call(s) but only {keys} Multra key binding(s)")
    return errors


def check_mirrors(root: Path) -> list[str]:
    # Mirrors belong to the Verity template factory, not installed customer repos.
    # A present but incomplete factory must still fail the individual pair checks.
    if not (root / "backend/verity_templates/bootstrap").exists():
        return []
    errors: list[str] = []
    pairs = [
        (root / ".github/actions/verity-codex-direct/action.yml", root / "backend/verity_templates/bootstrap/v2/.github/actions/verity-codex-direct/action.yml"),
        (root / "scripts/wait_multra_context_ready.py", root / "backend/verity_templates/bootstrap/v2/scripts/wait_multra_context_ready.py"),
        (root / "scripts/probe_multra_context.py", root / "backend/verity_templates/bootstrap/v2/scripts/probe_multra_context.py"),
        (root / "scripts/check_no_direct_openai.py", root / "backend/verity_templates/bootstrap/v2/scripts/check_no_direct_openai.py"),
    ]
    for source, mirror in pairs:
        if not source.exists() or not mirror.exists():
            errors.append(f"missing bootstrap mirror for {source.relative_to(root)}")
        elif source.read_bytes() != mirror.read_bytes():
            errors.append(f"bootstrap mirror drift: {mirror.relative_to(root)}")
    return errors


def check_provider_boundary(root: Path) -> list[str]:
    errors: list[str] = []
    patterns = [
        (re.compile(r"(?:from\s+['\"]openai['\"]|require\(['\"]openai['\"]\)|import\s+OpenAI\s+from\s+['\"]openai['\"])", re.I), "direct OpenAI SDK import"),
        (re.compile(r"process\.env\.OPENAI_API_KEY"), "OPENAI_API_KEY credential access"),
        (re.compile(r"api\.openai\.com", re.I), "api.openai.com endpoint"),
    ]
    for base in [root / "backend/services/ai", root / "backend/services/utils"]:
        if not base.exists():
            continue
        for path in base.rglob("*.ts"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern, label in patterns:
                if pattern.search(text):
                    errors.append(f"{path.relative_to(root)} contains {label}; product model traffic must use Multra")
    return errors


def main() -> int:
    root = Path(".").resolve()
    errors: list[str] = []
    workflow_dir = root / ".github/workflows"
    for name in REQUIRED_WORKFLOWS:
        if not (workflow_dir / name).exists():
            errors.append(f"missing required workflow .github/workflows/{name}")

    bootstrap = root / ".verity/bootstrap.json"
    config = root / ".verity/config.yml"
    template_repo = bootstrap.exists() and ("__INSTALLED_FILES__" in bootstrap.read_text(encoding="utf-8", errors="ignore") or "__VERITY_PROJECT_ID__" in bootstrap.read_text(encoding="utf-8", errors="ignore"))
    if config.exists() and not template_repo:
        text = config.read_text(encoding="utf-8", errors="ignore")
        for token in PLACEHOLDERS:
            if token in text:
                errors.append(f"{config.relative_to(root)} still contains {token}")

    errors += check_action(root)
    errors += check_workflows(root, workflow_dir)
    errors += check_workflows(root, root / "backend/verity_templates/bootstrap/v2/.github/workflows")
    errors += check_workflows(root, root / "backend/verity_templates/bootstrap/v3/.github/workflows")
    errors += check_mirrors(root)
    errors += check_provider_boundary(root)

    allow_prefixes = (
        ".github/", ".claude/", "docs/", "backend/services/bootstrapTemplatePacks.ts",
        "backend/services/bootstrap/bootstrapTemplatePacks.ts", "backend/services/ai/", "backend/services/utils/aiClient.ts",
    )
    for path in files(root):
        rel = str(path.relative_to(root)).replace("\\", "/")
        # This existing companion scanner declares the same forbidden strings;
        # declarations in a scanner are not a direct provider call.
        if rel.endswith("scripts/check_no_direct_openai.py") or rel == "scripts/check_no_direct_ai_secrets.py" or rel.startswith(allow_prefixes):
            continue
        try:
            if path.stat().st_size > 1_000_000 or path.suffix.lower() in {".png", ".jpg", ".pdf", ".zip", ".mp4", ".woff2", ".pyc"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for token in BAD_SUBSTRINGS:
            if token in text:
                errors.append(f"{rel} contains direct OpenAI usage: {token}")

    if errors:
        print("Direct Codex -> Multra architecture violations:", file=sys.stderr)
        for error in errors:
            print(f" - {error}", file=sys.stderr)
        return 1
    print("Direct Codex -> Multra contract verified; retired proxy/action path is absent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
