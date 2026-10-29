"""``client.api_keys`` and ``client.account.whoami()`` (platform OPL-5053), and
the ``mandala-py`` commands on top of them: ``whoami``, ``api-keys``,
``logout`` and ``--version``."""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
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
    "This API key cannot manage API keys. Allow it in the dashboard under Settings → "
    "Credentials → API keys → the key’s menu → Allow managing keys, or use a key that has it."
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


RAW_KEY = "com_" + "ab" * 24
RAW_KEY_SENTENCE = "that is an API key, not a key id; run api-keys list to find its id (key-...)"
# A pasted key with a stray space or newline, or in capitals, is still one; so
# is a full key behind a byte-order mark or a zero-width space (neither is
# whitespace to str.strip), in quotes, or after a label.
RAW_KEY_SHAPES = [
    RAW_KEY,
    f" {RAW_KEY}\n",
    RAW_KEY.upper(),
    "com_short",
    "\ufeff" + RAW_KEY,
    "\u200b" + RAW_KEY,
    f'"{RAW_KEY}"',
    f"Bearer {RAW_KEY}",
    f"MANDALA_API_KEY={RAW_KEY}",
]


@pytest.mark.parametrize("raw", RAW_KEY_SHAPES)
@respx.mock
def test_revoke_refuses_an_api_key_before_any_request(raw: str) -> None:
    anything = respx.route().mock(return_value=httpx.Response(404, json={"error": "no"}))
    with pytest.raises(ValueError, match="not a key id") as caught:
        client().api_keys.revoke(raw)
    assert not anything.called
    assert str(caught.value) == RAW_KEY_SENTENCE
    assert raw.strip().lower() not in str(caught.value).lower()
    assert RAW_KEY not in str(caught.value).lower()


@pytest.mark.parametrize("raw", RAW_KEY_SHAPES)
@respx.mock
async def test_async_revoke_refuses_an_api_key_before_any_request(raw: str) -> None:
    anything = respx.route().mock(return_value=httpx.Response(404, json={"error": "no"}))
    async with mc.AsyncClient("com_test", base_url=BASE) as c:
        with pytest.raises(ValueError, match="not a key id") as caught:
            await c.api_keys.revoke(raw)
    assert not anything.called
    assert str(caught.value) == RAW_KEY_SENTENCE
    assert RAW_KEY not in str(caught.value).lower()


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


# --- minted_by_key_id and whoami's nulls (OPL-5322) --------------------------

MINTED = {**API_KEY, "id": "key-0a0b0c0d0e0f", "minted_by_key_id": "key-000000000001"}
SCOPED_WHOAMI: dict[str, Any] = {
    **WHOAMI,
    "user": {"id": "usr-1", "email": None, "name": None},
    "account": {"id": "acc-1", "name": None, "plan": None, "status": "active"},
    "workspace": {"id": "wsp-1", "name": "ci", "created_at": "2026-09-01T00:00:00Z"},
    "key": {**MINTED, "workspace_id": "wsp-1", "workspace_name": "ci"},
}


def check_minted(keys: list[mc.ApiKey], created: mc.ApiKeyCreated, who: mc.Whoami) -> None:
    # A key made in the dashboard has none, and an old platform sends none.
    assert [k.minted_by_key_id for k in keys] == [None, "key-000000000001"]
    assert created.minted_by_key_id == "key-000000000001"
    assert who.key is not None and who.key.minted_by_key_id == "key-000000000001"
    # Keyword-only at the end, so positional construction is unchanged.
    positional = mc.ApiKey("key-1", None, "com_1…", "2026-09-20", None, None, None, False)
    assert positional.minted_by_key_id is None


def check_scoped(who: mc.Whoami) -> None:
    # null for a workspace-scoped key, not "": an empty email is not a withheld one.
    assert who.user.email is None and who.user.name is None
    assert who.account.plan is None and who.account.name is None


def mock_minted_and_scoped() -> None:
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[API_KEY, MINTED]))
    respx.post(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(
            201, json={**API_KEY_CREATED, **MINTED, "raw": "com_" + "cd" * 24}
        )
    )
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=SCOPED_WHOAMI))


