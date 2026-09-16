"""Async launch retains the same staged lifecycle as sync launch."""

import asyncio
import json
import sys

import httpx
import pytest
from tests.test_launch import BASE, COMPUTER, CREATE_ARGS, GUEST, Scenario, state, step

import mandala_computer as mc
import mandala_computer._async_computer as computers
import mandala_computer._async_resources as resources


async def test_launch_ready():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=GUEST
            if request.url.path.endswith("/exec")
            else {**COMPUTER, "name": request.method},
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.launch(size="large")
        assert c.id == "launch-42"
        assert c.name == "GET"
    assert json.loads(requests[0].content) == {"size": "large", "start": True}
    assert json.loads(requests[-1].content)["command"] == "exit 0"
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/api/v1/computers"),
        ("GET", "/api/v1/computers/launch-42"),
        ("POST", "/api/v1/computers/launch-42/exec"),
    ]


async def test_deferred_launch_preserves_options_and_remaining_budget(monkeypatch):
    scenario = Scenario(
        [
            step("POST", "", state("building", 0), elapsed=1000),
            step("GET", "/launch-42", state("stopped", 0), elapsed=6),
            step("POST", "/launch-42/start", {"ok": True}, elapsed=2),
            step("GET", "/launch-42", state("stopped", 2048)),
            step("GET", "/launch-42", {**COMPUTER, "name": "ready"}, elapsed=1),
            step("POST", "/launch-42/exec", GUEST),
        ]
    )
    scenario.install(monkeypatch, resources, computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.launch(**CREATE_ARGS, timeout=10, poll=0)
        assert c.id == "launch-42"
        assert c.name == "ready"
    assert not scenario.steps
    assert json.loads(scenario.requests[0].content) == CREATE_ARGS
    assert max(scenario.requests[1].extensions["timeout"].values()) == 10
    assert max(scenario.requests[4].extensions["timeout"].values()) == 2
    assert max(scenario.requests[5].extensions["timeout"].values()) == 1
    assert max(scenario.requests[2].extensions["timeout"].values()) > 10


@pytest.mark.parametrize("status", ["stopped", "suspended"])
@pytest.mark.parametrize("held", [0, 2048, None])
async def test_launch_distinguishes_admitted_start(status, held):
    initial = state(status, held)
    if held is None:
        del initial["running_ram_mb"]
    steps = [step("POST", "", initial)]
    if held == 0:
        steps.extend(
            [
                step("POST", "/launch-42/start", {"ok": True}),
                step("GET", "/launch-42", COMPUTER),
            ]
        )
    steps.extend(
        [
            step("GET", "/launch-42", COMPUTER),
            step("POST", "/launch-42/exec", GUEST),
        ]
    )
    scenario = Scenario(steps)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        await client.computers.launch()
    assert not scenario.steps


@pytest.mark.parametrize("option", ["timeout", "poll"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "10"])
async def test_invalid_wait_options_fail_before_create(option, value):
    scenario = Scenario([])
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(ValueError):
            await client.computers.launch(**{option: value})
    assert not scenario.requests


async def test_guest_timeout_keeps_id_and_cause(monkeypatch):
    scenario = Scenario(
        [
            step("POST", "", COMPUTER),
            step("GET", "/launch-42", COMPUTER),
            step("POST", "/launch-42/exec", {"error": "guest booting"}, 503, 1),
        ]
    )
    scenario.install(monkeypatch, resources, computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.TimeoutError, match="launch-42") as caught:
            await client.computers.launch(timeout=1, poll=0)
    assert isinstance(caught.value.__cause__, mc.UnavailableError)
    assert "guest did not respond" in str(caught.value)
    assert not scenario.steps


@pytest.mark.parametrize("stage", ["build", "start"])
async def test_exhausted_budget_does_not_enter_next_stage(monkeypatch, stage):
    scenario = Scenario(
        [
            step("POST", "", state("building" if stage == "build" else "stopped", 0)),
            step("GET", "/launch-42", state("stopped", 0), elapsed=1)
            if stage == "build"
            else step("POST", "/launch-42/start", {"ok": True}, elapsed=1),
        ]
        + ([step("GET", "/launch-42", COMPUTER)] if stage == "start" else [])
    )
    scenario.install(monkeypatch, resources, computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.TimeoutError, match="launch-42"):
            await client.computers.launch(timeout=1, poll=0)
    assert not scenario.steps


@pytest.mark.parametrize(
    "result, reason",
    [
        ({**state("build-failed", 0), "build_error": "disk copy failed"}, "disk copy failed"),
        ({**state("stopped", 0), "start_error": "boot refused"}, "boot refused"),
    ],
)
async def test_failed_create_stage_is_not_retried(result, reason):
    scenario = Scenario([step("POST", "", result)])
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.MandalaError, match=reason) as caught:
            await client.computers.launch()
    assert not isinstance(caught.value, mc.TimeoutError)
    assert "launch-42" in str(caught.value)
    assert not scenario.steps


