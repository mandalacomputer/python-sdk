"""Explicit workspace changes, with identical sync and async behavior."""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
WORKSPACE = "wsp-0123456789ab"
OTHER = "wsp-ba9876543210"
COMPUTER = {
    "id": "vm-1",
    "name": "dev",
    "status": "stopped",
    "workspace_id": OTHER,
    "operation_id": "op-previous",
    "start_error": "old error",
}


@asynccontextmanager
async def computer(asynchronous: bool):
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    if asynchronous:
        async with mc.AsyncClient("com_test", base_url=BASE) as client:
            yield await client.computers.get("vm-1")
    else:
        with mc.Client("com_test", base_url=BASE) as client:
            yield client.computers.get("vm-1")


async def move(c: mc.Computer | mc.AsyncComputer, *args: Any):
    result = c.move_to_workspace(*args)
    return await result if inspect.isawaitable(result) else result


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("destination", [WORKSPACE, OTHER, None])
@respx.mock
async def test_move_returns_same_updated_handle(
    asynchronous: bool, destination: str | None
) -> None:
    updated = {
        "id": "vm-1",
        "name": "current name",
        "status": "stopped",
        "workspace_id": destination,
        "future_public_field": True,
    }
    route = respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(200, json={"computer": updated})
    )
    async with computer(asynchronous) as c:
        assert await move(c, destination) is c
        assert c.raw == updated
        assert c.workspace_id == (destination or "")
        assert c.name == "current name"
        assert c.operation_id is None
    assert route.call_count == 1
    request = route.calls.last.request
    assert json.loads(request.content) == {"workspace_id": destination}
    assert not request.url.params
    assert "Idempotency-Key" not in request.headers
    assert [(call.request.method, call.request.url.path) for call in respx.calls] == [
        ("GET", "/api/v1/computers/vm-1"),
        ("PUT", "/api/v1/computers/vm-1/workspace"),
    ]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("fields", [{}, {"workspace_id": None}, {"workspace_id": ""}])
@respx.mock
async def test_unassignment_accepts_public_unassigned_shapes(asynchronous: bool, fields) -> None:
    row = {"id": "vm-1", "name": "dev", "status": "stopped", **fields}
    respx.put(f"{BASE}/computers/vm-1/workspace").mock(httpx.Response(200, json={"computer": row}))
    async with computer(asynchronous) as c:
        assert await move(c, None) is c
        assert c.raw == row
        assert c.workspace_id == ""


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "destination",
    [
        "",
        " ",
        "unassigned",
        "null",
        "wsp-1",
        "wsp-0123456789AB",
        WORKSPACE + "\n",
        " " + WORKSPACE,
        WORKSPACE + "/../stop",
        "wsp-0123456789abc",
        False,
        0,
        {},
        [],
        b"wsp-0123456789ab",
    ],
)
@respx.mock
async def test_invalid_destination_is_local(asynchronous: bool, destination: Any) -> None:
    async with computer(asynchronous) as c:
        before, operation = c.raw, c.operation_id
        with pytest.raises(ValueError, match="workspace_id"):
            await move(c, destination)
        assert c.raw == before and c.operation_id == operation
    assert len(respx.calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@respx.mock
async def test_destination_is_required(asynchronous: bool) -> None:
    async with computer(asynchronous) as c:
        with pytest.raises(TypeError, match="workspace_id"):
            await move(c)
        assert c.raw == COMPUTER
    assert len(respx.calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@respx.mock
async def test_string_subclass_cannot_change_the_destination(asynchronous: bool) -> None:
    class MisleadingString(str):
        def __str__(self):
            return OTHER

    route = respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(200, json={"computer": {**COMPUTER, "workspace_id": WORKSPACE}})
    )
    async with computer(asynchronous) as c:
        await move(c, MisleadingString(WORKSPACE))
        assert c.workspace_id == WORKSPACE
    assert json.loads(route.calls.last.request.content) == {"workspace_id": WORKSPACE}


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("status", "reason", "exception"),
    [
        (403, "unsupported", mc.PermissionDeniedError),
        (404, "not_found", mc.NotFoundError),
        (409, "unsupported", mc.ConflictError),
        (409, "try_again", mc.ConflictError),
        (409, "outcome_unknown", mc.ConflictError),
        (503, "try_again", mc.UnavailableError),
    ],
)
@respx.mock
async def test_refusal_keeps_cache_and_error_without_cleanup_or_retry(
    asynchronous: bool, status: int, reason: str, exception: type[mc.APIError]
) -> None:
    body = {"error": "The workspace move was refused.", "reason": reason, "detail": "preserved"}
    route = respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(status, json=body, headers={"X-Request-Id": "req-move"})
    )
    async with computer(asynchronous) as c:
        before, operation = c.raw, c.operation_id
        with pytest.raises(exception) as caught:
            await move(c, WORKSPACE)
        assert caught.value.status == status
        assert caught.value.body == body
        assert caught.value.reason == reason
        assert caught.value.request_id == "req-move"
        assert c.raw == before and c.operation_id == operation
    assert route.call_count == 1 and len(respx.calls) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("destination", [WORKSPACE, OTHER, None])
