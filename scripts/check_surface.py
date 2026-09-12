#!/usr/bin/env python3
"""Diff the mirrors in tests/test_surface.py against the real tables upstream.

``ALLOWED`` in the surface test mirrors ``V1_ROUTES`` in the platform's
``web/lib/surface.ts``, and it is what keeps this SDK honest about which routes
exist: a client calling a route the server does not expose fails in a user's
hands rather than here. But a mirror nobody compares is a comment. This does the
comparison whenever the platform repo happens to be checked out — next door by
default, or wherever ``MANDALA_PLATFORM_REPO`` points. That variable is an
assertion rather than a hint: set to a path that does not hold a checkout, this
says so and exits 1 instead of quietly comparing against a neighbour or
skipping (OPL-4512).

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

from surface_text import (
    initializer_contents,
    literal_contents,
    literal_string,
    module_matches,
    object_entries,
    split_items,
    strip_comments,
    top_level_keys,
)

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
#: The reader supports both TypeScript and Go constants.
CLIPBOARD = Path("server/clipboard.go")
EXEC = Path("server/execbg.go")
API = Path("server/api.go")
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
    ("MAX_EXEC_TIMEOUT_SECONDS", API, "execMaxTimeoutSec"),
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
#: primary test: they are contents, and contents are what goes missing when the
#: mirror drifts. ``MIRROR_SOURCES`` separately says whether a recognized
#: checkout is complete enough to compare against.
PLATFORM_MARKERS = (SURFACE, APIDOC)


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
    complete and ``constant_drift`` died in ``Path.read_text``. Neither answer
    distinguishes "there is no checkout" from "the checkout drifted underneath
    the mirror"; this inventory is the third state that does.
    """
    return [source for source in MIRROR_SOURCES if not (platform / source).is_file()]


def table(source: str, name: str) -> set[tuple[str, str]]:
    """Read every route literal, or refuse a table this reader cannot compare."""
    try:
        source = strip_comments(source)
        declarations = module_matches(
            source,
            rf"export\s+const\s+{re.escape(name)}\s*:\s*Route\[\]\s*=\s*\[",
        )
        if len(declarations) != 1:
            raise ValueError("declaration absent, ambiguous, or unsupported")
        body = initializer_contents(source, declarations[0].end() - 1, "[", "]")
        routes = set()
        for entry in split_items(body):
            fields = object_entries(literal_contents(entry, "{", "}"))
            if "method" not in fields or "pattern" not in fields:
                raise ValueError("route needs literal method and pattern fields")
            routes.add((literal_string(fields["method"]), literal_string(fields["pattern"])))
        if not routes:
            raise ValueError("found no routes")
        return routes
    except ValueError as err:
        raise SystemExit(f"cannot read {name} in {SURFACE}: {err}") from None


def shared_query(source: str) -> dict[str, str]:
    """Read named parameter-entry literals from comment-blanked source.

    Unknown references are refused at their use site; shared arrays and
    arbitrary expressions are deliberately outside this reader's grammar.
    """
    found = {}
    for match in module_matches(
        source,
        r"(?:export\s+)?const ([A-Za-z_$][\w$]*):\s*Query\s*=\s*\{",
    ):
        name = match.group(1)
        try:
            fields = object_entries(initializer_contents(source, match.end() - 1, "{", "}"))
            if "name" not in fields or name in found:
                raise ValueError("shared parameter entry has no name or an ambiguous declaration")
            found[name] = literal_string(fields["name"])
        except ValueError as err:
            raise ValueError(f"shared parameter {name}: {err}") from None
    return found


def _parameter_names(value: str, shared: dict[str, str]) -> set[str]:
    """Account for every entry in a query/header array, including named entries."""
    names = set()
    for entry in split_items(literal_contents(value, "[", "]")):
        if entry.startswith("{"):
            fields = object_entries(literal_contents(entry, "{", "}"))
            if "name" not in fields:
                raise ValueError("parameter entry has no name")
            names.add(literal_string(fields["name"]))
        elif re.fullmatch(r"[A-Za-z_$][\w$]*", entry) and entry in shared:
            names.add(shared[entry])
        else:
            raise ValueError("unsupported or unresolved parameter entry")
    return names


