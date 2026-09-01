"""Async client behaviour against a mocked API.

Parity of shape is covered by test_parity.py; this covers behaviour that could
plausibly differ — awaiting, cleanup on exception, and the transport's own
lifecycle.
"""

from __future__ import annotations

import io
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
async def test_a_network_failure_is_still_a_mandala_error(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers").mock(side_effect=httpx.ConnectError("reset"))
    with pytest.raises(mc.MandalaError, match="ConnectError"):
        await client.computers.list()
    await client.aclose()


@respx.mock
async def test_exec_does_not_turn_an_empty_200_into_success(client: mc.AsyncClient) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200))
    with pytest.raises(mc.MandalaError, match="not a JSON object"):
        await mc.AsyncComputer(client._t, COMPUTER).exec("true")
    await client.aclose()


@respx.mock
@pytest.mark.parametrize("body", [{}, {"text": 42}, {"text": None}])
async def test_the_async_clipboard_read_guards_its_own_decode(
    client: mc.AsyncClient, body: object
) -> None:
    """The async half decodes for itself, so it has to be tested for itself.

    ``test_parity`` compares signatures, not bodies — by design, since a body
    comparison would be a diff nobody could act on — so a guard dropped from
    only this copy passes every other test in the suite. The sync twin is
    ``test_a_clipboard_read_with_no_text_raises_rather_than_coercing``, and the
    thing both are about is that ``str(None)`` is "None": a four-letter
    clipboard nobody copied, which a caller pastes.
    """
    respx.get(f"{BASE}/computers/vm-1/clipboard").mock(httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError, match="did not come back with any text"):
        await mc.AsyncComputer(client._t, COMPUTER).clipboard()
    await client.aclose()


@respx.mock
async def test_the_async_clipboard_keeps_an_empty_selection(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1/clipboard").mock(httpx.Response(200, json={"text": ""}))
    assert await mc.AsyncComputer(client._t, COMPUTER).clipboard() == ""
    await client.aclose()


@respx.mock
async def test_the_async_clipboard_write_sends_the_one_field(client: mc.AsyncClient) -> None:
    route = respx.put(f"{BASE}/computers/vm-1/clipboard").mock(
        httpx.Response(200, json={"ok": True})
    )
    await mc.AsyncComputer(client._t, COMPUTER).set_clipboard("hello")
    assert json.loads(route.calls.last.request.content) == {"text": "hello"}
    await client.aclose()


@respx.mock
async def test_the_async_clipboard_write_refuses_before_it_reaches_the_wire(
    client: mc.AsyncClient,
) -> None:
    """The public async method must keep using the shared body builder.

    The builder's own tests stay green if this method quietly replaces it with
    a raw ``{"text": text}``. Driving every refusal through the method and
    watching the route proves validation still happens before a write can
    resume and bill a suspended computer.
    """
    route = respx.put(f"{BASE}/computers/vm-1/clipboard").mock(
        httpx.Response(200, json={"ok": True})
    )
    computer = mc.AsyncComputer(client._t, COMPUTER)
    with pytest.raises(ValueError, match="must not be empty"):
        await computer.set_clipboard("")
    with pytest.raises(ValueError, match="NUL"):
        await computer.set_clipboard("a\0b")
    with pytest.raises(ValueError, match="at most"):
        await computer.set_clipboard("x" * (mc._api.MAX_CLIPBOARD_BYTES + 1))
    with pytest.raises(ValueError, match="must be a string"):
        await computer.set_clipboard(None)  # type: ignore[arg-type]
    assert not route.called


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
    with pytest.raises(ValueError, match="amount must be positive"):
        await c.scroll(amount=0)
    with pytest.raises(ValueError, match="timeout must be positive"):
        await c.exec("true", timeout=0)
    with pytest.raises(ValueError, match="hour"):
        await c.set_schedule(enabled=True, hour=99)


@respx.mock
async def test_long_input_actions_widen_the_request_budget(client: mc.AsyncClient) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    c = mc.AsyncComputer(client._t, COMPUTER)
    await c.wait(120)
    assert route.calls.last.request.extensions["timeout"]["read"] == (
        120 + mc._client.DEADLINE_SLACK
    )
    await c.hold_key("shift", seconds=90)
    assert route.calls.last.request.extensions["timeout"]["read"] == (
        90 + mc._client.DEADLINE_SLACK
    )
    await client.aclose()


@respx.mock
async def test_screenshot_returns_bytes(client: mc.AsyncClient) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/screenshot").mock(
        httpx.Response(200, content=b"\x89PNG\r\n", headers={"Content-Type": "image/png"})
    )
    assert (await mc.AsyncComputer(client._t, COMPUTER).screenshot()).startswith(b"\x89PNG")
    assert "image/png" in route.calls.last.request.headers["Accept"]


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


