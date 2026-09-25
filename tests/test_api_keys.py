"""``client.api_keys`` and ``client.account.whoami()`` (platform OPL-5053), and
the ``mandala-py`` commands on top of them: ``whoami``, ``api-keys``,
``logout`` and ``--version``."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _cli
from mandala_computer import _credentials as credentials

BASE = "https://api.test/api/v1"

NO_PERMISSION = (
    "This API key cannot manage API keys. Turn on “Manage keys” for it under Credentials "
    "in the dashboard, or use a key that has it."
)

API_KEY: dict[str, Any] = {
    "id": "key-a1b2c3d4e5f6",
    "name": "ci",
    "prefix": "com_1a2b3c4d…",
    "created_at": "2026-09-20T08:00:00.000Z",
    "last_used_at": "2026-09-25T14:05:00.000Z",
    "workspace_id": None,
    "workspace_name": None,
    "manage_keys": False,
}
API_KEY_CREATED: dict[str, Any] = {
    **API_KEY,
    "id": "key-0f1e2d3c4b5a",
    "last_used_at": None,
    "raw": "com_" + "ab" * 24,
}
WHOAMI: dict[str, Any] = {
    "user": {"id": "usr-1", "email": "dana@example.com", "name": "Dana"},
    "account": {"id": "acc-1", "name": "Acme", "plan": "team", "status": "active"},
    "role": "owner",
    "workspace": None,
    "key": {**API_KEY, "id": "key-000000000001", "name": "laptop", "manage_keys": True},
}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)
    monkeypatch.delenv("MANDALA_PROFILE", raising=False)


def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


# --- the SDK ----------------------------------------------------------------


@respx.mock
def test_whoami_decodes_every_part() -> None:
    route = respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=WHOAMI))
    who = client().account.whoami()
    assert route.called
    assert who.user == mc.WhoamiUser("usr-1", "dana@example.com", "Dana")
    assert who.account == mc.WhoamiAccount("acc-1", "Acme", "team", "active")
    assert who.role == "owner"
    assert who.workspace is None
    assert who.key is not None and who.key.manage_keys is True
    assert who.raw == WHOAMI


@respx.mock
def test_whoami_decodes_a_workspace_and_a_null_key() -> None:
    body = {
        **WHOAMI,
        "account": {**WHOAMI["account"], "name": None, "status": "suspended"},
        "workspace": {"id": "wsp-1", "name": "ci", "created_at": "2026-09-01T00:00:00Z"},
        "key": None,
    }
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=body))
    who = client().account.whoami()
    assert who.workspace == mc.WhoamiWorkspace("wsp-1", "ci", "2026-09-01T00:00:00Z")
    assert who.key is None
    assert who.account.name is None and who.account.status == "suspended"


@pytest.mark.parametrize(
    "body",
    [
        {**WHOAMI, "user": None},
        {**WHOAMI, "user": {"email": "x@example.com"}},
        {**WHOAMI, "role": ""},
        {**WHOAMI, "workspace": {"name": "ci"}},
        {**WHOAMI, "key": {**API_KEY, "manage_keys": "yes"}},
    ],
)
@respx.mock
def test_whoami_refuses_an_answer_it_cannot_read(body: dict[str, Any]) -> None:
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError):
        client().account.whoami()


@respx.mock
def test_list_decodes_each_key() -> None:
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[API_KEY]))
    [key] = client().api_keys.list()
    assert key == mc.ApiKey(
        id=API_KEY["id"],
        name="ci",
        prefix=API_KEY["prefix"],
        created_at=API_KEY["created_at"],
        last_used_at=API_KEY["last_used_at"],
        workspace_id=None,
        workspace_name=None,
        manage_keys=False,
    )


@pytest.mark.parametrize("row", [{**API_KEY, "id": ""}, {**API_KEY, "manage_keys": None}])
@respx.mock
def test_list_refuses_a_row_without_an_id_or_a_permission(row: dict[str, Any]) -> None:
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[row]))
    with pytest.raises(mc.MandalaError, match="API key 0"):
        client().api_keys.list()


@respx.mock
def test_create_sends_what_was_given_and_never_manage_keys() -> None:
    route = respx.post(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(201, json=API_KEY_CREATED)
    )
    created = client().api_keys.create(name="ci", workspace_id="wsp-1")
    assert json.loads(route.calls.last.request.content) == {"name": "ci", "workspace_id": "wsp-1"}
    assert created.key == API_KEY_CREATED["raw"]
    assert created.id == API_KEY_CREATED["id"]
    assert created.manage_keys is False
    assert API_KEY_CREATED["raw"] not in repr(created)
    client().api_keys.create()
    assert json.loads(route.calls.last.request.content) == {}


@respx.mock
def test_create_refuses_an_answer_without_the_key() -> None:
    body = {k: v for k, v in API_KEY_CREATED.items() if k != "raw"}
    respx.post(f"{BASE}/api-keys").mock(return_value=httpx.Response(201, json=body))
    with pytest.raises(mc.MandalaError, match="answers once"):
        client().api_keys.create(name="ci")


@pytest.mark.parametrize("workspace", ["", " wsp-1", "wsp-1 "])
@respx.mock
def test_create_refuses_a_blank_or_padded_workspace_before_sending(workspace: str) -> None:
    route = respx.post(f"{BASE}/api-keys")
    with pytest.raises(ValueError, match="workspace_id"):
        client().api_keys.create(workspace_id=workspace)
    assert not route.called


@respx.mock
def test_create_is_not_retried_on_a_503() -> None:
    route = respx.post(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(503, json={"error": "busy"})
    )
    with pytest.raises(mc.UnavailableError):
        mc.Client("com_test", base_url=BASE, retries={"idempotent": 3}).api_keys.create(name="ci")
    assert route.call_count == 1


@respx.mock
def test_revoke_deletes_by_id() -> None:
    route = respx.delete(f"{BASE}/api-keys/key-a1b2c3d4e5f6").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client().api_keys.revoke("key-a1b2c3d4e5f6")
    assert route.called


@respx.mock
def test_a_missing_permission_is_permission_denied_with_the_platform_sentence() -> None:
    respx.get(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(403, json={"error": NO_PERMISSION, "request_id": "r1"})
    )
    with pytest.raises(mc.PermissionDeniedError) as caught:
        client().api_keys.list()
    assert NO_PERMISSION in str(caught.value)


@respx.mock
async def test_the_async_half_matches() -> None:
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=WHOAMI))
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[API_KEY]))
    post = respx.post(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(201, json=API_KEY_CREATED)
    )
    delete = respx.delete(f"{BASE}/api-keys/{API_KEY['id']}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with mc.AsyncClient("com_test", base_url=BASE) as c:
        assert (await c.account.whoami()).role == "owner"
        assert [k.id for k in await c.api_keys.list()] == [API_KEY["id"]]
        assert (await c.api_keys.create(name="ci")).key == API_KEY_CREATED["raw"]
        await c.api_keys.revoke(API_KEY["id"])
    assert json.loads(post.calls.last.request.content) == {"name": "ci"}
    assert delete.called


# --- the CLI ----------------------------------------------------------------


@respx.mock
def test_cli_whoami(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=WHOAMI))
    assert _cli.main(["whoami"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Dana <dana@example.com> (usr-1)",
        "Account: Acme (acc-1), plan team, active",
        "Role: owner",
        "Scope: the whole account",
        "Key: laptop (key-000000000001, com_1a2b3c4d…); can manage API keys",
    ]
    assert _cli.main(["whoami", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == WHOAMI


@respx.mock
def test_cli_api_keys_list(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[API_KEY]))
    assert _cli.main(["api-keys", "list"]) == 0
    out = capsys.readouterr().out
    assert "MANAGES KEYS" in out and API_KEY["id"] in out and "account-wide" in out
    assert _cli.main(["api-keys", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [API_KEY]


@respx.mock
def test_cli_api_keys_create_prints_only_the_key(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.post(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(201, json=API_KEY_CREATED)
    )
    assert _cli.main(["api-keys", "create", "--name", "ci", "--workspace", "wsp-1"]) == 0
    out, err = capsys.readouterr()
    assert out == API_KEY_CREATED["raw"] + "\n"
    assert "shown once" in err and API_KEY_CREATED["id"] in err
    assert json.loads(route.calls.last.request.content) == {"name": "ci", "workspace_id": "wsp-1"}
    assert _cli.main(["api-keys", "create", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == API_KEY_CREATED


@respx.mock
def test_cli_api_keys_revoke(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.delete(f"{BASE}/api-keys/key-a1b2c3d4e5f6").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert _cli.main(["api-keys", "revoke", "key-a1b2c3d4e5f6"]) == 0
    assert route.called
    assert capsys.readouterr().out == "revoked key-a1b2c3d4e5f6\n"


@pytest.mark.parametrize(
    ("argv", "method", "path"),
    [
        (["api-keys", "list"], "GET", "/api-keys"),
        (["api-keys", "create"], "POST", "/api-keys"),
        (["api-keys", "revoke", "key-1"], "DELETE", "/api-keys/key-1"),
    ],
)
@respx.mock
def test_cli_surfaces_the_permission_sentence(
    argv: list[str], method: str, path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.route(method=method, url=f"{BASE}{path}").mock(
        return_value=httpx.Response(403, json={"error": NO_PERMISSION})
    )
    assert _cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert NO_PERMISSION in err
    assert _cli.main([*argv, "--json"]) == 1
    failure = json.loads(capsys.readouterr().err)["error"]
    assert failure["code"] == "permission_denied"
    assert failure["status"] == 403
    assert NO_PERMISSION in failure["message"]


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as done:
        _cli.main(["--version"])
    assert done.value.code == 0
    assert capsys.readouterr().out == f"mandala-py {mc.__version__}\n"


# --- logout -----------------------------------------------------------------


def _profile(key: str, key_id: str) -> dict[str, Any]:
    return {
        "api_key": key,
        "base_url": "https://app.mandala.computer/api/v1",
        "key_id": key_id,
        "account": {"id": "acc-1", "name": "Acme"},
        "scope": {"type": "account"},
    }


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


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_logout_removes_the_default_and_the_file_with_the_last_profile(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("MANDALA_API_KEY")
    path = _store(home, "default", default=_profile("com_one", "key-000000000001"))
    assert _cli.main(["logout"]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "removed profile default" in err
    assert "key-000000000001 still works until it is revoked" in err
    assert "MANDALA_API_KEY" not in err
    assert not path.exists()
    assert not (home / ".mandala" / ".credentials.lock").exists()


def test_logout_removes_only_the_named_profile(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _store(
        home,
        "home",
        home=_profile("com_one", "key-000000000001"),
        work=_profile("com_two", "key-000000000002"),
    )
    assert _cli.main(["logout", "--profile", "work", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "profile": "work",
        "removed": True,
        "path": str(path),
        "key_id": "key-000000000002",
        "default_profile": "home",
    }
    assert list(_read(path)["profiles"]) == ["home"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # Still a store every reader accepts.
    credentials._parse_store(path.read_bytes())


def test_logout_names_the_new_default(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _store(
        home,
        "main",
        main=_profile("com_one", "key-000000000001"),
        zeta=_profile("com_two", "key-000000000002"),
        alpha=_profile("com_three", "key-000000000003"),
    )
    assert _cli.main(["logout"]) == 0
    assert "The default profile is alpha." in capsys.readouterr().err
    assert _read(path)["default_profile"] == "alpha"
    assert sorted(_read(path)["profiles"]) == ["alpha", "zeta"]


def test_logout_follows_mandala_profile(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _store(
        home,
        "home",
        home=_profile("com_one", "key-000000000001"),
        work=_profile("com_two", "key-000000000002"),
    )
    monkeypatch.setenv("MANDALA_PROFILE", "work")
    assert _cli.main(["logout"]) == 0
    assert list(_read(path)["profiles"]) == ["home"]


def test_logout_warns_about_an_environment_key(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _store(home, "default", default=_profile("com_one", "key-000000000001"))
    assert _cli.main(["logout"]) == 0
    assert "MANDALA_API_KEY is set" in capsys.readouterr().err


def test_logout_refuses_a_profile_that_is_not_saved(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main(["logout", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_logged_in"
    path = _store(home, "home", home=_profile("com_one", "key-000000000001"))
    before = path.read_bytes()
    with pytest.raises(SystemExit, match="no saved profile named work"):
        _cli.main(["logout", "--profile", "work"])
    assert path.read_bytes() == before


def test_logout_waits_for_and_never_steals_a_held_lock(home: Path) -> None:
    path = _store(home, "home", home=_profile("com_one", "key-000000000001"))
    lock = home / ".mandala" / ".credentials.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.remove_profile(lock_timeout=0.1)
    assert caught.value.rule == "writer_lock_timeout"
    assert lock.exists()
    assert path.exists()


def test_logout_refuses_an_unsafe_store(home: Path) -> None:
    path = _store(home, "home", home=_profile("com_one", "key-000000000001"))
    path.chmod(0o644)
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.remove_profile()
    assert caught.value.rule == "unsafe_file"
    assert not (home / ".mandala" / ".credentials.lock").exists()


def test_logout_refuses_an_invalid_profile_name(home: Path) -> None:
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.remove_profile("../x")
    assert caught.value.rule == "invalid_profile"
