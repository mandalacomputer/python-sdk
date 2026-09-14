"""The platform surface check's checkout discovery and failure boundaries.

Two halves. The first is discovery — which directory is the platform, and what
happens when the answer is "none" or "not the one you named". The second is the
manifest reader, and every test in it asks the same question a different way:
when this cannot read the platform's inventory, does it FAIL, or does it print
a number and exit 0?

That question is the whole of OPL-4849. The reader this replaced scanned the
platform's TypeScript as text, and across eleven review rounds it produced at
least a dozen distinct fail-opens — runs that announced "the mirror matches the
platform" without having read it. Reading a generated JSON file removes the
grammar, but it does not by itself remove the failure mode: a missing key read
as an empty table, or an unknown version read as best-effort, is the same green
run over nothing. So the shape checks below are not defensive programming
around a file we control. They are the point.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def check_surface(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the standalone script without leaving its directory on sys.path."""
    scripts = str(Path(__file__).resolve().parent.parent / "scripts")
    sys.path.insert(0, scripts)
    try:
        import check_surface
    finally:
        sys.path.pop(0)

    # A real checkout ordinarily lives next to this repository. These tests
    # synthesize the exact states they exercise and must never fall through to,
    # much less modify, that working copy.
    monkeypatch.setattr(check_surface, "SIBLINGS", ())
    return check_surface


def _manifest_body(check_surface: ModuleType) -> dict[str, Any]:
    """A manifest that agrees with this SDK's mirror on all three tables.

    Built from the mirror rather than copied from the platform, so these tests
    exercise the READER without also re-pinning the surface — the comparison
    against the real platform is `scripts/check_surface.py` run against a real
    checkout, and duplicating 56 routes here would be a third mirror to keep in
    step.
    """
    from mandala_computer import _api

    routes = sorted(f"{method} {pattern}" for method, pattern in check_surface.mirrored())
    parameters = {
        route: sorted(names)
        for route, names in check_surface.mirrored_parameters().items()
        # The platform omits a route that documents no parameters, and the
        # reader fills those back in. Omitting them here is what makes that
        # behaviour load-bearing in these fixtures rather than incidental.
        if names
    }
    limits = {theirs: getattr(_api, ours) for ours, theirs in check_surface.CONSTANTS}
    return {"version": 1, "routes": routes, "parameters": parameters, "limits": limits}


def _platform(
    check_surface: ModuleType,
    tmp_path: Path,
    *,
    manifest: Any = ...,
    raw: str | None = None,
    missing_marker: Path | None = None,
) -> Path:
    """A synthetic platform checkout, recognized by its marker files.

    ``manifest`` is written as JSON; pass ``None`` to leave the file out
    entirely, or ``raw`` to write bytes that are not JSON at all.
    """
    platform = tmp_path / "platform"
    for marker in check_surface.PLATFORM_MARKERS:
        if marker == missing_marker:
            continue
        path = platform / marker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// synthesized platform source\n")

    path = platform / check_surface.MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw)
    elif manifest is not None:
        body = _manifest_body(check_surface) if manifest is ... else manifest
        path.write_text(json.dumps(body))
    return platform


def _clone_of(check_surface: ModuleType, remote: str, directory: Path) -> Path:
    """A git repository that says it came from ``remote``, and nothing else.

    Enough for recognition by identity: a real ``.git`` and a real remote URL,
    with no commits, no network and no working tree beyond what the caller put
    there.
    """
    directory.mkdir(parents=True, exist_ok=True)
    # Through `git_environment()` for the reason it exists: under an ambient
    # GIT_DIR — a hook, `git rebase --exec`, a wrapper — `git init` re-initializes
    # whatever that names instead of this directory, and the `remote add` then
    # writes a fixture's remote into a real repository.
    env = check_surface.git_environment()
    subprocess.run(
        ("git", "init", "--quiet", str(directory)), check=True, capture_output=True, env=env
    )
    subprocess.run(
        ("git", "-C", str(directory), "remote", "add", "origin", remote),
        check=True,
        capture_output=True,
        env=env,
    )
    return directory


# ---------------------------------------------------------------------------
# Discovery: which directory is the platform
# ---------------------------------------------------------------------------


