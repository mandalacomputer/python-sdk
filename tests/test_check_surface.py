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


def _clone_of(remote: str, directory: Path) -> Path:
    """A git repository that says it came from ``remote``, and nothing else.

    Enough for recognition by identity: a real ``.git`` and a real remote URL,
    with no commits, no network and no working tree beyond what the caller put
    there.
    """
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "--quiet", str(directory)), check=True, capture_output=True)
    subprocess.run(
        ("git", "-C", str(directory), "remote", "add", "origin", remote),
        check=True,
        capture_output=True,
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
    _clone_of(f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", platform)
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
        f"https://github.com/{check_surface.PLATFORM_REMOTE}", tmp_path / "platform"
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
    other = _clone_of("git@github.com:mandalacomputer/python-sdk.git", tmp_path / "sdk")
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(other))

    assert check_surface.platform_repo() is None
    assert check_surface.main() == 0
    assert "platform repo not found, skipping" in capsys.readouterr().out


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
    clone = _clone_of(f"git@github.com:{check_surface.PLATFORM_REMOTE}.git", tmp_path / "platform")
    inside = clone / "web"
    inside.mkdir()
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(inside))

    assert check_surface.remotes(inside) == frozenset()
    assert check_surface.platform_repo() is None


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    absent = tmp_path / "not-a-platform-checkout"
    absent.mkdir()
    monkeypatch.setenv("MANDALA_PLATFORM_REPO", str(absent))

    assert check_surface.platform_repo() is None
    assert check_surface.main() == 0
    assert "platform repo not found, skipping" in capsys.readouterr().out
