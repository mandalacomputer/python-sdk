#!/usr/bin/env python3
"""Refuse to publish the platform's internals from this public repository.

This repo is public. The platform it speaks to is not. Everything here is
written by reading that platform, and the reading leaves fingerprints — file
names, internal function names, quoted source comments — in comments that SHIP:
docstrings reach PyPI in the wheel and the sdist.

WHY THIS IS A CHECK AND NOT ANOTHER SCRUB. OPL-4613 scrubbed forty-one of them
out of the TypeScript client and said in its own commit message that the scan
"wants to be a check rather than a one-off". It stayed a one-off, seven survived
it, and five more arrived the following week. This repo had never been swept at
all: OPL-4635 removed twenty-eight, eighteen of them from ``src/``.

WHY THE LIST IS NOT WRITTEN BY HAND. The first cut of this file listed the names
that scrub had just removed, reported the repo clean, and left fifteen more
identifiers shipping in the wheel — a snapshot wearing a floor's clothes
(/code-review). ``internal-names.sha256`` is DERIVED instead, from the
declarations in the platform's own source, by a script that lives there.
Regenerating it is a command rather than an act of memory.

WHY THE NAMES ARE HASHED. A list of a private system's internal names, published
in a public repo, is a small map of that system. So this carries SHA-256
prefixes. A match is still reported BY NAME, because the name is already in the
file being scanned; what this file cannot do is hand a reader the set of names
to go looking for.

WHAT IS FORBIDDEN, and it is deliberately narrow — this is not a secret scanner:

* platform source paths, BY SHAPE rather than by prefix. This is a Python
  client: it has no Go, so any ``<something>.go`` in it names somebody else's
  file. Likewise ``lib/<module>`` and ``web/lib/<module>``, with or without the
  extension — the first cut required the ``web/`` prefix and the ``.ts``, and
  all four surviving references were spelled the other way.
* identifiers declared in the platform and not in this library.
* nothing else. Prose about what the platform DOES is the point of this client's
  comments and must stay.

WHAT TO WRITE INSTEAD. The caller-facing consequence: "the platform reads an
empty expectation as no expectation" says everything the internal spelling said,
to a reader who is holding this library rather than casing the platform.

WHAT IS ALLOWED THROUGH. The surface checker and its tests carry a mirror of the
platform's route and constant tables; that is their whole job, they are not
shipped, and the drift they catch is worth more than the names cost. Allowlisted
BY FILE, so a reference anywhere else still fails.

Run it over the working tree::

    python3 scripts/check_internals.py

or over commit messages, which is the half a file scan cannot see — a message is
public the moment it is pushed, and three of the five references found in one
week's batch were in messages and PR descriptions::

    python3 scripts/check_internals.py --messages origin/main..HEAD
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGESTS = Path(__file__).resolve().parent / "internal-names.sha256"

# Files that may carry the platform's names because mirroring them is their job.
ALLOWED_FILES = {
    "scripts/check_surface.py",
    "scripts/check_internals.py",
    "tests/test_surface.py",
    "tests/test_check_surface.py",
    "tests/test_check_internals.py",
    "tests/surface_tables.py",
}

# Everything a reader of this project can see. `src/` ships; the rest is public
# on GitHub, workflows and manifests included — the first cut scanned neither,
# so the CI comment it added was itself unchecked.
SCANNED_DIRS = ("src", "tests", "scripts", ".github")
SCANNED_FILES = ("README.md", "SECURITY.md", "pyproject.toml")
SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".cfg", ".txt"}

PATH_PATTERNS = [
    # A Go file named anywhere in a Python client is somebody else's file.
    (re.compile(r"\b[A-Za-z_][A-Za-z0-9_/]*\.go\b"), "names a platform source file"),
    # `lib/x`, `web/lib/x`, `server/x`, with or without an extension.
    (re.compile(r"\b(?:web/)?lib/[A-Za-z][A-Za-z0-9_]*(?:\.ts)?\b"), "names a platform module"),
    (re.compile(r"\bserver/[A-Za-z][A-Za-z0-9_/]*\b"), "names a platform module"),
]

IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
MIN_IDENTIFIER = 7


def load_digests(path: Path = DIGESTS) -> set[str]:
    if not path.exists():
        print(
            f"check-internals: {path.name} is missing — regenerate it from the platform.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return {
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }


def scan_text(text: str, label: str, digests: set[str]) -> list[str]:
    problems = []
    for lineno, line in enumerate(text.splitlines(), 1):
        hits: list[tuple[str, str]] = []
        for pattern, why in PATH_PATTERNS:
            hits += [(hit, why) for hit in pattern.findall(line)]
        # `server/vm.go` answers to two rules, and reporting one reference twice
        # trains a reader to skim the output. The longer span wins.
        hits = [(h, w) for h, w in hits if not any(h != o and h in o for o, _ in hits)]
        problems += [f"{label}:{lineno}: {why}: {hit}" for hit, why in hits]
        for token in IDENTIFIER.findall(line):
            if len(token) < MIN_IDENTIFIER:
                continue
            if hashlib.sha256(token.encode()).hexdigest()[:12] in digests:
                problems.append(f"{label}:{lineno}: names a platform identifier: {token}")
    return problems


def scan_files(digests: set[str]) -> list[str]:
    problems: list[str] = []
    targets: list[Path] = []
    for name in SCANNED_DIRS:
        base = ROOT / name
        if base.is_dir():
            targets += [p for p in base.rglob("*") if p.is_file() and p.suffix in SUFFIXES]
    for name in SCANNED_FILES:
        path = ROOT / name
        if path.is_file():
            targets.append(path)
    for path in sorted(set(targets)):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED_FILES:
            continue
        problems += scan_text(path.read_text(encoding="utf-8", errors="replace"), rel, digests)
    return problems


def scan_messages(rev_range: str, digests: set[str]) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%H%x00%B%x00", rev_range],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # A shallow checkout has no `origin/main` to diff against, and a
        # traceback here reads as the tool being broken rather than as the range
        # being unavailable — which is how a CI step gets deleted.
        detail = result.stderr.strip().splitlines()
        print(
            f"check-internals: cannot read commit messages for {rev_range!r} "
            f"({detail[-1] if detail else 'no such range'}).\n"
            "  In CI this usually means a shallow clone: set fetch-depth: 0 on actions/checkout.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    problems: list[str] = []
    chunks = result.stdout.split("\x00")
    for i in range(0, len(chunks) - 1, 2):
        sha = chunks[i].strip()
        if sha:
            problems += scan_text(chunks[i + 1], f"commit {sha[:9]}", digests)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Keep the platform's internals out of this public repository."
    )
    ap.add_argument(
        "--messages",
        metavar="RANGE",
        help="also scan the commit messages in RANGE (e.g. origin/main..HEAD)",
    )
    args = ap.parse_args(argv)
    if args.messages is not None and not args.messages.strip():
        ap.error("--messages needs a revision range, e.g. --messages origin/main..HEAD")

    digests = load_digests()
    problems = scan_files(digests)
    if args.messages:
        problems += scan_messages(args.messages, digests)

    if not problems:
        where = "the tree" + (f" and {args.messages}" if args.messages else "")
        print(f"check-internals — nothing of the platform's is being published from {where}.")
        return 0

    print("check-internals — the platform's internals are not this repo's to publish:\n")
    for problem in problems:
        print(f"  {problem}")
    print(
        "\n  Say what the platform DOES, not which of its files or functions does it. The\n"
        "  behaviour is what a reader of this library can act on; the name is only useful\n"
        "  to somebody casing the platform. See this script's docstring for the rule."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
