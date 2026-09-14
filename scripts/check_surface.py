#!/usr/bin/env python3
"""Diff the mirrors in ``tests/surface_tables.py`` against the platform's manifest.

``ALLOWED`` in the surface tables mirrors ``V1_ROUTES`` in the platform, and it
is what keeps this SDK honest about which routes exist: a client calling a route
the server does not expose fails in a user's hands rather than here. But a
mirror nobody compares is a comment. This does the comparison whenever the
platform repo happens to be checked out — next door by default, or wherever
``MANDALA_PLATFORM_REPO`` points. That variable is an assertion rather than a
hint: set to a path that does not hold a checkout, this says so and exits 1
instead of quietly comparing against a neighbour or skipping (OPL-3901,
OPL-4512).

Not having the script is how three routes went missing. ``GET`` and ``DELETE
computers/:id/exec/:pid`` (OPL-3584) and ``GET computers/:id/snapshots``
(OPL-3636) landed upstream and never reached the mirror, and every test here
stayed green throughout — "every call lands on an allowlisted route" is
trivially true of a route the allowlist has never heard of, and so is "the
unreached part of the surface is exactly what we think".

Exits 0 and says so when the platform repo is not there, which is most of the
time: nothing in this repository's CI has both, and failing over an absence
would make this a check people learn to ignore.

Where it is enforced is the platform's own CI, which checks this repo out
beside itself and runs this script against it (OPL-3916). That is the run a
route added upstream cannot get past, and it is deliberately not here. The
comparison prints the routes, parameters and limits that have not shipped yet,
and this repository's Actions logs are world-readable the day it goes public;
the platform's are not. Running it here would also mean a read key for a private
repo living in a public one, which is the wrong direction for a credential to
point.

So on a laptop with both checked out this is the check that catches drift
before a push, and everywhere else it is the thing the platform runs.

WHAT IT READS, AND WHY THAT CHANGED (OPL-4849)
==============================================

This used to scan the platform's TypeScript as TEXT — ``web/lib/surface.ts``
for the routes, ``web/lib/apidoc.ts`` for the parameters, and five more modules
in two languages for the numbers — through a hand-written reader in
``scripts/surface_text.py``.

That reader is gone, and so is the whole class of defect it kept producing.
Across eleven adversarial review rounds the three client repositories' scanners
turned up, and then closed, at least a dozen distinct FAIL-OPENS: runs that
printed "the mirror matches the platform" without having read it. A ``.concat``
after the leading array. A decoy table inside a type annotation. A projection
callback that ignored its argument. ``.add`` after the constructor. An extra
callback parameter whose default ran. A computed key in a destructured
parameter. A type assertion that was really a comparison chain. A line comment
ended by a carriage return. Each was fixed and the next spelling arrived,
because recognising TypeScript is a TypeScript parser's job and this was not
one.

The platform already had the data — these are the tables its own API reference
and OpenAPI document are built from. What was missing was a FILE, and since
OPL-4827 there is one: ``surface-manifest.json`` at the platform's repo root,
carrying the routes, the documented parameters and the numeric limits a client
refuses against.

Not the OpenAPI document, and the reason is the same one the platform gives:
it does not carry the limits, and a checker that has to walk paths/methods/
parameters to recover ``GET sizes`` is a second reader with its own bugs.

THE THREE THINGS COMPARED
=========================

*Routes*, against ``ALLOWED``. The original check, and the one that is not
enough on its own.

*Parameters*, against ``PARAMETERS``. Every query, header and body field the
platform documents, by route. This half exists because the route half was not
enough: ``Range`` on ``GET computers/:id/files`` (OPL-3727) is a whole feature
— the only way a file larger than one request moves comes off a computer at all
— and it is not a route. It arrived on a route the mirror already knew about,
so nothing here had anything to compare and this script went on reporting the
SDK in step. A route table cannot see a parameter: the call lands in the right
place either way, and what is missing is the argument that made it worth
making.

*Limits*, against the constants in ``_api``. A number copied out of the
platform is a route by another name: the SDK refuses a value early to save the
caller a round trip, and a ceiling that has drifted turns that favour into a
refusal of a run the platform would have taken — with nothing failing here to
say so.

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

#: The platform's generated inventory of its own v1 surface (OPL-4827).
#:
#: The single file this check reads. Everything it used to parse out of seven
#: modules in two languages is in here, generated from the same tables the
#: platform's reference and OpenAPI document are built from.
MANIFEST = Path("surface-manifest.json")

#: The manifest layouts this reader understands.
#:
#: A version it has never heard of is an ERROR rather than a best-effort read,
#: on the rule the rest of this file follows: "could not tell" and "they agree"
#: must never be the same answer. A future version that moved ``parameters``
#: under a new key would otherwise read as a platform that documents no
#: parameters at all, which is the exact shape of a silent pass.
SUPPORTED_VERSIONS = frozenset({1})

#: Platform limits this SDK mirrors, as ``(our constant, their manifest key)``.
#:
#: Every key here must be present in the manifest's ``limits``: a number that
#: has moved or been renamed upstream is the news, not a reason to skip the
#: comparison. The manifest may carry limits this SDK does not mirror — it is
#: the platform's whole inventory, not our subset — and those are ignored
#: rather than refused, because a ceiling this SDK never refuses against is
#: nothing it can drift from.
CONSTANTS = [
    ("MAX_STEPS", "agent.maxSteps"),
    ("MAX_CLIPBOARD_BYTES", "clipboard.writeMaxBytes"),
    # A mirrored number that nothing compares is a number that drifts, which is
    # the whole reason this list exists — and these two arrived as local
    # constants without an entry here.
    ("MAX_ENV_ENTRIES", "exec.maxEnvEntries"),
    ("MAX_ENV_ENTRY_BYTES", "exec.maxEnvEntryBytes"),
    ("MAX_EXEC_TIMEOUT_SECONDS", "exec.maxTimeoutSeconds"),
    # The two webhook caps the SDK refuses at, and the replay window the
    # verifier defaults to — which is the one number a RECEIVER codes against.
    ("WEBHOOK_DESCRIPTION_MAX", "webhook.descriptionMaxChars"),
    ("WEBHOOK_COMPUTERS_MAX", "webhook.computersMax"),
    ("WEBHOOK_REPLAY_WINDOW_S", "webhook.replayWindowSeconds"),
]

#: The files whose contents this check mirrors. Kept separately from the
#: markers that identify a platform checkout: a checkout with one of these
#: missing is evidence of drift (or an incomplete checkout), not evidence that
#: there is no checkout and therefore permission to skip the comparison.
MIRROR_SOURCES = (MANIFEST,)

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


#: The files that identify a platform checkout git cannot vouch for — an export,
#: a vendored copy, a clone whose remote was removed. A fallback rather than the
#: primary test.
#:
#: Deliberately NOT the manifest, even though the manifest is now the only file
#: this check reads. A marker and a mirror source have opposite failure modes:
#: the marker says "this is the platform", the mirror source is the thing whose
#: absence is the news. Make them the same file and a checkout that LOST the
#: manifest stops being recognized as the platform at all — so instead of the
#: loud "mirror sources are missing" that :func:`missing_mirror_sources` exists
#: to print, the search falls through to a silent skip at exit 0. That is
#: OPL-3901 reopened by tidying.
#:
#: These two are still the platform's, and still where the manifest is
#: generated FROM; this reader simply no longer parses them.
PLATFORM_MARKERS = (Path("web/lib/surface.ts"), Path("web/lib/apidoc.ts"))


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


def missing_mirror_sources(platform: Path) -> list[Path]:
    """Files a recognized platform checkout is missing for this comparison.

    This used to be folded into :func:`platform_repo`: a checkout that had the
    route and parameter tables but had lost ``server/clipboard.go`` looked
    absent, so the entire check skipped. Before that skip was added it looked
    complete and the constant comparison died in ``Path.read_text``. Neither
    answer distinguishes "there is no checkout" from "the checkout drifted
    underneath the mirror"; this inventory is the third state that does.

    One file now instead of seven, and it matters MORE rather than less: a
    platform checkout without its manifest is a platform this check cannot
    compare against at all, and the one thing it must not do about that is
    print a number and exit 0.
    """
    return [source for source in MIRROR_SOURCES if not (platform / source).is_file()]


class ManifestError(Exception):
    """The manifest is absent, unreadable, or not the shape this reader knows.

    Its own exception so every raise site can say what it found and every one
    of them lands on the same exit — there is no shape of this file that means
    "compare what you can". The generated article either describes the
    platform's surface or this check has nothing to compare, and the second of
    those is a failure, not a quiet zero.
    """


def _routes(raw: object) -> set[tuple[str, str]]:
    """``["GET computers", ...]`` as ``{("GET", "computers"), ...}``."""
    if not isinstance(raw, list) or not raw:
        raise ManifestError("'routes' is not a non-empty array")
    found: set[tuple[str, str]] = set()
    for entry in raw:
        if not isinstance(entry, str):
            raise ManifestError(f"'routes' holds a non-string entry: {entry!r}")
        # One space, method first. Split on the FIRST space rather than any
        # run of whitespace: a pattern cannot contain a space, so a second one
        # is a malformed entry rather than something to normalize away.
        method, sep, pattern = entry.partition(" ")
        if not sep or not method.isupper() or not pattern or " " in pattern:
            raise ManifestError(f"'routes' entry is not 'METHOD pattern': {entry!r}")
        if (method, pattern) in found:
            raise ManifestError(f"'routes' lists {entry!r} twice")
        found.add((method, pattern))
    return found


def _parameters(raw: object, routes: set[tuple[str, str]]) -> dict[str, set[str]]:
    """The documented parameters, keyed the way ``PARAMETERS`` is.

    The manifest omits a route that documents none, so those are filled back in
    as empty sets here. Without that, every one of the 27 routes that take no
    argument would read as "in PARAMETERS, no longer documented upstream" — 27
    lines of drift on a mirror that is exactly right, which is the kind of
    noise that gets a check ignored.

    A key that is not a known route is refused rather than filled in: the two
    halves of the manifest describing different surfaces is a generator bug,
    and the safe reading of a parameter table for a route that does not exist
    is not to quietly compare it.
    """
    if not isinstance(raw, dict):
        raise ManifestError("'parameters' is not an object")
    known = {f"{method} {pattern}" for method, pattern in routes}
    table = {route: set() for route in known}
    for route, names in raw.items():
        if route not in known:
            raise ManifestError(f"'parameters' documents {route!r}, which is not in 'routes'")
        if not isinstance(names, list):
            raise ManifestError(f"'parameters' for {route!r} is not an array")
        for name in names:
            if not isinstance(name, str):
                raise ManifestError(f"'parameters' for {route!r} holds a non-string: {name!r}")
            # `query:`, `header:` and `body:` are the three kinds the mirror
            # spells out. An unprefixed name would compare equal to nothing in
            # PARAMETERS and read as one missing parameter plus one stale one,
            # which describes a reader that has lost the vocabulary rather than
            # a platform that changed.
            if not name.startswith(("query:", "header:", "body:")):
                raise ManifestError(
                    f"'parameters' for {route!r} holds {name!r}, which names no "
                    "query:, header: or body: field"
                )
        table[route] = set(names)
    return table


def _limits(raw: object) -> dict[str, int]:
    """The numeric ceilings, as ``{key: value}``.

    ``bool`` is excluded explicitly because it is an ``int`` in Python and
    ``True`` would otherwise compare equal to a mirrored ``1``.
    """
    if not isinstance(raw, dict):
        raise ManifestError("'limits' is not an object")
    found: dict[str, int] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ManifestError(f"'limits' entry {key!r} is {value!r}, which is not an integer")
        found[key] = value
    return found


def manifest(platform: Path) -> tuple[set[tuple[str, str]], dict[str, set[str]], dict[str, int]]:
    """The platform's own inventory of its v1 surface.

    Fails closed at every step, and that is the entire point of reading a
    generated file rather than parsing source: an absent file, a truncated one,
    a version this reader does not know, a missing top-level key, or an entry
    in a shape it cannot read all raise. None of them may reach the comparison
    as "the platform documents nothing here", because that is indistinguishable
    from agreement — and a dozen closed fail-opens in the reader this replaces
    is what indistinguishable looks like in practice.
    """
    path = platform / MANIFEST
    try:
        raw = path.read_text()
    except OSError as err:
        raise ManifestError(f"cannot be read: {err.strerror or err}") from err
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ManifestError(f"is not valid JSON: {err}") from err
    if not isinstance(data, dict):
        raise ManifestError("is not a JSON object")

    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ManifestError(f"has no integer 'version' (got {version!r})")
    if version not in SUPPORTED_VERSIONS:
        known = ", ".join(str(v) for v in sorted(SUPPORTED_VERSIONS))
        raise ManifestError(
            f"is version {version}, and this reader knows {known}. "
            "Teach it the new layout rather than comparing against a guess"
        )

    for key in ("routes", "parameters", "limits"):
        if key not in data:
            raise ManifestError(f"has no {key!r}")

    routes = _routes(data["routes"])
    return routes, _parameters(data["parameters"], routes), _limits(data["limits"])


def limit_drift(limits: dict[str, int]) -> list[str]:
    """Every mirrored constant that no longer matches the platform's.

    ``_api`` is imported rather than scraped, for the reason :func:`mirrored`
    is: the module is the mirror, and a second parser over it would be one more
    thing that can disagree with what the SDK actually sends.

    A key the manifest does not carry raises rather than being skipped. A
    ceiling this SDK refuses against and the platform no longer publishes is
    the most dangerous of the three drifts — the SDK goes on turning away calls
    the platform would take, and nothing anywhere says why.
    """
    sys.path.insert(0, str(REPO / "src"))
    from mandala_computer import _api

    drifted = []
    for ours, theirs in CONSTANTS:
        mine = getattr(_api, ours)
        if theirs not in limits:
            raise ManifestError(
                f"does not publish the limit {theirs!r}, which this SDK mirrors as "
                f"{ours} = {mine}. Has it been renamed or withdrawn upstream?"
            )
        upstream = limits[theirs]
        if mine != upstream:
            drifted.append(f"  ! {ours} is {mine}, but the manifest's {theirs} is {upstream}")
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
    """The parameter mirror, imported for the same reason :func:`mirrored` is.

    Keyed ``"METHOD pattern"`` to match the manifest. The table itself is keyed
    that way already; this only copies the sets so a caller cannot mutate the
    module's own.
    """
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

    missing = missing_mirror_sources(platform)
    if missing:
        print("check-surface — platform repo found, but mirror sources are missing.")
        for source in missing:
            print(f"  ! {platform / source}")
        print("  Restore or update these paths before comparing the mirrored surface.")
        return 1

    try:
        upstream, upstream_params, limits = manifest(platform)
        drifted = limit_drift(limits)
    except ManifestError as err:
        print(
            f"check-surface — {platform / MANIFEST} {err}.\n"
            "  This check compares nothing it cannot read: a manifest it does not\n"
            "  understand is a failure rather than a green run over an empty table."
        )
        return 1

    mirror = mirrored()
    added = sorted(upstream - mirror)
    removed = sorted(mirror - upstream)
    params = parameter_drift(upstream_params, mirrored_parameters())

    if not added and not removed and not drifted and not params:
        n = len(CONSTANTS)
        counted = sum(len(names) for names in mirrored_parameters().values())
        print(
            f"check-surface — {len(mirror)} routes, {counted} parameters and {n} limit"
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
        "  nobody's to notice. A limit that has moved belongs in\n"
        "  src/mandala_computer/_api.py."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