@pytest.mark.parametrize(
    "wait",
    [
        lambda c, **kw: c.wait_for_move(**kw),
        lambda c, **kw: c.wait_until_built(**kw),
        lambda c, **kw: c.wait_until_running(**kw),
        lambda c, **kw: c.wait_for_guest(**kw),
    ],
    ids=["wait_for_move", "wait_until_built", "wait_until_running", "wait_for_guest"],
)
async def test_computer_waits_refuse_a_timeout_or_poll_they_cannot_honour(
    client: mc.AsyncClient, wait: object
) -> None:
    """The two halves diverged here, which is the part that makes it a parity
    defect as well as a correctness one."""
    c = mc.AsyncComputer(client._t, COMPUTER)
    with pytest.raises(ValueError, match="poll must be a finite, non-negative"):
        await wait(c, timeout=1.0, poll=-1)  # type: ignore[misc]
    with pytest.raises(ValueError, match="timeout must be a finite, non-negative"):
        await wait(c, timeout=float("nan"))  # type: ignore[misc]


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
async def test_wait_until_running_caps_refresh_to_its_remaining_budget(
    client: mc.AsyncClient,
) -> None:
    route = respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "running"})
    )
    await mc.AsyncComputer(client._t, COMPUTER).wait_until_running(timeout=2, poll=0)
    assert max(route.calls.last.request.extensions["timeout"].values()) <= 2
    await client.aclose()


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
async def test_a_malformed_delete_count_is_an_sdk_error(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.delete(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={"snapshots_deleted": "many"})
    )
    c = await client.computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="invalid snapshots_deleted"):
        await c.delete()


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
async def test_a_background_exec_requires_a_positive_pid(client: mc.AsyncClient) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json={}))
    with pytest.raises(mc.MandalaError, match="positive pid"):
        await mc.AsyncComputer(client._t, COMPUTER).start_exec("make")
    await client.aclose()


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
async def test_an_action_that_says_gone_and_describes_the_window_is_refused(
    client: mc.AsyncClient,
) -> None:
    """The same refusal the sync client makes, on the half that shares only the
    reading and not the call site (OPL-4200).

    `window_contradiction` lives in _models; each `window_action` raises on it
    itself, so the async one is its own line of code and its own way to be
    forgotten.
    """
    respx.post(f"{BASE}/computers/vm-1/windows/0x26").mock(
        httpx.Response(200, json={"ok": True, "gone": True, "window": {"id": "0x26"}})
    )
    with pytest.raises(mc.MandalaError, match="reports the window gone and describes window"):
        await mc.AsyncComputer(client._t, COMPUTER).window_action("0x26", "close")
    await client.aclose()