@respx.mock
async def test_confined_credential_refusal_is_never_assumed_to_be_a_noop(
    asynchronous: bool, destination: str | None
) -> None:
    route = respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(403, json={"error": "Use an account-wide credential."})
    )
    async with computer(asynchronous) as c:
        with pytest.raises(mc.PermissionDeniedError):
            await move(c, destination)
        assert c.raw == COMPUTER
    assert route.call_count == 1


BAD_RECORDS = [
    None,
    [],
    {},
    COMPUTER,
    {"computer": None},
    {"computer": []},
    {"computer": {}},
    {"computer": {"id": "vm-2", "workspace_id": WORKSPACE}},
    {"computer": {"id": "vm-1"}},
    {"computer": {"id": "vm-1", "workspace_id": OTHER}},
    {"computer": {"id": "vm-1", "workspace_id": None}},
    {"computer": {"id": "vm-1", "workspace_id": ""}},
]


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("answer", BAD_RECORDS)
@respx.mock
async def test_invalid_success_leaves_cache_unchanged(asynchronous: bool, answer: Any) -> None:
    route = respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(
            200, content=json.dumps(answer), headers={"Content-Type": "application/json"}
        )
    )
    async with computer(asynchronous) as c:
        before, operation = c.raw, c.operation_id
        with pytest.raises(mc.MandalaError, match="outcome is unconfirmed"):
            await move(c, WORKSPACE)
        assert c.raw == before and c.operation_id == operation
    assert route.call_count == 1 and len(respx.calls) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("value", [False, 0, [], {}, OTHER, "unassigned"])
@respx.mock
async def test_unassignment_never_accepts_other_falsey_or_named_workspaces(
    asynchronous: bool, value: Any
) -> None:
    respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(200, json={"computer": {**COMPUTER, "workspace_id": value}})
    )
    async with computer(asynchronous) as c:
        with pytest.raises(mc.MandalaError, match="outcome is unconfirmed"):
            await move(c, None)
        assert c.raw == COMPUTER
    assert len(respx.calls) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("status", [201, 202, 204])
@respx.mock
async def test_other_success_statuses_do_not_claim_completed_move(
    asynchronous: bool, status: int
) -> None:
    respx.put(f"{BASE}/computers/vm-1/workspace").mock(
        httpx.Response(status, json={"computer": {**COMPUTER, "workspace_id": WORKSPACE}})
    )
    async with computer(asynchronous) as c:
        with pytest.raises(mc.MandalaError, match="outcome is unconfirmed"):
            await move(c, WORKSPACE)
        assert c.raw == COMPUTER
    assert len(respx.calls) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("content", [b"", b"<html>login</html>", b'{"computer":'])
@respx.mock
async def test_malformed_json_does_not_replace_cache(asynchronous: bool, content: bytes) -> None:
    respx.put(f"{BASE}/computers/vm-1/workspace").mock(httpx.Response(200, content=content))
    async with computer(asynchronous) as c:
        with pytest.raises(mc.MandalaError, match="outcome is unconfirmed"):
            await move(c, WORKSPACE)
        assert c.raw == COMPUTER
    assert len(respx.calls) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "failure", [httpx.ReadError("disconnected"), httpx.ReadTimeout("timed out")]
)
@respx.mock
async def test_transport_failure_is_not_retried(asynchronous: bool, failure: Exception) -> None:
    route = respx.put(f"{BASE}/computers/vm-1/workspace").mock(side_effect=failure)
    async with computer(asynchronous) as c:
        before, operation = c.raw, c.operation_id
        with pytest.raises((mc.ConnectionError, mc.TimeoutError)):
            await move(c, WORKSPACE)
        assert c.raw == before and c.operation_id == operation
    assert route.call_count == 1 and len(respx.calls) == 2
