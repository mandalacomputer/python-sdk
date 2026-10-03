"""An answer that is not a computer, where one was promised — sync and async.

Create, get, clone, a snapshot clone, a refresh and every listing row refuse an
answer with no ``id`` and name the route, rather than handing back a handle
whose id is ``""`` and whose next call fails somewhere else blaming the caller.
Every ``PATCH`` of a handle refreshes after an answer with no id, and replaces
:attr:`operation_id` with what that answer carried — so a setting change after
a ``start`` does not leave the start's id for ``operations.wait`` to follow.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
COMPUTER: dict[str, Any] = {"id": "vm-1", "name": "demo", "status": "stopped", "os": "linux"}
ID_LESS: list[dict[str, Any]] = [{"ok": True}, {}, {"id": ""}, {"id": 7, "name": "demo"}]


def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


def aclient() -> mc.AsyncClient:
    return mc.AsyncClient("com_test", base_url=BASE)


# --- create, get, clone, snapshot clone, refresh -------------------------------


@pytest.mark.parametrize("body", ID_LESS)
@respx.mock
def test_create_get_and_both_clones_refuse_an_answer_with_no_id(body: dict[str, Any]) -> None:
    respx.post(f"{BASE}/computers").mock(return_value=httpx.Response(201, json=body))
    respx.get(f"{BASE}/computers/vm-2").mock(return_value=httpx.Response(200, json=body))
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    respx.post(f"{BASE}/computers/vm-1/clone").mock(return_value=httpx.Response(201, json=body))
    respx.post(f"{BASE}/snapshots/snap-1/clone").mock(return_value=httpx.Response(201, json=body))
    c = client()
    with pytest.raises(mc.MandalaError, match=r"^expected a computer from POST computers$"):
        c.computers.create(name="demo")
    with pytest.raises(mc.MandalaError, match=r"^expected a computer from GET computers/vm-2$"):
        c.computers.get("vm-2")
    vm = c.computers.get("vm-1")
    with pytest.raises(
        mc.MandalaError, match=r"^expected a computer from POST computers/vm-1/clone$"
    ):
        vm.clone("copy")
    with pytest.raises(
        mc.MandalaError, match=r"^expected a computer from POST snapshots/snap-1/clone$"
    ):
        c.snapshots.clone("snap-1")


@pytest.mark.parametrize("body", ID_LESS)
async def test_async_create_get_and_both_clones_refuse_an_answer_with_no_id(
    body: dict[str, Any],
) -> None:
    with respx.mock:
        respx.post(f"{BASE}/computers").mock(return_value=httpx.Response(201, json=body))
        respx.get(f"{BASE}/computers/vm-2").mock(return_value=httpx.Response(200, json=body))
        respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
        respx.post(f"{BASE}/computers/vm-1/clone").mock(return_value=httpx.Response(201, json=body))
        respx.post(f"{BASE}/snapshots/snap-1/clone").mock(
            return_value=httpx.Response(201, json=body)
        )
        c = aclient()
        with pytest.raises(mc.MandalaError, match=r"^expected a computer from POST computers$"):
            await c.computers.create(name="demo")
        with pytest.raises(mc.MandalaError, match=r"^expected a computer from GET computers/vm-2$"):
            await c.computers.get("vm-2")
        vm = await c.computers.get("vm-1")
        with pytest.raises(
            mc.MandalaError, match=r"^expected a computer from POST computers/vm-1/clone$"
        ):
            await vm.clone("copy")
        with pytest.raises(
            mc.MandalaError, match=r"^expected a computer from POST snapshots/snap-1/clone$"
        ):
            await c.snapshots.clone("snap-1")


@respx.mock
def test_a_refresh_with_no_id_is_refused_and_the_handle_keeps_its_record() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        side_effect=[httpx.Response(200, json=COMPUTER), httpx.Response(200, json={"ok": True})]
    )
    vm = client().computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match=r"^expected a computer from GET computers/vm-1$"):
        vm.refresh()
    assert (vm.id, vm.name) == ("vm-1", "demo")


async def test_async_a_refresh_with_no_id_is_refused_and_the_handle_keeps_its_record() -> None:
    with respx.mock:
        respx.get(f"{BASE}/computers/vm-1").mock(
            side_effect=[
                httpx.Response(200, json=COMPUTER),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        vm = await aclient().computers.get("vm-1")
        with pytest.raises(mc.MandalaError, match=r"^expected a computer from GET computers/vm-1$"):
            await vm.refresh()
        assert (vm.id, vm.name) == ("vm-1", "demo")


# --- listing ---------------------------------------------------------------------


LISTING = [COMPUTER, {"name": "nameless", "status": "running"}]
ROW = r"^expected a computer from GET computers \(row 1 of 2 has no id\)$"


@respx.mock
def test_a_listing_row_with_no_id_refuses_the_listing_whole() -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=LISTING))
    with pytest.raises(mc.MandalaError, match=ROW):
        client().computers.list()


async def test_async_a_listing_row_with_no_id_refuses_the_listing_whole() -> None:
    with respx.mock:
        respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=LISTING))
        with pytest.raises(mc.MandalaError, match=ROW):
            await aclient().computers.list()


# --- PATCH: refresh after an id-less answer, and operation_id every time -------


SyncPatch = Callable[[mc.Computer], object]
AsyncPatch = Callable[[mc.AsyncComputer], Awaitable[object]]

PATCHES: dict[str, tuple[SyncPatch, AsyncPatch]] = {
    "rename": (lambda vm: vm.rename("renamed"), lambda vm: vm.rename("renamed")),
    "resize": (lambda vm: vm.resize(ram_mb=8192), lambda vm: vm.resize(ram_mb=8192)),
    "set_idle_suspend": (lambda vm: vm.set_idle_suspend(30), lambda vm: vm.set_idle_suspend(30)),
    "set_browser_proxy": (
        lambda vm: vm.set_browser_proxy(None),
        lambda vm: vm.set_browser_proxy(None),
    ),
    "set_egress_proxy": (
        lambda vm: vm.set_egress_proxy(None),
        lambda vm: vm.set_egress_proxy(None),
    ),
}
REFRESHED: dict[str, Any] = {**COMPUTER, "name": "refreshed"}


@pytest.mark.parametrize("method", list(PATCHES))
@respx.mock
def test_a_patch_answered_without_a_computer_refreshes_and_keeps_the_id(method: str) -> None:
    get = respx.get(f"{BASE}/computers/vm-1").mock(
        side_effect=[httpx.Response(200, json=COMPUTER), httpx.Response(200, json=REFRESHED)]
    )
    respx.patch(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json={"ok": True}))
    vm = client().computers.get("vm-1")
    assert PATCHES[method][0](vm) is vm
    assert get.call_count == 2
    assert (vm.id, vm.name) == ("vm-1", "refreshed")


@pytest.mark.parametrize("method", list(PATCHES))
async def test_async_a_patch_answered_without_a_computer_refreshes_and_keeps_the_id(
    method: str,
) -> None:
    with respx.mock:
        get = respx.get(f"{BASE}/computers/vm-1").mock(
            side_effect=[httpx.Response(200, json=COMPUTER), httpx.Response(200, json=REFRESHED)]
        )
        respx.patch(f"{BASE}/computers/vm-1").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        vm = await aclient().computers.get("vm-1")
        assert await PATCHES[method][1](vm) is vm
        assert get.call_count == 2
        assert (vm.id, vm.name) == ("vm-1", "refreshed")


@respx.mock
def test_a_patch_answered_with_a_computer_does_not_refresh() -> None:
    get = respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    respx.patch(f"{BASE}/computers/vm-1").mock(
        return_value=httpx.Response(200, json={**COMPUTER, "name": "renamed"})
    )
    vm = client().computers.get("vm-1").rename("renamed")
    assert get.call_count == 1
    assert vm.name == "renamed"


@respx.mock
def test_a_failed_refresh_after_a_patch_says_the_patch_succeeded() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        side_effect=[httpx.Response(200, json=COMPUTER), httpx.Response(200, json={})]
    )
    respx.patch(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json={"ok": True}))
    vm = client().computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match=r"^set_idle_suspend succeeded, but refreshing vm-1"):
        vm.set_idle_suspend(30)
    assert (vm.id, vm.name) == ("vm-1", "demo")


START_OP = "op_000000000000000000000001"


@pytest.mark.parametrize("method", list(PATCHES))
@pytest.mark.parametrize("answer", [COMPUTER, {"ok": True}])
@respx.mock
def test_a_patch_replaces_the_start_s_operation_id(method: str, answer: dict[str, Any]) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    respx.post(f"{BASE}/computers/vm-1/start").mock(
        return_value=httpx.Response(200, json={"ok": True, "operation_id": START_OP})
    )
    respx.patch(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=answer))
    vm = client().computers.get("vm-1")
    vm.start()
    assert vm.operation_id == START_OP
    PATCHES[method][0](vm)
    assert vm.operation_id is None


@pytest.mark.parametrize("method", list(PATCHES))
@pytest.mark.parametrize("answer", [COMPUTER, {"ok": True}])
async def test_async_a_patch_replaces_the_start_s_operation_id(
    method: str, answer: dict[str, Any]
) -> None:
    with respx.mock:
        respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
        respx.post(f"{BASE}/computers/vm-1/start").mock(
            return_value=httpx.Response(200, json={"ok": True, "operation_id": START_OP})
        )
        respx.patch(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=answer))
        vm = await aclient().computers.get("vm-1")
        await vm.start()
        assert vm.operation_id == START_OP
        await PATCHES[method][1](vm)
        assert vm.operation_id is None


@pytest.mark.parametrize("method", list(PATCHES))
@respx.mock
def test_a_patch_takes_the_operation_id_its_own_answer_carried(method: str) -> None:
    # Off the PATCH answer, not the refresh after it: reads never carry one.
    respx.get(f"{BASE}/computers/vm-1").mock(return_value=httpx.Response(200, json=COMPUTER))
    respx.patch(f"{BASE}/computers/vm-1").mock(
        return_value=httpx.Response(200, json={"ok": True, "operation_id": "op_patch"})
    )
    vm = client().computers.get("vm-1")
    PATCHES[method][0](vm)
    assert vm.operation_id == "op_patch"
