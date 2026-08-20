"""Async client behaviour against a mocked API.

Parity of shape is covered by test_parity.py; this covers behaviour that could
plausibly differ — awaiting, cleanup on exception, and the transport's own
lifecycle.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import mandala_computer as mc

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
def client() -> mc.AsyncClient:
    return mc.AsyncClient("gck_test", base_url=BASE)


@respx.mock
async def test_list_and_fields(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[COMPUTER]))
    (c,) = await client.computers.list()
    assert (c.id, c.name, c.status, c.ram_mb) == ("vm-1", "dev", "running", 2048)


@respx.mock
async def test_sends_bearer_token(client: mc.AsyncClient) -> None:
    route = respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[]))
    await client.computers.list()
    assert route.calls.last.request.headers["Authorization"] == "Bearer gck_test"


@respx.mock
@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, mc.AuthenticationError),
        (402, mc.PlanLimitError),
        (403, mc.PermissionDeniedError),
        (404, mc.NotFoundError),
        (500, mc.APIError),
    ],
)
async def test_status_maps_to_exception(client: mc.AsyncClient, status: int, exc: type) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(exc) as e:
        await client.computers.list()
    assert e.value.status == status  # type: ignore[attr-defined]


@respx.mock
async def test_click_payload(client: mc.AsyncClient) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    await mc.AsyncComputer(client._t, COMPUTER).click(10, 20)
    assert json.loads(route.calls.last.request.content) == {
        "action": "left_click",
        "x": 10,
        "y": 20,
    }


async def test_validation_happens_before_any_request(client: mc.AsyncClient) -> None:
    """Shared validation must fire in the async path too, not just the sync one."""
    c = mc.AsyncComputer(client._t, COMPUTER)
    with pytest.raises(ValueError):
        await c.key()
    with pytest.raises(ValueError, match="up.*down"):
        await c.scroll(direction="sideways")
    with pytest.raises(ValueError, match="hour"):
        await c.set_schedule(enabled=True, hour=99)


@respx.mock
async def test_screenshot_returns_bytes(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1/screenshot").mock(httpx.Response(200, content=b"\x89PNG\r\n"))
    assert (await mc.AsyncComputer(client._t, COMPUTER).screenshot()).startswith(b"\x89PNG")


@respx.mock
async def test_exec_nonzero_exit_is_returned_not_raised(client: mc.AsyncClient) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(
            200, json={"exit_code": 1, "stdout": "", "stderr": "boom", "timed_out": False}
        )
    )
    res = await mc.AsyncComputer(client._t, COMPUTER).exec("false")
    assert res.exit_code == 1 and not res.ok


@respx.mock
@pytest.mark.asyncio
async def test_exec_omits_session_unless_desktop_requested(client: mc.AsyncClient) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
    )
    await mc.AsyncComputer(client._t, COMPUTER).exec("whoami")
    assert "session" not in json.loads(route.calls.last.request.content)


@respx.mock
@pytest.mark.asyncio
async def test_exec_desktop_sends_session_desktop(client: mc.AsyncClient) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
    )
    await mc.AsyncComputer(client._t, COMPUTER).exec("whoami", desktop=True)
    assert json.loads(route.calls.last.request.content)["session"] == "desktop"


@respx.mock
async def test_wait_until_running_polls(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        side_effect=[
            httpx.Response(200, json={**COMPUTER, "status": "stopped"}),
            httpx.Response(200, json={**COMPUTER, "status": "running"}),
        ]
    )
    c = mc.AsyncComputer(client._t, {**COMPUTER, "status": "stopped"})
    assert (await c.wait_until_running(timeout=5, poll=0)).status == "running"


@respx.mock
async def test_wait_until_running_times_out(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "stopped"})
    )
    with pytest.raises(mc.TimeoutError):
        await mc.AsyncComputer(client._t, COMPUTER).wait_until_running(timeout=0, poll=0)


@respx.mock
async def test_ephemeral_deletes_on_exit(client: mc.AsyncClient) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    delete = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    async with client.computers.ephemeral(template="base") as c:
        assert c.id == "vm-1"
    assert delete.called


@respx.mock
async def test_ephemeral_deletes_even_when_block_raises(client: mc.AsyncClient) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    delete = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    with pytest.raises(RuntimeError):
        async with client.computers.ephemeral(template="base"):
            raise RuntimeError("boom")
    assert delete.called, "a leaked computer bills until someone notices"


@respx.mock
async def test_context_manager_closes_transport() -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[]))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        await client.computers.list()
    assert client._t._http.is_closed


@respx.mock
async def test_supplied_http_client_is_not_closed() -> None:
    """A caller-owned client outlives us — closing it would break their app."""
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[]))
    http = httpx.AsyncClient()
    async with mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client:
        await client.computers.list()
    assert not http.is_closed
    await http.aclose()


@respx.mock
async def test_rename_updates_the_handle_in_place() -> None:
    """The async mirror of the sync rename test."""
    client = mc.AsyncClient("gck_test", base_url=BASE)
    route = respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "name": "build box"})
    )
    c = mc.AsyncComputer(client._t, COMPUTER)
    assert await c.rename("build box") is c
    assert c.name == "build box"
    assert json.loads(route.calls[0].request.content) == {"name": "build box"}


async def test_rename_refuses_an_empty_name_without_asking() -> None:
    client = mc.AsyncClient("gck_test", base_url=BASE)
    c = mc.AsyncComputer(client._t, COMPUTER)
    with pytest.raises(ValueError, match="must not be empty"):
        await c.rename("   ")


@respx.mock
@pytest.mark.asyncio
async def test_open_sends_the_same_command_as_the_sync_client(
    client: mc.AsyncClient,
) -> None:
    """Both build it in _api, so the only way they diverge is one of them
    stopping calling it — which a shape-only parity test would not catch."""
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
    )
    await mc.AsyncComputer(client._t, COMPUTER).open("https://example.com")
    body = json.loads(route.calls.last.request.content)
    assert body["command"] == mc._api.open_url_command("https://example.com")
    assert body["session"] == "desktop"


# --- the routes added in this pass, on the async side -----------------------
#
# Shape is held to the sync client by test_parity and the two clients are held
# to the same routes by test_surface, so what is worth covering here is the
# behaviour that reads a response rather than merely sending one — an await in
# the wrong place turns each of these into a coroutine nobody ran.


@respx.mock
async def test_a_short_listing_says_so(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers").mock(
        httpx.Response(200, json=[COMPUTER], headers={"X-GC-Incomplete": "2"})
    )
    computers = await client.computers.list(allow_partial=True)
    assert not computers.is_complete
    assert computers.incomplete == 2


@respx.mock
async def test_a_listing_that_would_be_short_is_refused_by_default(
    client: mc.AsyncClient,
) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(503, json={"error": "host unreachable"}))
    with pytest.raises(mc.UnavailableError):
        await client.computers.list()


@respx.mock
async def test_a_purge_is_bound_to_its_fingerprint(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.get(f"{BASE}/computers/vm-1/snapshots").mock(
        httpx.Response(200, json={"count": 2, "size_bytes": 42, "fingerprint": "fp-abc"})
    )
    route = respx.delete(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={"ok": True, "snapshots_deleted": 2})
    )
    c = await client.computers.get("vm-1")
    held = await c.snapshot_holdings()
    assert held.fingerprint == "fp-abc"
    assert await c.delete(purge_snapshots=True, expect=held.fingerprint) == 2
    assert route.calls.last.request.url.params["expect"] == "fp-abc"


@respx.mock
async def test_a_purge_without_a_fingerprint_deletes_nothing(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    route = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    c = await client.computers.get("vm-1")
    with pytest.raises(ValueError, match="snapshot_holdings"):
        await c.delete(purge_snapshots=True)
    assert not route.called


@respx.mock
async def test_a_background_command_polls_and_is_killed(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"pid": 4242, "command": "make", "running": True})
    )
    respx.get(f"{BASE}/computers/vm-1/exec/4242").mock(
        httpx.Response(200, json={"pid": 4242, "running": True, "stdout": "cc\n", "more": True})
    )
    respx.delete(f"{BASE}/computers/vm-1/exec/4242").mock(
        httpx.Response(200, json={"pid": 4242, "killed": True, "stdout": "tail\n"})
    )
    c = await client.computers.get("vm-1")
    job = await c.start_exec("make")
    assert job.pid == 4242

    status = await job.poll()
    assert status.stdout == "cc\n" and status.more and not status.done

    final = await job.kill()
    assert final.killed and final.stdout == "tail\n"


@respx.mock
async def test_windows_and_one_window_acted_on(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.get(f"{BASE}/computers/vm-1/windows").mock(
        httpx.Response(200, json={"windows": [{"id": "0x26", "class": "Navigator"}]})
    )
    respx.post(f"{BASE}/computers/vm-1/windows/0x26").mock(
        httpx.Response(200, json={"ok": True, "window": {"id": "0x26", "focused": True}})
    )
    c = await client.computers.get("vm-1")
    (w,) = await c.windows()
    assert w.wm_class == "Navigator"

    res = await c.window_action(w.id, "focus")
    assert res.window is not None and res.window.focused
    assert not res.gone


@respx.mock
async def test_resize_and_the_idle_window(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    route = respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "cpu": 8})
    )
    c = await client.computers.get("vm-1")
    await c.resize(cpu=8)
    assert json.loads(route.calls.last.request.content) == {"cpu": 8}
    assert c.cpu == 8

    route.mock(httpx.Response(200, json={**COMPUTER, "idle_suspend_min": 30}))
    await c.set_idle_suspend(30)
    assert json.loads(route.calls.last.request.content) == {"idle_suspend_min": 30}
    assert c.idle_suspend_min == 30


@respx.mock
async def test_wait_for_guest_does_not_wait_out_a_revoked_key(client: mc.AsyncClient) -> None:
    """The async half of the same rule: 401 will not clear by waiting."""
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(401, json={"error": "revoked"}))
    with pytest.raises(mc.AuthenticationError):
        await mc.AsyncComputer(client._t, COMPUTER).wait_for_guest(timeout=30, poll=0.01)
    await client.aclose()


@respx.mock
async def test_a_failing_ephemeral_cleanup_keeps_the_original_error(
    client: mc.AsyncClient,
) -> None:
    """The async half of the same rule: the block's exception is the news."""
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    respx.delete(f"{BASE}/computers/vm-1").mock(
        httpx.Response(409, json={"error": "a snapshot is being taken"})
    )

    async def caller_raises() -> None:
        async with client.computers.ephemeral():
            raise ZeroDivisionError("the caller's own bug")

    with pytest.warns(UserWarning, match="still billable"), pytest.raises(ZeroDivisionError):
        await caller_raises()
    await client.aclose()


