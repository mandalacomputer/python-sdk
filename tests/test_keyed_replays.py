"""A keyed create or clone resent after its answer was lost, which the platform
answers with the call's operation rather than the computer: read as the
computer the operation names, not refused as "expected a computer". And the
``computer_id`` an in-progress refusal carries — sync and async."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, computer, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414

import mandala_computer as mc

OP_ID = "op_0123456789abcdef01234567"
MADE: dict[str, Any] = {"id": "vm-9", "name": "made", "status": "stopped", "os": "linux"}
REPLAYED = {"Idempotent-Replayed": "true"}


def operation(computer_id: str | None, kind: str = "create") -> dict[str, Any]:
    return {
        "id": OP_ID,
        "kind": kind,
        "computer_id": computer_id,
        "state": "succeeded",
        "error": None,
        "created_at": "2026-11-09T08:00:00.000Z",
        "updated_at": "2026-11-09T08:00:01.000Z",
        "finished_at": "2026-11-09T08:00:01.000Z",
    }


#: The answer shapes that replay an operation, not a computer: a lost answer
#: the platform has since linked to its computer, and an answer too large to
#: keep (whose operation names the computer once it is known).
LINKED = {"operation_id": OP_ID, "operation": operation("vm-9")}

#: Each call that makes a computer, the route it posts to, and how to call it.
MAKERS: list[tuple[str, str, Any]] = [
    ("create", "computers", lambda cl: cl.computers.create(template="base", idempotency_key="k")),
    ("clone", "computers/vm-1/clone", lambda cl: computer(cl).clone("copy", idempotency_key="k")),
    (
        "snapshot clone",
        "snapshots/snap-1/clone",
        lambda cl: cl.snapshots.clone("snap-1", name="copy", idempotency_key="k"),
    ),
]


@pytest.mark.parametrize(("label", "route", "call"), MAKERS, ids=[m[0] for m in MAKERS])
@respx.mock
async def test_a_replayed_operation_answer_reads_the_computer_it_names(
    client: Any, label: str, route: str, call: Any
) -> None:
    posted = respx.post(f"{BASE}/{route}").mock(
        return_value=httpx.Response(200, json=LINKED, headers=REPLAYED)
    )
    read = respx.get(f"{BASE}/computers/vm-9").mock(return_value=httpx.Response(200, json=MADE))
    made = await resolved(call(client))
    assert posted.call_count == 1 and read.call_count == 1
    assert made.id == "vm-9" and made.name == "made"


@pytest.mark.parametrize("op", [None, operation(None)], ids=["null", "no-computer"])
@pytest.mark.parametrize(("label", "route", "call"), MAKERS, ids=[m[0] for m in MAKERS])
@respx.mock
async def test_a_replayed_operation_naming_no_computer_says_which_operation(
    client: Any, label: str, route: str, call: Any, op: Any
) -> None:
    """The answer too large to keep, whose operation is gone or names no
    computer yet: refused with the operation's id, which is what to read."""
    respx.post(f"{BASE}/{route}").mock(
        return_value=httpx.Response(
            200, json={"operation_id": OP_ID, "operation": op}, headers=REPLAYED
        )
    )
    with pytest.raises(mc.MandalaError, match=OP_ID):
        await resolved(call(client))


@pytest.mark.parametrize(
    "body",
    [
        {"operation_id": OP_ID},
        {"operation": operation("vm-9")},
        {},
    ],
    ids=["no-operation", "no-operation-id", "empty"],
)
@respx.mock
async def test_any_other_id_less_answer_is_still_refused_as_not_a_computer(
    client: Any, body: dict[str, Any]
) -> None:
    respx.post(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError, match="expected a computer from POST computers"):
        await resolved(client.computers.create(template="base"))


@respx.mock
async def test_a_computer_answer_is_read_as_before_without_a_second_request(
    client: Any,
) -> None:
    respx.post(f"{BASE}/computers").mock(
        return_value=httpx.Response(201, json={**MADE, "operation_id": OP_ID})
    )
    read = respx.get(f"{BASE}/computers/vm-9")
    made = await resolved(client.computers.create(template="base"))
    assert made.id == "vm-9" and not read.called


def test_an_in_progress_refusal_names_the_computer_the_call_made() -> None:
    err = mc.ConflictError(
        "still in progress",
        status=409,
        body={
            "code": "idempotency_in_progress",
            "reason": "contention",
            "operation_id": OP_ID,
            "computer_id": "vm-9",
        },
    )
    assert err.computer_id == "vm-9"
    assert err.operation_id == OP_ID
    for body in ({"code": "idempotency_in_progress"}, {"computer_id": ""}, "text", None):
        assert mc.ConflictError("x", status=409, body=body).computer_id is None


@respx.mock
def test_the_in_progress_computer_id_reaches_a_caller_of_create() -> None:
    respx.post(f"{BASE}/computers").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": "The call with this Idempotency-Key is still in progress.",
                "code": "idempotency_in_progress",
                "reason": "contention",
                "operation_id": OP_ID,
                "computer_id": "vm-9",
            },
            headers={"Retry-After": "2"},
        )
    )
    with mc.Client("com_test", base_url=BASE) as cl, pytest.raises(mc.ConflictError) as caught:
        cl.computers.create(template="base", idempotency_key="k")
    assert caught.value.computer_id == "vm-9"