@respx.mock
async def test_windows_refuses_non_object_rows(client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1/windows").mock(httpx.Response(200, json={"windows": [None]}))
    with pytest.raises(mc.MandalaError, match="array of objects"):
        await mc.AsyncComputer(client._t, COMPUTER).windows()


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
async def test_wait_for_guest_polls_through_a_rate_limit_on_the_platform_cadence(
    client: mc.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async half of the same rule: honour Retry-After, do not fail the wait."""
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "8"}, json={"error": "slow down"}),
            httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""}),
        ]
    )
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    slept: list[float] = []

    async def _record(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(mc._async_computer.asyncio, "sleep", _record)
    await mc.AsyncComputer(client._t, COMPUTER).wait_for_guest(timeout=30, poll=0)
    assert route.call_count == 2
    assert 8 in slept
    await client.aclose()


@respx.mock
async def test_wait_for_guest_caps_the_probe_to_its_remaining_budget(
    client: mc.AsyncClient,
) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    await mc.AsyncComputer(client._t, COMPUTER).wait_for_guest(timeout=2, poll=0)
    assert json.loads(route.calls.last.request.content)["timeout_s"] == 2
    assert max(route.calls.last.request.extensions["timeout"].values()) <= 2
    await client.aclose()


@respx.mock
async def test_wait_for_guest_reports_an_async_failed_start_without_probing(
    client: mc.AsyncClient,
) -> None:
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json={}))
    computer = mc.AsyncComputer(
        client._t,
        {**COMPUTER, "status": "stopped", "start_error": "boot allocation failed"},
    )
    with pytest.raises(mc.MandalaError, match="boot allocation failed"):
        await computer.wait_for_guest(timeout=30, poll=0)
    assert not probe.called
    await client.aclose()


@respx.mock
async def test_wait_for_guest_refreshes_an_async_stale_running_handle(
    client: mc.AsyncClient,
) -> None:
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(400, json={"error": "not running"})
    )
    refresh = respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "stopped"})
    )
    with pytest.raises(mc.MandalaError, match="stopped.+call start"):
        await mc.AsyncComputer(client._t, COMPUTER).wait_for_guest(timeout=30, poll=0)
    assert probe.call_count == 1 and refresh.call_count == 1
    await client.aclose()


@respx.mock
async def test_async_write_file_refuses_an_oversized_body_before_the_request(
    client: mc.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200))
    monkeypatch.setattr(mc._computer, "FILE_SIZE_LIMIT", 2)
    with pytest.raises(ValueError, match="may not exceed"):
        await mc.AsyncComputer(client._t, COMPUTER).write_file("/tmp/a", "€")
    assert not put.called
    await client.aclose()


@respx.mock
async def test_async_download_file_pages_until_the_file_ends(
    client: mc.AsyncClient, tmp_path
) -> None:
    """The paging loop is written out on both halves, so it is checked on both.

    The shape is what test_parity.py pins; this is the behaviour underneath it,
    which two hand-written loops can differ on while both keep the signature.
    """
    body = b"onetwothree"

    def handler(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Range"].removeprefix("bytes=").split("-")[0])
        window = body[first : first + 3]
        return httpx.Response(
            206,
            content=window,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {first}-{first + len(window) - 1}/{len(body)}",
            },
        )

    route = respx.get(f"{BASE}/computers/vm-1/files").mock(side_effect=handler)
    dst = tmp_path / "out.bin"

    written = await mc.AsyncComputer(client._t, COMPUTER).download_file(
        "/home/user/out.bin", dst, part_size=3
    )

    assert written == len(body)
    assert dst.read_bytes() == body
    assert [call.request.headers["Range"] for call in route.calls] == [
        "bytes=0-2",
        "bytes=3-5",
        "bytes=6-8",
        "bytes=9-11",
    ]
    await client.aclose()


@respx.mock
async def test_async_download_file_drains_short_writes(client: mc.AsyncClient) -> None:
    class ShortSink(io.BytesIO):
        def write(self, data: bytes) -> int:
            return super().write(data[:2])

    body = b"abcdefgh"

    def handler(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Range"].removeprefix("bytes=").split("-")[0])
        return httpx.Response(
            206,
            content=body[first : first + 4],
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {first}-{first + 3}/{len(body)}",
            },
        )

    respx.get(f"{BASE}/computers/vm-1/files").mock(side_effect=handler)
    sink = ShortSink()

    assert await mc.AsyncComputer(client._t, COMPUTER).download_file(
        "/x", sink, part_size=4
    ) == len(body)
    assert sink.getvalue() == body
    await client.aclose()


@respx.mock
async def test_async_read_file_part_reads_the_answer_not_the_ask(
    client: mc.AsyncClient,
) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(
            206,
            content=b"ab",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 4-5/64",
            },
        )
    )
    part = await mc.AsyncComputer(client._t, COMPUTER).read_file_part("/tmp/a", offset=4, length=16)
    assert (part.offset, part.total, part.end, part.at_end) == (4, 64, 6, False)
    await client.aclose()


@respx.mock
async def test_async_a_partial_answer_wider_than_requested_is_refused(
    client: mc.AsyncClient,
) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(
            206,
            content=b"x" * 40,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-39/100",
            },
        )
    )

    with pytest.raises(mc.MandalaError, match="outside the requested Range bytes=0-9"):
        await mc.AsyncComputer(client._t, COMPUTER).read_file_part("/x", offset=0, length=10)
    await client.aclose()


@respx.mock
async def test_async_an_empty_file_downloads_as_nothing(client: mc.AsyncClient, tmp_path) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(416, json={"error": "no"}, headers={"Content-Range": "bytes */0"})
    )
    dst = tmp_path / "empty"
    assert await mc.AsyncComputer(client._t, COMPUTER).download_file("/tmp/empty", dst) == 0
    assert dst.read_bytes() == b""
    await client.aclose()


@respx.mock
async def test_async_a_file_that_shrinks_mid_download_raises(client: mc.AsyncClient) -> None:
    """The loop is written out twice, so the thing that stops it is checked twice."""

    def handler(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Range"].removeprefix("bytes=").split("-")[0])
        data, total = (b"OLDOLD", 100) if first == 0 else (b"new", 9)
        return httpx.Response(
            206,
            content=data,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {first}-{first + len(data) - 1}/{total}",
            },
        )

    respx.get(f"{BASE}/computers/vm-1/files").mock(side_effect=handler)
    with pytest.raises(mc.MandalaError, match="was 100 bytes and is 9"):
        await mc.AsyncComputer(client._t, COMPUTER).download_file("/x", io.BytesIO(), part_size=6)
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

    with (
        pytest.warns(UserWarning, match="still billable") as warning,
        pytest.raises(ZeroDivisionError),
    ):
        await caller_raises()
    assert warning[0].filename == __file__
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
    """The async half derives its deadline from timeout too.

    The budget is threaded through the transport, so this is exactly where the
    two halves could drift: an await that kept the fixed 60-second default would
    abandon a long command while the sync half waited it out.
    """
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    c = await client.computers.get("vm-1")

    await c.exec("make", timeout=300)
    assert route.calls.last.request.extensions["timeout"]["read"] == (
        300 + mc._client.DEADLINE_SLACK
    )

    files = respx.get(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200, content=b"hi"))
    await c.read_file("/tmp/a")
    assert files.calls.last.request.extensions["timeout"]["read"] == mc._client.FILE_TIMEOUT
    assert files.calls.last.request.headers["Accept"] == "application/octet-stream"
    await client.aclose()


@respx.mock
async def test_a_transport_timeout_arrives_as_a_mandala_error(client: mc.AsyncClient) -> None:
    """A timeout is the SDK's own error on both halves."""
    respx.post(f"{BASE}/computers/vm-1/exec").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(mc.TimeoutError, match="did not answer") as caught:
        await mc.AsyncComputer(client._t, COMPUTER).exec("sleep 999", timeout=100)
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

    c = mc.AsyncComputer(client._t, COMPUTER)
    assert await c.set_schedule(enabled=True) == stored
    assert c.snapshot_schedule == stored
    assert (put.call_count, get.call_count) == (1, 0)
    await client.aclose()


@respx.mock
async def test_schedule_writes_through_to_the_cached_property(client: mc.AsyncClient) -> None:
    """A GET used to leave snapshot_schedule stale against the just-fetched value."""
    stored = {"enabled": False, "hour": 0, "minute": 0, "tz": "UTC"}
    respx.get(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json=stored))
    c = mc.AsyncComputer(client._t, COMPUTER)
    assert await c.schedule() == stored
    assert c.snapshot_schedule == stored
    await client.aclose()


@respx.mock
async def test_an_empty_schedule_does_not_cache_as_a_window(client: mc.AsyncClient) -> None:
    """GET {} is "no schedule"; snapshot_schedule is None, not {}."""
    respx.get(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json={}))
    c = mc.AsyncComputer(client._t, {**COMPUTER, "snapshot_schedule": {"enabled": True}})
    assert await c.schedule() == {}
    assert c.snapshot_schedule is None
    await client.aclose()