@respx.mock
def test_minted_by_key_id_and_scoped_nulls_are_read() -> None:
    mock_minted_and_scoped()
    c = client()
    who = c.account.whoami()
    check_minted(c.api_keys.list(), c.api_keys.create(name="ci"), who)
    check_scoped(who)


@respx.mock
async def test_async_minted_by_key_id_and_scoped_nulls_are_read() -> None:
    mock_minted_and_scoped()
    async with mc.AsyncClient("com_test", base_url=BASE) as c:
        who = await c.account.whoami()
        check_minted(await c.api_keys.list(), await c.api_keys.create(name="ci"), who)
    check_scoped(who)


@pytest.mark.parametrize(
    "body",
    [
        {**WHOAMI, "user": {**WHOAMI["user"], "email": 7}},
        {**WHOAMI, "account": {**WHOAMI["account"], "plan": ["team"]}},
        {**WHOAMI, "key": {**API_KEY, "minted_by_key_id": 1}},
    ],
)
@respx.mock
def test_a_nullable_field_that_is_not_text_is_refused(body: dict[str, Any]) -> None:
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError):
        client().account.whoami()


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
def test_cli_whoami_names_what_a_scoped_key_is_not_told(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scoped = {
        **WHOAMI,
        "role": "member",
        "user": {**WHOAMI["user"], "email": None, "name": None},
        "account": {**WHOAMI["account"], "name": None, "plan": None},
        "workspace": {"id": "ws-1", "name": "ci", "created_at": "2026-09-20T08:00:00.000Z"},
    }
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=scoped))
    assert _cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "User usr-1 (name and email withheld from a workspace-scoped key)",
        "Account: acc-1, active (name and plan withheld from a workspace-scoped key)",
        "Role: member",
        "Scope: workspace ci (ws-1)",
        "Key: laptop (key-000000000001, com_1a2b3c4d…); can manage API keys",
    ]
    assert "<>" not in out and "(unnamed)" not in out and "plan ," not in out


@respx.mock
def test_cli_whoami_names_only_what_a_scoped_key_is_not_told(
    capsys: pytest.CaptureFixture[str],
) -> None:
    scoped = {
        **WHOAMI,
        "user": {**WHOAMI["user"], "email": None},
        "account": {**WHOAMI["account"], "plan": None},
        "workspace": {"id": "ws-1", "name": "ci", "created_at": "x"},
    }
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=scoped))
    assert _cli.main(["whoami"]) == 0
    assert capsys.readouterr().out.splitlines()[:2] == [
        "Dana (usr-1) (email withheld from a workspace-scoped key)",
        "Account: Acme (acc-1), active (plan withheld from a workspace-scoped key)",
    ]


@respx.mock
def test_cli_whoami_unscoped_account_with_no_name_is_unnamed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    unnamed = {**WHOAMI, "account": {**WHOAMI["account"], "name": None}}
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=unnamed))
    assert _cli.main(["whoami"]) == 0
    assert capsys.readouterr().out.splitlines()[1] == (
        "Account: (unnamed) (acc-1), plan team, active"
    )


@respx.mock
def test_cli_whoami_unscoped_escapes_names(capsys: pytest.CaptureFixture[str]) -> None:
    hostile = {
        **WHOAMI,
        "user": {**WHOAMI["user"], "name": "Dana\x1b]0;x\x07", "email": "dana\n@example.com"},
        "account": {**WHOAMI["account"], "name": "Acme\x9b"},
    }
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=hostile))
    assert _cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[:2] == [
        "Dana\\u001b]0;x\\u0007 <dana\\u000a@example.com> (usr-1)",
        "Account: Acme\\u009b (acc-1), plan team, active",
    ]
    assert not any(c in out.replace("\n", "") for c in map(chr, [*range(0x20), *range(0x7F, 0xA0)]))


