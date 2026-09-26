"""``client.operations`` (platform OPL-5055): the read, the page, the wait, and
the ``operation_id`` every lifecycle answer now carries — sync and async."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"

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
RUNNING: dict[str, Any] = {**OPERATION, "state": "running", "finished_at": None}
FAILED: dict[str, Any] = {
    **OPERATION,
    "kind": "create",
    "state": "failed",
    "error": {"code": "start_failed", "message": "The computer was created and would not start."},
}
COMPUTER: dict[str, Any] = {"id": "vm-1", "name": "demo", "status": "running", "os": "linux"}
MOVE_STARTED: dict[str, Any] = {
    "computer_id": "vm-1",
    "state": "moving",
    "detail": "",
    "live": True,
    "ram_mb": 26000,
    "started_at": "2026-08-23T02:00:12.699Z",
}
OP_PATH = re.compile(rf"{re.escape(BASE)}/operations/[^/]+$")


def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


def aclient() -> mc.AsyncClient:
    return mc.AsyncClient("com_test", base_url=BASE)


def sequence(*bodies: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Answers each read with the next body in turn, and the last one forever."""
    answers = list(bodies)

    def answer(_request: httpx.Request) -> httpx.Response:
        body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx.Response(200, json=body)

    return answer


# --- get and list ----------------------------------------------------------------


@respx.mock
def test_get_decodes_every_field() -> None:
    route = respx.get(f"{BASE}/operations/{OP_ID}").mock(
        return_value=httpx.Response(200, json=OPERATION)
    )
    op = client().operations.get(OP_ID)
    assert route.called
    assert route.calls.last.request.url.query == b""
    assert op == mc.Operation(
        id=OP_ID,
        kind="clone",
        computer_id="vm-2",
        state="succeeded",
        error=None,
        created_at=OPERATION["created_at"],
        updated_at=OPERATION["updated_at"],
        finished_at=OPERATION["finished_at"],
    )
    assert op.raw == OPERATION


@respx.mock
def test_get_keeps_a_kind_and_state_it_does_not_know() -> None:
    respx.get(OP_PATH).mock(
        return_value=httpx.Response(200, json={**OPERATION, "kind": "delete", "state": "queued"})
    )
    op = client().operations.get(OP_ID)
    assert (op.kind, op.state) == ("delete", "queued")


@respx.mock
def test_get_decodes_a_failure_and_a_restore_with_no_computer() -> None:
    respx.get(OP_PATH).mock(
        return_value=httpx.Response(200, json={**FAILED, "kind": "restore", "computer_id": None})
    )
    op = client().operations.get(OP_ID)
    assert op.error == mc.OperationError("start_failed", FAILED["error"]["message"])
    assert op.computer_id is None


@pytest.mark.parametrize(
    "bad",
    [
        {**OPERATION, "id": ""},
        {**OPERATION, "state": 7},
        {**OPERATION, "kind": None},
        {**OPERATION, "error": "start_failed"},
        {**OPERATION, "error": {"code": "start_failed"}},
        {**OPERATION, "computer_id": 7},
    ],
)
@respx.mock
def test_get_refuses_an_answer_a_wait_could_not_decide_on(bad: dict[str, Any]) -> None:
    respx.get(OP_PATH).mock(return_value=httpx.Response(200, json=bad))
    with pytest.raises(mc.MandalaError):
        client().operations.get(OP_ID)


def test_get_refuses_an_empty_id_before_sending() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            client().operations.get("")
        assert not mock.calls


@respx.mock
def test_list_sends_every_parameter_and_decodes_the_page() -> None:
    route = respx.get(f"{BASE}/operations").mock(
        return_value=httpx.Response(
            200,
            json={"operations": [OPERATION, RUNNING], "next_cursor": "op_00000000000000000000000a"},
        )
    )
    page = client().operations.list(
        computer_id="vm-1", limit=2, cursor="op_00000000000000000000000b"
    )
    assert dict(route.calls.last.request.url.params) == {
        "computer_id": "vm-1",
        "limit": "2",
        "cursor": "op_00000000000000000000000b",
    }
    assert [o.state for o in page.operations] == ["succeeded", "running"]
    assert page.operations[1].finished_at is None
    assert page.next_cursor == "op_00000000000000000000000a"


@respx.mock
def test_list_sends_nothing_it_was_not_given() -> None:
    route = respx.get(f"{BASE}/operations").mock(
        return_value=httpx.Response(200, json={"operations": [], "next_cursor": None})
    )
    page = client().operations.list()
    assert route.calls.last.request.url.query == b""
    assert page.next_cursor is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": 1.5},
        {"computer_id": ""},
        {"computer_id": " vm-1"},
        {"cursor": ""},
    ],
)
def test_list_refuses_what_the_platform_would_400_before_sending(kwargs: dict[str, Any]) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises((ValueError, TypeError)):
            client().operations.list(**kwargs)
        assert not mock.calls


