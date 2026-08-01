"""Client behaviour against a mocked API."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import gorillacloud as gc

BASE = "https://api.test/api/v1"

COMPUTER = {
    "id": "vm-1",
    "name": "dev",
    "status": "running",
    "os": "linux",
    "template": "base",
    "cpu": 2,
    "ram_mb": 2048,
    "disk_gb": 20,
    "created_at": "2026-07-31T00:00:00Z",
}


@pytest.fixture
def client() -> gc.Client:
    return gc.Client("gck_test", base_url=BASE)


def test_api_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GORILLACLOUD_API_KEY", raising=False)
    with pytest.raises(gc.GorillaCloudError, match="No API key"):
        gc.Client()


def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GORILLACLOUD_API_KEY", "gck_env")
    assert gc.Client(base_url=BASE).base_url == BASE


@respx.mock
def test_sends_bearer_token(client: gc.Client) -> None:
    route = respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[COMPUTER]))
    client.computers.list()
    assert route.calls.last.request.headers["Authorization"] == "Bearer gck_test"


@respx.mock
def test_list_and_fields(client: gc.Client) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[COMPUTER]))
    (c,) = client.computers.list()
    assert (c.id, c.name, c.status, c.cpu, c.ram_mb) == ("vm-1", "dev", "running", 2, 2048)


@respx.mock
def test_create_omits_unset_fields(client: gc.Client) -> None:
    route = respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    client.computers.create(name="dev", template="base")
    body = route.calls.last.request.content.decode()
    assert '"name"' in body and '"template"' in body
    # Unspecified sizing must not be sent as null — the server applies the
    # template's defaults only when the key is absent.
    assert "cpu" not in body and "ram_mb" not in body and "disk_gb" not in body


@respx.mock
def test_unknown_response_fields_survive(client: gc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "future_field": 42})
    )
    assert client.computers.get("vm-1").raw["future_field"] == 42


# --- errors ---------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, gc.AuthenticationError),
        (402, gc.PlanLimitError),
        (403, gc.PermissionDeniedError),
        (404, gc.NotFoundError),
        (500, gc.APIError),
    ],
)
def test_status_maps_to_exception(client: gc.Client, status: int, exc: type) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(exc) as e:
        client.computers.list()
    assert e.value.status == status  # type: ignore[attr-defined]
    assert "nope" in str(e.value)


@respx.mock
def test_non_json_error_still_raises(client: gc.Client) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(502, text="bad gateway"))
    with pytest.raises(gc.APIError, match="bad gateway"):
        client.computers.list()


# --- control --------------------------------------------------------------


@respx.mock
def test_click_payload(client: gc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    gc.Computer(client._t, COMPUTER).click(10, 20)
    assert json.loads(route.calls.last.request.content) == {
        "action": "left_click",
        "x": 10,
        "y": 20,
    }


@respx.mock
def test_key_chord(client: gc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    gc.Computer(client._t, COMPUTER).key("ctrl", "c")
    assert json.loads(route.calls.last.request.content)["keys"] == ["ctrl", "c"]


def test_key_requires_at_least_one(client: gc.Client) -> None:
    with pytest.raises(ValueError):
        gc.Computer(client._t, COMPUTER).key()


def test_scroll_rejects_bad_direction(client: gc.Client) -> None:
    with pytest.raises(ValueError, match="up.*down"):
        gc.Computer(client._t, COMPUTER).scroll(direction="sideways")


@respx.mock
def test_screenshot_returns_bytes(client: gc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/screenshot").mock(
        httpx.Response(200, content=b"\x89PNG\r\n", headers={"Content-Type": "image/png"})
    )
    assert gc.Computer(client._t, COMPUTER).screenshot().startswith(b"\x89PNG")


@respx.mock
def test_screenshot_width_becomes_query_param(client: gc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/screenshot").mock(httpx.Response(200, content=b"jpg"))
    gc.Computer(client._t, COMPUTER).screenshot(width=320)
    assert route.calls.last.request.url.params["w"] == "320"


@respx.mock
def test_exec_nonzero_exit_is_returned_not_raised(client: gc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 1, "stdout": "", "stderr": "boom", "timed_out": False})
    )
    res = gc.Computer(client._t, COMPUTER).exec("false")
    assert res.exit_code == 1 and res.stderr == "boom" and not res.ok


# --- waiting --------------------------------------------------------------


@respx.mock
def test_wait_until_running_polls_until_ready(client: gc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        side_effect=[
            httpx.Response(200, json={**COMPUTER, "status": "stopped"}),
            httpx.Response(200, json={**COMPUTER, "status": "running"}),
        ]
    )
    c = gc.Computer(client._t, {**COMPUTER, "status": "stopped"})
    assert c.wait_until_running(timeout=5, poll=0).status == "running"


@respx.mock
def test_wait_until_running_times_out(client: gc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "stopped"})
    )
    c = gc.Computer(client._t, COMPUTER)
    with pytest.raises(gc.TimeoutError):
        c.wait_until_running(timeout=0, poll=0)


@respx.mock
def test_wait_for_guest_ignores_errors_while_booting(client: gc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        side_effect=[
            httpx.Response(400, json={"error": "not running"}),
            httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}),
        ]
    )
    gc.Computer(client._t, COMPUTER).wait_for_guest(timeout=5, poll=0)


# --- schedule -------------------------------------------------------------


@respx.mock
def test_schedule_is_the_window_only(client: gc.Client) -> None:
    """The schedule carries no last_run, by design.

    It was the scheduler's own bookkeeping and lied in both directions — as a
    zero time it reported the last backup two millennia ago, and as a creation
    stamp it reported one that never happened. Snapshot capture times are the
    honest source; pinned here so it cannot quietly come back.
    """
    respx.get(f"{BASE}/computers/vm-1/schedule").mock(
        httpx.Response(200, json={"enabled": False, "hour": 0, "minute": 0, "tz": "UTC"})
    )
    sched = gc.Computer(client._t, COMPUTER).schedule()
    assert "last_run" not in sched
    # Empty string would mean "UTC" to the daemon but is rejected by every
    # timezone library, so the surface must name the zone.
    assert sched["tz"] == "UTC"


@respx.mock
def test_scheduled_snapshots_are_distinguishable(client: gc.Client) -> None:
    """`auto` is what makes snapshot times usable as backup history.

    It also marks the only snapshots retention will ever age out.
    """
    respx.get(f"{BASE}/snapshots").mock(
        httpx.Response(
            200,
            json=[
                {"id": "s1", "computer_id": "vm-1", "created_at": "2026-07-31T04:00:00Z", "auto": True},
                {"id": "s2", "computer_id": "vm-1", "created_at": "2026-07-30T12:00:00Z", "auto": False},
            ],
        )
    )
    snaps = client.snapshots.list()
    assert [s.auto for s in snaps] == [True, False]
    assert [s.is_scheduled for s in snaps] == [True, False]
    last_backup = max((s.created_at for s in snaps if s.auto), default=None)
    assert last_backup == "2026-07-31T04:00:00Z"


@respx.mock
def test_clear_schedule_is_a_delete_not_a_disable(client: gc.Client) -> None:
    """Disabling keeps the time and the bookkeeping; clearing removes both."""
    cleared = {"enabled": False, "hour": 0, "minute": 0, "tz": "UTC"}
    route = respx.delete(f"{BASE}/computers/vm-1/schedule").mock(
        httpx.Response(200, json=cleared)
    )
    put = respx.put(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json={}))

    assert gc.Computer(client._t, COMPUTER).clear_schedule() == cleared
    assert route.called
    assert not put.called, "clearing must not go through the set path"


@respx.mock
def test_set_schedule_validates_before_sending(client: gc.Client) -> None:
    route = respx.put(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json={}))
    c = gc.Computer(client._t, COMPUTER)
    with pytest.raises(ValueError, match="hour"):
        c.set_schedule(enabled=True, hour=24)
    with pytest.raises(ValueError, match="minute"):
        c.set_schedule(enabled=True, minute=60)
    assert not route.called, "invalid input must not reach the API"


# --- ephemeral ------------------------------------------------------------


@respx.mock
def test_ephemeral_deletes_on_exit(client: gc.Client) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    delete = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    with client.computers.ephemeral(template="base") as c:
        assert c.id == "vm-1"
    assert delete.called


@respx.mock
def test_ephemeral_deletes_even_when_block_raises(client: gc.Client) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    delete = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    with pytest.raises(RuntimeError), client.computers.ephemeral(template="base"):
        raise RuntimeError("boom")
    assert delete.called, "a leaked computer bills until someone notices"


@respx.mock
def test_create_does_not_delete(client: gc.Client) -> None:
    """create() is not scoped to a block — only ephemeral() may destroy a disk."""
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    delete = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200))
    client.computers.create(template="base")
    assert not delete.called