def parameters(platform: Path) -> dict[str, set[str]]:
    """Read all documented route parameters, refusing unknown declarations."""
    try:
        source = strip_comments((platform / APIDOC).read_text())
        shared = shared_query(source)
        declarations = module_matches(
            source,
            r"export\s+const\s+DOCS\s*:\s*Record<string,\s*Doc>\s*=\s*\{",
        )
        if len(declarations) != 1:
            raise ValueError("DOCS declaration absent, ambiguous, or unsupported")
        docs = object_entries(initializer_contents(source, declarations[0].end() - 1, "{", "}"))
        table: dict[str, set[str]] = {}
        for route, value in docs.items():
            if re.fullmatch(r"[A-Z]+ .+", route) is None:
                raise ValueError("unsupported route key in DOCS")
            try:
                fields = object_entries(literal_contents(value, "{", "}"))
                found: set[str] = set()
                for key, kind in (("query", "query"), ("headers", "header")):
                    if key in fields:
                        try:
                            found.update(
                                f"{kind}:{name}" for name in _parameter_names(fields[key], shared)
                            )
                        except ValueError as err:
                            raise ValueError(f"{key}: {err}") from None
                if "body" in fields:
                    body = fields["body"]
                    call = re.match(r"object\s*(?=\()", body)
                    if call:
                        args = split_items(literal_contents(body[call.end() :], "(", ")"))
                        if not args or not args[0].startswith("{"):
                            raise ValueError("unreadable body fields")
                        found.update(
                            f"body:{key}"
                            for key in top_level_keys(literal_contents(args[0], "{", "}"))
                        )
                    elif body.startswith("{"):
                        # Raw schemas describe binary bodies, with no named fields.
                        object_entries(literal_contents(body, "{", "}"))
                    else:
                        raise ValueError("unreadable body fields")
                table[route] = found
            except ValueError as err:
                if str(err) == "unreadable body fields":
                    raise SystemExit(
                        f"'{route}' in {APIDOC} documents a body in a form this\n"
                        "  reader does not know — neither object(...) nor a raw schema literal."
                    ) from None
                raise SystemExit(f"cannot read '{route}' in {APIDOC}: {err}") from None
        if not table:
            raise ValueError("found no routes in DOCS")
        return table
    except ValueError as err:
        raise SystemExit(f"cannot read DOCS in {APIDOC}: {err}") from None