@pytest.mark.parametrize("stage", ["create", "start", "running", "guest"])
async def test_permanent_refusal_retains_its_type_and_body(stage):
    initial = state("stopped", 0) if stage == "start" else COMPUTER
    steps = [] if stage == "create" else [step("POST", "", initial)]
    if stage == "guest":
        steps.append(step("GET", "/launch-42", COMPUTER))
    method, suffix = {
        "create": ("POST", ""),
        "start": ("POST", "/launch-42/start"),
        "running": ("GET", "/launch-42"),
        "guest": ("POST", "/launch-42/exec"),
    }[stage]
    steps.append(step(method, suffix, {"error": "key revoked"}, 401))
    scenario = Scenario(steps)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.AuthenticationError) as caught:
            await client.computers.launch()
    assert caught.value.status == 401
    assert caught.value.body == {"error": "key revoked"}
    if stage != "create":
        assert "launch-42" in str(caught.value)
    assert not scenario.steps


async def test_start_transport_timeout_names_the_created_computer():
    original = mc.TimeoutError("request expired")
    scenario = Scenario(
        [
            step("POST", "", state("stopped", 0)),
            step("POST", "/launch-42/start", original),
        ]
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.TimeoutError, match="launch-42") as caught:
            await client.computers.launch()
    assert caught.value is original
    assert not scenario.steps


@pytest.mark.parametrize("stage", ["create", "build", "start", "running", "guest"])
async def test_cancellation_mid_request_preserves_cancelled_error(stage):
    entered = asyncio.Event()
    requests = []

    async def handle(request):
        requests.append(request)
        suffix = request.url.path.removeprefix("/api/v1/computers")
        cancel_here = (
            stage == "create"
            or (stage in ("build", "running") and request.method == "GET")
            or (stage == "start" and suffix.endswith("/start"))
            or (stage == "guest" and suffix.endswith("/exec"))
        )
        if cancel_here:
            entered.set()
            await asyncio.Future()
        initial = (
            state("building", 0)
            if stage == "build"
            else state("stopped", 0)
            if stage == "start"
            else COMPUTER
        )
        return httpx.Response(200, json=initial)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        task = asyncio.create_task(client.computers.launch(poll=0))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel("caller cancelled")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        if sys.version_info >= (3, 11):
            assert str(cancelled.value) == "caller cancelled"
        count = len(requests)
        await asyncio.sleep(0)
        assert len(requests) == count
    assert all(r.method != "DELETE" for r in requests)


async def test_cancellation_during_build_sleep():
    entered = asyncio.Event()
    requests = []

    async def handle(request):
        requests.append(request)
        if request.method == "GET":
            entered.set()
        return httpx.Response(200, json=state("building", 0))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        task = asyncio.create_task(client.computers.launch(poll=100))
        await asyncio.wait_for(entered.wait(), timeout=1)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(requests) == 2


