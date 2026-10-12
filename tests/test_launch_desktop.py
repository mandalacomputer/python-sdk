"""Launch waits for the desktop session, on both handles.

The guest agent answers a few seconds before the desktop user is logged in, and
a desktop exec sent in between is refused with a 409 carrying no ``reason``.
"""

import asyncio
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

NO_DESKTOP = {
    "error": "no active desktop session in the guest (it may still be booting, "
    "or nobody is logged in)"
}


def linux(**extra):
    record = {**COMPUTER, "os": "linux", "desktop": "x11"}
    for key, value in extra.items():
        if value is None:
            record.pop(key, None)
        else:
            record[key] = value
    return record


def is_desktop_probe(request):
    if not request.url.path.endswith("/exec"):
        return False
    return json.loads(request.content).get("session") == "desktop"


class Rig:
    """A mocked platform whose desktop probe answers from ``probe``.

    Every read and the guest probe answer as a running computer described by
    ``record``. A fake clock advances ``tick`` seconds per request, so a wait
    whose refusals never stop reaches its deadline without sleeping.
    """

    def __init__(self, record, probe, tick=0.0):
        self.record = record
        self.probe = probe
        self.tick = tick
        self.requests = []
        self.now = 0.0
        self.sleeps = []
        self.probe_times = []

    def handle(self, request):
        self.requests.append(request)
        self.now += self.tick
        if is_desktop_probe(request):
            self.probe_times.append(self.now)
            return self.probe(len(self.desktop_probes()))
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, json=GUEST)
        return httpx.Response(200, json=self.record)

    def desktop_probes(self):
        return [r for r in self.requests if is_desktop_probe(r)]

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


def refused_twice(n):
    return httpx.Response(409, json=NO_DESKTOP) if n <= 2 else httpx.Response(200, json=GUEST)


