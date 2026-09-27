"""``Idempotency-Key`` on every lifecycle call (platform OPL-5127): what is sent,
how often a key is made, what an unknown outcome carries, and the operations
filter that finds a call whose answer was lost — sync and async."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _api
from mandala_computer._client import error_for_status

BASE = "https://api.test/api/v1"
HEADER = "Idempotency-Key"
KEY_SYNTAX = re.compile(r"[\x21-\x7e]{1,255}")

COMPUTER: dict[str, Any] = {"id": "vm-1", "name": "demo", "status": "stopped", "os": "linux"}
MOVE_STARTED: dict[str, Any] = {
    "computer_id": "vm-1",
    "state": "moving",
    "detail": "",
    "live": True,
    "ram_mb": 32768,
    "started_at": "2026-08-23T02:00:12.699Z",
}
OPERATION: dict[str, Any] = {
    "id": "op_0123456789abcdef01234567",
    "kind": "delete",
    "computer_id": "vm-1",
    "state": "succeeded",
    "error": None,
    "created_at": "2026-09-26T08:00:00.000Z",
    "updated_at": "2026-09-26T08:00:01.000Z",
    "finished_at": "2026-09-26T08:00:01.000Z",
}


def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


def aclient() -> mc.AsyncClient:
    return mc.AsyncClient("com_test", base_url=BASE)


def platform(mock: respx.MockRouter) -> None:
    """Every lifecycle route answering as the platform does on success."""
    ok = httpx.Response(200, json={"ok": True, "operation_id": OPERATION["id"]})
    mock.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    mock.post(f"{BASE}/computers").mock(return_value=httpx.Response(201, json=COMPUTER))
    for verb in ("start", "stop", "suspend", "restart"):
        mock.post(f"{BASE}/computers/vm-1/{verb}").mock(return_value=ok)
    mock.post(f"{BASE}/computers/vm-1/clone").mock(return_value=httpx.Response(201, json=COMPUTER))
    mock.patch(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    mock.post(f"{BASE}/computers/vm-1/move").mock(
        return_value=httpx.Response(202, json=MOVE_STARTED)
    )
    mock.delete(f"{BASE}/computers/vm-1").mock(
        return_value=httpx.Response(200, json={"ok": True, "snapshots_deleted": 0})
    )
    mock.post(f"{BASE}/snapshots/snap-1/restore").mock(return_value=ok)
    mock.post(f"{BASE}/snapshots/snap-1/clone").mock(
        return_value=httpx.Response(201, json=COMPUTER)
    )


Call = Callable[[mc.Client, mc.Computer, Any], object]

#: Every lifecycle call this SDK makes, once each. ``k`` is the caller's key or None.
LIFECYCLE: list[tuple[str, Call]] = [
    ("create", lambda c, _vm, k: c.computers.create(template="base", idempotency_key=k)),
    ("start", lambda _c, vm, k: vm.start(idempotency_key=k)),
    ("stop", lambda _c, vm, k: vm.stop(idempotency_key=k)),
    ("suspend", lambda _c, vm, k: vm.suspend(idempotency_key=k)),
    ("restart", lambda _c, vm, k: vm.restart(idempotency_key=k)),
    ("clone", lambda _c, vm, k: vm.clone("copy", idempotency_key=k)),
    ("rename", lambda _c, vm, k: vm.rename("renamed", idempotency_key=k)),
    ("resize", lambda _c, vm, k: vm.resize(ram_mb=4096, idempotency_key=k)),
    ("set_idle_suspend", lambda _c, vm, k: vm.set_idle_suspend(30, idempotency_key=k)),
    ("set_browser_proxy", lambda _c, vm, k: vm.set_browser_proxy(None, idempotency_key=k)),
    ("relocate", lambda _c, vm, k: vm.relocate(ram_mb=32768, idempotency_key=k)),
    ("delete", lambda _c, vm, k: vm.delete(idempotency_key=k)),
    ("snapshots.restore", lambda c, _vm, k: c.snapshots.restore("snap-1", idempotency_key=k)),
    ("snapshots.clone", lambda c, _vm, k: c.snapshots.clone("snap-1", "copy", idempotency_key=k)),
]


def keyed(mock: respx.MockRouter) -> list[str | None]:
    """The Idempotency-Key of every request that was not a read, in order."""
    return [c.request.headers.get(HEADER) for c in mock.calls if c.request.method != "GET"]


@pytest.mark.parametrize(("name", "run"), LIFECYCLE, ids=[n for n, _ in LIFECYCLE])
def test_a_lifecycle_call_sends_a_key_the_platform_accepts(name: str, run: Call) -> None:
    with respx.mock(assert_all_called=False) as mock:
        platform(mock)
        c = client()
        run(c, c.computers.get("vm-1"), None)
        sent = keyed(mock)
    assert len(sent) == 1
    assert sent[0] is not None and KEY_SYNTAX.fullmatch(sent[0])


@pytest.mark.parametrize(("name", "run"), LIFECYCLE, ids=[n for n, _ in LIFECYCLE])
def test_a_lifecycle_call_sends_a_callers_key_verbatim(name: str, run: Call) -> None:
    with respx.mock(assert_all_called=False) as mock:
        platform(mock)
        c = client()
        run(c, c.computers.get("vm-1"), "order-4711:create")
        assert keyed(mock) == ["order-4711:create"]


def test_each_call_sends_its_own_key_and_reads_send_none() -> None:
    with respx.mock(assert_all_called=False) as mock:
        platform(mock)
        vm = client().computers.get("vm-1")
        vm.start()
        vm.start()
        sent = keyed(mock)
        reads = [c.request.headers.get(HEADER) for c in mock.calls if c.request.method == "GET"]
    assert len(sent) == 2 and sent[0] != sent[1]
    assert reads and all(r is None for r in reads)


def test_a_call_makes_its_key_once_before_the_first_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[uuid.UUID] = []
    real = uuid.uuid4

    def counted() -> uuid.UUID:
        made.append(real())
        return made[-1]

    monkeypatch.setattr(_api.uuid, "uuid4", counted)
    with respx.mock(assert_all_called=False) as mock:
        platform(mock)
        c = client()
        c.computers.get("vm-1").start()
        assert len(made) == 1
        c.computers.create(template="base")
        assert len(made) == 2
        assert keyed(mock) == [u.hex for u in made]


@pytest.mark.parametrize("key", ["", "k" * 256, "a b", "a\nb", "café", 7])
def test_a_key_the_platform_would_refuse_is_refused_before_sending(key: object) -> None:
    with respx.mock(assert_all_called=False) as mock:
        platform(mock)
        c = client()
        vm = c.computers.get("vm-1")
        with pytest.raises(ValueError):
            vm.start(idempotency_key=key)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            c.computers.create(template="base", idempotency_key=key)  # type: ignore[arg-type]
        assert keyed(mock) == []


def failing_start(response: httpx.Response | Exception) -> Callable[[], mc.MandalaError]:
    def run() -> mc.MandalaError:
        with respx.mock(assert_all_called=False) as mock:
            mock.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
            route = mock.post(f"{BASE}/computers/vm-1/start")
            if isinstance(response, Exception):
                route.mock(side_effect=response)
            else:
                route.mock(return_value=response)
            vm = client().computers.get("vm-1")
            with pytest.raises(mc.MandalaError) as caught:
                vm.start(idempotency_key="k-unknown")
            return caught.value

    return run


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"error": "No hypervisor could answer that right now."}),
        httpx.Response(500, json={"error": "boom"}),
        httpx.Response(
            409,
            json={"error": "x", "code": "idempotency_in_progress", "reason": "contention"},
        ),
        httpx.Response(409, json={"error": "x", "code": "idempotency_outcome_unknown"}),
        httpx.ReadError("connection reset after the request was sent"),
        httpx.ReadTimeout("no answer in time"),
    ],
    ids=["503", "500", "in-progress", "outcome-unknown", "read-error", "timeout"],
)
def test_an_unknown_outcome_carries_the_key(response: httpx.Response | Exception) -> None:
    err = failing_start(response)()
    assert err.idempotency_key == "k-unknown"


def test_a_refusal_that_released_the_key_does_not_carry_it() -> None:
    err = failing_start(httpx.Response(409, json={"error": "busy", "reason": "contention"}))()
    assert err.idempotency_key is None


def test_the_key_the_sdk_made_is_the_one_on_the_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
        route = mock.post(f"{BASE}/computers/vm-1/stop").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        vm = client().computers.get("vm-1")
        with pytest.raises(mc.APIError) as caught:
            vm.stop()
        assert caught.value.idempotency_key == route.calls.last.request.headers[HEADER]


def test_is_transient_on_the_keyed_refusals() -> None:
    in_progress = error_for_status(
        409, "x", {"error": "x", "code": "idempotency_in_progress", "reason": "contention"}
    )
    unknown = error_for_status(409, "x", {"error": "x", "code": "idempotency_outcome_unknown"})
    reused = error_for_status(422, "x", {"error": "x", "code": "idempotency_key_reused"})
    assert mc.is_transient(in_progress) is True
    assert mc.is_transient(unknown) is False
    assert mc.is_transient(reused) is False


@respx.mock
def test_operations_list_by_key_and_the_key_on_an_operation() -> None:
    route = respx.get(f"{BASE}/operations").mock(
        return_value=httpx.Response(
            200,
            json={
                "operations": [{**OPERATION, "idempotency_key": "order-4711:create"}],
                "next_cursor": None,
            },
        )
    )
    page = client().operations.list(idempotency_key="order-4711:create")
    assert dict(route.calls.last.request.url.params) == {"idempotency_key": "order-4711:create"}
    assert page.operations[0].idempotency_key == "order-4711:create"
    assert page.operations[0].kind == "delete"


def test_an_operation_from_an_older_platform_has_no_key() -> None:
    assert mc.Operation.from_api(OPERATION).idempotency_key is None


def test_operations_list_refuses_a_filter_no_key_could_be() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            client().operations.list(idempotency_key="a b")
        assert not mock.calls


async def test_async_lifecycle_calls_send_keys_and_carry_them_on_failure() -> None:
    with respx.mock(assert_all_called=False) as mock:
        platform(mock)
        c = aclient()
        vm = await c.computers.get("vm-1")
        await vm.start()
        await vm.start(idempotency_key="k-async")
        await c.computers.create(template="base")
        await vm.delete(idempotency_key="k-async-delete")
        sent = keyed(mock)
    assert len(sent) == 4
    assert sent[0] != sent[2]
    assert sent[1] == "k-async" and sent[3] == "k-async-delete"
    assert all(s is not None and KEY_SYNTAX.fullmatch(s) for s in sent)

    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
        mock.post(f"{BASE}/snapshots/snap-1/restore").mock(
            return_value=httpx.Response(503, json={"error": "gone quiet"})
        )
        with pytest.raises(mc.APIError) as caught:
            await aclient().snapshots.restore("snap-1", idempotency_key="k-async-restore")
        assert caught.value.idempotency_key == "k-async-restore"


async def test_async_operations_list_by_key() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{BASE}/operations").mock(
            return_value=httpx.Response(200, json={"operations": [], "next_cursor": None})
        )
        await aclient().operations.list(idempotency_key="create-7f3a")
        assert dict(route.calls.last.request.url.params) == {"idempotency_key": "create-7f3a"}