@respx.mock
async def test_a_proxy_giving_up_is_not_reported_as_a_bare_status(
    client: mc.AsyncClient,
) -> None:
    """The async half maps 524 exactly as the sync half does."""
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(524, content=b""))
    with pytest.raises(mc.GatewayTimeoutError) as e:
        await mc.AsyncComputer(client._t, COMPUTER).exec("sleep 130", timeout=300)
    assert e.value.status == 524
    assert "start_exec()" in str(e.value)


@respx.mock
async def test_an_ordinary_gateway_timeout_lands_in_the_same_place(
    client: mc.AsyncClient,
) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(504, content=b""))
    with pytest.raises(mc.GatewayTimeoutError) as e:
        await client.computers.get("vm-1")
    assert e.value.status == 504


@respx.mock
async def test_a_platform_that_named_the_failure_keeps_its_own_words(
    client: mc.AsyncClient,
) -> None:
    """The async half substitutes on the same condition the sync half does."""
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(504, json={"error": "upstream unavailable before dispatch"})
    )
    with pytest.raises(mc.GatewayTimeoutError) as e:
        await client.computers.get("vm-1")
    assert str(e.value) == "upstream unavailable before dispatch"


@respx.mock
async def test_an_origin_never_reached_is_not_one_that_stopped_answering(
    client: mc.AsyncClient,
) -> None:
    """The async half classifies the 52x range exactly as the sync half does."""
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(522, content=b""))
    with pytest.raises(mc.OriginUnreachableError) as e:
        await client.computers.get("vm-1")
    assert e.value.status == 522
    assert not isinstance(e.value, mc.GatewayTimeoutError)
    assert "never sent" in str(e.value)


@respx.mock
async def test_a_520_does_not_claim_the_work_never_happened(
    client: mc.AsyncClient,
) -> None:
    """The async half tells 520 apart from its neighbours the same way."""
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(520, content=b""))
    with pytest.raises(mc.OriginResponseError) as e:
        await client.computers.get("vm-1")
    assert e.value.status == 520
    assert not isinstance(e.value, mc.OriginUnreachableError)
    assert "never arrived" not in str(e.value)
