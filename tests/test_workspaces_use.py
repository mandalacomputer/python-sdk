"""``mandala-py workspaces use`` and ``workspaces current`` (OPL-5499): a default
workspace per saved profile, kept in ``~/.mandala/defaults.json`` beside
``credentials.json``, which never changes."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mandala_computer import _cli
from mandala_computer import _credentials as credentials
from mandala_computer import _defaults as defaults

BASE = "https://api.test/api/v1"
ACCOUNT = "acc-000000000001"
WORKSPACE = {"id": "wsp-0123456789ab", "name": "customers", "created_at": "2026-09-01T00:00:00Z"}
OTHER = {"id": "wsp-ba9876543210", "name": "research", "created_at": "2026-09-01T00:00:00Z"}
SECRET = {
    "id": "csec-0123456789abcdef",
    "name": "OPENAI_API_KEY",
    "workspace_id": None,
    "revision_id": "csr-0123456789abcdef01234567",
    "created_at": "2026-09-20T12:00:00Z",
    "updated_at": "2026-09-20T12:00:00Z",
    "last_used_at": None,
}
LIMITS = {
    "name_max_chars": 60,
    "value_max_bytes": 4096,
    "active_per_account": 100,
    "created_per_account": 1000,
}
LISTING = {"secrets": [SECRET], "delivery": True, "limits": LIMITS}
API_KEY_CREATED: dict[str, Any] = {
    "id": "key-0f1e2d3c4b5a",
    "name": "ci",
    "prefix": "com_1a2b3c4d…",
    "created_at": "2026-09-20T08:00:00.000Z",
    "last_used_at": None,
    "workspace_id": OTHER["id"],
    "workspace_name": OTHER["name"],
    "manage_keys": False,
    "raw": "com_" + "ab" * 24,
}
FIXTURE = Path(__file__).parent / "fixtures" / "defaults-v1.json"


def _profile(scope: dict[str, Any] | None = None, account: str = ACCOUNT) -> dict[str, Any]:
    return {
        "api_key": "com_profile_key",
        "base_url": BASE,
        "key_id": "key-000000000001",
        "account": {"id": account, "name": "Acme"},
        "scope": scope or {"type": "account"},
    }


CONFINED = {"type": "workspace", "workspace_id": WORKSPACE["id"], "workspace_name": "customers"}


def _store(root: Path, default_profile: str, **profiles: dict[str, Any]) -> Path:
    directory = root / ".mandala"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "credentials.json"
    path.write_text(
        json.dumps(
            {"version": 1, "default_profile": default_profile, "profiles": profiles}, indent=2
        )
    )
    path.chmod(0o600)
    return path


def _default(workspace: dict[str, Any], account: str = ACCOUNT) -> defaults.WorkspaceDefault:
    return defaults.WorkspaceDefault(account, workspace["id"], workspace["name"])


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MANDALA_API_KEY", raising=False)
    monkeypatch.delenv("MANDALA_BASE_URL", raising=False)
    monkeypatch.delenv("MANDALA_PROFILE", raising=False)
    return tmp_path


@pytest.fixture
def api() -> Any:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/workspaces").mock(httpx.Response(200, json=[WORKSPACE, OTHER]))
        router.get(f"{BASE}/workspaces/{OTHER['id']}").mock(httpx.Response(200, json=OTHER))
        router.get(f"{BASE}/workspaces/{WORKSPACE['id']}").mock(httpx.Response(200, json=WORKSPACE))
        router.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
        router.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
        router.delete(f"{BASE}/secrets/{SECRET['id']}").mock(httpx.Response(200, json={"ok": True}))
        router.post(f"{BASE}/api-keys").mock(httpx.Response(201, json=API_KEY_CREATED))
        yield router


def _defaults_path(home: Path) -> Path:
    return home / ".mandala" / "defaults.json"


def _sent(request: httpx.Request) -> str | None:
    """The workspace a secrets or api-keys request was sent for, wherever it went."""
    if "workspace_id" in request.url.params:
        return request.url.params["workspace_id"]
    if request.content:
        return json.loads(request.content).get("workspace_id")
    return None


def _routes(api: Any) -> list[tuple[str, str]]:
    return [(c.request.method, c.request.url.path) for c in api.calls]


class _Stdin(io.TextIOWrapper):
    def isatty(self) -> bool:
        return False


# --- workspaces use -------------------------------------------------------------


def test_use_saves_the_default_alone_and_current_reports_it(
    home: Path, api: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _store(home, "default", default=_profile())
    before = path.read_bytes()
    assert _cli.main(["workspaces", "use", OTHER["id"]]) == 0
    assert capsys.readouterr().out == (
        f"Profile default now uses workspace research ({OTHER['id']}) by default "
        "for secrets and api-keys create.\n"
    )
    # Resolved through the API (an id-shaped target is taken as the id); no key minted.
    assert _routes(api) == [("GET", f"/api/v1/workspaces/{OTHER['id']}")]
    written = _defaults_path(home)
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600
    assert json.loads(written.read_text()) == {
        "version": 1,
        "profiles": {
            "default": {"account_id": ACCOUNT, "workspace": {"id": OTHER["id"], "name": "research"}}
        },
    }
    assert not (home / ".mandala" / ".defaults.lock").exists()
    # credentials.json is byte for byte what it was, so an older reader, whose
    # profile schema is closed, still reads it.
    assert path.read_bytes() == before
    credentials._parse_store(path.read_bytes())

    assert _cli.main(["workspaces", "current", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "profile": "default",
        "workspace": {"id": OTHER["id"], "name": "research"},
        "source": "profile",
    }
    assert _cli.main(["workspaces", "current"]) == 0
    assert f"workspace research ({OTHER['id']}): profile default's default" in (
        capsys.readouterr().out
    )


def test_use_takes_a_name_and_a_profile(
    home: Path, api: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "home", home=_profile(), work=_profile())
    assert _cli.main(["workspaces", "use", "research", "--profile", "work", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "profile": "work",
        "workspace": {"id": OTHER["id"], "name": "research"},
        "source": "profile",
    }
    assert set(defaults.read_defaults()) == {"work"}
    assert _cli.main(["workspaces", "use", WORKSPACE["id"], "--profile", "home"]) == 0
    assert defaults.read_defaults() == {
        "home": _default(WORKSPACE),
        "work": _default(OTHER),
    }


def test_use_refuses_another_workspace_for_a_confined_key(
    home: Path, api: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "default", default=_profile(CONFINED))
    with pytest.raises(SystemExit) as refused:
        _cli.main(["workspaces", "use", OTHER["id"]])
    assert str(refused.value) == "mandala-py: " + (
        f"This profile's key is confined to workspace customers ({WORKSPACE['id']}); it "
        "cannot use another workspace. Log in again without --workspace for an account-wide key."
    )
    assert not _defaults_path(home).exists()
    assert api.calls.call_count == 0

    assert _cli.main(["workspaces", "use", "customers"]) == 0
    assert "is already confined to workspace customers" in capsys.readouterr().out
    assert not _defaults_path(home).exists()
    assert _cli.main(["workspaces", "current", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "profile": "default",
        "workspace": {"id": WORKSPACE["id"], "name": "customers"},
        "source": "key",
    }


def test_use_is_refused_while_an_environment_key_supplies_the_key(
    home: Path, api: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "default", default=_profile())
    monkeypatch.setenv("MANDALA_API_KEY", "com_env_key")
    with pytest.raises(SystemExit) as refused:
        _cli.main(["workspaces", "use", OTHER["id"]])
    assert str(refused.value) == "mandala-py: " + (
        "workspaces use saves a default in a saved profile; MANDALA_API_KEY is set, "
        "so there is no profile to save it in."
    )
    assert not _defaults_path(home).exists()
    assert api.calls.call_count == 0
    assert _cli.main(["workspaces", "current", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "profile": None,
        "workspace": None,
        "source": "none",
    }


def test_clear_removes_the_entry_idempotently_without_a_request(
    home: Path, api: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "home", home=_profile(), work=_profile())
    defaults.save_workspace_default("home", _default(OTHER))
    defaults.save_workspace_default("work", _default(OTHER))
    monkeypatch.setenv("MANDALA_PROFILE", "work")
    assert _cli.main(["workspaces", "use", "--clear", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "profile": "work",
        "workspace": None,
        "removed": True,
    }
    assert set(defaults.read_defaults()) == {"home"}
    assert _cli.main(["workspaces", "use", "--clear"]) == 0
    assert "Profile work has no default workspace; nothing to clear." in capsys.readouterr().out
    assert api.calls.call_count == 0
    # The last entry removed takes the file with it.
    assert _cli.main(["workspaces", "use", "--clear", "--profile", "home"]) == 0
    assert not _defaults_path(home).exists()


def test_use_needs_a_workspace_or_clear_not_both(home: Path) -> None:
    _store(home, "default", default=_profile())
    with pytest.raises(SystemExit, match="give a workspace or --clear, not both"):
        _cli.main(["workspaces", "use", OTHER["id"], "--clear"])
    with pytest.raises(SystemExit, match="say which workspace"):
        _cli.main(["workspaces", "use"])


def test_use_never_overwrites_a_defaults_file_it_cannot_read(
    home: Path, api: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "default", default=_profile())
    path = _defaults_path(home)
    path.write_text('{"version":1,')
    path.chmod(0o600)
    assert _cli.main(["workspaces", "use", OTHER["id"]]) == 1
    assert (
        "~/.mandala/defaults.json cannot be read (it is not valid JSON), so it was not "
        "changed. Delete or fix it"
    ) in capsys.readouterr().err
    assert path.read_text() == '{"version":1,'
    path.write_text('{"version":2,"profiles":{}}')
    assert _cli.main(["workspaces", "use", "--clear"]) == 1
    assert "it is a version this CLI does not read" in capsys.readouterr().err
    assert path.read_text() == '{"version":2,"profiles":{}}'


# --- the default applied --------------------------------------------------------


def test_the_default_scopes_secrets_and_api_keys_unless_overridden_or_cleared(
    home: Path, api: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "default", default=_profile())
    assert _cli.main(["workspaces", "use", OTHER["id"]]) == 0
    capsys.readouterr()
    api.calls.reset()

    assert _cli.main(["secrets", "list"]) == 0
    assert (
        "(workspace research from `workspaces use`; `workspaces use --clear` for account-wide)"
        in capsys.readouterr().err
    )
    assert _sent(api.calls.last.request) == OTHER["id"]

    monkeypatch.setattr(sys, "stdin", _Stdin(io.BytesIO(b"sk-value\n"), encoding="utf-8"))
    assert _cli.main(["secrets", "set", "NEW_SECRET", "--json"]) == 0
    assert _cli.main(["secrets", "rm", "OPENAI_API_KEY"]) == 0
    secret_calls = [
        c.request for c in api.calls if c.request.url.path.startswith("/api/v1/secrets")
    ]
    assert len(secret_calls) >= 4
    assert [_sent(r) for r in secret_calls] == [OTHER["id"]] * len(secret_calls)

    capsys.readouterr()
    assert _cli.main(["api-keys", "create", "--name", "ci", "--json"]) == 0
    assert json.loads(api.calls.last.request.content) == {"name": "ci", "workspace_id": OTHER["id"]}
    # JSON mode keeps the note off stderr.
    assert "workspaces use" not in capsys.readouterr().err

    # An explicit --workspace always wins.
    assert _cli.main(["secrets", "list", "--workspace", WORKSPACE["id"]]) == 0
    assert _sent(api.calls.last.request) == WORKSPACE["id"]
    assert _cli.main(["api-keys", "create", "--workspace", WORKSPACE["id"], "--json"]) == 0
    assert json.loads(api.calls.last.request.content) == {"workspace_id": WORKSPACE["id"]}

    # --clear is the way back to account-wide.
    assert _cli.main(["workspaces", "use", "--clear"]) == 0
    capsys.readouterr()
    assert _cli.main(["secrets", "list"]) == 0
    assert _sent(api.calls.last.request) is None
    assert "workspaces use" not in capsys.readouterr().err
    assert _cli.main(["api-keys", "create", "--json"]) == 0
    assert json.loads(api.calls.last.request.content or b"{}") == {}


def test_the_default_is_not_applied_with_an_environment_key(
    home: Path, api: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "default", default=_profile())
    defaults.save_workspace_default("default", _default(OTHER))
    monkeypatch.setenv("MANDALA_API_KEY", "com_env_key")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)
    assert _cli.main(["secrets", "list"]) == 0
    assert _sent(api.calls.last.request) is None
    assert "workspaces use" not in capsys.readouterr().err


def test_the_default_is_not_applied_to_a_confined_key(home: Path, api: Any) -> None:
    _store(home, "default", default=_profile(CONFINED))
    # Saved while the profile held an account-wide key.
    defaults.save_workspace_default("default", _default(OTHER))
    assert _cli.main(["secrets", "list"]) == 0
    assert _sent(api.calls.last.request) is None


def test_a_default_for_another_account_is_ignored_and_current_says_so(
    home: Path, api: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "default", default=_profile())
    defaults.save_workspace_default("default", _default(OTHER, "acc-00000000000f"))
    assert _cli.main(["secrets", "list"]) == 0
    assert _sent(api.calls.last.request) is None
    capsys.readouterr()
    assert _cli.main(["workspaces", "current", "--json"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"profile": "default", "workspace": None, "source": "none"}
    assert (
        f"The default workspace research ({OTHER['id']}) saved for profile default is "
        "ignored: it was saved for account acc-00000000000f"
    ) in err
    assert _cli.main(["workspaces", "current"]) == 0
    assert "none: account-wide" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ('{"version":1,', "it is not valid JSON"),
        ('{"version":2,"profiles":{}}', "it is a version this CLI does not read"),
        ('{"version":1,"profiles":{},"x":1}', "its contents are not in the expected form"),
    ],
)
def test_an_unreadable_defaults_file_is_read_as_none_with_a_note(
    home: Path, api: Any, capsys: pytest.CaptureFixture[str], text: str, why: str
) -> None:
    _store(home, "default", default=_profile())
    path = _defaults_path(home)
    path.write_text(text)
    path.chmod(0o600)
    assert _cli.main(["secrets", "list"]) == 0
    assert _sent(api.calls.last.request) is None
    err = capsys.readouterr().err
    assert f"ignoring ~/.mandala/defaults.json: {why}" in err
    assert len([line for line in err.splitlines() if "defaults.json" in line]) == 1
    # --json keeps stderr quiet and still works.
    assert _cli.main(["secrets", "list", "--json"]) == 0
    assert capsys.readouterr().err == ""


def test_a_defaults_file_others_can_read_is_read_as_none_with_a_note(
    home: Path, api: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "default", default=_profile())
    defaults.save_workspace_default("default", _default(OTHER))
    _defaults_path(home).chmod(0o644)
    assert _cli.main(["secrets", "list"]) == 0
    assert _sent(api.calls.last.request) is None
    assert "ignoring ~/.mandala/defaults.json: it must be a regular file" in (
        capsys.readouterr().err
    )


# --- logout ---------------------------------------------------------------------


def test_logout_removes_the_profiles_default_and_keeps_the_others(home: Path) -> None:
    _store(home, "home", home=_profile(), work=_profile())
    defaults.save_workspace_default("home", _default(OTHER))
    defaults.save_workspace_default("work", _default(OTHER))
    assert _cli.main(["logout", "--profile", "work"]) == 0
    assert set(defaults.read_defaults()) == {"home"}
    assert _cli.main(["logout"]) == 0
    assert not _defaults_path(home).exists()


def test_logout_still_logs_out_when_the_defaults_file_cannot_change(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _store(home, "default", default=_profile())
    _defaults_path(home).write_text("not json")
    _defaults_path(home).chmod(0o600)
    assert _cli.main(["logout"]) == 0
    err = capsys.readouterr().err
    assert "removed profile default" in err
    assert (
        "the profile was removed, but its default workspace in ~/.mandala/defaults.json "
        "was not: it is not valid JSON"
    ) in err
    assert not path.exists()


# --- the shared vectors ---------------------------------------------------------

VECTORS = json.loads(FIXTURE.read_bytes())


def _as_dict(d: defaults.WorkspaceDefault | None) -> dict[str, str] | None:
    return None if d is None else {"id": d.workspace_id, "name": d.workspace_name}


@pytest.mark.parametrize("vector", VECTORS["valid"], ids=lambda v: v["name"])
def test_the_shared_vectors_read_the_same(vector: dict[str, Any]) -> None:
    parsed = defaults.parse_defaults(vector["text"].encode("utf-8"))
    for lookup in vector["lookups"]:
        entry, ignored = defaults.workspace_default(parsed, lookup["profile"], lookup["account_id"])
        assert _as_dict(entry) == lookup["entry"]
        assert _as_dict(ignored) == lookup["ignored"]


@pytest.mark.parametrize("vector", VECTORS["invalid"], ids=lambda v: v["name"])
def test_the_shared_vectors_are_refused_the_same(vector: dict[str, Any]) -> None:
    with pytest.raises(credentials.CredentialError) as refused:
        defaults.parse_defaults(vector["text"].encode("utf-8"))
    assert refused.value.rule == vector["code"]


def test_the_fixture_is_the_typescript_clis_bytes() -> None:
    # The npm CLI's test/fixtures/defaults-v1.json carries these same bytes; a
    # change to one is made to both.
    assert FIXTURE.read_bytes().startswith(b'{\n  "format": "mandala.defaults.conformance.v1",')


# --- the command tree -----------------------------------------------------------


def test_help_lists_use_and_current(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["workspaces", "--help"])
    out = capsys.readouterr().out
    assert "use" in out
    assert "current" in out