def constant(source: str, name: str, module: Path) -> int:
    """One integer constant out of a platform module, TypeScript or Go.

    Supports TypeScript ``export const NAME = <expr>``, standalone Go
    ``const name = <expr>``, and ``name = <expr>`` inside a Go ``const`` block.
    Declarations are matched at the start of a line over source whose comments have been
    blanked first, so that a mention of the name in a comment or in another
    expression is not read as its declaration — the Go form always was, and the
    TypeScript one was not, which made a commented-out declaration upstream a
    silent match here.

    Quoted text is blanked along with the comments, the declaration must be at the
    top level, and the match must be the ONLY one there. Both halves close what the comment blanking left open: a
    declaration-shaped LINE inside a template or a Go raw string — a snippet in a
    generated document, a script embedded in a module — matched, and the first
    match won, so prose earlier in the file decided what the platform's constant
    was. A number read out of prose is not a comparison, and where it happens to
    equal the mirror the check passes over a constant that has drifted, which is
    the one direction that matters for a published package. Two declarations of
    one name is the same situation from the other side and is refused rather than
    settled by position, which is the rule :func:`table` already follows.

    None of that is enough on its own, because blanking a literal depends on
    knowing where it starts, and in TypeScript that depends on a slash whose role
    is sometimes undecidable: a backtick inside a regex literal this reader places
    wrongly moves a template's boundary, and the real declaration can end up
    blanked with the prose left standing — the same false pass, reached through the
    lexer instead of through the pattern. So the file is read BOTH ways, under each
    ``undecided_slash`` policy, and a name whose value depends on which way is
    refused. That is the whole guarantee here: not that this reader lexes
    JavaScript correctly, but that it will not report a number it had to guess at
    (OPL-4805, and its adversarial review).

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
    go = module.suffix == ".go"
    pattern = (
        # A Go declaration may name its type, and one that does is still the
        # declaration: a pattern blind to `const name int = 4` reads the file as
        # if the constant were declared somewhere else — which, where a local of
        # the same name exists, it then finds (adversarial review, OPL-4805).
        rf"^[ \t]*(?:const[ \t]+)?{re.escape(name)}"
        rf"(?:[ \t]+[A-Za-z_][\w.\[\]*]*)?[ \t]*=[ \t]*([0-9*+()\s]+?)[ \t]*$"
        if go
        else rf"^[ \t]*export const {re.escape(name)}[ \t]*=[ \t]*([0-9*+()\s]+?)[ \t]*;?[ \t]*$"
    )
    readings = {
        policy: _declared(source, pattern, go=go, undecided_slash=policy)
        for policy in ("operator", "regex")
    }
    if len(set(readings.values())) != 1:
        raise SystemExit(
            f"{name} in {module} depends on how an undecidable slash in that file is\n"
            "  read — one way it is "
            + " and the other ".join(repr(r) for r in readings.values())
            + ".\n  A constant this reader cannot establish without guessing is one it refuses."
        )
    found = readings["operator"]
    if found is None:
        raise SystemExit(f"{name} not found in {module} — has it moved or changed shape?")
    if found == _UNBALANCED:
        raise SystemExit(
            f"{name} is in a {module} this reader cannot place declarations in — the braces\n"
            "  before it do not balance, so whether it is at the top level or inside\n"
            "  something is exactly what cannot be established."
        )
    if found == _AMBIGUOUS:
        raise SystemExit(
            f"{name} is declared more than once at the top level of {module} — this reader\n"
            "  cannot tell which one the platform uses, and picking by position is how it\n"
            "  would agree with the wrong one."
        )
    try:
        return _arith(ast.parse(found.strip(), mode="eval").body)
    except (SyntaxError, ValueError) as err:
        raise SystemExit(
            f"{name} is {found!r} in {module}, which this reader cannot evaluate"
        ) from err


#: What :func:`_declared` returns for a name declared more than once where this
#: reader can see it. A sentinel rather than an exception so that the two slash
#: policies can be compared before either is reported: "declared twice under one
#: reading and once under the other" is itself a refusal, and the ambiguity is
#: the more useful half of it.
_AMBIGUOUS = "<more than one declaration>"

#: What it returns for a file whose braces do not balance where the declaration is.
#: The depth rule below is a COUNT, not a parse, and a count that has gone negative
#: is evidence that something it cannot see is contributing braces — so the answer
#: is that the declaration cannot be placed, rather than a depth that happens to
#: read as zero (adversarial review, OPL-4805).
_UNBALANCED = "<braces do not balance>"


def _declared(source: str, pattern: str, *, go: bool, undecided_slash: str) -> str | None:
    """The expression this pattern finds at the top level, under one slash policy.

    ``None`` where the file declares it nowhere this reader can see, and
    :data:`_AMBIGUOUS` where it is declared more than once.

    Top level means brace depth zero, counted over the blanked source. A constant
    a package exports is not declared inside a function body, and a name that IS
    declared in one shadows nothing the SDK mirrors — reading it as the platform's
    value is how a local `36` answered for an exported `99`. Go groups its
    constants in parentheses rather than braces, so a `const (…)` block counts as
    the top level it is. A declaration only inside a TypeScript ``namespace`` block
    is not at the top level either, and reads as absent: the mirror is of a module's
    exports, so "declared somewhere this does not mirror" and "not declared" are the
    same news.

    The depth is a count, so what is checked is the whole prefix and not the number
    at the declaration: a prefix that ever went NEGATIVE refuses, even where the
    count has come back to zero since. A closing brace nobody opened is evidence of
    text this reader is not seeing as text, and the honest answer there is that the
    declaration cannot be placed — :data:`_UNBALANCED` — rather than a depth that
    balances by accident and reads a nested declaration as a top-level one.
    """
    blanked = strip_comments(
        source,
        language="go" if go else "typescript",
        literals=True,
        undecided_slash="regex" if undecided_slash == "regex" else "operator",
    )
    depth = 0
    floor = 0
    at = 0
    found = []
    for match in re.finditer(pattern, blanked, re.MULTILINE):
        for ch in blanked[at : match.start()]:
            depth += 1 if ch == "{" else -1 if ch == "}" else 0
            floor = min(floor, depth)
        at = match.start()
        # The floor and not only the current depth. A brace this reader could not
        # see closes a scope nobody opened and the count recovers at the next real
        # `{`, which reads a nested declaration as a top-level one — so a prefix
        # that ever went negative refuses, rather than the count happening to come
        # back to zero (adversarial review, OPL-4805).
        if floor < 0:
            return _UNBALANCED
        if depth == 0:
            found.append(match)
    if not found:
        return None
    return _AMBIGUOUS if len(found) > 1 else found[0].group(1)


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
        # Only ever the unset case now: a path that was named and is not a
        # checkout has already exited 1 from `platform_repo`, so this no longer
        # claims to have looked somewhere the operator pointed.
        looked = ", ".join(str(REPO.parent / sibling) for sibling in SIBLINGS)
        print(
            "check-surface — platform repo not found, skipping.\n"
            f"  Looked for a clone of {PLATFORM_REMOTE} in: {looked or '(nowhere)'}\n"
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