async def test_zero_budget_preserves_id_without_starting():
    scenario = Scenario([step("POST", "", state("stopped", 0))])
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.TimeoutError, match="launch-42"):
            await client.computers.launch(timeout=0)
    assert not scenario.steps


async def test_explicitly_deferred_create_starts_without_reservation_field():
    scenario = Scenario(
        [
            step("POST", "", {"id": "launch-42", "status": "stopped"}),
            step("POST", "/launch-42/start", {"ok": True}),
            step("GET", "/launch-42", COMPUTER),
            step("GET", "/launch-42", COMPUTER),
            step("POST", "/launch-42/exec", GUEST),
        ]
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        await client.computers.launch(start=False)
    assert json.loads(scenario.requests[0].content) == {"start": False}
    assert not scenario.steps


@pytest.mark.parametrize("initial", [True, False])
async def test_admitted_start_is_not_replayed_after_disk_preparation(initial):
    reads = 0
    replayed = False
    requests = []

    def handle(request):
        nonlocal reads, replayed
        requests.append(request)
        path = request.url.path
        if path == "/api/v1/computers":
            return httpx.Response(200, json=state("building", 2048 if initial else 0))
        if path.endswith("/start"):
            replayed = True
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/exec"):
            return httpx.Response(200, json=GUEST)
        reads += 1
        if not initial and reads == 1:
            return httpx.Response(200, json=state("building", 2048))
        return httpx.Response(200, json=COMPUTER if replayed else state("stopped", 0))

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.MandalaError, match="launch-42"):
            await client.computers.launch(poll=0)
    assert not replayed
    assert all(not r.url.path.endswith("/exec") and r.method != "DELETE" for r in requests)


@pytest.mark.parametrize("status", [None, "", ["stopped"], "unrecognized"])
async def test_unknown_build_status_does_not_enter_another_stage(monkeypatch, status):
    row = {**COMPUTER, "status": status, "running_ram_mb": 0}
    scenario = Scenario([step("POST", "", row), step("GET", "/launch-42", row, elapsed=1)])
    scenario.install(monkeypatch, resources, computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.TimeoutError, match="launch-42") as caught:
            await client.computers.launch(timeout=1, poll=0)
    assert "still building" not in str(caught.value)
    assert not scenario.steps


@pytest.mark.parametrize(
    "status, expected", [(401, mc.AuthenticationError), (503, mc.TimeoutError)]
)
async def test_build_refresh_failure_at_deadline(monkeypatch, status, expected):
    scenario = Scenario(
        [
            step("POST", "", state("building", 0)),
            step("GET", "/launch-42", {"error": "refresh failed"}, status, 1),
        ]
    )
    scenario.install(monkeypatch, resources, computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(expected, match="launch-42") as caught:
            await client.computers.launch(timeout=1, poll=0)
    assert "still building" not in str(caught.value)
    assert not scenario.steps


async def test_build_transient_failures_honour_retry_after(monkeypatch):
    scenario = Scenario([])
    scenario.install(monkeypatch, resources, computers)
    requests = []
    reads = 0

    def handle(request):
        nonlocal reads
        requests.append(request)
        path = request.url.path
        if path == "/api/v1/computers":
            return httpx.Response(200, json=state("building", 0))
        if path.endswith("/start"):
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/exec"):
            return httpx.Response(200, json=GUEST)
        reads += 1
        if reads == 1:
            return httpx.Response(429, json={"error": "rate limited"}, headers={"Retry-After": "1"})
        if reads == 2:
            assert scenario.now == 1
            return httpx.Response(503, json={"error": "busy"})
        assert scenario.now == 1.25
        return httpx.Response(200, json=state("stopped", 0) if reads == 3 else COMPUTER)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.launch(start=False, timeout=2, poll=0.25)
        assert c.id == "launch-42"
    assert json.loads(requests[0].content) == {"start": False}
    assert len([r for r in requests if r.url.path.endswith("/start")]) == 1
