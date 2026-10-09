"""Renewal preserves an existing session and fails closed at its bounds."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import respx
from tests.test_browser_toolset import BASE, IDENT, PAYLOAD, TOKEN, chrome  # noqa: F401

from mandala_computer import (
    AsyncClient,
    BrowserConnection,
    BrowserSessionLease,
    BrowserSessionPolicy,
    Client,
    MandalaError,
)
from mandala_computer._browser_cdp import BrowserCDP, BrowserError

# Deliberately decades away from the SDK wall clock.
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)


def payload(**changes):
    return {
        **PAYLOAD,
        "lifecycle_version": 2,
        "server_time": EPOCH.isoformat(),
        "attach_expires_at": (EPOCH + timedelta(minutes=10)).isoformat(),
        "expires_at": (EPOCH + timedelta(minutes=10)).isoformat(),
        "lease_expires_at": (EPOCH + timedelta(minutes=30)).isoformat(),
        "absolute_expires_at": (EPOCH + timedelta(hours=2)).isoformat(),
        "lease_seconds": 1800,
        "idle_timeout_seconds": 0,
        **changes,
    }


@respx.mock
@pytest.mark.parametrize("asynchronous", [False, True])
def test_versioned_lifecycle_uses_account_auth(asynchronous):
    respx.get(f"{BASE}/computers/vm-1").respond(200, json={"id": "vm-1", "status": "running"})
    create = respx.post(f"{BASE}/computers/vm-1/browser-connections").respond(201, json=payload())
    renew = respx.post(f"{BASE}/computers/vm-1/browser-connections/{IDENT}/renew").respond(
        200, json=payload()
    )
    if asynchronous:

        async def run():
            async with AsyncClient("com_test", base_url=BASE) as client:
                c = await client.computers.get("vm-1")
                grant = await c.create_browser_connection(session_policy=BrowserSessionPolicy())
                lease = await c.renew_browser_connection(grant.id)
                return grant, lease

        grant, lease = asyncio.run(run())
    else:
        with Client("com_test", base_url=BASE) as client:
            c = client.computers.get("vm-1")
            grant = c.create_browser_connection(session_policy=BrowserSessionPolicy())
            lease = c.renew_browser_connection(grant.id)
    assert grant.lease == lease
    assert json.loads(create.calls[0].request.content) == BrowserSessionPolicy().to_api()
    assert json.loads(renew.calls[0].request.content) == {}
    assert renew.calls[0].request.headers["Authorization"] == "Bearer com_test"


@respx.mock
@pytest.mark.parametrize("asynchronous", [False, True])
def test_old_server_cannot_silently_downgrade_policy(asynchronous):
    respx.get(f"{BASE}/computers/vm-1").respond(200, json={"id": "vm-1"})
    respx.post(f"{BASE}/computers/vm-1/browser-connections").respond(201, json=PAYLOAD)
    revoke = respx.delete(f"{BASE}/computers/vm-1/browser-connections/{IDENT}").respond(
        200, json={"ok": True}
    )
    with pytest.raises(MandalaError, match="invalid browser session"):
        if asynchronous:

            async def run():
                async with AsyncClient("com_test", base_url=BASE) as client:
                    c = await client.computers.get("vm-1")
                    await c.create_browser_connection(session_policy=BrowserSessionPolicy())

            asyncio.run(run())
        else:
            with Client("com_test", base_url=BASE) as client:
                client.computers.get("vm-1").create_browser_connection(
                    session_policy=BrowserSessionPolicy()
                )
    assert revoke.called


@pytest.mark.parametrize(
    "options",
    [
        {"lease_seconds": 59},
        {"lease_seconds": True},
        {"max_duration_seconds": 7201},
        {"lease_seconds": 120, "max_duration_seconds": 60},
        {"auto_renew": 1},
    ],
)
def test_invalid_policy(options):
    with pytest.raises(ValueError):
        BrowserSessionPolicy(**options)


@pytest.mark.parametrize(
    "change",
    [
        {"id": "b" * 32},
        {"lifecycle_version": True},
        {"idle_timeout_seconds": 1},
        {"lease_seconds": 59},
        {"server_time": "2026-10-09T00:00:00"},
        {"absolute_expires_at": EPOCH.isoformat()},
    ],
)
def test_invalid_renewal_response(change):
    with pytest.raises(MandalaError):
        BrowserSessionLease.from_api(payload(**change), IDENT)


def short_lease(ttl=0.09, *, server=0.0, end=1.0, attach=0.02):
    return BrowserSessionLease(
        IDENT,
        EPOCH + timedelta(seconds=server),
        EPOCH + timedelta(seconds=attach),
        EPOCH + timedelta(seconds=server + ttl),
        EPOCH + timedelta(seconds=end),
        60,
    )


def backend(renew, policy=None):
    policy = policy or BrowserSessionPolicy(60, 120)
    b = BrowserCDP(lambda: None, AsyncMock(), None, renew=renew, session_policy=policy)
    b.grant = SimpleNamespace(id=IDENT)
    b.ws = SimpleNamespace(close=AsyncMock())
    b._accept_lease(short_lease(), 0)
    return b


async def test_renewal_preserves_socket_and_uses_monotonic_time():
    first = short_lease()
    renew = AsyncMock(return_value=short_lease(ttl=0.9, server=0.1))
    b = backend(renew)
    ws = b.ws
    b.lease_task = asyncio.create_task(b._maintain_lease())
    try:
        await asyncio.sleep(0.13)
        assert renew.await_count == 1 and b.ws is ws
        assert b.lease.id == first.id and b.lease.absolute_expires_at == first.absolute_expires_at
        assert b.session_status()["state"] == "active"
        assert 0.7 < b.session_status()["remaining_seconds"] < 1
        ws.close.assert_not_awaited()
    finally:
        await b.close()


@pytest.mark.parametrize("auto", [True, False])
async def test_no_renewal_after_absolute_limit_or_when_disabled(auto):
    renew = AsyncMock()
    b = backend(renew, BrowserSessionPolicy(60, 120, auto))
    b.lease = None
    b._accept_lease(short_lease(ttl=0.06, end=1), 0)
    if auto:
        b.lease = None
        b._accept_lease(short_lease(ttl=0.06, end=0.06), 0)
    await b._maintain_lease()
    assert b.failed and b.ws.close.await_count == 1 and renew.await_count == 0
    assert "expired" in b.session_status()["terminal_error"]


async def test_renewal_failure_is_terminal_and_redacted():
    renew = AsyncMock(side_effect=RuntimeError(TOKEN))
    b = backend(renew)
    await b._maintain_lease()
    assert b.failed and b.ws.close.await_count == 1
    assert "renewal failed" in b.session_status()["terminal_error"]
    with pytest.raises(BrowserError) as caught:
        await b.start()
    assert TOKEN not in str(caught.value)
    assert renew.await_count == 1


async def test_close_cancels_renewal_without_accepting_late_result():
    pending = asyncio.Event()

    async def renew(_):
        pending.set()
        await asyncio.Event().wait()

    b = backend(renew)
    b.lease_task = asyncio.create_task(b._maintain_lease())
    await asyncio.wait_for(pending.wait(), 1)
    before = b.lease
    await b.close()
    assert b.lease == before and b.session_status()["state"] == "ended"
    assert b.lease_task.done()


async def test_request_latency_cannot_extend_lease():
    b = backend(AsyncMock())
    with pytest.raises(BrowserError, match="expired before"):
        b._accept_lease(short_lease(), 0.2)


async def test_real_browser_state_survives_renewal_during_pending_command(chrome):  # noqa: F811
    renewed = asyncio.Event()
    start = time.monotonic()
    first = short_lease(ttl=0.6, attach=0.5, end=5)
    grant = BrowserConnection(IDENT, chrome, TOKEN, first.attach_expires_at, first)

    async def renew(ident):
        assert ident == IDENT
        elapsed = time.monotonic() - start
        renewed.set()
        return short_lease(ttl=5 - elapsed, server=elapsed, attach=0.5, end=5)

    b = BrowserCDP(
        lambda: grant,
        AsyncMock(),
        lambda *_: None,
        renew=renew,
        session_policy=BrowserSessionPolicy(60, 120),
    )
    try:
        await b.start()
        session = b.sessions[b.active]
        answer = await b.send(
            "Runtime.evaluate",
            {
                "expression": "globalThis.leaseSentinel=5880; new Promise(r=>setTimeout(()=>r(leaseSentinel),1000))",
                "awaitPromise": True,
                "returnByValue": True,
            },
            session,
        )
        assert renewed.is_set() and answer["result"]["value"] == 5880
        assert b.sessions[b.active] == session and b.session_status()["state"] == "active"
    finally:
        await b.close()


def test_sync_status_is_readable_while_worker_closes(monkeypatch):
    import threading

    from tests.test_browser_toolset import use

    from mandala_computer.anthropic import MandalaBrowserToolset

    browser = MandalaBrowserToolset(
        SimpleNamespace(
            create_browser_connection=lambda: None, revoke_browser_connection=lambda _: None
        )
    )
    assert browser.tool_result(use("javascript_exec", text="1+1")).get("is_error")
    worker = browser._worker
    assert worker is not None
    stopped, release = threading.Event(), threading.Event()
    original = worker.close

    def paused_close():
        original()
        stopped.set()
        assert release.wait(2)

    monkeypatch.setattr(worker, "close", paused_close)
    closer = threading.Thread(target=browser.close)
    closer.start()
    try:
        assert stopped.wait(2)
        assert browser.session_status["state"] == "ended"
    finally:
        release.set()
        closer.join(2)
    assert not closer.is_alive()