@respx.mock
def test_cli_api_keys_list(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[API_KEY]))
    assert _cli.main(["api-keys", "list"]) == 0
    out = capsys.readouterr().out
    assert "MANAGES KEYS" in out and API_KEY["id"] in out and "account-wide" in out
    assert _cli.main(["api-keys", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [API_KEY]


@respx.mock
def test_cli_api_keys_list_names_the_minting_key(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[API_KEY, MINTED]))
    assert _cli.main(["api-keys", "list"]) == 0
    header, dashboard, minted = capsys.readouterr().out.splitlines()
    assert header.split()[-2:] == ["MINTED", "BY"]
    assert dashboard.split()[-1] == "-"
    assert minted.split()[-1] == "key-000000000001"


@respx.mock
def test_cli_escapes_bidi_controls_in_names(capsys: pytest.CaptureFixture[str]) -> None:
    # A right-to-left override visually reverses the rest of the row on a
    # bidi-aware terminal; embeddings and isolates do the same.
    controls = [chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))]
    named = {**API_KEY, "name": "ci" + "".join(controls)}
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[named]))
    assert _cli.main(["api-keys", "list"]) == 0
    out = capsys.readouterr().out
    assert not any(c in out for c in controls)
    assert "ci\\u202a\\u202b\\u202c\\u202d\\u202e\\u2066\\u2067\\u2068\\u2069" in out


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


