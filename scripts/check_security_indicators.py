#!/usr/bin/env python3
"""Fail when known incident indicators are present in a checkout."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


SKIP_DIRS = {
    ".git",
    ".aws-sam",
    ".codex-sam-appdata",
    ".incident-evidence",
    ".incident-worktrees",
    ".sam-appdata",
    ".tmp",
    ".tmp-appdata",
    ".tmp-sam-appdata",
    ".verify-appdata",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "playwright-report",
    "test-results",
}

SKIP_FILES = {
    "backend/cfn-events.json",
    "scripts/check_security_indicators.py",
    "backend/verity_templates/bootstrap/v2/scripts/check_security_indicators.py",
}

TEXT_SUFFIXES = {
    ".cjs",
    ".cts",
    ".js",
    ".json",
    ".jsx",
    ".mjs",
    ".mts",
    ".py",
    ".sh",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}

TEXT_FILENAMES = {
    ".gitignore",
    "AGENTS.md",
    "Dockerfile",
    "next.config.js",
    "next.config.mjs",
    "next.config.cjs",
    "package.json",
    "postcss.config.mjs",
    "tailwind.config.js",
    "eslint.config.js",
    "eslint.config.mjs",
}

CONFIG_FILENAMES = {
    "next.config.js",
    "next.config.mjs",
    "next.config.cjs",
    "postcss.config.mjs",
    "tailwind.config.js",
    "eslint.config.js",
    "eslint.config.mjs",
}

INDICATORS = [
    ("obfuscated_global_marker", "global" + "['!']"),
    ("folder_open_task", "runOn" + ": folderOpen"),
    ("folder_open_task_json", '"runOn"' + ': "folderOpen"'),
    ("automatic_vscode_tasks", "task" + ".allowAutomaticTasks"),
    ("bun_download_loader", "oven-sh" + "/bun/releases/download"),
    ("known_loader_ip", "23" + ".27.13.43"),
    ("oast_exfil_domain", "oast" + ".fun"),
    ("python_exec_urlopen_loader", "exec" + "(urlopen"),
    ("config_create_require_loader", "createRequire" + "(import.meta.url)"),
]

DISALLOWED_PATHS = {
    ".claude/index.js": "agent_hook_loader",
    ".claude/setup.mjs": "agent_hook_setup",
    ".vscode/setup.mjs": "vscode_setup_loader",
}


def to_posix(path: Path) -> str:
    return path.as_posix()


def should_skip_dir(path: Path, root: Path) -> bool:
    rel = to_posix(path.relative_to(root))
    return path.name in SKIP_DIRS or rel in SKIP_DIRS


def is_text_candidate(path: Path) -> bool:
    return path.name in TEXT_FILENAMES or path.suffix in TEXT_SUFFIXES


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def scan(root: Path) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            name for name in dirnames
            if not should_skip_dir(current / name, root)
        ]

        for filename in filenames:
            path = current / filename
            rel = to_posix(path.relative_to(root))
            if rel in SKIP_FILES:
                continue

            normalized_rel = rel.lstrip("./")
            if normalized_rel in DISALLOWED_PATHS:
                findings.append((rel, DISALLOWED_PATHS[normalized_rel]))
                continue

            if normalized_rel.endswith(".claude/settings.json"):
                text = read_text(path)
                if "SessionStart" in text or "hooks" in text:
                    findings.append((rel, "claude_session_hook"))
                continue

            if normalized_rel.endswith("public/fonts/fa-solid-400.woff2"):
                try:
                    header = path.read_bytes()[:8]
                except OSError:
                    header = b""
                if header[:4] != b"wOF2":
                    findings.append((rel, "fake_woff2_payload"))
                continue

            if not is_text_candidate(path):
                continue

            text = read_text(path)
            for name, needle in INDICATORS:
                if needle in text:
                    if name == "config_create_require_loader":
                        if path.name not in CONFIG_FILENAMES and "global" + "['!']" not in text:
                            continue
                    findings.append((rel, name))

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan for known incident indicators.")
    parser.add_argument("--root", default=".", help="checkout root to scan")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    findings = scan(root)
    if not findings:
        print("No known incident indicators found.")
        return 0

    print("Known incident indicators found:")
    for rel, name in findings:
        print(f"- {rel}: {name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