def launch_sync(rig, **kwargs):
    with (
        httpx.Client(transport=httpx.MockTransport(rig.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        return client.computers.launch(**kwargs)


async def launch_async(rig, **kwargs):
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(rig.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        return await client.computers.launch(**kwargs)


async def launch(rig, flavour, **kwargs):
    if flavour == "sync":
        return launch_sync(rig, **kwargs)
    return await launch_async(rig, **kwargs)


FLAVOURS = ["sync", "async"]


async def wait_for_desktop(rig, flavour, **kwargs):
    if flavour == "sync":
        with (
            httpx.Client(transport=httpx.MockTransport(rig.handle)) as http,
            mc.Client("com_test", base_url=BASE, http_client=http) as client,
        ):
            c = client.computers.get("launch-42")
            assert c.wait_for_desktop(**kwargs) is c
    else:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(rig.handle)) as http,
            mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
        ):
            c = await client.computers.get("launch-42")
            assert await c.wait_for_desktop(**kwargs) is c


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize(
    ("poll", "sleeps"),
    [
        (3, [0.25, 0.5, 1, 2, 3, 3]),
        (0.1, [0.1] * 6),
        (0, [0] * 6),
    ],
)
async def test_reasonless_desktop_conflicts_use_the_ramp_and_poll_cap(
    monkeypatch, flavour, poll, sleeps
):
    rig = Rig(
        linux(),
        lambda n: (
            httpx.Response(409, json=NO_DESKTOP) if n <= 6 else httpx.Response(200, json=GUEST)
        ),
    )
    rig.install(monkeypatch)
    await wait_for_desktop(rig, flavour, timeout=60, poll=poll)
    assert rig.sleeps == sleeps
    assert rig.probe_times == pytest.approx([sum(sleeps[:n]) for n in range(7)])


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_bound_launch_notices_the_desktop_within_its_remaining_budget(monkeypatch, flavour):
    class BoundRig(Rig):
        def handle(self, request):
            if request.method == "GET" and self.now >= 0.25:
                self.record = linux(**bound(False, secrets_applied=RECEIPT))
            return super().handle(request)

    rig = BoundRig(linux(**bound(True)), refused_twice)
    rig.install(monkeypatch)
    c = await launch(
        rig,
        flavour,
        timeout=1.2,
        secrets=[{"secret_id": BINDING["secret_id"], "env": "TOKEN"}],
    )
    assert c.secrets_delivering is False
    assert rig.sleeps == [0.25, 0.25, 0.5]
    assert rig.probe_times == [0.25, 0.5, 1.0]
    assert len([r for r in rig.requests if r.method == "GET"]) == 3
    assert len([r for r in rig.requests if r.url.path == "/api/v1/computers"]) == 1
    assert [max(r.extensions["timeout"].values()) for r in rig.desktop_probes()] == pytest.approx(
        [0.95, 0.7, 0.2]
    )


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize(
    ("status", "body", "headers", "delay"),
    [
        (409, {"error": "busy", "reason": "contention"}, {}, 3),
        (409, {"error": "starting", "reason": "starting"}, {}, 3),
        (409, {"error": "conflict", "reason": "future_reason"}, {}, 3),
        (429, {"error": "rate limited"}, {}, 3),
        (429, {"error": "rate limited"}, {"Retry-After": "5"}, 5),
        (500, {"error": "internal error"}, {}, 3),
        (502, {"error": "bad gateway"}, {}, 3),
        (503, {"error": "temporarily unavailable"}, {}, 3),
    ],
)
async def test_desktop_failures_keep_their_delay_without_advancing_the_ramp(
    monkeypatch, flavour, status, body, headers, delay
):
    answers = [
        httpx.Response(409, json=NO_DESKTOP),
        httpx.Response(status, json=body, headers=headers),
        httpx.Response(409, json=NO_DESKTOP),
        httpx.Response(200, json=GUEST),
    ]
    rig = Rig(linux(), lambda n: answers[n - 1])
    rig.install(monkeypatch)
    await wait_for_desktop(rig, flavour, timeout=60, poll=3)
    assert rig.sleeps == [0.25, delay, 0.5]


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_desktop_ramp_and_probe_timeouts_respect_the_deadline(monkeypatch, flavour):
    def probe(n):
        rig.now += 0.15
        return httpx.Response(409, json=NO_DESKTOP)

    rig = Rig(linux(), probe)
    rig.install(monkeypatch)
    with pytest.raises(mc.TimeoutError, match="desktop session was not active within 0.6s"):
        await wait_for_desktop(rig, flavour, timeout=0.6, poll=3)
    assert rig.now == pytest.approx(0.6)
    assert rig.sleeps == pytest.approx([0.25, 0.05])
    assert rig.probe_times == pytest.approx([0, 0.4])
    assert [max(r.extensions["timeout"].values()) for r in rig.desktop_probes()] == pytest.approx(
        [0.6, 0.2]
    )


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_desktop_retry_after_cannot_extend_the_deadline(monkeypatch, flavour):
    rig = Rig(
        linux(),
        lambda n: httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "5"}),
    )
    rig.install(monkeypatch)
    with pytest.raises(mc.TimeoutError, match="desktop session was not active within 2s"):
        await wait_for_desktop(rig, flavour, timeout=2)
    assert rig.sleeps == [2]
    assert len(rig.desktop_probes()) == 1


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (400, {"error": "invalid request"}, mc.APIError),
        (401, {"error": "unauthorized"}, mc.AuthenticationError),
        (403, {"error": "forbidden", "reason": "revoked"}, mc.PermissionDeniedError),
        (404, {"error": "not found"}, mc.NotFoundError),
        (409, {"error": "not running", "reason": "unavailable"}, mc.ConflictError),
    ],
)
async def test_desktop_final_refusals_do_not_sleep(monkeypatch, flavour, status, body, kind):
    rig = Rig(linux(), lambda n: httpx.Response(status, json=body))
    rig.install(monkeypatch)
    with pytest.raises(kind) as caught:
        await wait_for_desktop(rig, flavour, timeout=1)
    assert caught.value.status == status
    assert caught.value.body == body
    assert rig.sleeps == []
    assert len(rig.desktop_probes()) == 1


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_desktop_wait_does_not_swallow_an_interrupted_ramp_sleep(monkeypatch, flavour):
    rig = Rig(linux(), lambda n: httpx.Response(409, json=NO_DESKTOP))
    rig.install(monkeypatch)
    interruption = KeyboardInterrupt() if flavour == "sync" else asyncio.CancelledError()

    def interrupt(delay):
        rig.sleeps.append(delay)
        raise interruption

    async def async_interrupt(delay):
        interrupt(delay)

    if flavour == "sync":
        monkeypatch.setattr(computers.time, "sleep", interrupt)
    else:
        monkeypatch.setattr(async_computers.asyncio, "sleep", async_interrupt)
    with pytest.raises(type(interruption)) as caught:
        await wait_for_desktop(rig, flavour)
    assert caught.value is interruption
    assert rig.sleeps == [0.25]
    assert len(rig.desktop_probes()) == 1


# The platform leaves `desktop` out for X11, which is what most Linux templates
# run: the absence is the case this wait matters most for.
@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize("desktop", [None, "x11", "wayland"])
async def test_launch_returns_only_after_the_desktop_probe_is_accepted(
    monkeypatch, flavour, desktop
):
    rig = Rig(linux(desktop=desktop), refused_twice)
    rig.install(monkeypatch)
    c = await launch(rig, flavour, poll=0)
    assert c.id == "launch-42"
    assert len(rig.desktop_probes()) == 3
    assert rig.requests[-1] is rig.desktop_probes()[-1]
    assert json.loads(rig.requests[-1].content) == {
        "command": "true",
        "session": "desktop",
        "timeout_s": 5,
    }
    # The guest probe came first, in the agent's own session.
    first_exec = next(r for r in rig.requests if r.url.path.endswith("/exec"))
    assert json.loads(first_exec.content) == {"command": "exit 0", "timeout_s": 5}


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize(
    "record",
    [
        linux(desktop=""),
        linux(os=None),
        linux(os="windows"),
    ],
    ids=["explicit-empty-desktop", "os-not-reported", "windows"],
)
async def test_launch_asks_nothing_of_a_computer_without_a_linux_desktop(
    monkeypatch, flavour, record
):
    rig = Rig(record, lambda n: pytest.fail("the desktop was probed"))
    rig.install(monkeypatch)
    await launch(rig, flavour, poll=0)
    assert [(r.method, r.url.path) for r in rig.requests] == [
        ("POST", "/api/v1/computers"),
        ("GET", "/api/v1/computers/launch-42"),
        ("POST", "/api/v1/computers/launch-42/exec"),
    ]


