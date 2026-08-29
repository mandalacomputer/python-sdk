"""The platform surface check's checkout discovery and failure boundaries."""

from __future__ import annotations

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
