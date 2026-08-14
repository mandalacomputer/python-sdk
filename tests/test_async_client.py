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
async def test_status_maps_to_exception(
    client: mc.AsyncClient, status: int, exc: type
) -> None:
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
    respx.get(f"{BASE}/computers/vm-1/screenshot").mock(
        httpx.Response(200, content=b"\x89PNG\r\n")
    )
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
