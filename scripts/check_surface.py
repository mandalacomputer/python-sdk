#!/usr/bin/env python3
"""Diff the mirrors in tests/test_surface.py against the real tables upstream.

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

Exits 0 and says so when the platform repo is not there, which is most of the
time: nothing in this repository's CI has both, and failing over an absence
would make this a check people learn to ignore.

Where it is enforced is the platform's own CI, which checks this repo out
beside itself and runs this script against it (OPL-3916). That is the run a
route added upstream cannot get past, and it is deliberately not here. The
comparison prints the routes, parameters and constant values that have not
shipped yet, and this repository's Actions logs are world-readable the day it
goes public; the platform's are not. Running it here would also mean a read key
for a private repo living in a public one, which is the wrong direction for a
credential to point.

So on a laptop with both checked out this is the check that catches drift
before a push, and everywhere else it is the thing the platform runs.

The parameter half exists because the route half was not enough. `Range` on
`GET computers/:id/files` (OPL-3727) is a whole feature — the only way a file
larger than one request moves comes off a computer at all — and it is not a
route. It arrived on a route the mirror already knew about, so nothing here had
anything to compare and this script went on reporting the SDK in step. A route
table cannot see a parameter: the call lands in the right place either way, and
what is missing is the argument that made it worth making.

    python scripts/check_surface.py
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from surface_text import balanced, strip_comments, top_level_keys

REPO = Path(__file__).resolve().parent.parent
SURFACE = Path("web/lib/surface.ts")
AGENT = Path("web/lib/agent.ts")
APIDOC = Path("web/lib/apidoc.ts")

#: Platform constants this SDK mirrors, as ``(our name, their file, their name)``.
#:
#: A number copied out of the platform is a route by another name: the SDK
#: refuses a value early to save the caller a round trip, and a ceiling that has
#: drifted turns that favour into a refusal of a run the platform would have
#: taken — with nothing failing here to say so.
#: ``clipboardWriteMax`` is the one entry that is not TypeScript. The platform
#: states that ceiling in its daemon rather than in ``web/lib``, and the first
#: version of this SDK's mirror wrote "NOT machine-checked" in a docstring and
#: left it there — which is an admission, not a check. The reader below takes
#: either language, so the note could become true.
CLIPBOARD = Path("server/clipboard.go")
EXEC = Path("server/execbg.go")
WEBHOOKS = Path("web/lib/webhooks.ts")
WEBHOOKSIGN = Path("web/lib/webhooksign.ts")

CONSTANTS = [
    ("MAX_STEPS", AGENT, "MAX_MAX_STEPS"),
    ("MAX_CLIPBOARD_BYTES", CLIPBOARD, "clipboardWriteMax"),
    # A mirrored number that nothing compares is a number that drifts, which is
    # the whole reason this list exists — and these two arrived as local
    # constants without an entry here.
    ("MAX_ENV_ENTRIES", EXEC, "execMaxEnv"),
    ("MAX_ENV_ENTRY_BYTES", EXEC, "execMaxEnvLen"),
    # The two webhook caps the SDK refuses at, and the replay window the
    # verifier defaults to — which is the one number a RECEIVER codes against.
    ("WEBHOOK_DESCRIPTION_MAX", WEBHOOKS, "DESCRIPTION_MAX"),
    ("WEBHOOK_COMPUTERS_MAX", WEBHOOKS, "COMPUTERS_MAX"),
    ("WEBHOOK_REPLAY_WINDOW_S", WEBHOOKSIGN, "REPLAY_WINDOW_S"),
]

#: The files whose contents this check mirrors. Kept separately from the
#: markers that identify a platform checkout: a checkout with one of these
#: missing is evidence of drift (or an incomplete checkout), not evidence that
#: there is no checkout and therefore permission to skip the comparison.
MIRROR_SOURCES = tuple(dict.fromkeys((SURFACE, APIDOC, *(module for _, module, _ in CONSTANTS))))

#: The platform repository, as ``owner/name`` on whatever remote it was cloned
#: from — which is what a checkout *is*, and the one thing about it that does not
#: depend on which of its files happen to be present.
#:
#: Recognizing a checkout by its contents is fail-open in the case this script
#: exists for (OPL-3901): the file that has gone missing is the news, and a
#: recognizer that reads its absence as "no checkout here" reports nothing at all.
#: Every marker set has that hole somewhere; identity has it nowhere.
#:
#: The remote's name rather than the directory's, because the working copy is
#: routinely called something else — ``app``, after the repository.
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
#: primary test: they are contents, and contents are what goes missing when the
#: mirror drifts. ``MIRROR_SOURCES`` separately says whether a recognized
#: checkout is complete enough to compare against.
PLATFORM_MARKERS = (SURFACE, APIDOC)


#: Directory names the platform repo answers to when checked out beside this one.
#:
#: ``app`` is the repository's own name, so a plain clone is called that, and it is
#: still what most working copies are called. Kept as a fallback so a machine
#: that already has one kept working through the rename; ``MANDALA_PLATFORM_REPO``
#: is the way to point at a checkout that is called neither, or lives elsewhere.
SIBLINGS = ("mandala-computer", "app")


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


def platform_repo() -> Path | None:
    """Where the platform is checked out, if it is."""
    candidates = [
        Path(p)
        for p in (os.environ.get("MANDALA_PLATFORM_REPO"), *(REPO.parent / s for s in SIBLINGS))
        if p
    ]
    return next((d for d in candidates if is_platform_checkout(d)), None)


def missing_mirror_sources(platform: Path) -> list[Path]:
    """Files a recognized platform checkout is missing for this comparison.

    This used to be folded into :func:`platform_repo`: a checkout that had the
    route and parameter tables but had lost ``server/clipboard.go`` looked
    absent, so the entire check skipped. Before that skip was added it looked
    complete and ``constant_drift`` died in ``Path.read_text``. Neither answer
    distinguishes "there is no checkout" from "the checkout drifted underneath
    the mirror"; this inventory is the third state that does.
    """
    return [source for source in MIRROR_SOURCES if not (platform / source).is_file()]


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


def shared_query(source: str) -> dict[str, str]:
    """Module-level ``const NAME: Query = {...}`` entries, by identifier.

    A route's ``query`` list can name one of these instead of spelling it out —
    ``ALLOW_PARTIAL`` is shared by two routes — so the identifier has to resolve
    to a parameter name or those routes read as taking none.
    """
    found = {}
    for m in re.finditer(r"^const ([A-Z_]+): Query = \{", source, re.MULTILINE):
        body = balanced(source, m.end() - 1, "{", "}")
        named = re.search(r"name:\s*'([^']+)'", strip_comments(body))
        if named:
            found[m.group(1)] = named.group(1)
    return found


def parameters(platform: Path) -> dict[str, set[str]]:
    """Every query, header and body field the platform documents, by route.

    Read out of ``apidoc.ts``'s ``DOCS`` rather than ``surface.ts``, because the
    route table has no parameters in it — which is the whole reason a route
    comparison could not see the one that prompted this.
    """
    source = (platform / APIDOC).read_text()
    shared = shared_query(source)
    start = source.find("export const DOCS: Record<string, Doc> = {")
    if start == -1:
        raise SystemExit(f"DOCS not found in {APIDOC} — has its shape changed?")
    # Comments blanked once, over the whole table: `strip_comments` replaces
    # them with spaces rather than deleting them, so every offset still names the
    # same character and one pass serves both the scan and the bracket matching.
    docs = strip_comments(balanced(source, source.index("{", start + 40), "{", "}"))

    table: dict[str, set[str]] = {}
    entry = re.compile(r"'([A-Z]+) ([^']+)':\s*\{")
    at = 0
    while (m := entry.search(docs, at)) is not None:
        clean = balanced(docs, m.end() - 1, "{", "}")
        # Past this entry rather than into it. A description is prose in quotes,
        # and prose about this API quotes routes — `'GET computers/:id/files'`
        # inside one would otherwise open a route of its own, nested inside the
        # entry that is still being read.
        at = m.end() + len(clean)
        found: set[str] = set()
        for key, kind in (("query", "query"), ("headers", "header")):
            listed = clean.find(f"{key}: [")
            if listed == -1:
                continue
            listing = balanced(clean, clean.index("[", listed), "[", "]")
            for name in re.finditer(r"name:\s*'([^']+)'", listing):
                found.add(f"{kind}:{name.group(1)}")
            # An identifier standing in for a whole entry. Bounded by a
            # separator on each side rather than by a trailing comma: `query:
            # [ALLOW_PARTIAL]` on one line has nothing after the identifier at
            # all, and demanding one read GET computers as taking no parameters.
            for ident in re.finditer(r"(?:^|[\[,\s])([A-Z_]{2,})(?=[,\s\]]|$)", listing):
                if ident.group(1) in shared:
                    found.add(f"query:{shared[ident.group(1)]}")
        # Only an `object(...)` body has named fields. A raw one — the file
        # upload's own bytes — has none to name.
        body_at = clean.find("body: object(")
        if body_at != -1:
            args = balanced(clean, clean.index("(", body_at), "(", ")")
            fields = balanced(args, args.index("{"), "{", "}")
            found.update(f"body:{k}" for k in top_level_keys(fields))
        table[f"{m.group(1)} {m.group(2)}"] = found
    if not table:
        raise SystemExit(f"parsed DOCS but found no routes in {APIDOC} — has its shape changed?")
    return table


def constant(source: str, name: str, module: Path) -> int:
    """One integer constant out of a platform module, TypeScript or Go.

    Two declaration forms because the platform states these numbers in two
    languages: ``export const NAME = <expr>`` in ``web/lib``, and a bare
    ``name = <expr>`` inside a Go ``const`` block in ``server/``. BOTH forms are
    matched at the start of a line, and over source whose comments have been
    blanked first, so that a mention of the name in a comment or in another
    expression is not read as its declaration — the Go form always was, and the
    TypeScript one was not, which made a commented-out declaration upstream a
    silent match here.

    Which form is tried is decided by the module's suffix rather than by trying
    both: the two patterns are close enough that a file answering to the wrong
    one is a way for this to agree by accident.

    The value is an EXPRESSION, not a literal, because both languages write
    these as products — ``64 * 1024`` is how a byte ceiling is legible, and a
    reader that demanded a bare integer could not see the very constants it exists to
    compare. Evaluated with a grammar that admits integers, ``*``, ``+`` and
    parentheses and nothing else: no names, no calls, no attribute access. A
    declaration this cannot evaluate raises rather than being skipped, on the
    rule the rest of this file follows — "could not tell" and "they agree" must
    never be the same answer.
    """
    blanked = strip_comments(source)
    pattern = (
        rf"^\s*{re.escape(name)}\s*=\s*([0-9*+()\s]+?)[ \t]*$"
        if module.suffix == ".go"
        else rf"^\s*export const {re.escape(name)}\s*=\s*([0-9*+()\s]+?)[ \t]*;?[ \t]*$"
    )
    m = re.search(pattern, blanked, re.MULTILINE)
    if m is None:
        raise SystemExit(f"{name} not found in {module} — has it moved or changed shape?")
    try:
        return _arith(ast.parse(m.group(1).strip(), mode="eval").body)
    except (SyntaxError, ValueError) as err:
        raise SystemExit(
            f"{name} is {m.group(1)!r} in {module}, which this reader cannot evaluate"
        ) from err


def _arith(node: ast.expr) -> int:
    """Evaluate an integer arithmetic expression, and nothing else.

    Deliberately not :func:`eval`, and not :func:`ast.literal_eval` either —
    the first would run whatever a platform file happened to contain, and the
    second refuses ``64 * 1024``, which is the only shape these constants are
    ever written in.
    """
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add)):
        left, right = _arith(node.left), _arith(node.right)
        return left * right if isinstance(node.op, ast.Mult) else left + right
    raise ValueError(f"not integer arithmetic: {ast.dump(node)}")


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
        try:
            source = (platform / module).read_text()
        except OSError as err:
            raise SystemExit(
                f"{module} is not readable in the platform checkout — has it moved or changed shape?"
            ) from err
        upstream = constant(source, theirs, module)
        if mine != upstream:
            drifted.append(f"  ! {ours} is {mine}, but {module}'s {theirs} is {upstream}")
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
        print(
            "check-surface — platform repo not found, skipping.\n"
            f"  Looked for a clone of {PLATFORM_REMOTE} in $MANDALA_PLATFORM_REPO\n"
            f"  and next to {REPO.name}.\n"
            f"  Set MANDALA_PLATFORM_REPO to compare against {SURFACE}."
        )
        return 0

    missing = missing_mirror_sources(platform)
    if missing:
        print("check-surface — platform repo found, but mirror sources are missing.")
        for source in missing:
            print(f"  ! {platform / source}")
        print("  Restore or update these paths before comparing the mirrored surface.")
        return 1

    upstream = table((platform / SURFACE).read_text(), "V1_ROUTES")
    mirror = mirrored()
    added = sorted(upstream - mirror)
    removed = sorted(mirror - upstream)
    drifted = constant_drift(platform)
    params = parameter_drift(parameters(platform), mirrored_parameters())

    if not added and not removed and not drifted and not params:
        n = len(CONSTANTS)
        counted = sum(len(names) for names in mirrored_parameters().values())
        print(
            # `platform`, not `platform / SURFACE.parent`. The routes and
            # parameters come from web/lib and the constants no longer all do —
            # clipboardWriteMax is read out of server/ — so naming one
            # directory understated what had been compared.
            f"check-surface — {len(mirror)} routes, {counted} parameters and {n} constant"
            f"{'' if n == 1 else 's'}, in step with {platform}."
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
