"""The gate that keeps the platform's internals out of this public repo.

Its sibling `scripts/check_surface.py` has had tests since it was written, and
this one shipped without any — which is how its first cut came to report the repo
clean while fifteen internal identifiers were still going out in the wheel
(/code-review). By its own argument, an unchecked mirror is a comment.

What these pin is the shape of what it catches, not a list of what it caught: a
scanner that matches only the spellings of the last scrub is the snapshot the
hashed, platform-derived name list exists to replace.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_internals.py"
sys.path.insert(0, str(SCRIPT.parent))

import check_internals as ci


def digests_for(*names: str) -> set[str]:
    return {hashlib.sha256(n.encode()).hexdigest()[:12] for n in names}


def test_the_repo_itself_is_clean() -> None:
    """The check every other test here is only a proxy for."""
    assert ci.main([]) == 0


@pytest.mark.parametrize(
    "line",
    [
        "# see server/vm.go for the ordering",
        "# the switch in fileevents.go decides",  # no `server/` prefix
        "# server/pkg/nested/thing.go",  # nested
        "# webhookSign.go",  # capitals
        "# api_v2.go",  # digits and underscores
    ],
)
def test_a_go_file_is_caught_however_it_is_spelled(line: str) -> None:
    """This client has no Go, so any .go in it is somebody else's file.

    The first cut required a `server/` prefix and missed four references
    spelled without one.
    """
    assert ci.scan_text(line, "f.py", set())


@pytest.mark.parametrize(
    "line",
    [
        "# lib/apidoc says otherwise",
        "# web/lib/projection.ts sets it",
        "# lib/hvproxy forwards the header",
        "# server/api.go",
    ],
)
def test_a_platform_module_is_caught_with_or_without_its_extension(line: str) -> None:
    assert ci.scan_text(line, "f.py", set())


def test_an_identifier_is_caught_by_its_hash_and_reported_by_its_name() -> None:
    """The point of hashing: the list is not a map, but the message still names
    the token, because the token is already in the file being read."""
    found = ci.scan_text("# mirrored from snapCtxWidget", "f.py", digests_for("snapCtxWidget"))
    assert len(found) == 1
    assert "snapCtxWidget" in found[0]


def test_short_words_are_never_identifiers() -> None:
    """`status`, `list` and `start` are every client's own vocabulary. A gate
    that fires on them is a gate somebody deletes."""
    assert not ci.scan_text("# status list start", "f.py", digests_for("status", "list", "start"))


def test_ordinary_prose_about_the_platform_survives() -> None:
    """The comments this repo is FOR. A scanner that eats these is worse than
    none: it trains the next author to say less about behaviour."""
    prose = (
        "# The platform reads an empty expectation as no expectation, so the\n"
        "# interlock is disarmed on the one route that destroys a computer.\n"
        "# A start that has been admitted holds its memory before its process exists.\n"
    )
    assert not ci.scan_text(prose, "f.py", ci.load_digests())


def test_an_allowlisted_file_is_the_only_way_past_it() -> None:
    assert "scripts/check_surface.py" in ci.ALLOWED_FILES
    # And the allowlist is by FILE: the same text elsewhere still fails.
    assert ci.scan_text("# web/lib/surface.ts", "src/mandala_computer/_api.py", set())


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_a_range_that_does_not_exist_explains_itself_rather_than_crashing() -> None:
    """A traceback reads as the tool being broken, which is how a CI step gets
    deleted. The likely cause is a shallow clone, so it says so."""
    result = run("--messages", "nosuchref..HEAD")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "fetch-depth" in result.stderr


def test_messages_without_a_range_is_refused_rather_than_silently_empty() -> None:
    """`--messages` with nothing after it scanned no commits and still printed
    the all-clear (/code-review)."""
    result = run("--messages", "")
    assert result.returncode == 2
    assert "revision range" in result.stderr


def test_the_message_scan_reads_every_commit_in_the_range(tmp_path: Path) -> None:
    """Two commits, one clean and one not, so a scanner that reads only the
    first or only the last fails here."""
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a").write_text("1")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "first, and clean"], cwd=repo, check=True)
    (repo / "a").write_text("2")
    subprocess.run(["git", "commit", "-qam", "second: see server/vm.go"], cwd=repo, check=True)

    original = ci.ROOT
    try:
        ci.ROOT = repo
        found = ci.scan_messages("HEAD~1..HEAD", set())
    finally:
        ci.ROOT = original
    assert len(found) == 1
    assert "server/vm.go" in found[0]
