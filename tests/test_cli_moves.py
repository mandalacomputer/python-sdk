"""Workspace mode of the existing top-level move command."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from tests.test_cli_coverage import BASE, COMPUTERS

from mandala_computer import _cli

WORKSPACE = "wsp-0123456789ab"


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


@pytest.mark.parametrize("target", ["dev", "vm-1"])
@pytest.mark.parametrize("workspace", [WORKSPACE, "unassigned"])
@respx.mock
def test_workspace_move_is_explicit_and_returns_computer(
    target, workspace, capsys, monkeypatch
) -> None:
    def unexpected_default(*args, **kwargs):
        raise AssertionError("workspace move must not consult a saved default")

    monkeypatch.setattr(_cli, "_scope", unexpected_default)
    monkeypatch.setattr(_cli, "read_defaults", unexpected_default)
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTERS))
    destination = None if workspace == "unassigned" else workspace
    row = {**COMPUTERS[0], "workspace_id": destination, "vnc": {"password": "hidden"}}
    route = respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(200, json={"computer": row})
    )
    assert _cli.main(["move", target, "--workspace", workspace, "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {key: value for key, value in row.items() if key != "vnc"}
    assert output.err == ""
    request = route.calls.last.request
    assert json.loads(request.content) == {"workspace_id": destination}
    assert "Idempotency-Key" not in request.headers
    assert len(respx.calls) == 2


@respx.mock
def test_workspace_move_human_output(capsys) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTERS))
    respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(200, json={"computer": {**COMPUTERS[0], "workspace_id": ""}})
    )
    assert _cli.main(["move", "dev", "--workspace", "unassigned"]) == 0
    assert capsys.readouterr().out == "vm-1 workspace: unassigned\n"


@pytest.mark.parametrize(
    "extra",
    [
        ["--ram-mb", "2048"],
        ["--cpu", "2"],
        ["--disk-gb", "20"],
        ["--wait"],
        ["--timeout-ms", "100"],
        ["--poll-ms", "100"],
        ["--idempotency-key", "key"],
    ],
)
@respx.mock
def test_workspace_mode_refuses_host_options_before_requests(extra, capsys) -> None:
    args = ["move", "dev", "--workspace", WORKSPACE, *extra, "--json"]
    if extra[0] == "--ram-mb":
        with pytest.raises(SystemExit) as caught:
            _cli.main(args)
        assert caught.value.code == 2
    else:
        assert _cli.main(args) == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "invalid_arguments"
    assert not respx.calls


@pytest.mark.parametrize("workspace", ["", "research", "null", "wsp-1", WORKSPACE + "\n"])
@respx.mock
def test_workspace_id_is_validated_without_name_lookup(workspace, capsys) -> None:
    assert _cli.main(["move", "dev", "--workspace", workspace, "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_arguments"
    assert not respx.calls


@respx.mock
def test_missing_destination_does_not_use_a_saved_default(capsys, monkeypatch) -> None:
    monkeypatch.setattr(_cli, "_scope", lambda args: WORKSPACE)
    with pytest.raises(SystemExit) as caught:
        _cli.main(["move", "dev", "--json"])
    assert caught.value.code == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "invalid_arguments"
    assert not respx.calls


@pytest.mark.parametrize(
    ("status", "reason", "code"),
    [
        (403, "unsupported", "permission_denied"),
        (409, "try_again", "conflict"),
        (409, "outcome_unknown", "conflict"),
        (503, "try_again", "unavailable"),
    ],
)
@respx.mock
def test_workspace_move_preserves_structured_refusals_without_recovery_actions(
    status, reason, code, capsys
) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTERS))
    route = respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(status, json={"error": "Move refused.", "reason": reason})
    )
    assert _cli.main(["move", "dev", "--workspace", WORKSPACE, "--json"]) == 1
    output = capsys.readouterr()
    assert not output.out
    error = json.loads(output.err)["error"]
    assert (error["status"], error["reason"], error["code"]) == (status, reason, code)
    assert route.call_count == 1 and len(respx.calls) == 2
