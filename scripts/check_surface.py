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

Exits 0 and says so when the platform repo is not there. That is the ordinary
case in CI on this repository, and failing over it would make this a check
people learn to ignore. Where it earns its keep is on a machine that has both,
and in any job that checks out both — which is where a route added upstream
stops being invisible.

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
import sys
from pathlib import Path

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

CONSTANTS = [
    ("MAX_STEPS", AGENT, "MAX_MAX_STEPS"),
    ("MAX_CLIPBOARD_BYTES", CLIPBOARD, "clipboardWriteMax"),
]


#: Directory names the platform repo answers to when checked out beside this one.
#:
#: ``gorillacloud`` is the name it had before the product was renamed, and it is
#: still what most working copies are called. Kept as a fallback so a machine
#: that already has one kept working through the rename; ``MANDALA_PLATFORM_REPO``
#: is the way to point at a checkout that is called neither, or lives elsewhere.
SIBLINGS = ("mandala-computer", "gorillacloud")


def platform_repo() -> Path | None:
    """Where the platform is checked out, if it is."""
    candidates = [
        Path(p)
        for p in (os.environ.get("MANDALA_PLATFORM_REPO"), *(REPO.parent / s for s in SIBLINGS))
        if p
    ]
    return next(
        (d for d in candidates if (d / SURFACE).is_file() and (d / APIDOC).is_file()),
        None,
    )


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


def constant(source: str, name: str) -> int:
    """One integer constant out of a platform module, TypeScript or Go.

    Two declaration forms because the platform states these numbers in two
    languages: ``export const NAME = <expr>`` in ``web/lib``, and a bare
    ``name = <expr>`` inside a Go ``const`` block in ``server/``. The Go form is
    matched at the start of a line so that a mention of the name in a comment or
    in another expression is not read as its declaration.

    The value is an EXPRESSION, not a literal, because both languages write
    these as products — ``64 * 1024`` is how a byte ceiling is legible, and a
    reader that demanded a bare integer could not see the very constants it exists to
    compare. Evaluated with a grammar that admits integers, ``*``, ``+`` and
    parentheses and nothing else: no names, no calls, no attribute access. A
    declaration this cannot evaluate raises rather than being skipped, on the
    rule the rest of this file follows — "could not tell" and "they agree" must
    never be the same answer.
    """
    for pattern in (
        rf"export const {re.escape(name)}\s*=\s*([0-9*+()\s]+?)\s*(?:;|//|\n)",
        rf"^\s*{re.escape(name)}\s*=\s*([0-9*+()\s]+?)\s*(?://|\n)",
    ):
        m = re.search(pattern, source, re.MULTILINE)
        if m is None:
            continue
        try:
            return _arith(ast.parse(m.group(1).strip(), mode="eval").body)
        except (SyntaxError, ValueError) as err:
            raise SystemExit(
                f"{name} is {m.group(1)!r}, which this reader cannot evaluate"
            ) from err
    raise SystemExit(f"{name} not found — has it moved or changed shape?")


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


def mirrored_parameters() -> dict[str, set[str]]:
    """The parameter mirror, imported for the same reason :func:`mirrored` is."""
    sys.path.insert(0, str(REPO / "tests"))
    from test_surface import PARAMETERS

    return {route: set(names) for route, names in PARAMETERS.items()}


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
            f"  Looked in $MANDALA_PLATFORM_REPO and next to {REPO.name}.\n"
            f"  Set MANDALA_PLATFORM_REPO to compare against {SURFACE}."
        )
        return 0

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
        "  Update ALLOWED and PARAMETERS in tests/test_surface.py, and add anything\n"
        "  new to UNIMPLEMENTED or UNIMPLEMENTED_PARAMETERS until this SDK can send\n"
        "  it — which is the line that makes a gap somebody's to close rather than\n"
        "  nobody's to notice. A constant that has moved belongs in\n"
        "  src/mandala_computer/_api.py."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
