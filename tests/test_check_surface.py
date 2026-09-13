"""The platform surface check's checkout discovery and failure boundaries."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

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


#: The files that make a directory the platform to a copy git cannot vouch for.
IDENTITY = (Path("web/lib/surface.ts"), Path("web/lib/apidoc.ts"))


def _manifest_of(check_surface: ModuleType) -> dict[str, object]:
    """A manifest exactly in step with this repository's own mirror.

    Built from the tables and constants rather than copied from a platform
    checkout, so every drift case below starts from a known agreement and
    changes one thing.
    """
    from tests.surface_tables import ALLOWED, PARAMETERS

    from mandala_computer import _api

    return {
        "version": check_surface.MANIFEST_VERSION,
        "routes": sorted(f"{method} {pattern}" for method, pattern in ALLOWED),
        "parameters": {route: sorted(names) for route, names in PARAMETERS.items() if names},
        "limits": {key: getattr(_api, ours) for ours, key in check_surface.LIMITS},
    }


def _platform_with(
    check_surface: ModuleType, tmp_path: Path, manifest: dict[str, object] | str | None
) -> Path:
    """A recognized synthetic checkout holding ``manifest`` — or, for ``None``, no manifest."""
    platform = tmp_path / "platform"
    platform.mkdir(parents=True, exist_ok=True)
    # Spelled here rather than read off the implementation: a fixture that wrote
    # whatever PLATFORM_MARKERS named would, on a version where the manifest WAS
    # the marker, write a manifest for the very test that asserts there is none
    # (second review).
    for marker in IDENTITY:
        (platform / marker).parent.mkdir(parents=True, exist_ok=True)
        (platform / marker).write_text("// synthesized platform source\n")
    # A test may build the platform twice in one directory; whatever the earlier
    # build wrote is not this call's manifest.
    (platform / check_surface.MANIFEST).unlink(missing_ok=True)
    if manifest is not None:
        text = manifest if isinstance(manifest, str) else json.dumps(manifest)
        (platform / check_surface.MANIFEST).write_text(text)
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


def test_a_checkout_that_lost_the_manifest_is_recognized_by_its_remote(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OPL-3901: the fail-open that marker files left one file wide.

    The manifest is the whole comparison, so losing it is precisely the drift
    this check exists to notice — and while recognition was a test of contents,
    losing the file made the checkout look absent and the comparison skipped at
    exit 0. Identity does not care which files are there.
    """
    platform = _platform_with(check_surface, tmp_path, None)
    _clone_of(check_surface, f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", platform)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.platform_repo() == platform
    assert check_surface.main() == 1
    out = capsys.readouterr().out
    assert str(platform / check_surface.MANIFEST) in out
    assert "in step" not in out


def test_an_empty_clone_is_still_the_platform(
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
    assert str(platform / check_surface.MANIFEST) in capsys.readouterr().out


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
    platform = _platform_with(check_surface, tmp_path, _manifest_of(check_surface))
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
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(inside))

    assert check_surface.remotes(inside) == frozenset()
    assert check_surface.is_platform_checkout(inside) is False
    with pytest.raises(SystemExit):
        check_surface.platform_repo()


def test_an_ambient_git_dir_does_not_answer_for_the_directory_asked_about(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GIT_DIR`` outranks ``-C``, and a git hook exports one.

    Both directions are wrong and both are reachable from a hook or a wrapper
    that runs this check: an unrelated clone answering with the platform's name,
    and a platform sibling answering with the SDK's — the second being OPL-3901
    again, since recognition then falls back to the marker files that a missing
    ``apidoc.ts`` defeats.
    """
    platform = _clone_of(
        check_surface, f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", tmp_path / "app"
    )
    other = _clone_of(
        check_surface, "git@github.com:mandalacomputer/python-sdk.git", tmp_path / "sdk"
    )

    monkeypatch.setenv("GIT_DIR", str(platform / ".git"))
    assert check_surface.remotes(other) == frozenset({"mandalacomputer/python-sdk"})

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert check_surface.remotes(platform) == frozenset({check_surface.PLATFORM_REMOTE})


def test_an_export_recognized_by_its_files_but_lacking_the_manifest_fails(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Identity is not the manifest: a copy that lost it must fail, not skip.

    With the manifest as the only marker, a sibling export that predated or lost
    it was "not the platform", and the automatic search skipped at exit 0 — the
    silent path this check exists to refuse (review of OPL-4837).
    """
    monkeypatch.delenv("MANDALA_PLATFORM_REPO", raising=False)
    sibling = _platform_with(check_surface, tmp_path / "next-door", None)
    monkeypatch.setattr(check_surface, "REPO", sibling.parent / "sdk")
    monkeypatch.setattr(check_surface, "SIBLINGS", (sibling.name,))

    assert check_surface.platform_repo() == sibling
    assert check_surface.main() == 1
    out = capsys.readouterr().out
    assert str(sibling / check_surface.MANIFEST) in out
    assert "skipping" not in out


def test_a_key_that_appears_twice_is_refused_rather_than_read_last(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``json.loads`` keeps the last of two equal keys; this must not (review of OPL-4837)."""
    manifest = json.dumps(_manifest_of(check_surface))
    twice_limits = manifest.replace('"limits": {', '"limits": {"exec.maxTimeoutSeconds": 601, ', 1)
    assert twice_limits != manifest
    # A route the real table already carries, so the second entry is a repeat and
    # not an addition the diff would report for a different reason.
    twice_route = manifest.replace(
        '"parameters": {', '"parameters": {"DELETE computers/:id": ["query:new_required"], ', 1
    )
    for text in (twice_limits, twice_route):
        platform = _platform_with(check_surface, tmp_path, text)
        monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))
        assert check_surface.main() == 1
        out = capsys.readouterr().out
        assert "appears twice" in out
        assert "in step" not in out


def test_constants_are_read_from_this_checkout_and_not_the_installed_sdk(
    check_surface: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A constant changed only on the branch under review must be the one compared.

    A bare import answered with whatever SDK the interpreter had installed — on a
    laptop, a sibling checkout — so the branch's own value was never looked at
    (review of OPL-4837).
    """
    import types

    fake = types.ModuleType("mandala_computer._api")
    fake.MAX_STEPS = -1  # type: ignore[attr-defined]
    fake.__file__ = "/elsewhere/mandala_computer/_api.py"
    monkeypatch.setitem(sys.modules, "mandala_computer._api", fake)
    api = check_surface._sdk_api()
    assert Path(api.__file__).resolve().is_relative_to(check_surface.REPO / "src")
    assert api.MAX_STEPS > 0
    # And the interpreter's own module table is as it was.
    assert sys.modules["mandala_computer._api"] is fake

    monkeypatch.setattr(check_surface, "REPO", Path("/nowhere/at/all"))
    with pytest.raises(check_surface.ManifestError, match="this checkout"):
        check_surface._sdk_api()


def test_the_installed_sdk_does_not_answer_for_this_checkouts_constants(
    check_surface: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the comparison itself: a wrong installed copy, a right checkout, no drift."""
    import types

    # Built first: the fixture reads the real constants, and must not meet the fake.
    manifest = _manifest_of(check_surface)
    fake = types.ModuleType("mandala_computer._api")
    fake.MAX_STEPS = -1  # type: ignore[attr-defined]
    fake.__file__ = "/elsewhere/mandala_computer/_api.py"
    monkeypatch.setitem(sys.modules, "mandala_computer", types.ModuleType("mandala_computer"))
    monkeypatch.setitem(sys.modules, "mandala_computer._api", fake)
    assert check_surface.constant_drift(manifest) == []


def test_a_src_that_is_a_symlink_elsewhere_is_refused(
    check_surface: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving both sides to the same foreign place is not agreement (second review)."""
    elsewhere = tmp_path / "sibling" / "src"
    elsewhere.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(check_surface, "REPO", repo)
    with pytest.raises(check_surface.ManifestError, match="outside this checkout"):
        check_surface._sdk_api()


def test_an_importer_that_moves_the_path_and_raises_leaves_it_as_it_was(
    check_surface: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    before = list(sys.path)

    def explode(name: str) -> ModuleType:
        sys.path.insert(0, "/an/importer/prepended/this")
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", explode)
    with pytest.raises(ImportError):
        check_surface._sdk_api()
    assert sys.path == before


def test_a_manifest_in_step_with_the_mirror_says_so_with_the_counts(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The success line names what was compared, so a shrunken comparison reads as one."""
    from tests.surface_tables import ALLOWED, PARAMETERS

    platform = _platform_with(check_surface, tmp_path, _manifest_of(check_surface))
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 0
    out = capsys.readouterr().out
    counted = sum(len(names) for names in PARAMETERS.values())
    assert (
        f"{len(ALLOWED)} routes, {counted} parameters and {len(check_surface.LIMITS)} constants"
        in out
    )


@pytest.mark.parametrize(
    ("broken", "said"),
    [
        ("not json {", "cannot be read"),
        ("[]", "not a JSON object"),
        ({"version": 2, "routes": ["GET x"], "parameters": {}, "limits": {}}, "version 2"),
        ({"version": 1, "routes": [], "parameters": {}, "limits": {}}, "lists no routes"),
        ({"version": 1, "parameters": {}, "limits": {}}, "lists no routes"),
        (
            {"version": 1, "routes": ["sizes"], "parameters": {}, "limits": {}},
            "not 'METHOD pattern'",
        ),
        (
            {"version": 1, "routes": ["FETCH sizes"], "parameters": {}, "limits": {}},
            "not 'METHOD pattern'",
        ),
        ({"version": 1, "routes": ["GET sizes"], "limits": {}}, "no parameters table"),
        (
            {"version": 1, "routes": ["GET sizes"], "parameters": {"GET gone": []}, "limits": {}},
            "route it does not list",
        ),
        (
            {
                "version": 1,
                "routes": ["GET sizes"],
                "parameters": {"GET sizes": ["fresh"]},
                "limits": {},
            },
            "parameter list this cannot read",
        ),
        ({"version": 1, "routes": ["GET sizes"], "parameters": {}}, "no limits table"),
        ({"version": 1, "routes": ["GET sizes"], "parameters": {}, "limits": {}}, "agent.maxSteps"),
    ],
)
def test_a_manifest_this_cannot_read_fails_instead_of_comparing_nothing(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broken: dict[str, object] | str,
    said: str,
) -> None:
    """The false all-clear, made into a failure that names itself (OPL-4837).

    The recurring defect in the scanners this replaced was printing "in step"
    because the scan had silently read nothing. A diff over a half-read manifest
    passes for the same reason, so every shape short of the whole one is refused
    — and the refusal says which.
    """
    platform = _platform_with(check_surface, tmp_path, broken)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1
    out = capsys.readouterr().out
    assert said in out
    assert "in step" not in out


def test_a_limit_that_moved_upstream_is_named(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from mandala_computer import _api

    manifest = _manifest_of(check_surface)
    assert check_surface.constant_drift(manifest) == []

    limits = manifest["limits"]
    assert isinstance(limits, dict)
    limits["exec.maxTimeoutSeconds"] = _api.MAX_EXEC_TIMEOUT_SECONDS + 1
    expected = (
        f"  ! MAX_EXEC_TIMEOUT_SECONDS is {_api.MAX_EXEC_TIMEOUT_SECONDS}, "
        f"but the platform's exec.maxTimeoutSeconds is {_api.MAX_EXEC_TIMEOUT_SECONDS + 1}"
    )
    assert check_surface.constant_drift(manifest) == [expected]
    platform = _platform_with(check_surface, tmp_path, manifest)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))
    assert check_surface.main() == 1
    assert "exec.maxTimeoutSeconds" in capsys.readouterr().out


def test_a_parameter_that_moved_in_either_direction_is_named(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A route the mirror already knows can still gain or lose a parameter."""
    manifest = _manifest_of(check_surface)
    parameters = manifest["parameters"]
    assert isinstance(parameters, dict)
    parameters["GET sizes"] = ["query:fresh"]
    parameters["DELETE computers/:id"] = ["query:expect"]
    platform = _platform_with(check_surface, tmp_path, manifest)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1
    out = capsys.readouterr().out
    assert "+ GET sizes  query:fresh  (upstream, missing from PARAMETERS)" in out
    assert "- DELETE computers/:id  query:snapshots  (in PARAMETERS, gone from upstream)" in out


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
    # Whatever the shell running this suite points at is not this test's concern.
    monkeypatch.delenv("MANDALA_PLATFORM_REPO", raising=False)
    absent = tmp_path / "not-a-platform-checkout"
    absent.mkdir()
    # A sibling that would answer, so the failure is the variable being wrong
    # rather than there being nothing else to find.
    sibling = _platform_with(check_surface, tmp_path / "next-door", _manifest_of(check_surface))
    monkeypatch.setattr(check_surface, "REPO", sibling.parent / "sdk")
    monkeypatch.setattr(check_surface, "SIBLINGS", (sibling.name,))
    assert check_surface.platform_repo() == sibling

    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(absent))
    with pytest.raises(SystemExit) as exit_info:
        check_surface.platform_repo()
    message = str(exit_info.value)
    assert str(absent) in message
    assert all(str(marker) in message for marker in check_surface.PLATFORM_MARKERS)
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
    sibling = _platform_with(check_surface, tmp_path / "next-door", _manifest_of(check_surface))
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
    platform = _platform_with(check_surface, tmp_path, _manifest_of(check_surface))
    monkeypatch.setattr(check_surface, "REPO", tmp_path / "sdk")
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", "../platform")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert check_surface.named_platform_repo() == platform
    assert check_surface.platform_repo() == platform


def test_a_route_that_moved_in_either_direction_is_named(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The comparison this exists for: the manifest and the mirror disagree on a route."""
    manifest = _manifest_of(check_surface)
    listed = manifest["routes"]
    assert isinstance(listed, list)
    listed.remove("GET sizes")
    listed.append("POST widgets")
    platform = _platform_with(check_surface, tmp_path, manifest)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.main() == 1
    out = capsys.readouterr().out
    assert "+ POST widgets  (upstream, missing from ALLOWED)" in out
    assert "- GET sizes  (in ALLOWED, gone from upstream)" in out
    assert "in step" not in out