@pytest.mark.parametrize("raw", RAW_KEY_SHAPES)
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("logged_in", [True, False])
@respx.mock
def test_cli_api_keys_revoke_refuses_an_api_key_without_repeating_it(
    raw: str,
    as_json: bool,
    logged_in: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not logged_in:
        monkeypatch.delenv("MANDALA_API_KEY")
        monkeypatch.setenv("HOME", str(tmp_path))
    anything = respx.route().mock(return_value=httpx.Response(404, json={"error": "no"}))
    argv = ["api-keys", "revoke", raw, *(["--json"] if as_json else [])]
    assert _cli.main(argv) == 1
    out, err = capsys.readouterr()
    assert not anything.called
    assert out == ""
    assert RAW_KEY not in err.lower()
    assert raw.strip().lower() not in err.lower()
    if as_json:
        assert json.loads(err)["error"] == {
            "code": "invalid_arguments",
            "message": RAW_KEY_SENTENCE,
        }
    else:
        assert err == f"mandala-py: {RAW_KEY_SENTENCE}\n"


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


def test_logout_with_nothing_saved_succeeds_and_says_so(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # It exited 1 (`not_logged_in`), failing a script that logs out before it
    # logs in (OPL-5471).
    monkeypatch.delenv("MANDALA_API_KEY")
    assert _cli.main(["logout"]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert "Not logged in; nothing to remove." in err
    assert _cli.main(["logout", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "profile": "default",
        "removed": False,
        "path": str(home / ".mandala" / "credentials.json"),
        "key_id": None,
        "default_profile": None,
    }
    assert not (home / ".mandala").exists()


def test_logout_refuses_a_profile_that_is_not_saved_while_others_are(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_logout_removes_its_own_lock_when_securing_it_fails(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _store(home, "home", home=_profile("com_one", "key-000000000001"))
    lock = home / ".mandala" / ".credentials.lock"
    real = os.fchmod
    calls = 0

    def failing_once(fd: int, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("fchmod failed")
        real(fd, mode)

    monkeypatch.setattr(credentials.os, "fchmod", failing_once)
    with pytest.raises(credentials.CredentialError) as caught:
        credentials.remove_profile(lock_timeout=0.1)
    assert caught.value.rule == "credential_remove_failed"
    # A lock left behind would time out every later writer for good.
    assert not lock.exists()
    assert path.exists()
    assert credentials.remove_profile(lock_timeout=0.1).removed


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


# The dashboard control is the key's menu → "Allow managing keys"; there is no
# "Manage keys" checkbox to look for, so neither the help nor the README names one.
README = Path(__file__).resolve().parents[1] / "README.md"


def test_api_keys_help_names_the_permission_as_the_dashboard_does(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _cli._parser().parse_args(["--help"])
    top = capsys.readouterr().out
    with pytest.raises(SystemExit):
        _cli._parser().parse_args(["api-keys", "--help"])
    own = capsys.readouterr().out
    for text in (top, own):
        assert "Manage keys" not in text
    assert "allowed to manage keys" in " ".join(top.split())
    assert "Allow managing keys" in " ".join(own.split())


def test_readme_names_the_permission_as_the_dashboard_does() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "Manage keys" not in readme
    assert "checkbox" not in readme
    assert "the key's menu → **Allow managing keys**" in " ".join(readme.split())


@respx.mock
def test_create_sends_an_expiry_and_a_computer_list_and_reads_them_back() -> None:
    answer = {
        **API_KEY_CREATED,
        "expires_at": "2026-11-01T12:00:00.000Z",
        "computer_ids": ["vm-1", "vm-2"],
    }
    route = respx.post(f"{BASE}/api-keys").mock(return_value=httpx.Response(201, json=answer))
    created = client().api_keys.create(
        expires_at="2026-11-01T12:00:00Z", computer_ids=["vm-1", "vm-2"]
    )
    assert json.loads(route.calls.last.request.content) == {
        "expires_at": "2026-11-01T12:00:00Z",
        "computer_ids": ["vm-1", "vm-2"],
    }
    assert created.expires_at == "2026-11-01T12:00:00.000Z"
    assert created.computer_ids == ("vm-1", "vm-2")
    soon = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    route.mock(return_value=httpx.Response(201, json={**API_KEY_CREATED, "expires_at": soon}))
    assert client().api_keys.create(expires_in_hours=24).expires_at == soon
    assert json.loads(route.calls.last.request.content) == {"expires_in_hours": 24}


@respx.mock
def test_a_key_without_limits_reads_as_none() -> None:
    respx.get(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(
            200, json=[API_KEY, {**API_KEY, "expires_at": None, "computer_ids": None}]
        )
    )
    for key in client().api_keys.list():
        assert key.expires_at is None
        assert key.computer_ids is None


@pytest.mark.parametrize("ids", ["vm-1", 7, {"vm-1": True}, ["vm-1", 7]])
@respx.mock
def test_list_refuses_an_unreadable_computer_list(ids: object) -> None:
    respx.get(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(200, json=[{**API_KEY, "computer_ids": ids}])
    )
    with pytest.raises(mc.MandalaError, match="computer_ids"):
        client().api_keys.list()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"expires_at": "2026-11-01T12:00:00Z", "expires_in_hours": 1}, "not both"),
        ({"expires_in_hours": 0}, "expires_in_hours"),
        ({"expires_at": "2026-11-01"}, "RFC 3339"),
        ({"computer_ids": "vm-1"}, "list of computer ids"),
        ({"computer_ids": []}, "at least one"),
        ({"computer_ids": [" vm-1"]}, "no spaces"),
    ],
)
def test_create_refuses_limits_it_can_tell_are_wrong_before_sending(
    kwargs: dict[str, Any], match: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE}/api-keys")
        with pytest.raises(ValueError, match=match):
            client().api_keys.create(**kwargs)
        assert not route.called


# --- a mint that does not confirm the limits asked for is revoked -------------
#
# A server that predates key limits ignores the fields and mints a key that
# never expires and reaches every computer. That key is revoked, never handed
# back, and the error says whether the revoke went through.

LIMITED = {"expires_at": "2026-11-01T12:00:00Z", "computer_ids": ["vm-1", "vm-2"]}

UNCONFIRMED = [
    pytest.param({"expires_at": "2026-11-01T12:00:00Z"}, {}, "no expiry", id="expiry-omitted"),
    pytest.param(
        {"expires_at": "2026-11-01T12:00:00Z"}, {"expires_at": None}, "no expiry", id="expiry-null"
    ),
    pytest.param(
        {"expires_at": "2026-11-01T12:00:00Z"},
        {"expires_at": "2026-11-01T12:00:02Z"},
        "later than asked",
        id="expiry-later",
    ),
    pytest.param(
        {"expires_at": "2026-11-01T12:00:00Z"},
        {"expires_at": "next week"},
        "no expiry",
        id="expiry-unreadable",
    ),
    pytest.param({"expires_in_hours": 1}, {}, "no expiry", id="hours-omitted"),
    pytest.param(
        {"expires_in_hours": 1},
        {"expires_at": "2099-01-01T00:00:00Z"},
        "later than asked",
        id="hours-later",
    ),
    pytest.param({"computer_ids": ["vm-1"]}, {}, "not confined", id="ids-omitted"),
    pytest.param({"computer_ids": ["vm-1"]}, {"computer_ids": None}, "not confined", id="ids-null"),
    pytest.param(
        {"computer_ids": ["vm-1"]},
        {"computer_ids": ["vm-1", "vm-9"]},
        "reaches computers that were not named",
        id="ids-wider",
    ),
    pytest.param(
        {"computer_ids": ["vm-1"]},
        {"computer_ids": "vm-1"},
        "could not be read",
        id="ids-unreadable",
    ),
    # The key decodes no further than its id: still revoked, by that id.
    pytest.param(
        {"expires_in_hours": 1}, {"expires_at": 17}, "could not be read", id="expiry-not-a-string"
    ),
    pytest.param(
        {"computer_ids": ["vm-1"]},
        {"manage_keys": "yes", "computer_ids": ["vm-1"]},
        "could not be read",
        id="model-unreadable",
    ),
]


@pytest.mark.parametrize(("asked", "answered", "problem"), UNCONFIRMED)
@respx.mock
def test_a_key_minted_without_its_limits_is_revoked_and_not_returned(
    asked: dict[str, Any], answered: dict[str, Any], problem: str
) -> None:
    respx.post(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(201, json={**API_KEY_CREATED, **answered})
    )
    revoke = respx.delete(f"{BASE}/api-keys/{API_KEY_CREATED['id']}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with pytest.raises(mc.MandalaError, match="did not apply the requested limits") as caught:
        client().api_keys.create(**asked)
    message = str(caught.value)
    assert problem in message
    assert f"the new key {API_KEY_CREATED['id']} has been revoked" in message
    assert API_KEY_CREATED["raw"] not in message
    assert revoke.call_count == 1


@respx.mock
def test_a_failed_revoke_is_said_and_names_the_key_to_revoke() -> None:
    respx.post(f"{BASE}/api-keys").mock(return_value=httpx.Response(201, json=API_KEY_CREATED))
    respx.delete(f"{BASE}/api-keys/{API_KEY_CREATED['id']}").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    with pytest.raises(mc.MandalaError, match="FAILED") as caught:
        client().api_keys.create(expires_in_hours=1)
    assert API_KEY_CREATED["id"] in str(caught.value)
    assert API_KEY_CREATED["raw"] not in str(caught.value)


@respx.mock
def test_a_key_minted_with_its_limits_is_returned() -> None:
    respx.post(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(
            201, json={**API_KEY_CREATED, **LIMITED, "computer_ids": ["vm-1"]}
        )
    )
    created = client().api_keys.create(**LIMITED)
    assert created.key == API_KEY_CREATED["raw"]
    assert created.computer_ids == ("vm-1",)


@pytest.mark.parametrize(("asked", "answered", "problem"), UNCONFIRMED)
@respx.mock
async def test_the_async_mint_revokes_the_same(
    asked: dict[str, Any], answered: dict[str, Any], problem: str
) -> None:
    respx.post(f"{BASE}/api-keys").mock(
        return_value=httpx.Response(201, json={**API_KEY_CREATED, **answered})
    )
    revoke = respx.delete(f"{BASE}/api-keys/{API_KEY_CREATED['id']}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    aclient = mc.AsyncClient("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match="did not apply the requested limits") as caught:
        await aclient.api_keys.create(**asked)
    assert problem in str(caught.value)
    assert API_KEY_CREATED["raw"] not in str(caught.value)
    assert revoke.call_count == 1


@respx.mock
def test_the_revoke_has_its_own_bounded_budget() -> None:
    """However patient the caller's client, the compensating revoke is capped."""
    respx.post(f"{BASE}/api-keys").mock(return_value=httpx.Response(201, json=API_KEY_CREATED))
    revoke = respx.delete(f"{BASE}/api-keys/{API_KEY_CREATED['id']}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    patient = mc.Client("com_test", base_url=BASE, timeout=3600)
    with pytest.raises(mc.MandalaError, match="has been revoked"):
        patient.api_keys.create(expires_in_hours=1)
    sent = revoke.calls.last.request.extensions["timeout"]
    assert all(v is not None and v <= 30 for v in sent.values())


@respx.mock
async def test_cancelling_the_async_mint_does_not_cancel_its_revoke() -> None:
    """A caller's cancellation (a timeout around the call, say) reaches the
    caller, but the revoke of the key it would have stranded still finishes."""
    started, finished = asyncio.Event(), asyncio.Event()

    async def slow_revoke(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(0.05)
        finished.set()
        return httpx.Response(200, json={"ok": True})

    respx.post(f"{BASE}/api-keys").mock(return_value=httpx.Response(201, json=API_KEY_CREATED))
    revoke = respx.delete(f"{BASE}/api-keys/{API_KEY_CREATED['id']}").mock(side_effect=slow_revoke)
    aclient = mc.AsyncClient("com_test", base_url=BASE)
    mint = asyncio.ensure_future(aclient.api_keys.create(expires_in_hours=1))
    await asyncio.wait_for(started.wait(), 5)
    mint.cancel()
    with pytest.raises(asyncio.CancelledError):
        await mint
    await asyncio.wait_for(finished.wait(), 5)
    assert revoke.call_count == 1


# --- a client clock set wrong does not decide an expiry ----------------------


def minting_server(off: timedelta) -> respx.Route:
    """A server that sets the expiry from ITS clock (plus ``off``, to be wrong)
    and says what that clock reads in its Date header."""

    def answer(request: httpx.Request) -> httpx.Response:
        hours = json.loads(request.content)["expires_in_hours"]
        now = datetime.now(timezone.utc)
        body = {**API_KEY_CREATED, "expires_at": (now + timedelta(hours=hours) + off).isoformat()}
        return httpx.Response(201, json=body, headers={"Date": format_datetime(now, usegmt=True)})

    respx.delete(f"{BASE}/api-keys/{API_KEY_CREATED['id']}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    return respx.post(f"{BASE}/api-keys").mock(side_effect=answer)


@pytest.mark.parametrize("skew", [timedelta(seconds=5), timedelta(minutes=-2)])
@respx.mock
def test_a_skewed_client_clock_accepts_the_servers_expiry(
    monkeypatch: pytest.MonkeyPatch, skew: timedelta
) -> None:
    monkeypatch.setattr(mc._api, "utcnow", lambda: datetime.now(timezone.utc) + skew)
    minting_server(timedelta(0))
    assert client().api_keys.create(expires_in_hours=1).key == API_KEY_CREATED["raw"]


@pytest.mark.parametrize("skew", [timedelta(seconds=5), timedelta(minutes=-2)])
@respx.mock
async def test_a_skewed_client_clock_accepts_the_servers_expiry_async(
    monkeypatch: pytest.MonkeyPatch, skew: timedelta
) -> None:
    monkeypatch.setattr(mc._api, "utcnow", lambda: datetime.now(timezone.utc) + skew)
    minting_server(timedelta(0))
    created = await mc.AsyncClient("com_test", base_url=BASE).api_keys.create(expires_in_hours=1)
    assert created.key == API_KEY_CREATED["raw"]


@pytest.mark.parametrize("skew", [timedelta(seconds=5), timedelta(minutes=-2)])
@respx.mock
def test_an_expiry_later_by_the_servers_own_clock_is_refused(
    monkeypatch: pytest.MonkeyPatch, skew: timedelta
) -> None:
    monkeypatch.setattr(mc._api, "utcnow", lambda: datetime.now(timezone.utc) + skew)
    minting_server(timedelta(minutes=5))
    with pytest.raises(mc.MandalaError, match="later than asked"):
        client().api_keys.create(expires_in_hours=1)
