"""The async half of test_building.py.

Only the awaits differ, but the waiting is where the two could drift without
anyone noticing until a clone hung.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from tests.test_building import BASE, BUILDING, BUILT, FAILED

import mandala_computer as mc

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
    respx.get(f"{BASE}/computers/vm-new").mock(return_value=httpx.Response(200, json=FAILED))
    with pytest.raises(mc.MandalaError, match="no space left on device"):
        await mc.AsyncComputer(client._t, FAILED).wait_until_built(timeout=5, poll=0)
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
