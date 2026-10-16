#!/usr/bin/env python3
"""Print the lowest version each declared dependency permits, as pins.

The Audit workflow checks two sets of versions. The packages a fresh install
resolves today come from ``pip freeze``; this prints the other set, the floors
``pyproject.toml`` declares for the runtime dependencies and the
``anthropic`` extra (``httpx>=0.27`` becomes ``httpx==0.27``). The SDK ships
no lockfile, so a user's environment can hold anything from the floor up, and
a floor with a published advisory is one this package still invites (OPL-6036).

A requirement without a ``>=`` floor has no lowest version to check and is
skipped. Development extras are not shipped and are not printed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import tomllib

FLOOR = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*>=\s*([^,;\s]+)")


def floors(pyproject: Path) -> list[str]:
    project = tomllib.loads(pyproject.read_text())["project"]
    requirements = [
        *project.get("dependencies", []),
        *project.get("optional-dependencies", {}).get("anthropic", []),
    ]
    pins = []
    for requirement in requirements:
        match = FLOOR.match(requirement)
        if match:
            pins.append(f"{match.group(1)}=={match.group(2)}")
    return pins


def main() -> int:
    pins = floors(Path(__file__).resolve().parent.parent / "pyproject.toml")
    if not pins:
        print("no floors found in pyproject.toml", file=sys.stderr)
        return 1
    print("\n".join(pins))
    return 0


if __name__ == "__main__":
    sys.exit(main())
