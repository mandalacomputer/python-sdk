#!/usr/bin/env python3
"""Diff the mirrors in tests/surface_tables.py against the platform's surface manifest.

``ALLOWED`` mirrors the platform's v1 route table, ``PARAMETERS`` the documented
parameters of each route, and eight constants in ``mandala_computer._api`` mirror
the platform's limits. They are what keep this SDK honest about which routes
exist and what each takes: a client calling a route the server does not expose,
or refusing a value the platform would take, fails in a user's hands rather than
here. But a mirror nobody compares is a comment. This does the comparison
whenever the platform repo happens to be checked out — next door by default, or
wherever ``MANDALA_PLATFORM_REPO`` points. That variable is an assertion rather
than a hint: set to a path that does not hold a checkout, this says so and exits
1 instead of quietly comparing against a neighbour or skipping (OPL-4512).

What it compares against is the platform's ``surface-manifest.json`` (platform
OPL-4827): a file the platform generates from its own tables, verifies
byte-for-byte in its own suite, and commits like a lockfile. It carries the
routes, the parameters per route, and the limits keyed by what each number
means. This used to be a scanner over the platform's TypeScript and Go source
— eleven hundred lines of it, in a language that is not Python's to parse, and
every review of it found another construct it read wrong. The manifest is the
platform saying what its surface is, once, in a form a client can read without
guessing (OPL-4837).

The reader is FAIL-CLOSED. The recurring defect in the scanners was a false
all-clear — reporting the mirror in step because the scan silently read nothing
— and a JSON diff can do that too. So a manifest that is missing, unparseable,
of a version this does not know, with no routes, with a parameter on a route it
does not list, or without one of the limits this SDK mirrors, is a failure that
names the problem, never an empty comparison that passes.

Exits 0 and says so when the platform repo is not there, which is most of the
time: nothing in this repository's CI has both, and failing over an absence
would make this a check people learn to ignore.

Where it is enforced is the platform's own CI, which checks this repo out
beside itself and runs this script against it (OPL-3916). That is the run a
route added upstream cannot get past, and it is deliberately not here: the
comparison prints what has not shipped yet, and this repository's Actions logs
are world-readable.

    python scripts/check_surface.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent

#: The platform's surface manifest, at the root of its checkout.
MANIFEST = Path("surface-manifest.json")

#: The manifest format this reader understands. The platform bumps it only when
#: the shape changes in a way a reader must notice; a version this has not heard
#: of is refused rather than read on the assumption that nothing moved.
MANIFEST_VERSION = 1

#: Platform limits this SDK mirrors, as ``(our name, the manifest's key)``.
#:
#: A number copied out of the platform is a route by another name: the SDK
#: refuses a value early to save the caller a round trip, and a ceiling that has
#: drifted turns that favour into a refusal of a run the platform would have
#: taken — with nothing failing here to say so. The manifest keys them by what
#: each number MEANS, never by the platform's own name for it.
LIMITS = [
    ("MAX_STEPS", "agent.maxSteps"),
    ("MAX_CLIPBOARD_BYTES", "clipboard.writeMaxBytes"),
    ("MAX_ENV_ENTRIES", "exec.maxEnvEntries"),
    ("MAX_ENV_ENTRY_BYTES", "exec.maxEnvEntryBytes"),
    ("MAX_EXEC_TIMEOUT_SECONDS", "exec.maxTimeoutSeconds"),
    ("WEBHOOK_DESCRIPTION_MAX", "webhook.descriptionMaxChars"),
    ("WEBHOOK_COMPUTERS_MAX", "webhook.computersMax"),
    ("WEBHOOK_REPLAY_WINDOW_S", "webhook.replayWindowSeconds"),
]

#: The methods a route entry may begin with. Anything else is not a route this
#: SDK could mirror, and a manifest that says so is a manifest this misread.
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

#: Where a documented parameter lives, as the manifest and the mirror both spell it.
PARAMETER_KINDS = ("query:", "header:", "body:")

#: The platform repository, as ``owner/name`` on whatever remote it was cloned
#: from — which is what a checkout *is*, and the one thing about it that does not
#: depend on which of its files happen to be present.
#:
#: Recognizing a checkout by its contents is fail-open in the case this script
#: exists for (OPL-3901): the file that has gone missing is the news, and a
#: recognizer that reads its absence as "no checkout here" reports nothing at all.
#: Every marker set has that hole somewhere; identity has it nowhere.
#:
#: The remote's name rather than the directory's, because a working copy can be
#: called anything — ``app``, after the repository, is common.
#: Forks are covered by their ``upstream`` remote where they have one, since
#: :func:`remotes` reads all of them, and by ``PLATFORM_MARKERS`` where they
#: do not.
PLATFORM_REMOTE = "mandalacomputer/app"

#: The ``owner/name`` tail of a remote URL, in any of the forms git accepts:
#: ``git@host:owner/name.git``, ``https://host/owner/name``, ``ssh://…/owner/name``.
REMOTE_TAIL = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<name>[^/:]+?)(?:\.git)?/?$")

#: Environment variables that tell git which repository it is looking at,
#: regardless of where it was pointed. A git hook exports all three, so a check
#: run from one would otherwise ask about a candidate directory and be answered
#: about the repository the hook fired in — the SDK, usually, which is a wrong
#: answer in both directions and reopens OPL-3901 when it is a platform sibling
#: being denied its own name.
GIT_ELSEWHERE = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")


def git_environment() -> dict[str, str]:
    """This environment, minus the variables that name a repository of their own.

    A helper rather than a line inside :func:`remotes` because the tests build
    their fixtures with git too, and ``git init`` under an ambient ``GIT_DIR``
    does not create the repository it was handed — it re-initializes the one the
    variable names, and the ``git remote add`` after it writes there as well.
    """
    return {name: value for name, value in os.environ.items() if name not in GIT_ELSEWHERE}


#: The file that identifies a platform checkout git cannot vouch for — an export,
#: a vendored copy, a clone whose remote was removed. A fallback rather than the
#: primary test: it is contents, and contents are what goes missing when the
#: mirror drifts. :func:`read_manifest` separately says whether a recognized
#: checkout holds a manifest this can compare against.
PLATFORM_MARKERS = (MANIFEST,)


#: Directory names the platform repo answers to when checked out beside this one.
#: Guesses, which is why they are allowed to miss: ``MANDALA_PLATFORM_REPO`` is
#: the way to point at a checkout that is called something else or lives
#: elsewhere, and being told a path is not the same as guessing one.
SIBLINGS = ("mandala-computer",)


def remotes(directory: Path) -> frozenset[str]:
    """Every remote configured in ``directory``, as ``owner/name``.

    Empty for anything that is not a git repository *root* — the ``.git`` test
    comes first because ``git -C`` happily answers about an enclosing repository,
    and a plain directory sitting inside one would otherwise borrow its identity.
    Empty, too, where git is not installed or the URL is a local path with no
    owner in it: callers fall back to :data:`PLATFORM_MARKERS` rather than read
    an empty answer as "not the platform".

    :data:`GIT_ELSEWHERE` is dropped from the environment because ``-C`` does not
    win against it. ``GIT_DIR`` and ``GIT_COMMON_DIR`` name a repository outright
    and outrank both ``-C`` and ``--git-dir``, so under an ambient one — a git
    hook, a wrapper that exports it — this would report some other repository's
    remotes for every directory it was asked about.
    """
    if not (directory / ".git").exists():
        return frozenset()
    try:
        done = subprocess.run(
            # --local: a remote belongs to a checkout, and the unqualified query
            # would also read whatever global or system config had to say.
            (
                "git",
                "-C",
                str(directory),
                "config",
                "--local",
                "--get-regexp",
                r"^remote\..*\.url$",
            ),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=git_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if done.returncode != 0:
        return frozenset()
    found = (
        REMOTE_TAIL.search(line.split(maxsplit=1)[-1]) for line in done.stdout.splitlines() if line
    )
    return frozenset(f"{m['owner']}/{m['name']}".lower() for m in found if m)


def is_platform_checkout(directory: Path) -> bool:
    """Whether ``directory`` is the platform repo, by identity first.

    A clone that says it came from :data:`PLATFORM_REMOTE` is the platform
    whatever state its working tree is in, which is the point: the file that is
    missing is the news, and a check that reads the same absence as "no checkout
    here" reports nothing at all. The marker files remain for the copies git
    cannot speak for.
    """
    return PLATFORM_REMOTE in remotes(directory) or all(
        (directory / marker).is_file() for marker in PLATFORM_MARKERS
    )


def named_platform_repo() -> Path | None:
    """The directory ``MANDALA_PLATFORM_REPO`` names, or ``None`` when it is unset.

    Set and empty is not unset. A variable that failed to expand is the case
    this whole distinction exists for, and in a shell — in the platform's CI
    workflow especially — that arrives as an empty string rather than an absent
    key. Reading it as "no variable" would put the failure that motivated
    OPL-4512 back on the silent path; the caller reports it instead.

    Normalized against :data:`REPO` the way the sibling guesses are. Left as
    given, a relative value names a different directory depending on where the
    script was invoked from, and the paths this prints would be one relative
    entry beside two absolute ones — which reads as the directory the operator
    meant rather than the one that was searched.
    """
    value = os.environ.get("MANDALA_PLATFORM_REPO")
    if value is None:
        return None
    named = value.strip()
    if not named:
        raise SystemExit(
            "check-surface — MANDALA_PLATFORM_REPO is set and empty, which names no\n"
            "  directory: a path that failed to expand looks exactly like this.\n"
            "  Point it at a platform checkout, or unset it to skip the comparison."
        )
    return Path(os.path.normpath(REPO / named))


def platform_repo() -> Path | None:
    """Where the platform is checked out, if it is.

    ``MANDALA_PLATFORM_REPO`` is an assertion and the siblings are guesses, so
    the two get different treatment: a path that turns out not to hold the
    platform is a mistake to report rather than a repo to go looking for
    elsewhere. Falling through to the siblings has two outcomes and both are
    silent — nothing next door, so "not found, skipping" at exit 0; something
    next door, so a green answer about a checkout nobody named.

    Which matters most where this gate is actually enforced: the platform's own
    CI sets this variable for three SDKs at once (OPL-3916), so a checkout path
    that moves or a variable that fails to expand would otherwise be
    indistinguishable from "no platform here" — three green no-ops over three
    surface mirrors nobody compared, on the one run that compares them.

    Unset stays a search: the siblings, then a skip at exit 0 when there is
    none, which is the ordinary case in this repository's CI and not something
    to fail over.
    """
    named = named_platform_repo()
    if named is not None:
        if not is_platform_checkout(named):
            markers = " and ".join(str(marker) for marker in PLATFORM_MARKERS)
            raise SystemExit(
                f"check-surface — MANDALA_PLATFORM_REPO is set to {named},\n"
                f"  which is no checkout of {PLATFORM_REMOTE}: no remote there names it,\n"
                f"  and it does not hold {markers}.\n"
                "  Point it at a platform checkout, or unset it to skip the comparison."
            )
        return named
    return next((d for d in (REPO.parent / s for s in SIBLINGS) if is_platform_checkout(d)), None)


class ManifestError(Exception):
    """A manifest this cannot compare against, and why — never an empty answer."""


def read_manifest(platform: Path) -> dict[str, object]:
    """The platform's surface manifest, checked to the shape this compares.

    Every check here is a way the scanners once printed a false all-clear, made
    into a failure that names itself: a file that is not there, that does not
    parse, that is a version this has not heard of, that lists no routes, that
    documents a parameter on a route it does not list, or that lacks a limit this
    SDK mirrors. Anything short of the full shape is refused, because a diff over
    a half-read manifest passes for the same reason a scan that read nothing did.
    """
    path = platform / MANIFEST
    if not path.is_file():
        raise ManifestError(f"{path} is not there — the checkout predates the manifest, or lost it")
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise ManifestError(f"{path} cannot be read: {error}") from error
    if not isinstance(manifest, dict):
        raise ManifestError(f"{path} is not a JSON object")
    if manifest.get("version") != MANIFEST_VERSION:
        raise ManifestError(
            f"{path} is manifest version {manifest.get('version')!r}; this reader knows {MANIFEST_VERSION}"
        )
    routes = manifest.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ManifestError(f"{path} lists no routes")
    for entry in routes:
        if not (
            isinstance(entry, str) and len(entry.split(" ")) == 2 and entry.split(" ")[0] in METHODS
        ):
            raise ManifestError(f"{path} holds a route that is not 'METHOD pattern': {entry!r}")
    listed = set(routes)
    parameters = manifest.get("parameters")
    if not isinstance(parameters, dict):
        raise ManifestError(f"{path} has no parameters table")
    for route, names in parameters.items():
        if route not in listed:
            raise ManifestError(
                f"{path} documents parameters for a route it does not list: {route}"
            )
        if not isinstance(names, list) or not all(
            isinstance(name, str) and name.startswith(PARAMETER_KINDS) for name in names
        ):
            raise ManifestError(
                f"{path} holds a parameter list this cannot read, on {route}: {names!r}"
            )
    limits = manifest.get("limits")
    if not isinstance(limits, dict):
        raise ManifestError(f"{path} has no limits table")
    for _, key in LIMITS:
        value = limits.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ManifestError(
                f"{path} does not carry the limit {key}, or it is not a positive integer"
            )
    return manifest


def routes(manifest: dict[str, object]) -> set[tuple[str, str]]:
    """The platform's routes, in the mirror's own ``(method, pattern)`` spelling."""
    found = manifest["routes"]
    assert isinstance(found, list)
    return {(entry.split(" ")[0], entry.split(" ")[1]) for entry in found}


def parameters(manifest: dict[str, object]) -> dict[str, set[str]]:
    """The documented parameters of EVERY route, empty for the ones that take none.

    The manifest lists only the routes that have any; the mirror lists every
    route, so a route with an empty set is a claim about it, and the diff has to
    see both sides the same way.
    """
    listed = manifest["parameters"]
    assert isinstance(listed, dict)
    found = manifest["routes"]
    assert isinstance(found, list)
    return {route: set(listed.get(route, [])) for route in found}


def constant_drift(manifest: dict[str, object]) -> list[str]:
    """Every mirrored constant whose value is not the manifest's limit."""
    from mandala_computer import _api

    limits = manifest["limits"]
    assert isinstance(limits, dict)
    drifted = []
    for ours, key in LIMITS:
        mine = getattr(_api, ours)
        upstream = limits[key]
        if mine != upstream:
            drifted.append(f"  ! {ours} is {mine}, but the platform's {key} is {upstream}")
    return drifted


