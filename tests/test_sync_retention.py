"""Optional retained metadata must not change an executed command's outcome."""

from typing import Any

import httpx
import respx
from tests.test_api_contract import BASE, computer, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414

RESULT_ID = "res_" + "a" * 32


@respx.mock
async def test_explicit_retention_returns_optional_identity_without_an_extra_read(
    client: Any,
) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(
            200,
            json={
                "exit_code": 0,
                "stdout_b64": "",
                "stderr_b64": "",
                "timed_out": False,
                "out_truncated": False,
                "err_truncated": False,
                "result_id": RESULT_ID,
            },
        )
    )
    result = await resolved(computer(client).exec("true", retain_output=True))
    assert result.result_id == RESULT_ID
    assert result.ok
    assert route.call_count == 1


import dataclasses
import inspect
import json

import pytest

import mandala_computer as mc
from mandala_computer import _api


@pytest.mark.parametrize(
    "option,wire",
    [
        (False, None),
        (True, True),
        ({}, {}),
        (
            {"max_bytes_per_stream": 12, "retention_seconds": 60},
            {"max_bytes_per_stream": 12, "retention_seconds": 60},
        ),
    ],
)
@respx.mock
async def test_opt_in_is_strict_and_false_preserves_default_wire(
    client: Any, option: Any, wire: Any
) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0})
    )
    await resolved(computer(client).exec("true", retain_output=option))
    expected = {"command": "true", "timeout_s": 30}
    if wire is not None:
        expected["retain_output"] = wire
    assert json.loads(route.calls[0].request.content) == expected
    assert route.call_count == 1


@pytest.mark.parametrize(
    "option",
    [
        None,
        1,
        [],
        "true",
        {"unknown": 1},
        {"retention_seconds": None},
        {"max_bytes_per_stream": True},
        {"retention_seconds": 604801},
        {"max_bytes_per_stream": 0},
    ],
)
@respx.mock
async def test_bad_options_do_not_execute(client: Any, option: Any) -> None:
    with pytest.raises((TypeError, ValueError)):
        await resolved(computer(client).exec("true", retain_output=option))
    assert not respx.calls


@pytest.mark.parametrize("status", [200, 201, 202])
@pytest.mark.parametrize("code", [0, -9, 2147483647, -2147483648])
@respx.mock
async def test_optional_id_requires_200_but_preserves_signed_outcome(
    client: Any, status: int, code: int
) -> None:
    payload = {
        "exit_code": code,
        "stdout_b64": "YQ==",
        "stderr_b64": "",
        "timed_out": False,
        "out_truncated": False,
        "err_truncated": False,
        "result_id": RESULT_ID,
    }
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(status, json=payload))
    got = await resolved(computer(client).exec("true", retain_output=True))
    assert got.exit_code == code and got.stdout == b"a"
    assert got.result_id == (RESULT_ID if status == 200 else None)
    assert got.raw == payload and route.call_count == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"result_id": "../secret"},
        {"result_id": True},
        {"result_id": None},
        {"timed_out": True},
        {"timed_out": 0},
        {"timed_out": "false"},
        {"timed_out": None},
        {"out_truncated": 0},
        {"err_truncated": "false"},
        {"exit_code": 2147483648},
        {"out_truncated": "false"},
        {"stdout_b64": "invalid"},
        {"stdout_b64": "YR=="},
    ],
)
@respx.mock
async def test_malformed_optional_metadata_never_replays_or_changes_legacy_result(
    client: Any, extra: dict
) -> None:
    payload = {
        "exit_code": 0,
        "stdout_b64": "YQ==",
        "stderr_b64": "",
        "timed_out": False,
        "out_truncated": False,
        "err_truncated": False,
        "result_id": RESULT_ID,
        **extra,
    }
    baseline = mc.ExecResult.from_api({k: v for k, v in payload.items() if k != "result_id"})
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json=payload))
    result = await resolved(computer(client).exec("true", retain_output=True))
    assert result == baseline and hash(result) == hash(baseline) and result.result_id is None
    assert route.call_count == 1


def test_result_identity_preserves_all_old_value_protocols() -> None:
    old = mc.ExecResult(-9, b"a", b"b", False, True, False, {"private": 1})
    linked = dataclasses.replace(old, result_id=RESULT_ID)
    assert old == linked and hash(old) == hash(linked) and {old, linked} == {old}
    assert mc.ExecResult.__match_args__ == (
        "exit_code",
        "stdout",
        "stderr",
        "timed_out",
        "out_truncated",
        "err_truncated",
        "raw",
    )
    assert (
        inspect.signature(mc.ExecResult).parameters["result_id"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
    match linked:
        case mc.ExecResult(-9, b"a", b"b", False, True, False, {"private": 1}):
            pass
        case _:
            pytest.fail("old positional matching changed")


def test_background_and_readiness_do_not_acquire_retention() -> None:
    for cls in (mc.Computer, mc.AsyncComputer):
        for name in ("start_exec", "open", "wait_for_guest"):
            assert "retain_output" not in inspect.signature(getattr(cls, name)).parameters
    assert "retain_output" not in _api.exec_body("true", 30)
    assert "retain_output" not in _api.exec_body("true", 0, background=True)
    with pytest.raises(ValueError):
        _api.exec_body("true", 0, background=True, retain_output={})
