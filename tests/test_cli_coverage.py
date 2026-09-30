"""``mandala-py move``, ``moves list``, ``secrets get``, and ``workspaces get`` and
``members`` by name (OPL-5524): the audit's coverage gaps in this CLI."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mandala_computer import _cli

BASE = "https://api.test/api/v1"

COMPUTERS = [
    {"id": "vm-1", "name": "dev", "status": "stopped", "os": "linux"},
    {"id": "vm-2", "name": "other", "status": "running", "os": "linux"},
]
STARTED = {
    "computer_id": "vm-1",
    "state": "moving",
    "detail": "",
    "live": True,
    "ram_mb": 26000,
    "started_at": "2026-08-23T02:00:12.699Z",
}
DONE = {**STARTED, "state": "done", "live": False, "finished_at": "2026-08-23T02:00:17.336Z"}
OTHER = {**DONE, "computer_id": "vm-2"}
SECRET = {
    "id": "csec-0123456789abcdef",
    "name": "OPENAI_API_KEY",
    "workspace_id": None,
    "revision_id": "csr-0123456789abcdef01234567",
    "created_at": "2026-09-16T12:00:00.000Z",
    "updated_at": "2026-09-16T12:00:00.000Z",
    "last_used_at": None,
}
SECRET_LIST = {"secrets": [SECRET], "delivery": True, "limits": {}}
WSP = {"id": "wsp-0123456789ab", "name": "acme", "created_at": "2026-09-01T00:00:00.000Z"}
MEMBER = {
    "user_id": "usr-0123456789abcdef",
    "email": "dana@example.com",
    "name": "Dana",
    "role": "owner",
    "accepted_at": "2026-08-01T00:00:00.000Z",
    "suspended": False,
}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


# --- move ----------------------------------------------------------------------


@respx.mock
def test_move_resolves_a_name_and_sends_the_sizing(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    route = respx.post(f"{BASE}/computers/vm-1/move").mock(
        return_value=httpx.Response(202, json=STARTED)
    )
    assert _cli.main(["move", "dev", "--ram-mb", "26000", "--cpu", "4"]) == 0
    assert json.loads(route.calls.last.request.content) == {"ram_mb": 26000, "cpu": 4}
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "COMPUTER",
        "STATE",
        "LIVE",
        "CPU",
        "RAM",
        "MB",
        "DISK",
        "GB",
        "STARTED",
        "FINISHED",
    ]
    assert lines[1].split() == [
        "vm-1",
        "moving",
        "yes",
        "-",
        "26000",
        "-",
        STARTED["started_at"],
        "-",
    ]


@respx.mock
def test_move_json_is_the_move(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.post(f"{BASE}/computers/vm-1/move").mock(return_value=httpx.Response(202, json=STARTED))
    assert _cli.main(["move", "vm-1", "--ram-mb", "26000", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == STARTED


@respx.mock
def test_move_waits_and_exits_0_when_it_is_done(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.post(f"{BASE}/computers/vm-1/move").mock(return_value=httpx.Response(202, json=STARTED))
    moves = respx.get(f"{BASE}/moves").mock(
        return_value=httpx.Response(200, json={"moves": [DONE]})
    )
    assert _cli.main(["move", "dev", "--ram-mb", "26000", "--wait", "--json"]) == 0
    assert moves.called
    assert json.loads(capsys.readouterr().out)["state"] == "done"


@pytest.mark.parametrize(("state", "note"), [("moved", "OLD size"), ("failed", "where it was")])
@respx.mock
def test_a_waited_move_that_is_not_done_exits_1_and_says_why(
    capsys: pytest.CaptureFixture[str], state: str, note: str
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.post(f"{BASE}/computers/vm-1/move").mock(return_value=httpx.Response(202, json=STARTED))
    respx.get(f"{BASE}/moves").mock(
        return_value=httpx.Response(200, json={"moves": [{**DONE, "state": state}]})
    )
    assert _cli.main(["move", "dev", "--ram-mb", "26000", "--wait"]) == 1
    out, err = capsys.readouterr()
    assert state in out
    assert note in err


@respx.mock
def test_move_needs_ram_and_keeps_the_wait_flags_to_wait(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        _cli.main(["move", "dev", "--cpu", "4"])
    assert caught.value.code == 2
    assert "--ram-mb" in capsys.readouterr().err
    assert _cli.main(["move", "dev", "--ram-mb", "26000", "--timeout-ms", "5000", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert (error["code"], error["message"]) == (
        "invalid_arguments",
        "--timeout-ms and --poll-ms go with --wait",
    )
    assert not respx.calls


# --- moves list ----------------------------------------------------------------


@respx.mock
def test_moves_list_prints_every_move(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/moves").mock(return_value=httpx.Response(200, json={"moves": [DONE, OTHER]}))
    assert _cli.main(["moves", "list"]) == 0
    rows = capsys.readouterr().out.splitlines()[1:]
    assert [r.split()[:2] for r in rows] == [["vm-1", "done"], ["vm-2", "done"]]


@respx.mock
def test_moves_list_keeps_one_computer_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.get(f"{BASE}/moves").mock(return_value=httpx.Response(200, json={"moves": [DONE, OTHER]}))
    assert _cli.main(["moves", "list", "--computer", "dev", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"moves": [DONE]}


# --- secrets get ---------------------------------------------------------------


@respx.mock
def test_secrets_get_finds_a_name_and_reads_it_by_id(capsys: pytest.CaptureFixture[str]) -> None:
    listing = respx.get(f"{BASE}/secrets").mock(return_value=httpx.Response(200, json=SECRET_LIST))
    one = respx.get(f"{BASE}/secrets/{SECRET['id']}").mock(
        return_value=httpx.Response(200, json=SECRET)
    )
    assert _cli.main(["secrets", "get", "openai_api_key", "--json"]) == 0
    assert listing.called and one.called
    assert json.loads(capsys.readouterr().out) == SECRET


@respx.mock
def test_secrets_get_prints_one_row_with_no_value(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/secrets").mock(return_value=httpx.Response(200, json=SECRET_LIST))
    respx.get(f"{BASE}/secrets/{SECRET['id']}").mock(return_value=httpx.Response(200, json=SECRET))
    assert _cli.main(["secrets", "get", SECRET["id"]]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [
        "ID",
        "NAME",
        "SCOPE",
        "REVISION",
        "CREATED",
        "UPDATED",
        "LAST",
        "USED",
    ]
    assert lines[1].split() == [
        SECRET["id"],
        "OPENAI_API_KEY",
        "account",
        SECRET["revision_id"],
        SECRET["created_at"],
        SECRET["updated_at"],
        "never",
    ]


@respx.mock
def test_secrets_get_reads_an_unlisted_id_in_the_workspace_given(
    capsys: pytest.CaptureFixture[str],
) -> None:
    other = "csec-00000000000000aa"
    respx.get(f"{BASE}/secrets").mock(return_value=httpx.Response(200, json=SECRET_LIST))
    one = respx.get(f"{BASE}/secrets/{other}").mock(
        return_value=httpx.Response(200, json={**SECRET, "id": other, "workspace_id": WSP["id"]})
    )
    assert _cli.main(["secrets", "get", other, "--workspace", WSP["id"], "--json"]) == 0
    assert dict(one.calls.last.request.url.params) == {"workspace_id": WSP["id"]}


@respx.mock
def test_secrets_get_of_a_name_it_does_not_hold_is_not_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.get(f"{BASE}/secrets").mock(return_value=httpx.Response(200, json=SECRET_LIST))
    assert _cli.main(["secrets", "get", "NOPE", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "not_found"
    assert "NOPE" not in error["message"]
    assert len(respx.calls) == 1


# --- workspaces get and members by name ----------------------------------------


@respx.mock
def test_workspaces_get_takes_a_name(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/workspaces").mock(return_value=httpx.Response(200, json=[WSP]))
    one = respx.get(f"{BASE}/workspaces/{WSP['id']}").mock(
        return_value=httpx.Response(200, json=WSP)
    )
    assert _cli.main(["workspaces", "get", "acme", "--json"]) == 0
    assert one.called
    assert json.loads(capsys.readouterr().out) == WSP


@respx.mock
def test_workspaces_members_takes_a_name(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/workspaces").mock(return_value=httpx.Response(200, json=[WSP]))
    members = respx.get(f"{BASE}/workspaces/{WSP['id']}/members").mock(
        return_value=httpx.Response(200, json=[MEMBER])
    )
    assert _cli.main(["workspaces", "members", "acme", "--json"]) == 0
    assert members.called
    assert json.loads(capsys.readouterr().out) == [MEMBER]


@respx.mock
def test_workspaces_get_refuses_a_name_two_workspaces_share(
    capsys: pytest.CaptureFixture[str],
) -> None:
    twin = {**WSP, "id": "wsp-ba9876543210"}
    respx.get(f"{BASE}/workspaces").mock(return_value=httpx.Response(200, json=[WSP, twin]))
    assert _cli.main(["workspaces", "get", "acme", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "ambiguous_workspace"
    assert len(respx.calls) == 1
