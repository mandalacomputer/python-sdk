"""A computer that exists before its disk does.

Cloning — from a snapshot, or from another computer — returns as soon as the
computer record exists, which is minutes before the disk copy finishes. That is
deliberate on the server: the copy used to run under the daemon's map lock, so
one tenant's clone froze every other tenant's reads, and the caller got nothing
back until it was over.

What it means here is that a clone hands back a computer nobody can start yet,
and an SDK that did not know about the state would look like it had returned a
broken machine.
"""

from __future__ import annotations

import httpx
import pytest
import respx

import gorillacloud as gc

BASE = "https://api.test/api/v1"

BUILDING = {
    "id": "vm-new",
    "name": "from-snap",
    "status": "building",
    "os": "windows",
    "template": "windows",
    "cpu": 4,
    "ram_mb": 8192,
    "disk_gb": 40,
    "created_at": "2026-08-01T00:00:00Z",
    "build": {"started": "2026-08-01T00:00:00Z", "source": "snap-1"},
}
BUILT = {**BUILDING, "status": "stopped", "build": None}
FAILED = {
    **BUILDING,
    "status": "build-failed",
    "build": {**BUILDING["build"], "failed": "no space left on device"},
}


@pytest.fixture
def client() -> gc.Client:
    return gc.Client("gck_test", base_url=BASE)


# --- reading the state ------------------------------------------------------


def test_build_state_reads_off_the_payload() -> None:
    building = gc.Computer(None, BUILDING)  # type: ignore[arg-type]
    assert building.is_building
    assert not building.build_failed
    assert building.build_error == ""

    failed = gc.Computer(None, FAILED)  # type: ignore[arg-type]
    assert failed.build_failed
    assert not failed.is_building
    assert failed.build_error == "no space left on device"

    done = gc.Computer(None, BUILT)  # type: ignore[arg-type]
    assert not done.is_building
    assert not done.build_failed


def test_an_older_server_that_says_nothing_about_why() -> None:
    """build-failed with no detail must still read as failed, not as fine."""
    c = gc.Computer(None, {**BUILDING, "status": "build-failed", "build": None})  # type: ignore[arg-type]
    assert c.build_failed
    assert c.build_error == ""


# --- what a clone gives you -------------------------------------------------


@respx.mock
def test_cloning_a_snapshot_returns_a_computer_that_is_still_building(
    client: gc.Client,
) -> None:
    respx.post(f"{BASE}/snapshots/snap-1/clone").mock(
        return_value=httpx.Response(201, json=BUILDING)
    )
    computer = client.snapshots.clone("snap-1")
    assert computer.id == "vm-new"
    assert computer.is_building


@respx.mock
def test_wait_until_built_polls_until_the_disk_lands(client: gc.Client) -> None:
    respx.post(f"{BASE}/snapshots/snap-1/clone").mock(
        return_value=httpx.Response(201, json=BUILDING)
    )
    route = respx.get(f"{BASE}/computers/vm-new").mock(
        side_effect=[
            httpx.Response(200, json=BUILDING),
            httpx.Response(200, json=BUILT),
        ]
    )
    computer = client.snapshots.clone("snap-1").wait_until_built(timeout=5, poll=0)
    assert computer.status == "stopped"
    assert not computer.is_building
    assert route.call_count == 2


@respx.mock
def test_wait_until_built_returns_at_once_for_anything_not_building(
    client: gc.Client,
) -> None:
    """Safe to call on any computer, so a caller need not ask first."""
    route = respx.get(f"{BASE}/computers/vm-new").mock(return_value=httpx.Response(200, json=BUILT))
    gc.Computer(client._t, BUILT).wait_until_built(timeout=5, poll=0)
    assert route.call_count == 0


@respx.mock
def test_wait_until_built_raises_with_the_reason(client: gc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-new").mock(
        side_effect=[
            httpx.Response(200, json=BUILDING),
            httpx.Response(200, json=FAILED),
        ]
    )
    with pytest.raises(gc.GorillaCloudError, match="no space left on device"):
        gc.Computer(client._t, BUILDING).wait_until_built(timeout=5, poll=0)


@respx.mock
def test_wait_until_built_times_out_without_claiming_the_build_stopped(
    client: gc.Client,
) -> None:
    respx.get(f"{BASE}/computers/vm-new").mock(return_value=httpx.Response(200, json=BUILDING))
    with pytest.raises(gc.TimeoutError, match="only this wait has"):
        gc.Computer(client._t, BUILDING).wait_until_built(timeout=0, poll=0)


@respx.mock
def test_wait_until_running_gives_up_on_a_computer_with_no_disk(
    client: gc.Client,
) -> None:
    """It will never start, and waiting out the full timeout to say so is no help."""
    respx.get(f"{BASE}/computers/vm-new").mock(return_value=httpx.Response(200, json=FAILED))
    with pytest.raises(gc.GorillaCloudError, match="no space left on device"):
        gc.Computer(client._t, FAILED).wait_until_running(timeout=30, poll=0)


# --- the refusal ------------------------------------------------------------


@respx.mock
def test_starting_a_computer_that_is_still_building_raises_conflict(
    client: gc.Client,
) -> None:
    respx.post(f"{BASE}/computers/vm-new/start").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": "this computer's disk is still being copied; "
                "try again once it has finished building"
            },
        )
    )
    with pytest.raises(gc.ConflictError, match="still being copied") as e:
        gc.Computer(client._t, BUILDING).start()
    assert e.value.status == 409


@respx.mock
def test_conflict_is_distinct_from_a_plan_limit_and_a_plain_error(
    client: gc.Client,
) -> None:
    """The two mean opposite things: come back later, versus do not."""
    respx.post(f"{BASE}/computers/vm-1/start").mock(
        return_value=httpx.Response(409, json={"error": "a snapshot is being taken"})
    )
    respx.post(f"{BASE}/computers/vm-2/start").mock(
        return_value=httpx.Response(402, json={"error": "your plan allows 2 computers"})
    )
    with pytest.raises(gc.ConflictError):
        gc.Computer(client._t, {"id": "vm-1"}).start()
    with pytest.raises(gc.PlanLimitError):
        gc.Computer(client._t, {"id": "vm-2"}).start()
    # And a ConflictError is not mistaken for one by an except clause upstream.
    assert not issubclass(gc.ConflictError, gc.PlanLimitError)
    assert issubclass(gc.ConflictError, gc.APIError)
