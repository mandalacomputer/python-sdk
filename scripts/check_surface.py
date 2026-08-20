#!/usr/bin/env python3
"""Diff the mirrored allowlist in tests/test_surface.py against the real one.

``ALLOWED`` in the surface test mirrors ``V1_ROUTES`` in the platform's
``web/lib/surface.ts``, and it is what keeps this SDK honest about which routes
exist: a client calling a route the server does not expose fails in a user's
hands rather than here. But a mirror nobody compares is a comment. This does the
comparison whenever the platform repo happens to be checked out — next door by
default, or wherever ``MANDALA_PLATFORM_REPO`` points.

Not having the script is how three routes went missing. ``GET`` and ``DELETE
computers/:id/exec/:pid`` (OPL-3584) and ``GET computers/:id/snapshots``
(OPL-3636) landed upstream and never reached the mirror, and every test here
stayed green throughout — "every call lands on an allowlisted route" is
trivially true of a route the allowlist has never heard of, and so is "the
unreached part of the surface is exactly what we think".

Exits 0 and says so when the platform repo is not there. That is the ordinary
case in CI on this repository, and failing over it would make this a check
people learn to ignore. Where it earns its keep is on a machine that has both,
and in any job that checks out both — which is where a route added upstream
stops being invisible.

    python scripts/check_surface.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SURFACE = Path("web/lib/surface.ts")
AGENT = Path("web/lib/agent.ts")

#: Platform constants this SDK mirrors, as ``(our name, their file, their name)``.
#:
#: A number copied out of the platform is a route by another name: the SDK
#: refuses a value early to save the caller a round trip, and a ceiling that has
#: drifted turns that favour into a refusal of a run the platform would have
#: taken — with nothing failing here to say so.
CONSTANTS = [("MAX_STEPS", AGENT, "MAX_MAX_STEPS")]


def platform_repo() -> Path | None:
    """Where the platform is checked out, if it is."""
    candidates = [
        Path(p)
        for p in (
            os.environ.get("MANDALA_PLATFORM_REPO"),
            REPO.parent / "app",
            REPO.parent / "mandala-computer",
        )
        if p
    ]
    return next((d for d in candidates if (d / SURFACE).is_file()), None)


def table(source: str, name: str) -> set[tuple[str, str]]:
    """Pull one ``export const NAME: Route[] = [...]`` table out, by bracket depth.

    Balanced rather than a regex to the next ``]``: the entries contain arrays
    of their own, and the first closing bracket is nowhere near the end of the
    table.
    """
    decl = f"export const {name}: Route[] = ["
    start = source.find(decl)
    if start == -1:
        raise SystemExit(f"{name} not found in {SURFACE} — has its shape changed?")
    # The opening bracket of the table, not the one in `Route[]` a few
    # characters earlier, which closes immediately.
    i = start + len(decl) - 1
    depth = 0
    for i in range(i, len(source)):  # noqa: B020
        if source[i] == "[":
            depth += 1
        elif source[i] == "]":
            depth -= 1
            if depth == 0:
                break
    body = source[start + len(decl) : i]
    routes = {
        (m.group(1), m.group(2))
        for entry in re.finditer(r"\{[^{}]*\}", body)
        if (m := re.search(r"method:\s*'([^']+)'[\s\S]*?pattern:\s*'([^']+)'", entry.group(0)))
    }
    if not routes:
        raise SystemExit(f"parsed {name} but found no routes — has its shape changed?")
    return routes


def constant(source: str, name: str) -> int:
    """One ``export const NAME = <int>`` out of a platform module."""
    m = re.search(rf"export const {re.escape(name)}\s*=\s*(\d+)", source)
    if m is None:
        raise SystemExit(f"{name} not found — has it moved or changed shape?")
    return int(m.group(1))


def constant_drift(platform: Path) -> list[str]:
    """Every mirrored constant that no longer matches the platform's.

    Imported rather than scraped, for the reason :func:`mirrored` is: the module
    is the mirror, and a second parser over it would be one more thing that can
    disagree with what the SDK actually sends.
    """
    sys.path.insert(0, str(REPO / "src"))
    from mandala_computer import _api

    drifted = []
    for ours, module, theirs in CONSTANTS:
        mine = getattr(_api, ours)
        upstream = constant((platform / module).read_text(), theirs)
        if mine != upstream:
            drifted.append(f"  ! {ours} is {mine}, but {module}'s {theirs} is {upstream}")
    return drifted


def mirrored() -> set[tuple[str, str]]:
    """This repo's mirror, read from the test rather than re-parsed.

    Imported, not scraped: the test is the mirror, and a second parser over it
    would be one more thing that can disagree with what the suite actually pins.
    """
    sys.path.insert(0, str(REPO / "tests"))
    from test_surface import ALLOWED

    return set(ALLOWED)


def main() -> int:
    platform = platform_repo()
    if platform is None:
        print(
            "check-surface — platform repo not found, skipping.\n"
            f"  Looked in: $MANDALA_PLATFORM_REPO, {REPO.parent / 'app'}, "
            f"{REPO.parent / 'mandala-computer'}\n"
            f"  Set MANDALA_PLATFORM_REPO to compare against {SURFACE}."
        )
        return 0

    upstream = table((platform / SURFACE).read_text(), "V1_ROUTES")
    mirror = mirrored()
    added = sorted(upstream - mirror)
    removed = sorted(mirror - upstream)
    drifted = constant_drift(platform)

    if not added and not removed and not drifted:
        n = len(CONSTANTS)
        print(
            f"check-surface — {len(mirror)} routes and {n} constant"
            f"{'' if n == 1 else 's'}, in step with {platform / SURFACE.parent}."
        )
        return 0

    for method, pattern in added:
        print(f"  + {method} {pattern}  (upstream, missing from ALLOWED)")
    for method, pattern in removed:
        print(f"  - {method} {pattern}  (in ALLOWED, gone from upstream)")
    for line in drifted:
        print(line)
    print(
        "\ncheck-surface — the mirror has drifted from the platform.\n"
        "  Update ALLOWED in tests/test_surface.py, and add anything new to\n"
        "  UNIMPLEMENTED until this SDK can call it. A constant that has moved\n"
        "  belongs in src/mandala_computer/_api.py."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