@pytest.mark.parametrize(
    "body",
    [
        {"operations": [], "next_cursor": 7},
        {"operations": [], "next_cursor": ""},
        {"operations": {}, "next_cursor": None},
    ],
)
@respx.mock
def test_list_refuses_a_page_that_could_not_be_walked(body: dict[str, Any]) -> None:
    respx.get(f"{BASE}/operations").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError):
        client().operations.list()


# --- wait ----------------------------------------------------------------------


@respx.mock
def test_wait_polls_through_live_states_and_returns_on_succeeded() -> None:
    route = respx.get(OP_PATH).mock(
        side_effect=sequence({**RUNNING, "state": "pending"}, RUNNING, RUNNING, OPERATION)
    )
    op = client().operations.wait(OP_ID, timeout=5, poll=0.001)
    assert op.state == "succeeded"
    assert route.call_count == 4


@respx.mock
def test_wait_takes_the_operation_itself() -> None:
    route = respx.get(OP_PATH).mock(return_value=httpx.Response(200, json=OPERATION))
    c = client()
    c.operations.wait(c.operations.get(OP_ID), poll=0.001)
    assert route.call_count == 2


@respx.mock
def test_wait_raises_operation_failed_with_the_code_and_sentence() -> None:
    route = respx.get(OP_PATH).mock(side_effect=sequence(RUNNING, FAILED))
    with pytest.raises(mc.OperationFailedError) as caught:
        client().operations.wait(OP_ID, timeout=5, poll=0.001)
    err = caught.value
    assert isinstance(err, mc.MandalaError)
    assert err.code == "start_failed"
    assert err.detail == "The computer was created and would not start."
    assert err.operation.id == OP_ID
    assert "start_failed" in str(err) and "would not start" in str(err)
    # A failed step is the platform's verdict, not a moment to retry through.
    assert mc.is_transient(err) is False
    assert route.call_count == 2


@respx.mock
def test_wait_raises_on_failed_with_no_error_rather_than_returning() -> None:
    respx.get(OP_PATH).mock(return_value=httpx.Response(200, json={**FAILED, "error": None}))
    with pytest.raises(mc.OperationFailedError) as caught:
        client().operations.wait(OP_ID, poll=0.001)
    assert caught.value.code == ""


@respx.mock
def test_wait_refuses_a_finished_state_it_does_not_know() -> None:
    route = respx.get(OP_PATH).mock(
        return_value=httpx.Response(200, json={**OPERATION, "state": "cancelled"})
    )
    with pytest.raises(mc.MandalaError, match="finished in state 'cancelled'"):
        client().operations.wait(OP_ID, timeout=5, poll=0.001)
    assert route.call_count == 1


@respx.mock
def test_wait_polls_through_a_live_state_it_does_not_know() -> None:
    respx.get(OP_PATH).mock(side_effect=sequence({**RUNNING, "state": "queued"}, OPERATION))
    assert client().operations.wait(OP_ID, poll=0.001).state == "succeeded"


@respx.mock
def test_wait_rides_out_a_transient_failure() -> None:
    answers = [httpx.Response(503, json={"error": "down"}), httpx.Response(200, json=OPERATION)]
    respx.get(OP_PATH).mock(side_effect=answers)
    assert client().operations.wait(OP_ID, poll=0.001).state == "succeeded"


@respx.mock
def test_wait_stops_at_once_on_an_id_it_cannot_see() -> None:
    route = respx.get(OP_PATH).mock(
        return_value=httpx.Response(404, json={"error": "operation not found"})
    )
    with pytest.raises(mc.NotFoundError):
        client().operations.wait(OP_ID, poll=0.001)
    assert route.call_count == 1


@respx.mock
def test_wait_times_out_on_one_that_stays_live() -> None:
    respx.get(OP_PATH).mock(return_value=httpx.Response(200, json=RUNNING))
    with pytest.raises(mc.TimeoutError, match="still running.*only this wait has"):
        client().operations.wait(OP_ID, timeout=0.05, poll=0.01)


