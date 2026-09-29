"""Launch waits for the desktop session, on both handles.

The guest agent answers a few seconds before the desktop user is logged in, and
a desktop exec sent in between is refused with a 409 carrying no ``reason``.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from tests.test_launch import BASE, COMPUTER, GUEST

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

    def handle(self, request):
        self.requests.append(request)
        self.now += self.tick
        if is_desktop_probe(request):
            return self.probe(len(self.desktop_probes()))
        if request.url.path.endswith("/exec"):
            return httpx.Response(200, json=GUEST)
        return httpx.Response(200, json=self.record)

    def desktop_probes(self):
        return [r for r in self.requests if is_desktop_probe(r)]

    def install(self, monkeypatch):
        def sleep(delay):
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