def _surface_tables() -> ModuleType:
    """Import the mirror tables without pulling in pytest, httpx or respx.

    They used to live in ``test_surface``, which imports those at module level,
    so the comparison path — the one that does real work — could not run without
    the test extra. ``sys.path`` is restored so a later import cannot pick up
    this repo as a top-level package by accident.
    """
    path = str(REPO)
    sys.path.insert(0, path)
    try:
        from tests import surface_tables

        return surface_tables
    finally:
        if sys.path and sys.path[0] == path:
            sys.path.pop(0)


def mirrored() -> set[tuple[str, str]]:
    """This repo's mirror, read from the tables rather than re-parsed.

    Imported, not scraped: the tables are the mirror, and a second parser over
    them would be one more thing that can disagree with what the suite actually
    pins.
    """
    return set(_surface_tables().ALLOWED)


def mirrored_parameters() -> dict[str, set[str]]:
    """The parameter mirror, imported for the same reason :func:`mirrored` is."""
    return {route: set(names) for route, names in _surface_tables().PARAMETERS.items()}


def parameter_drift(upstream: dict[str, set[str]], mirror: dict[str, set[str]]) -> list[str]:
    """Every documented parameter the mirror does not list, and the reverse.

    Routes are diffed too, but quietly: a route in one table and not the other
    is already the route check's news, and saying it twice buries the parameters
    this exists to find.
    """
    lines = []
    for route in sorted(set(upstream) & set(mirror)):
        for name in sorted(upstream[route] - mirror[route]):
            lines.append(f"  + {route}  {name}  (upstream, missing from PARAMETERS)")
        for name in sorted(mirror[route] - upstream[route]):
            lines.append(f"  - {route}  {name}  (in PARAMETERS, gone from upstream)")
    for route in sorted(set(upstream) - set(mirror)):
        lines.append(f"  + {route}  (documented upstream, absent from PARAMETERS)")
    for route in sorted(set(mirror) - set(upstream)):
        lines.append(f"  - {route}  (in PARAMETERS, no longer documented upstream)")
    return lines


