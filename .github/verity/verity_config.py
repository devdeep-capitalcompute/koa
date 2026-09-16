#!/usr/bin/env python3
"""Read one command list out of `.verity/config.yml`.

Its own file rather than a heredoc inside `verity_stage.sh`: a nested heredoc
is fragile to edit, and this is the piece most likely to need changing as more
repositories are onboarded.

A deliberately small reader — `commands:`, then `  <key>:`, then a scalar, an
inline list, or a block list. Anything more elaborate is not something to guess
at, because guessing wrong runs the wrong command against someone's repository.

**`test: []` is why this exists.** An empty inline list means *nothing is
configured*. Reading it as the literal string "[]" made the runner execute `[]`
as a shell command, which exits 127 — and the planner reads a non-zero exit as
the tests *failing*, not as absent. So a repository with no test command got a
model call spent on "fixing" a suite that does not exist, and a red gate on a
change that was fine. Absent and broken are different answers, and only the
exit code distinguishes them: nothing printed here means the caller exits 78.
"""

import re
import sys


def entries(raw: str) -> list[str]:
    """One YAML value as a list of commands. Empty means nothing configured."""
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return []
    if raw.startswith("["):
        inner = raw[1:-1] if raw.endswith("]") else raw[1:]
        return [part.strip().strip("'\"") for part in inner.split(",") if part.strip()]
    return [raw.strip("'\"")]


def read(text: str, key: str) -> list[str]:
    found: list[str] = []
    in_commands = False
    in_key = False

    for line in text.splitlines():
        if re.match(r"^commands:\s*$", line):
            in_commands, in_key = True, False
            continue
        # A new top-level key ends the commands block.
        if in_commands and re.match(r"^\S", line):
            break
        if not in_commands:
            continue

        match = re.match(r"^\s{1,4}([A-Za-z0-9_-]+):\s*(.*)$", line)
        if match:
            in_key = match.group(1) == key
            if in_key:
                found += entries(match.group(2))
                # A scalar or inline list is the whole value. Only a bare
                # `key:` opens a block list worth reading `- ` lines for.
                if match.group(2).strip():
                    in_key = False
            continue

        match = re.match(r"^\s*-\s*(.+)$", line)
        if match and in_key:
            found += entries(match.group(1))

    return [command for command in found if command]


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    try:
        text = open(".verity/config.yml", encoding="utf-8").read()
    except OSError:
        return 0
    for command in read(text, sys.argv[1]):
        print(command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
