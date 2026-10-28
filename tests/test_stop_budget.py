"""Stop keeps waiting for a 65-second answer under the platform's longer grace."""

import asyncio

import httpx
import pytest

import mandala_computer as mc

BASE = "https://api.test/api/v1"
COMPUTER = {"id": "vm-1", "status": "running"}


class SlowStop:
    def __init__(self, expected):
        self.expected = expected
        self.stops = 0

    def handle(self, request):
        if request.method == "GET":
            return httpx.Response(200, json={**COMPUTER, "status": "stopped"})
        assert request.url.path.endswith("/stop")
        self.stops += 1
        budget = request.extensions["timeout"]
        assert budget["read"] == self.expected
        assert budget["write"] == self.expected
        assert budget["connect"] == 3
        assert budget["pool"] == 4
        # Model a transport that enforces its supplied read deadline, without
        # making the regression suite sleep 65 seconds for each client shape.
        if budget["read"] is not None and budget["read"] < 65:
            raise httpx.ReadTimeout("65-second stop exceeded read budget", request=request)
        return httpx.Response(200, json={"ok": True})


@pytest.mark.parametrize("configured,expected", [(5, 95), (60, 95), (180, 180), (None, None)])
@pytest.mark.parametrize("force", [False, True])
def test_sync_slow_stop(configured, expected, force):
    probe = SlowStop(expected)
    with (
        httpx.Client(
            transport=httpx.MockTransport(probe.handle),
            timeout=httpx.Timeout(connect=3, pool=4, read=configured, write=configured),
        ) as http,
        mc.Client("gck_test", base_url=BASE, http_client=http) as client,
    ):
        computer = mc.Computer(client._t, COMPUTER)
        assert computer.stop(force=force).status == "stopped"
        assert probe.stops == 1


@pytest.mark.parametrize("configured,expected", [(5, 95), (60, 95), (180, 180), (None, None)])
@pytest.mark.parametrize("force", [False, True])
async def test_async_slow_stop(configured, expected, force):
    probe = SlowStop(expected)
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(probe.handle),
            timeout=httpx.Timeout(connect=3, pool=4, read=configured, write=configured),
        ) as http,
        mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
    ):
        computer = mc.AsyncComputer(client._t, COMPUTER)
        assert (await computer.stop(force=force)).status == "stopped"
        assert probe.stops == 1


async def test_async_stop_preserves_cancellation():
    entered = asyncio.Event()
    calls = 0

    async def handle(request):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled stop must not finish")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
    ):
        task = asyncio.create_task(mc.AsyncComputer(client._t, COMPUTER).stop())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls == 1