def test_wait_refuses_a_missing_id_with_a_sentence() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError, match="carried no operation_id"):
            client().operations.wait(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            client().operations.wait(OP_ID, timeout=float("nan"))
        assert not mock.calls


async def test_async_get_list_and_wait() -> None:
    with respx.mock:
        respx.get(f"{BASE}/operations").mock(
            return_value=httpx.Response(200, json={"operations": [OPERATION], "next_cursor": None})
        )
        route = respx.get(OP_PATH).mock(side_effect=sequence(RUNNING, OPERATION, RUNNING, FAILED))
        c = aclient()
        page = await c.operations.list(computer_id="vm-2")
        assert page.operations[0].id == OP_ID
        assert (await c.operations.wait(OP_ID, poll=0.001)).state == "succeeded"
        with pytest.raises(mc.OperationFailedError):
            await c.operations.wait(OP_ID, poll=0.001)
        assert (await c.operations.get(OP_ID)).state == "failed"
        assert route.call_count == 5


# --- operation_id on lifecycle answers -----------------------------------------


WITH_OP = {**COMPUTER, "operation_id": "op_000000000000000000000001"}


@respx.mock
def test_on_a_created_and_cloned_computer_surviving_a_refresh() -> None:
    respx.post(f"{BASE}/computers").mock(return_value=httpx.Response(201, json=WITH_OP))
    respx.post(f"{BASE}/computers/vm-1/clone").mock(return_value=httpx.Response(201, json=WITH_OP))
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    c = client()
    vm = c.computers.create(name="demo")
    assert vm.operation_id == "op_000000000000000000000001"
    vm.refresh()
    assert vm.operation_id == "op_000000000000000000000001"
    assert vm.clone("copy").operation_id == "op_000000000000000000000001"


@respx.mock
def test_on_a_create_that_would_not_boot_where_it_sits_on_the_envelope() -> None:
    respx.post(f"{BASE}/computers").mock(
        return_value=httpx.Response(
            201,
            json={
                "computer": {**COMPUTER, "status": "stopped"},
                "start_error": "no",
                "operation_id": "op_000000000000000000000002",
            },
        )
    )
    vm = client().computers.create()
    assert vm.start_error == "no"
    assert vm.operation_id == "op_000000000000000000000002"


@pytest.mark.parametrize("action", ["start", "stop", "suspend", "restart"])
@respx.mock
def test_taken_from_a_power_acknowledgement_before_the_refresh(action: str) -> None:
    op = f"op_{action:0<24}"
    respx.post(f"{BASE}/computers/vm-1/{action}").mock(
        return_value=httpx.Response(200, json={"ok": True, "operation_id": op})
    )
    get = respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    vm = client().computers.get("vm-1")
    assert vm.operation_id is None
    getattr(vm, action)()
    assert vm.operation_id == op
    assert get.call_count == 2


@pytest.mark.parametrize("action", ["start", "stop", "suspend", "restart"])
async def test_async_taken_from_a_power_acknowledgement(action: str) -> None:
    op = f"op_{action:0<24}"
    with respx.mock:
        respx.post(f"{BASE}/computers/vm-1/{action}").mock(
            return_value=httpx.Response(200, json={"ok": True, "operation_id": op})
        )
        respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
        vm = await aclient().computers.get("vm-1")
        await getattr(vm, action)()
        assert vm.operation_id == op


@respx.mock
def test_an_unreadable_acknowledgement_reads_as_none_and_does_not_fail_the_call() -> None:
    respx.post(f"{BASE}/computers/vm-1/stop").mock(
        side_effect=[httpx.Response(204), httpx.Response(200, text="<html>")]
    )
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    vm = client().computers.get("vm-1")
    vm.stop()
    assert vm.operation_id is None
    vm.stop()
    assert vm.operation_id is None


@respx.mock
def test_replaced_by_a_resize_and_cleared_by_a_rename() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    respx.patch(f"{BASE}/computers/vm-1").mock(
        side_effect=[
            httpx.Response(200, json={**COMPUTER, "ram_mb": 8192, "operation_id": "op_b"}),
            httpx.Response(200, json={**COMPUTER, "name": "renamed"}),
        ]
    )
    vm = client().computers.get("vm-1")
    vm.resize(ram_mb=8192)
    assert vm.operation_id == "op_b"
    vm.rename("renamed")
    assert vm.operation_id is None


@pytest.mark.parametrize("value", [None, "", 7, {"id": "op"}])
@respx.mock
def test_anything_but_a_nonempty_string_reads_as_absent(value: object) -> None:
    respx.post(f"{BASE}/computers").mock(
        return_value=httpx.Response(201, json={**COMPUTER, "operation_id": value})
    )
    assert client().computers.create().operation_id is None


@respx.mock
def test_on_a_snapshot_restore_which_now_answers_what_it_acknowledged() -> None:
    respx.post(f"{BASE}/snapshots/snap-1/restore").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "operation_id": "op_c"}),
            httpx.Response(200, json={"ok": True}),
            httpx.Response(204),
        ]
    )
    c = client()
    ack = c.snapshots.restore("snap-1")
    assert ack.operation_id == "op_c"
    assert ack.raw == {"ok": True, "operation_id": "op_c"}
    assert c.snapshots.restore("snap-1") == mc.LifecycleAck(None)
    assert c.snapshots.restore("snap-1").operation_id is None


async def test_async_restore_answers_its_operation() -> None:
    with respx.mock:
        respx.post(f"{BASE}/snapshots/snap-1/restore").mock(
            return_value=httpx.Response(200, json={"ok": True, "operation_id": "op_c"})
        )
        assert (await aclient().snapshots.restore("snap-1")).operation_id == "op_c"


@respx.mock
def test_on_the_move_a_relocate_accepted_and_never_on_a_listed_one() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    respx.post(f"{BASE}/computers/vm-1/move").mock(
        return_value=httpx.Response(202, json={**MOVE_STARTED, "operation_id": "op_d"})
    )
    respx.get(f"{BASE}/moves").mock(
        return_value=httpx.Response(200, json={"moves": [MOVE_STARTED]})
    )
    c = client()
    move = c.computers.get("vm-1").relocate(ram_mb=26000)
    assert move.operation_id == "op_d"
    assert c.moves.list()[0].operation_id is None