def main() -> int:
    platform = platform_repo()
    if platform is None:
        # Only ever the unset case now: a path that was named and is not a
        # checkout has already exited 1 from `platform_repo`, so this no longer
        # claims to have looked somewhere the operator pointed.
        looked = ", ".join(str(REPO.parent / sibling) for sibling in SIBLINGS)
        print(
            "check-surface — platform repo not found, skipping.\n"
            f"  Looked for a clone of {PLATFORM_REMOTE} in: {looked or '(nowhere)'}\n"
            f"  Set MANDALA_PLATFORM_REPO to compare against {MANIFEST}."
        )
        return 0

    try:
        manifest = read_manifest(platform)
    except ManifestError as error:
        # Named, and a failure: the checkout is the platform and the comparison
        # could not be made, which is the third state between "no checkout" and
        # "in step" — the one the scanners used to report as the second.
        print("check-surface — platform repo found, but its surface manifest cannot be compared.")
        print(f"  ! {error}")
        return 1

    upstream = routes(manifest)
    mirror = mirrored()
    added = sorted(upstream - mirror)
    removed = sorted(mirror - upstream)
    drifted = constant_drift(manifest)
    params = parameter_drift(parameters(manifest), mirrored_parameters())

    if not added and not removed and not drifted and not params:
        n = len(LIMITS)
        counted = sum(len(names) for names in mirrored_parameters().values())
        print(
            f"check-surface — {len(mirror)} routes, {counted} parameters and {n} constant"
            f"{'' if n == 1 else 's'}, in step with {platform / MANIFEST}."
        )
        return 0

    for method, pattern in added:
        print(f"  + {method} {pattern}  (upstream, missing from ALLOWED)")
    for method, pattern in removed:
        print(f"  - {method} {pattern}  (in ALLOWED, gone from upstream)")
    for line in params:
        print(line)
    for line in drifted:
        print(line)
    print(
        "\ncheck-surface — the mirror has drifted from the platform.\n"
        "  Update ALLOWED and PARAMETERS in tests/surface_tables.py, and add anything\n"
        "  new to UNIMPLEMENTED or UNIMPLEMENTED_PARAMETERS until this SDK can send\n"
        "  it — which is the line that makes a gap somebody's to close rather than\n"
        "  nobody's to notice. A constant that has moved belongs in\n"
        "  src/mandala_computer/_api.py."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
