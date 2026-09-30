"""``mandala-py operations list|get|wait`` (OPL-5471): the TS CLI's
``mandala operations``, which this command lacked."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mandala_computer import _cli

BASE = "https://api.test/api/v1"

OP = {
    "id": "op_0123456789abcdef01234567",
    "kind": "clone",
    "computer_id": "vm-2",
    "state": "succeeded",
    "error": None,
    "idempotency_key": None,
    "created_at": "2026-09-01T00:00:00.000Z",
    "updated_at": "2026-09-01T00:00:01.000Z",
    "finished_at": "2026-09-01T00:00:01.000Z",
}
FAILED = {
    **OP,
    "state": "failed",
    "error": {"code": "start_failed", "message": "The computer did not boot."},
}
COMPUTERS = [
    {"id": "vm-1", "name": "dev", "status": "running", "os": "linux"},
    {"id": "vm-2", "name": "scratch", "status": "stopped", "os": "linux"},
    {"id": "vm-3", "name": "scratch", "status": "running", "os": "linux"},
]


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


@respx.mock
def test_list_prints_one_row_each_and_the_next_cursor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = respx.get(f"{BASE}/operations").mock(
        return_value=httpx.Response(200, json={"operations": [OP, FAILED], "next_cursor": "c-2"})
    )
    assert _cli.main(["operations", "list", "--limit", "2", "--cursor", "c-1"]) == 0
    assert dict(route.calls.last.request.url.params) == {"limit": "2", "cursor": "c-1"}
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0].split() == ["ID", "KIND", "STATE", "COMPUTER", "CREATED", "ERROR"]
    assert lines[1].split() == [OP["id"], "clone", "succeeded", "vm-2", OP["created_at"], "-"]
    assert lines[2].split()[2::3] == ["failed", "start_failed"]
    assert "--cursor c-2" in err


@respx.mock
def test_list_json_is_the_page(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/operations").mock(
        return_value=httpx.Response(200, json={"operations": [OP], "next_cursor": None})
    )
    assert _cli.main(["operations", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"operations": [OP], "next_cursor": None}


@respx.mock
def test_list_takes_a_computer_by_name_and_an_idempotency_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    route = respx.get(f"{BASE}/operations").mock(
        return_value=httpx.Response(200, json={"operations": [], "next_cursor": None})
    )
    assert _cli.main(["operations", "list", "--computer", "dev", "--idempotency-key", "k1"]) == 0
    assert dict(route.calls.last.request.url.params) == {
        "computer_id": "vm-1",
        "idempotency_key": "k1",
    }
    assert "no operations" in capsys.readouterr().err


@respx.mock
def test_list_refuses_an_ambiguous_computer_name(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    route = respx.get(f"{BASE}/operations")
    assert _cli.main(["operations", "list", "--computer", "scratch", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "ambiguous_computer"
    assert "vm-2" in error["message"] and "vm-3" in error["message"]
    assert not route.called


@respx.mock
def test_list_sends_an_unknown_computer_as_typed_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    route = respx.get(f"{BASE}/operations").mock(
        return_value=httpx.Response(200, json={"operations": [OP], "next_cursor": None})
    )
    assert _cli.main(["operations", "list", "--computer", "vm-gone"]) == 0
    assert dict(route.calls.last.request.url.params) == {"computer_id": "vm-gone"}
    assert "recorded under the id vm-gone" in capsys.readouterr().err


@respx.mock
def test_get_prints_the_one(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/operations/{OP['id']}").mock(return_value=httpx.Response(200, json=OP))
    assert _cli.main(["operations", "get", OP["id"]]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[:3] for line in lines] == [
        ["ID", "KIND", "STATE"],
        [OP["id"], "clone", "succeeded"],
    ]
    assert _cli.main(["operations", "get", OP["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == OP


@respx.mock
def test_get_of_one_it_cannot_see_is_not_found(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/operations/op_missing").mock(
        return_value=httpx.Response(404, json={"error": "operation not found"})
    )
    assert _cli.main(["operations", "get", "op_missing", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"


@respx.mock
def test_wait_polls_until_it_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.get(f"{BASE}/operations/{OP['id']}").mock(
        side_effect=[
            httpx.Response(200, json={**OP, "state": "running", "finished_at": None}),
            httpx.Response(200, json=OP),
        ]
    )
    assert _cli.main(["operations", "wait", OP["id"], "--poll-ms", "10", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == OP
    assert route.call_count == 2


@respx.mock
def test_wait_on_a_failed_operation_exits_1_as_operation_failed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.get(f"{BASE}/operations/{OP['id']}").mock(return_value=httpx.Response(200, json=FAILED))
    assert _cli.main(["operations", "wait", OP["id"], "--json"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    error = json.loads(err)["error"]
    assert error["code"] == "operation_failed"
    assert "start_failed" in error["message"]
    assert error["details"]["operation"] == FAILED


def test_wait_refuses_a_deadline_that_is_not_positive(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(["operations", "wait", OP["id"], "--timeout-ms", "0", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "invalid_arguments"
    assert "--timeout-ms" in error["message"]