def test_a_checkout_that_lost_its_manifest_is_recognized_by_its_remote(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OPL-3901, restated against the one file this now reads.

    The manifest IS the comparison, so losing it is precisely the drift this
    check exists to notice — and while recognition was a test of contents,
    losing the compared file made the checkout look absent and the whole
    comparison skipped at exit 0. Identity does not care which files are there.
    """
    platform = _platform(check_surface, tmp_path, manifest=None)
    _clone_of(check_surface, f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", platform)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.platform_repo() == platform
    assert check_surface.main() == 1
    assert str(platform / check_surface.MANIFEST) in capsys.readouterr().out


def test_the_manifest_is_not_a_marker_so_losing_it_stays_loud(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The tidying that would quietly reopen OPL-3901.

    With the manifest as the only file read, it is tempting to make it the
    marker too. Then a copy git cannot vouch for that has LOST the manifest
    stops being the platform at all: instead of "mirror sources are missing" at
    exit 1, the search falls through to "not found, skipping" at exit 0 — the
    exact fail-open, wearing a tidier hat.

    So this checkout has no remote and no manifest, and is still recognized.
    """
    platform = _platform(check_surface, tmp_path, manifest=None)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.remotes(platform) == frozenset()
    assert check_surface.is_platform_checkout(platform) is True
    assert check_surface.main() == 1
    assert "mirror sources are missing" in capsys.readouterr().out


def test_a_checkout_that_lost_a_marker_still_compares_via_its_remote(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The markers are identity only — a checkout with the manifest still passes.

    ``apidoc.ts`` is where the parameter half of the manifest is generated from,
    but this reader no longer opens it. A clone that has the manifest and not
    the marker is a complete answer to the question being asked.
    """
    platform = _platform(check_surface, tmp_path, missing_marker=check_surface.PLATFORM_MARKERS[1])
    _clone_of(check_surface, f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", platform)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.platform_repo() == platform
    assert check_surface.missing_mirror_sources(platform) == []
    assert check_surface.main() == 0


def test_a_clone_with_nothing_in_it_at_all_is_still_the_platform(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The general form: an empty clone is a checkout that is missing everything."""
    platform = _clone_of(
        check_surface, f"https://github.com/{check_surface.PLATFORM_REMOTE}", tmp_path / "platform"
    )
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.platform_repo() == platform
    assert check_surface.main() == 1
    out = capsys.readouterr().out
    for source in check_surface.MIRROR_SOURCES:
        assert str(platform / source) in out


def test_a_clone_of_an_unrelated_repository_is_not_the_platform(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identity widens recognition; it does not hand the name to a neighbour."""
    other = _clone_of(
        check_surface, "git@github.com:mandalacomputer/python-sdk.git", tmp_path / "sdk"
    )
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(other))

    assert check_surface.is_platform_checkout(other) is False
    # And, being a directory somebody named rather than one this guessed at, it
    # is reported rather than passed over.
    with pytest.raises(SystemExit) as exit_info:
        check_surface.platform_repo()
    assert str(other) in str(exit_info.value)


def test_a_copy_git_cannot_vouch_for_is_still_recognized_by_its_files(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An export or a vendored copy has no remote to ask about, and still counts."""
    platform = _platform(check_surface, tmp_path)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.remotes(platform) == frozenset()
    assert check_surface.platform_repo() == platform


def test_a_directory_inside_a_repository_does_not_borrow_its_identity(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``git -C`` answers about the enclosing repo; a plain subdirectory is not it."""
    clone = _clone_of(
        check_surface, f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", tmp_path / "platform"
    )
    inside = clone / "web"
    inside.mkdir()

    assert check_surface.remotes(clone) == frozenset({check_surface.PLATFORM_REMOTE})
    assert check_surface.remotes(inside) == frozenset()
    assert check_surface.is_platform_checkout(inside) is False


def test_an_ambient_git_dir_does_not_answer_for_the_directory_asked_about(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A git hook exports GIT_DIR, and it outranks ``-C``.

    Left in place, every directory this asked about would report the remotes of
    whichever repository the hook fired in.
    """
    platform = _clone_of(
        check_surface, f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", tmp_path / "platform"
    )
    other = _clone_of(
        check_surface, "git@github.com:mandalacomputer/python-sdk.git", tmp_path / "sdk"
    )
    assert check_surface.remotes(other) == frozenset({"mandalacomputer/python-sdk"})

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert check_surface.remotes(platform) == frozenset({check_surface.PLATFORM_REMOTE})


def test_a_genuinely_absent_platform_checkout_still_skips_cleanly(
    check_surface: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No variable and no sibling: the ordinary case, and not a failure.

    This is what CI on this repository looks like, and what most laptops look
    like. Failing over it would make the check something people learn to ignore.
    """
    monkeypatch.delenv("MANDALA_PLATFORM_REPO", raising=False)

    assert check_surface.platform_repo() is None
    assert check_surface.main() == 0
    assert "platform repo not found, skipping" in capsys.readouterr().out


def test_a_variable_pointing_at_no_checkout_fails_instead_of_looking_elsewhere(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPL-4512: the assertion the siblings used to swallow.

    A set-but-wrong value fell through to the sibling search, and both outcomes
    were silent: nothing next door meant "not found, skipping" at exit 0, and
    something next door meant a green answer about a repository the operator did
    not name. The platform's CI sets this variable for three SDKs at once, so a
    path that moves is otherwise indistinguishable from "no platform here" on
    the one run where the comparison is enforced.
    """
    absent = tmp_path / "not-a-platform-checkout"
    absent.mkdir()
    # A sibling that would answer, so the failure is the variable being wrong
    # rather than there being nothing else to find.
    sibling = _platform(check_surface, tmp_path / "next-door")
    monkeypatch.setattr(check_surface, "REPO", sibling.parent / "sdk")
    monkeypatch.setattr(check_surface, "SIBLINGS", (sibling.name,))
    assert check_surface.platform_repo() == sibling

    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(absent))
    with pytest.raises(SystemExit) as exit_info:
        check_surface.platform_repo()
    message = str(exit_info.value)
    assert str(absent) in message
    assert str(check_surface.PLATFORM_MARKERS[0]) in message
    assert str(sibling) not in message


def test_a_variable_set_and_empty_is_not_read_as_no_variable(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed expansion, which is the case this ticket is actually about.

    `MANDALA_PLATFORM_REPO: ${{ github.workspace }}/platform` that does not
    expand leaves an empty string, not an absent key — so reading empty as unset
    would hand exactly that failure back to the sibling search it came from.
    """
    sibling = _platform(check_surface, tmp_path / "next-door")
    monkeypatch.setattr(check_surface, "REPO", sibling.parent / "sdk")
    monkeypatch.setattr(check_surface, "SIBLINGS", (sibling.name,))

    for value in ("", "  "):
        monkeypatch.setenv("MANDALA_PLATFORM_REPO", value)
        with pytest.raises(SystemExit) as exit_info:
            check_surface.platform_repo()
        assert str(sibling) not in str(exit_info.value)


def test_the_variable_is_read_relative_to_the_repository_not_the_caller(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A relative value names one directory, wherever the script was run from.

    The sibling guesses are built from :data:`REPO`; a value left as given would
    be read against the working directory instead, so the same setting would
    mean two different checkouts depending on where the check was invoked.
    """
    platform = _platform(check_surface, tmp_path)
    monkeypatch.setattr(check_surface, "REPO", tmp_path / "sdk")
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", "../platform")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert check_surface.named_platform_repo() == platform
    assert check_surface.platform_repo() == platform


# ---------------------------------------------------------------------------
# The manifest reader: every unreadable shape must fail, not pass
# ---------------------------------------------------------------------------


def test_a_manifest_that_agrees_with_the_mirror_passes_and_says_so(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The green path, and the counts it is allowed to print."""
    platform = _platform(check_surface, tmp_path)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 0
    out = capsys.readouterr().out
    assert f"{len(check_surface.mirrored())} routes" in out
    assert f"{len(check_surface.CONSTANTS)} limits" in out


def test_a_route_documenting_no_parameters_is_not_reported_as_drift(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The manifest omits them; the mirror lists them as empty sets.

    Read literally, that is 27 routes "no longer documented upstream" on a
    mirror that is exactly right — enough noise to make the check worth
    ignoring, which is the failure one step past a false green.
    """
    body = _manifest_body(check_surface)
    documented = set(body["parameters"])
    assert documented < set(body["routes"]), "fixture must exercise the omitted-route case"

    platform = _platform(check_surface, tmp_path, manifest=body)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))
    assert check_surface.main() == 0


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("truncated mid-object", '{"version": 1, "routes": ['),
        ("empty file", ""),
        ("HTML from a proxy", "<!doctype html><title>404</title>"),
        ("a JSON array", "[]"),
        ("a JSON string", '"surface"'),
    ],
)
def test_a_manifest_that_is_not_a_json_object_fails(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    raw: str,
) -> None:
    """None of these may reach the comparison as "the platform documents nothing"."""
    platform = _platform(check_surface, tmp_path, raw=raw)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1, name
    assert "compares nothing it cannot read" in capsys.readouterr().out


def _without(body: dict[str, Any], key: str) -> dict[str, Any]:
    """A valid manifest with one top-level key removed."""
    del body[key]
    return body


def _set(body: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    body[key] = value
    return body


def _first_route_with_parameters(body: dict[str, Any]) -> str:
    return min(body["parameters"])


#: One defect apiece, each applied to an OTHERWISE VALID manifest, with the
#: diagnostic it must produce.
#:
#: Built this way because the first version of this table was not. Its cases
#: were hand-written stubs like ``{"version": "1", "routes": [], ...}``, and an
#: empty ``routes`` list fails on its own — so the case meant to pin the VERSION
#: check passed with the version check deleted. A review found it by neutralising
#: the guard and watching its own regression test stay green, which is the exact
#: shape of a test that cannot regress.
#:
#: So: start from something that passes, break one thing, and assert the message
#: that names the thing broken. A guard removed now fails the case named for it,
#: and only that case.
MALFORMED: list[tuple[str, Any, str]] = [
    ("no version", lambda b: _without(b, "version"), "has no integer 'version'"),
    ("version is a string", lambda b: _set(b, "version", "1"), "has no integer 'version'"),
    ("version is a bool", lambda b: _set(b, "version", True), "has no integer 'version'"),
    # The sharpest of them. A future layout that moved `parameters` under a new
    # key reads, leniently, as a platform that documents no parameters at all.
    ("a version from the future", lambda b: _set(b, "version", 2), "this reader knows 1"),
    ("no routes", lambda b: _without(b, "routes"), "has no 'routes'"),
    ("no parameters", lambda b: _without(b, "parameters"), "has no 'parameters'"),
    ("no limits", lambda b: _without(b, "limits"), "has no 'limits'"),
    # A NON-EMPTY object, keyed by the routes that were there. `{}` would be
    # refused by the emptiness half of the same guard, so it pins nothing about
    # the type half — and iterating a dict yields its KEYS, so a mapping spelled
    # this way survives the loop and produces a valid-looking route set while
    # every value in it is ignored. That is the fail-open, not a hypothetical.
    (
        "routes is a non-empty object",
        lambda b: _set(b, "routes", dict.fromkeys(b["routes"])),
        "'routes' is not a non-empty array",
    ),
    ("routes is empty", lambda b: _set(b, "routes", []), "'routes' is not a non-empty array"),
    (
        "a route is a number",
        lambda b: _set(b, "routes", [*b["routes"], 7]),
        "'routes' holds a non-string entry",
    ),
    (
        "a route has no method",
        lambda b: _set(b, "routes", [*b["routes"], "widgets"]),
        "is not 'METHOD pattern'",
    ),
    (
        "a route method is lowercase",
        lambda b: _set(b, "routes", [*b["routes"], "get widgets"]),
        "is not 'METHOD pattern'",
    ),
    (
        "a route has no pattern",
        lambda b: _set(b, "routes", [*b["routes"], "GET "]),
        "is not 'METHOD pattern'",
    ),
    (
        "a route has a space in its pattern",
        lambda b: _set(b, "routes", [*b["routes"], "GET wid gets"]),
        "is not 'METHOD pattern'",
    ),
    (
        "a route is listed twice",
        lambda b: _set(b, "routes", [*b["routes"], b["routes"][0]]),
        "twice",
    ),
    (
        "parameters is an array",
        lambda b: _set(b, "parameters", []),
        "'parameters' is not an object",
    ),
    (
        "parameters documents an unknown route",
        lambda b: _set(b, "parameters", {**b["parameters"], "GET widgets": ["query:w"]}),
        "which is not in 'routes'",
    ),
    (
        "a parameter list is a string",
        lambda b: _set(
            b, "parameters", {**b["parameters"], _first_route_with_parameters(b): "query:w"}
        ),
        "is not an array",
    ),
    (
        "a parameter is a number",
        lambda b: _set(b, "parameters", {**b["parameters"], _first_route_with_parameters(b): [7]}),
        "holds a non-string",
    ),
    (
        "a parameter names no kind",
        lambda b: _set(
            b, "parameters", {**b["parameters"], _first_route_with_parameters(b): ["w"]}
        ),
        "names no query:, header: or body: field",
    ),
    ("limits is an array", lambda b: _set(b, "limits", []), "'limits' is not an object"),
    (
        "a limit is a string",
        lambda b: _set(b, "limits", {**b["limits"], "agent.maxSteps": "100"}),
        "which is not an integer",
    ),
    (
        "a limit is a bool",
        lambda b: _set(b, "limits", {**b["limits"], "agent.maxSteps": True}),
        "which is not an integer",
    ),
]


@pytest.mark.parametrize(("name", "break_it", "expected"), MALFORMED, ids=[m[0] for m in MALFORMED])
def test_a_manifest_this_reader_cannot_understand_fails(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    break_it: Any,
    expected: str,
) -> None:
    """Each of these used to have a plausible "read it leniently" answer.

    Every one of those answers is a green run over a table that is empty for a
    reason nobody was told about.
    """
    platform = _platform(check_surface, tmp_path, manifest=break_it(_manifest_body(check_surface)))
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1, name
    out = capsys.readouterr().out
    assert "compares nothing it cannot read" in out, name
    # The specific diagnostic, not merely a failure. Without this the case is
    # satisfied by any guard at all, including one it was not written for.
    assert expected in out, f"{name}: expected {expected!r} in:\n{out}"


def test_every_malformed_case_starts_from_a_manifest_that_passes(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property that makes the table above mean anything.

    If the unmutated body did not pass, a case could be satisfied by the defect
    it was born with rather than the one it names — which is how the first
    version of this table pinned nothing.
    """
    platform = _platform(check_surface, tmp_path)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))
    assert check_surface.main() == 0


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        (
            "two routes tables",
            (
                '{"version": 1, "routes": ["GET widgets"], "routes": ["GET sizes"], '
                '"parameters": {}, "limits": {}}'
            ),
        ),
        (
            "one route documented twice",
            (
                '{"version": 1, "routes": ["GET sizes"], "parameters": '
                '{"GET sizes": ["query:a"], "GET sizes": ["query:b"]}, "limits": {}}'
            ),
        ),
        (
            "one limit written twice",
            (
                '{"version": 1, "routes": ["GET sizes"], "parameters": {}, '
                '"limits": {"agent.maxSteps": 100, "agent.maxSteps": 999}}'
            ),
        ),
    ],
)
def test_a_manifest_naming_a_key_twice_is_refused(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    raw: str,
) -> None:
    """``json.loads`` keeps the LAST of a repeated key and says nothing.

    So a manifest carrying two ``routes`` tables, two entries for one route, or
    one limit written twice with different values would be compared against
    whichever copy came last, with the other discarded before any shape check
    saw it — a green run over data this never read. The TypeScript reader this
    replaced refused duplicate keys explicitly; dropping that on the way across
    would have been the one fail-open carried into the replacement.
    """
    platform = _platform(check_surface, tmp_path, raw=raw)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1, name
    assert "twice in one object" in capsys.readouterr().out, name


def test_a_limit_this_sdk_mirrors_and_the_manifest_drops_is_reported(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The most dangerous of the three drifts, and the quietest.

    The SDK goes on refusing values early against a ceiling the platform has
    stopped publishing — turning a favour into a refusal of a run the platform
    would have taken, with nothing anywhere saying why. Skipping an absent key
    would make that permanent.
    """
    body = _manifest_body(check_surface)
    dropped = check_surface.CONSTANTS[0][1]
    del body["limits"][dropped]

    platform = _platform(check_surface, tmp_path, manifest=body)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1
    assert dropped in capsys.readouterr().out


def test_a_drifted_limit_is_detected_and_named(check_surface: ModuleType, tmp_path: Path) -> None:
    from mandala_computer import _api

    body = _manifest_body(check_surface)
    assert check_surface.limit_drift(body["limits"]) == []

    body["limits"]["exec.maxTimeoutSeconds"] = _api.MAX_EXEC_TIMEOUT_SECONDS + 1
    assert check_surface.limit_drift(body["limits"]) == [
        (
            f"  ! MAX_EXEC_TIMEOUT_SECONDS is {_api.MAX_EXEC_TIMEOUT_SECONDS}, "
            f"but the manifest's exec.maxTimeoutSeconds is "
            f"{_api.MAX_EXEC_TIMEOUT_SECONDS + 1}"
        )
    ]


def test_an_added_route_cannot_agree_with_a_stale_mirror(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The original defect, end to end: a route upstream that the mirror lacks."""
    body = _manifest_body(check_surface)
    body["routes"].append("POST widgets")

    platform = _platform(check_surface, tmp_path, manifest=body)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1
    assert "+ POST widgets  (upstream, missing from ALLOWED)" in capsys.readouterr().out


def test_an_added_parameter_on_a_known_route_cannot_agree_either(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OPL-3727 restated: the whole feature that arrived without a route.

    ``Range`` on ``GET computers/:id/files`` is the only way a file larger than
    one request comes off a computer at all, and it landed on a route the mirror
    already listed. A route table cannot see it.
    """
    body = _manifest_body(check_surface)
    body["parameters"].setdefault("GET computers/:id/files", []).append("header:X-New-Thing")

    platform = _platform(check_surface, tmp_path, manifest=body)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1
    assert "header:X-New-Thing  (upstream, missing from PARAMETERS)" in capsys.readouterr().out
