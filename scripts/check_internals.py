#!/usr/bin/env python3
"""Refuse to publish the platform's internals from this public repository.

This repo is public. The platform it speaks to is not. Everything here is
written by reading that platform, and the reading leaves fingerprints: file
names, function names, quoted source comments. OPL-4613 scrubbed forty-one of
them from the TypeScript client after it went public, OPL-4617 caught two the
scrub missed, and OPL-4635 found twenty-eight more that had never been swept out
of THIS one — eighteen of them in ``src/``, which ships: the docstrings reach
PyPI in the wheel and the sdist.

Every one of those arrived the same way, in a comment explaining a client
decision by naming the platform code behind it. So this is a check rather than
another scrub. A scrub is a date; a check is a floor.

WHAT IS FORBIDDEN, and it is deliberately narrow — this is not a secret scanner:

* platform source paths: ``server/<file>.go``, ``web/lib/<file>.ts``
* platform identifiers: the internal function and constant names below
* nothing else. Prose about what the platform DOES is the point of this client's
  comments and must stay.

Names this library exports itself are deliberately NOT on the list, however
faithfully they mirror a platform constant: ``REPLAY_WINDOW_S`` is this package's
own public name, and a check that cannot be satisfied without renaming the public
API is a check that gets deleted. What it catches there is the platform's file,
which is the part a reader cannot already see.

WHAT TO WRITE INSTEAD. The caller-facing consequence, which is what a reader can
act on: "the platform reads an empty expectation as no expectation" says
everything "``checkExpectation`` in server/vm.go reads..." said, to a reader who
is holding this library rather than casing the platform.

WHAT IS ALLOWED THROUGH, and why. ``scripts/check_surface.py`` and the tests
around it name ``web/lib/surface.ts`` and ``web/lib/apidoc.ts`` because those are
the two files it READS; a tool that self-evidently reads a private repo is the
weakest thing on this list, and it already refuses to run against the platform in
public CI. Those paths are allowlisted by FILE, not by pattern, so a new
reference anywhere else still fails.

Run it over the working tree (``python3 scripts/check_internals.py``) or over a
range of commit messages (``--messages origin/main..HEAD``), which is the half a
file scan cannot see: a commit message is public the moment it is pushed.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Files that may name the two platform paths they read. Everything else may not.
ALLOWED_FILES = {
    "scripts/check_surface.py",
    "scripts/check_internals.py",
    "tests/test_surface.py",
    "tests/test_check_surface.py",
    "tests/surface_tables.py",
}

# Scanned in full: docstrings in src/ ship in the package, tests and the README
# are read on GitHub by anyone.
SCANNED = ("src", "tests", "scripts", "README.md")

# One name per line, so adding the next one is a one-line diff and the reason for
# each stays visible in `git blame`.
# One name per line, so adding the next one is a one-line diff and the reason
# for each stays visible in `git blame`.
_INTERNAL_NAMES = [
    "describeVM",
    "describeRow",
    "reserveBuild(?:Once)?",
    "CloneFromSnapshot",
    "holdHostRAM(?:Locked|Evicting)?",
    "runningRAMLocked",
    "startLocked",
    "buildErr",
    "publicComputer",
    "sessionComputer",
    "checkExpectation",
    "applyWindowGeom",
    "emitFile",
    "admitStartLocked",
    "hostUsageLocked",
    "resumeIfSuspended",
    "writeUseErr",
    "actionStatusDef",
    "clipboardWriteMax",
    "execMaxTimeoutSec",
    "execMaxEnvLen",
    "execMaxEnv",
    "MAX_MAX_STEPS",
]

_IDENTIFIERS = r"\b(?:{})\b".format("|".join(_INTERNAL_NAMES))

PATTERNS: list[tuple[str, str]] = [
    (r"\bserver/[a-z_]+\.go\b", "names a platform source file"),
    (r"\bweb/lib/[a-z_]+\.ts\b", "names a platform source file"),
    (_IDENTIFIERS, "names a platform identifier"),
]


def scan_text(text: str, label: str, allow: bool) -> list[str]:
    if allow:
        return []
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for pattern, why in PATTERNS:
            for hit in re.findall(pattern, line):
                out.append(f"{label}:{lineno}: {why}: {hit}")
    return out


def scan_files() -> list[str]:
    problems: list[str] = []
    for entry in SCANNED:
        path = ROOT / entry
        if path.is_file():
            files = [path]
        elif path.is_dir():
            files = [
                p for p in path.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".toml"}
            ]
        else:
            continue
        for f in sorted(files):
            rel = f.relative_to(ROOT).as_posix()
            problems += scan_text(
                f.read_text(encoding="utf-8", errors="replace"), rel, rel in ALLOWED_FILES
            )
    return problems


def scan_messages(rev_range: str) -> list[str]:
    log = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x00", rev_range],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    problems: list[str] = []
    chunks = log.split("\x00")
    for i in range(0, len(chunks) - 1, 2):
        sha, message = chunks[i].strip(), chunks[i + 1]
        if not sha:
            continue
        problems += scan_text(message, f"commit {sha[:9]}", allow=False)
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--messages", metavar="RANGE", help="also scan commit messages in RANGE")
    args = ap.parse_args()

    problems = scan_files()
    if args.messages:
        problems += scan_messages(args.messages)

    if not problems:
        print("check-internals — nothing of the platform's is being published from here.")
        return 0

    print("check-internals — the platform's internals are not this repo's to publish:\n")
    for p in problems:
        print(f"  {p}")
    print(
        "\n  Say what the platform DOES, not which of its files does it. The behaviour is\n"
        "  what a reader of this library can act on; the file name is only useful to\n"
        "  somebody casing the platform. See this script's docstring for the rule."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