@pytest.mark.parametrize("flavour", FLAVOURS)
@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (403, {"error": "forbidden"}, mc.PermissionDeniedError),
        (409, {"error": "not running", "reason": "unavailable"}, mc.ConflictError),
    ],
)
async def test_launch_raises_a_final_probe_refusal_at_once(
    monkeypatch, flavour, status, body, kind
):
    # A clock that moves, so a refusal wrongly polled through ends in a timeout
    # rather than a loop.
    rig = Rig(linux(), lambda n: httpx.Response(status, json=body), tick=1.0)
    rig.install(monkeypatch)
    with pytest.raises(kind) as caught:
        await launch(rig, flavour, poll=0)
    assert not isinstance(caught.value, mc.TimeoutError)
    assert caught.value.status == status
    assert "launch of launch-42 failed" in str(caught.value)
    assert len(rig.desktop_probes()) == 1


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_launch_spends_only_its_own_budget_on_refusals_that_never_stop(monkeypatch, flavour):
    rig = Rig(linux(), lambda n: httpx.Response(409, json=NO_DESKTOP), tick=1.0)
    rig.install(monkeypatch)
    with pytest.raises(mc.TimeoutError) as caught:
        await launch(rig, flavour, timeout=20, poll=0)
    message = str(caught.value)
    assert "launch of launch-42 failed" in message
    assert "desktop session was not active" in message
    # The earlier stages spent part of the 20s, so the desktop wait was handed
    # less than that and ran out on the launch's deadline, not its own default.
    assert rig.now <= 21
    assert len(rig.desktop_probes()) > 1


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_wait_for_desktop_polls_through_moments_that_clear(monkeypatch, flavour):
    answers = [
        httpx.Response(502, json={"error": "bad gateway"}),
        httpx.Response(409, json={"error": "busy", "reason": "contention"}),
        httpx.Response(409, json=NO_DESKTOP),
        httpx.Response(200, json=GUEST),
    ]
    rig = Rig(linux(desktop="wayland"), lambda n: answers[n - 1])
    rig.install(monkeypatch)
    if flavour == "sync":
        with (
            httpx.Client(transport=httpx.MockTransport(rig.handle)) as http,
            mc.Client("com_test", base_url=BASE, http_client=http) as client,
        ):
            c = client.computers.get("launch-42")
            assert c.wait_for_desktop(timeout=60, poll=0) is c
    else:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(rig.handle)) as http,
            mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
        ):
            ac = await client.computers.get("launch-42")
            assert await ac.wait_for_desktop(timeout=60, poll=0) is ac
    assert len(rig.desktop_probes()) == 4


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_wait_for_desktop_names_the_desktop_session_when_it_times_out(monkeypatch, flavour):
    rig = Rig(linux(), lambda n: httpx.Response(409, json=NO_DESKTOP), tick=1.0)
    rig.install(monkeypatch)
    with pytest.raises(mc.TimeoutError) as caught:
        if flavour == "sync":
            with (
                httpx.Client(transport=httpx.MockTransport(rig.handle)) as http,
                mc.Client("com_test", base_url=BASE, http_client=http) as client,
            ):
                client.computers.get("launch-42").wait_for_desktop(timeout=5, poll=0)
        else:
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(rig.handle)) as http,
                mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
            ):
                ac = await client.computers.get("launch-42")
                await ac.wait_for_desktop(timeout=5, poll=0)
    assert str(caught.value) == (
        "launch-42's desktop session was not active within 5s (it may still be "
        "logging in, or nobody is logged in)"
    )


# A probe whose in-guest session lookup outlived its timeout answers 200 with
# `timed_out` set: no evidence of a session, so it is not an answer.
UNFINISHED = {"exit_code": -1, "timed_out": True, "stdout_b64": "", "stderr_b64": ""}


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_launch_polls_through_a_desktop_probe_that_timed_out(monkeypatch, flavour):
    rig = Rig(
        linux(),
        lambda n: httpx.Response(200, json=UNFINISHED if n == 1 else GUEST),
    )
    rig.install(monkeypatch)
    c = await launch(rig, flavour, poll=0)
    assert c.id == "launch-42"
    assert len(rig.desktop_probes()) == 2


@pytest.mark.parametrize("flavour", FLAVOURS)
async def test_launch_times_out_when_every_desktop_probe_times_out(monkeypatch, flavour):
    rig = Rig(linux(), lambda n: httpx.Response(200, json=UNFINISHED), tick=1.0)
    rig.install(monkeypatch)
    with pytest.raises(mc.TimeoutError) as caught:
        await launch(rig, flavour, timeout=20, poll=0)
    assert "desktop session was not active" in str(caught.value)
    assert len(rig.desktop_probes()) > 1
