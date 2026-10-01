"""The readiness waits poll fast first, on both handles (OPL-5536).

A flat interval rounded every readiness stage up to a whole ``poll``: a secret
that landed just after a read made a bound ``launch`` sleep out the rest of
three seconds. The ordinary "not yet" sleep now ramps 0.25s, doubling, up to
``poll``; a failed poll keeps its own delay, ``Retry-After`` included.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from tests.test_launch import BASE, COMPUTER, GUEST
from tests.test_launch_secrets import BINDING, RECEIPT, bound

import mandala_computer as mc
import mandala_computer._async_computer as async_computers
import mandala_computer._async_resources as async_resources
import mandala_computer._computer as computers
import mandala_computer._resources as resources

FLAVOURS = ["sync", "async"]
UNFINISHED = {"exit_code": -1, "timed_out": True, "stdout_b64": "", "stderr_b64": ""}
RATE_LIMITED = {"error": "rate limited"}


class Clock:
    """A platform answering from ``respond`` on a fake clock that moves only
    when the SDK sleeps, recording every sleep it is asked for."""

    def __init__(self, respond):
        self.respond = respond
        self.now = 0.0
        self.sleeps = []
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        return self.respond(request)

    def install(self, monkeypatch):
        def sleep(delay):
            self.sleeps.append(delay)
            self.now += delay

        async def async_sleep(delay):
            sleep(delay)

        clock = SimpleNamespace(monotonic=lambda: self.now, sleep=sleep)
        for module in (resources, computers, async_resources, async_computers):
            monkeypatch.setattr(module, "time", clock)
        for module in (async_resources, async_computers):
            monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=async_sleep))


async def run(clock, flavour, act):
    """``act(client)`` on a client of ``flavour``, awaited if it is async."""
    if flavour == "sync":
        with (
            httpx.Client(transport=httpx.MockTransport(clock.handle)) as http,
            mc.Client("com_test", base_url=BASE, http_client=http) as client,
        ):
            return act(client)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(clock.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        return await act(client)


def handle(flavour, client, record):
    cls = mc.Computer if flavour == "sync" else mc.AsyncComputer
    return cls(client._t, record)


def reads(clock):
    return [r for r in clock.requests if r.method == "GET"]


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_wait_for_secrets_ramps_from_a_quarter_second_to_the_poll(monkeypatch, flavour):
    n = 0

    def respond(request):
        nonlocal n
        n += 1
        return httpx.Response(
            200, json=bound(True) if n < 8 else bound(False, secrets_applied=RECEIPT)
        )

    clock = Clock(respond)
    clock.install(monkeypatch)
    await run(
        clock,
        flavour,
        lambda client: handle(flavour, client, bound(True)).wait_for_secrets(
            timeout=60, poll=3, expect_secrets=True
        ),
    )
    assert len(reads(clock)) == 8
    assert clock.sleeps == [0.25, 0.5, 1.0, 2.0, 3.0, 3.0, 3.0]


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_a_rate_limited_read_waits_its_retry_after_and_the_ramp_goes_on(monkeypatch, flavour):
    n = 0

    def respond(request):
        nonlocal n
        n += 1
        if n == 3:
            return httpx.Response(429, json=RATE_LIMITED, headers={"Retry-After": "4"})
        return httpx.Response(
            200, json=bound(True) if n < 5 else bound(False, secrets_applied=RECEIPT)
        )

    clock = Clock(respond)
    clock.install(monkeypatch)
    await run(
        clock,
        flavour,
        lambda client: handle(flavour, client, bound(True)).wait_for_secrets(
            timeout=60, poll=3, expect_secrets=True
        ),
    )
    # 0.25 and 0.5 ramp; the 429 waits its four seconds rather than a ramped
    # 1.0; the next ordinary sleep resumes the ramp where it was.
    assert clock.sleeps == [0.25, 0.5, 4.0, 1.0]


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_launch_notices_secrets_that_land_just_after_a_read(monkeypatch, flavour):
    n = 0

    def respond(request):
        nonlocal n
        if request.method == "POST" and request.url.path.endswith("/exec"):
            return httpx.Response(200, json=GUEST)
        if request.method == "POST":
            return httpx.Response(201, json=bound(True))
        n += 1
        return httpx.Response(
            200, json=bound(True) if n < 3 else bound(False, secrets_applied=RECEIPT)
        )

    clock = Clock(respond)
    clock.install(monkeypatch)
    c = await run(
        clock,
        flavour,
        lambda client: client.computers.launch(
            secrets=[{"secret_id": BINDING["secret_id"], "env": "TOKEN"}]
        ),
    )
    assert c.secrets_delivering is False
    # The secrets wait read once, saw the delivery under way, and read again a
    # quarter second later, where it used to sleep launch's whole 3s poll.
    assert clock.sleeps == [0.25]


def desktop_probe(request):
    return request.url.path.endswith("/exec") and (
        json.loads(request.content).get("session") == "desktop"
    )


def desktop_platform(answer):
    probes = 0

    def respond(request):
        nonlocal probes
        if desktop_probe(request):
            probes += 1
            return answer(probes)
        return httpx.Response(200, json=LINUX)

    return respond


LINUX = {**COMPUTER, "os": "linux", "desktop": "wayland"}


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_wait_for_desktop_ramps_from_a_quarter_second_to_the_poll(monkeypatch, flavour):
    clock = Clock(
        desktop_platform(
            lambda n: httpx.Response(200, json=UNFINISHED if n < 6 else GUEST),
        )
    )
    clock.install(monkeypatch)
    await run(
        clock,
        flavour,
        lambda client: handle(flavour, client, LINUX).wait_for_desktop(timeout=60, poll=1.5),
    )
    assert clock.sleeps == [0.25, 0.5, 1.0, 1.5, 1.5]


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_wait_for_desktop_waits_out_a_retry_after_rather_than_ramping(monkeypatch, flavour):
    def answer(n):
        if n == 2:
            return httpx.Response(429, json=RATE_LIMITED, headers={"Retry-After": "5"})
        return httpx.Response(200, json=UNFINISHED if n < 4 else GUEST)

    clock = Clock(desktop_platform(answer))
    clock.install(monkeypatch)
    await run(
        clock,
        flavour,
        lambda client: handle(flavour, client, LINUX).wait_for_desktop(timeout=60, poll=3),
    )
    assert clock.sleeps == [0.25, 5.0, 0.5]
