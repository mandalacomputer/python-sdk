"""The platform surface check's checkout discovery and failure boundaries."""

from __future__ import annotations

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


def _platform_without(check_surface: ModuleType, tmp_path: Path, missing: Path) -> Path:
    """A recognized synthetic checkout with exactly one mirror source absent."""
    platform = tmp_path / "platform"
    for source in check_surface.MIRROR_SOURCES:
        if source == missing:
            continue
        path = platform / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// synthesized platform source\n")
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


def test_a_checkout_that_lost_only_apidoc_ts_is_recognized_by_its_remote(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OPL-3901: the fail-open that marker files left one file wide.

    ``apidoc.ts`` is the parameter table, so losing it is precisely the drift
    this check exists to notice — and while recognition was a test of contents,
    losing it made the checkout look absent and the whole comparison skipped at
    exit 0. Identity does not care which files are there.
    """
    platform = _platform_without(check_surface, tmp_path, check_surface.APIDOC)
    _clone_of(check_surface, f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", platform)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.platform_repo() == platform
    assert check_surface.main() == 1
    assert str(platform / check_surface.APIDOC) in capsys.readouterr().out


def test_a_clone_with_no_mirror_sources_at_all_is_still_the_platform(
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
    capsys: pytest.CaptureFixture[str],
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
    platform = _platform_without(check_surface, tmp_path, missing=Path("nothing/is/missing"))
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


def test_a_recognized_checkout_missing_a_constants_module_fails_and_names_it(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    platform = _platform_without(check_surface, tmp_path, check_surface.CLIPBOARD)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.platform_repo() == platform
    assert check_surface.main() == 1
    assert str(platform / check_surface.CLIPBOARD) in capsys.readouterr().out


def test_a_recognized_checkout_missing_agent_ts_fails_instead_of_skipping(
    check_surface: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    platform = _platform_without(check_surface, tmp_path, check_surface.AGENT)
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(platform))

    assert check_surface.platform_repo() == platform
    assert check_surface.main() == 1
    assert str(platform / check_surface.AGENT) in capsys.readouterr().out


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
    sibling = _platform_without(check_surface, tmp_path / "next-door", Path("nothing/is/missing"))
    monkeypatch.setattr(check_surface, "REPO", sibling.parent / "sdk")
    monkeypatch.setattr(check_surface, "SIBLINGS", (sibling.name,))
    assert check_surface.platform_repo() == sibling

    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(absent))
    with pytest.raises(SystemExit) as exit_info:
        check_surface.platform_repo()
    message = str(exit_info.value)
    assert str(absent) in message
    assert str(check_surface.SURFACE) in message
    assert str(sibling) not in message


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
    platform = _platform_without(check_surface, tmp_path, missing=Path("nothing/is/missing"))
    monkeypatch.setattr(check_surface, "REPO", tmp_path / "sdk")
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", "../platform")

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert check_surface.named_platform_repo() == platform
    assert check_surface.platform_repo() == platform
