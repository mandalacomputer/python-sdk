"""A 409 met by a ``wait_*`` helper ends the wait unless it says it will clear.

A permanent ``reason`` or an always-final ``code`` ends it with that error at
once; ``contention``/``starting`` and the passing codes keep it polling. A 409
with no word is unclassified, and only the desktop wait, whose probe documents
exactly that 409 as "no session yet", polls through one (OPL-6080).
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer._exceptions import _is_transient_for_poll

BASE = "https://api.test/api/v1"
COMPUTER: dict[str, Any] = {"id": "vm-1", "name": "demo", "status": "running", "os": "linux"}
OK = {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}
OP_ID = "op_0123456789abcdef01234567"
OPERATION: dict[str, Any] = {
    "id": OP_ID,
    "kind": "clone",
    "computer_id": "vm-2",
    "state": "succeeded",
    "error": None,
    "created_at": "2026-09-26T08:00:00.000Z",
    "updated_at": "2026-09-26T08:01:10.000Z",
    "finished_at": "2026-09-26T08:01:10.000Z",
}


def conflict(**body: Any) -> mc.ConflictError:
    return mc.ConflictError("refused", status=409, body={"error": "refused", **body})


@pytest.mark.parametrize(
    ("err", "settling", "polled"),
    [
        (conflict(reason="contention"), False, True),
        (conflict(reason="starting"), False, True),
        (conflict(code="idempotency_in_progress"), False, True),
        (conflict(code="WORKSPACE_MOVING"), False, True),
        (conflict(code="WORKSPACE_MOVING", reason="unsupported"), False, False),
        (conflict(reason="unavailable"), True, False),
        (conflict(reason="unsupported"), True, False),
        (conflict(reason="revoked"), False, False),
        (conflict(reason="stale_capture"), False, False),
        (conflict(code="idempotency_outcome_unknown"), True, False),
        (conflict(code="template_image_preparing", reason="contention"), True, False),
        (conflict(code="LABEL_TAKEN"), False, False),
        (conflict(), False, False),
        (conflict(), True, True),
        (conflict(reason="a-word-from-the-future"), False, False),
        (conflict(reason="a-word-from-the-future"), True, True),
    ],
)
def test_a_poll_reads_the_409s_word(err: mc.ConflictError, settling: bool, polled: bool) -> None:
    assert _is_transient_for_poll(err, settling_409=settling) is polled


def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


def aclient() -> mc.AsyncClient:
    return mc.AsyncClient("com_test", base_url=BASE)


@pytest.mark.parametrize(
    "body",
    [
        {"error": "refused"},
        {"error": "not running", "reason": "unavailable"},
        {"error": "no", "code": "idempotency_outcome_unknown"},
    ],
)
@respx.mock
def test_wait_for_guest_ends_on_a_409_that_does_not_clear(body: dict[str, Any]) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        return_value=httpx.Response(409, json=body)
    )
    with client() as c:
        computer = c.computers.get("vm-1")
        with pytest.raises(mc.ConflictError):
            computer.wait_for_guest(timeout=30, poll=0.01)
    assert len(probe.calls) == 1


@respx.mock
async def test_async_wait_for_guest_ends_on_a_reasonless_409() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        return_value=httpx.Response(409, json={"error": "refused"})
    )
    async with aclient() as c:
        computer = await c.computers.get("vm-1")
        with pytest.raises(mc.ConflictError):
            await computer.wait_for_guest(timeout=30, poll=0.01)
    assert len(probe.calls) == 1


@respx.mock
def test_wait_for_guest_still_polls_a_contention_409() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        side_effect=[
            httpx.Response(409, json={"error": "busy", "reason": "contention"}),
            httpx.Response(200, json=OK),
        ]
    )
    with client() as c:
        computer = c.computers.get("vm-1")
        assert computer.wait_for_guest(timeout=30, poll=0.01) is computer
    assert len(probe.calls) == 2


OP_PATH = re.compile(rf"{re.escape(BASE)}/operations/[^/]+$")


@respx.mock
def test_operations_wait_ends_on_a_permanent_409() -> None:
    route = respx.get(OP_PATH).mock(
        return_value=httpx.Response(409, json={"error": "no", "reason": "revoked"})
    )
    with client() as c, pytest.raises(mc.ConflictError):
        c.operations.wait(OP_ID, timeout=30, poll=0.01)
    assert len(route.calls) == 1


@respx.mock
async def test_async_operations_wait_polls_through_contention() -> None:
    route = respx.get(OP_PATH).mock(
        side_effect=[
            httpx.Response(409, json={"error": "busy", "reason": "contention"}),
            httpx.Response(200, json=OPERATION),
        ]
    )
    async with aclient() as c:
        op = await c.operations.wait(OP_ID, timeout=30, poll=0.01)
    assert op.state == "succeeded"
    assert len(route.calls) == 2