@respx.mock
async def test_ephemeral_still_deletes_on_the_ordinary_path(client: mc.AsyncClient) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    route = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={}))
    async with client.computers.ephemeral() as c:
        assert c.id == "vm-1"
    assert route.called
    await client.aclose()


@respx.mock
async def test_a_cleanup_that_fails_at_the_transport_keeps_the_original_error(
    client: mc.AsyncClient,
) -> None:
    """The async half of the same rule: a transport error is a failed cleanup too."""
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    respx.delete(f"{BASE}/computers/vm-1").mock(side_effect=httpx.ConnectError("reset"))

    async def caller_raises() -> None:
        async with client.computers.ephemeral():
            raise ZeroDivisionError("the caller's own bug")

    with pytest.warns(UserWarning, match="still billable"), pytest.raises(ZeroDivisionError):
        await caller_raises()
    await client.aclose()


# --- request deadlines ------------------------------------------------------


@respx.mock
async def test_a_long_exec_waits_as_long_as_it_asked_to(client: mc.AsyncClient) -> None:
    """The async half derives its deadline from timeout_s too.

    The budget is threaded through the transport, so this is exactly where the
    two halves could drift: an await that kept the fixed 60-second default would
    abandon a long command while the sync half waited it out.
    """
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    c = await client.computers.get("vm-1")

    await c.exec("make", timeout_s=300)
    assert route.calls.last.request.extensions["timeout"]["read"] == (
        300 + mc._client.DEADLINE_SLACK
    )

    files = respx.get(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200, content=b"hi"))
    await c.read_file("/tmp/a")
    assert files.calls.last.request.extensions["timeout"]["read"] == mc._client.FILE_TIMEOUT
    await client.aclose()


@respx.mock
async def test_a_transport_timeout_arrives_as_a_mandala_error(client: mc.AsyncClient) -> None:
    """A timeout is the SDK's own error on both halves."""
    respx.post(f"{BASE}/computers/vm-1/exec").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(mc.TimeoutError, match="did not answer") as caught:
        await mc.AsyncComputer(client._t, COMPUTER).exec("sleep 999", timeout_s=100)
    assert isinstance(caught.value, mc.MandalaError)
    await client.aclose()


@respx.mock
async def test_set_schedule_reads_its_own_answer(client: mc.AsyncClient) -> None:
    """No follow-up GET on this half either."""
    stored = {"enabled": True, "hour": 4, "minute": 0, "tz": "UTC"}
    put = respx.put(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json=stored))
    get = respx.get(f"{BASE}/computers/vm-1/schedule").mock(
        httpx.Response(200, json={"enabled": False})
    )

    assert await mc.AsyncComputer(client._t, COMPUTER).set_schedule(enabled=True) == stored
    assert (put.call_count, get.call_count) == (1, 0)
    await client.aclose()
