"""The async half of test_building.py.

Only the awaits differ, but the waiting is where the two could drift without
anyone noticing until a clone hung.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import respx
from tests.test_building import BASE, BUILDING, BUILT, FAILED

import mandala_computer as mc


@pytest.fixture
async def client() -> mc.AsyncClient:
    return mc.AsyncClient("gck_test", base_url=BASE)


@respx.mock
async def test_cloning_a_snapshot_returns_a_computer_that_is_still_building(
    client: mc.AsyncClient,
) -> None:
    respx.post(f"{BASE}/snapshots/snap-1/clone").mock(
        return_value=httpx.Response(201, json=BUILDING)
    )
    computer = await client.snapshots.clone("snap-1")
    assert computer.is_building
    await client.aclose()


@respx.mock
async def test_wait_until_built_polls_until_the_disk_lands(
    client: mc.AsyncClient,
) -> None:
    route = respx.get(f"{BASE}/computers/vm-new").mock(
        side_effect=[
            httpx.Response(200, json=BUILDING),
            httpx.Response(200, json=BUILT),
        ]
    )
    computer = await mc.AsyncComputer(client._t, BUILDING).wait_until_built(timeout=5, poll=0)
    assert computer.status == "stopped"
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_wait_until_built_raises_with_the_reason(client: mc.AsyncClient) -> None:
    """The failure has to be discovered by polling, not read off the handle.

    Seeded with FAILED this passed without making a single request — the cached
    check at the top of the loop raised, and the poll this file exists to cover
    never ran. So it starts building, like the sync analogue.
    """
    route = respx.get(f"{BASE}/computers/vm-new").mock(
        side_effect=[
            httpx.Response(200, json=BUILDING),
            httpx.Response(200, json=FAILED),
        ]
    )
    with pytest.raises(mc.MandalaError, match="no space left on device"):
        await mc.AsyncComputer(client._t, BUILDING).wait_until_built(timeout=5, poll=0)
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_wait_until_built_clamps_its_last_sleep(
    client: mc.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(f"{BASE}/computers/vm-new").mock(httpx.Response(200, json=BUILT))
    now = iter((0.0, 0.75))
    sleeps: list[float] = []

    async def capture_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(mc._async_computer, "time", SimpleNamespace(monotonic=lambda: next(now)))
    monkeypatch.setattr(mc._async_computer, "asyncio", SimpleNamespace(sleep=capture_sleep))
    await mc.AsyncComputer(client._t, BUILDING).wait_until_built(timeout=1, poll=5)
    assert sleeps == [0.25]
    await client.aclose()


@respx.mock
async def test_wait_until_running_points_building_computers_at_the_build_wait(
    client: mc.AsyncClient,
) -> None:
    respx.get(f"{BASE}/computers/vm-new").mock(httpx.Response(200, json=BUILDING))
    with pytest.raises(mc.MandalaError, match=r"wait_until_built\(\).+start\(\)"):
        await mc.AsyncComputer(client._t, BUILDING).wait_until_running(timeout=30, poll=0)
    await client.aclose()


@respx.mock
async def test_wait_for_guest_discovers_a_build_that_failed_while_waiting(
    client: mc.AsyncClient,
) -> None:
    respx.post(f"{BASE}/computers/vm-new/exec").mock(
        httpx.Response(409, json={"error": "still being copied"})
    )
    refresh = respx.get(f"{BASE}/computers/vm-new").mock(httpx.Response(200, json=FAILED))
    with pytest.raises(mc.MandalaError, match="no space left on device"):
        await mc.AsyncComputer(client._t, BUILDING).wait_for_guest(timeout=30, poll=0)
    assert refresh.call_count == 1
    await client.aclose()


@respx.mock
async def test_starting_a_computer_that_is_still_building_raises_conflict(
    client: mc.AsyncClient,
) -> None:
    respx.post(f"{BASE}/computers/vm-new/start").mock(
        return_value=httpx.Response(409, json={"error": "still being copied"})
    )
    with pytest.raises(mc.ConflictError):
        await mc.AsyncComputer(client._t, BUILDING).start()
    await client.aclose()
