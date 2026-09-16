"""Launch composes creation and readiness without replaying mutations."""

import json
from types import SimpleNamespace

import httpx
import pytest

import mandala_computer as mc
import mandala_computer._computer as computers
import mandala_computer._resources as resources

BASE = "https://api.test/api/v1"
COMPUTER = {"id": "launch-42", "status": "running", "running_ram_mb": 1024}
GUEST = {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}


def test_launch_ready():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=GUEST
            if request.url.path.endswith("/exec")
            else {**COMPUTER, "name": request.method},
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.launch(size="large")
        assert c.id == "launch-42"
        assert c.name == "GET"
    assert json.loads(requests[0].content) == {"size": "large", "start": True}
    assert json.loads(requests[-1].content)["command"] == "exit 0"
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/api/v1/computers"),
        ("GET", "/api/v1/computers/launch-42"),
        ("POST", "/api/v1/computers/launch-42/exec"),
    ]


class Scenario:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []
        self.now = 0.0

    def handle(self, request):
        self.requests.append(request)
        method, suffix, payload, status, elapsed = self.steps.pop(0)
        assert (request.method, request.url.path) == (method, "/api/v1/computers" + suffix)
        self.now += elapsed
        if isinstance(payload, BaseException):
            raise payload
        return httpx.Response(status, json=payload)

    def install(self, monkeypatch, resource_module, computer_module):
        def sleep(delay):
            self.now += delay

        async def async_sleep(delay):
            sleep(delay)

        clock = SimpleNamespace(monotonic=lambda: self.now, sleep=sleep)
        monkeypatch.setattr(resource_module, "time", clock)
        monkeypatch.setattr(computer_module, "time", clock)
        if hasattr(computer_module, "asyncio"):
            monkeypatch.setattr(computer_module, "asyncio", SimpleNamespace(sleep=async_sleep))
            monkeypatch.setattr(resource_module, "asyncio", SimpleNamespace(sleep=async_sleep))


def state(status, held):
    return {**COMPUTER, "status": status, "running_ram_mb": held}


def step(method, suffix, payload, status=200, elapsed=0.0):
    return (method, suffix, payload, status, elapsed)


CREATE_ARGS = {
    "name": "example",
    "template": "base",
    "template_transfer": " opaque-token ",
    "cpu": 2,
    "ram_mb": 4096,
    "disk_gb": 40,
    "resolution": "1920x1080",
    "start": False,
}


def test_deferred_launch_preserves_options_and_remaining_budget(monkeypatch):
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
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.launch(**CREATE_ARGS, timeout=10, poll=0)
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
def test_launch_distinguishes_admitted_start(status, held):
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
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        client.computers.launch()
    assert not scenario.steps


@pytest.mark.parametrize("option", ["timeout", "poll"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "10"])
def test_invalid_wait_options_fail_before_create(option, value):
    scenario = Scenario([])
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(ValueError),
    ):
        client.computers.launch(**{option: value})
    assert not scenario.requests


def test_guest_timeout_keeps_id_and_cause(monkeypatch):
    scenario = Scenario(
        [
            step("POST", "", COMPUTER),
            step("GET", "/launch-42", COMPUTER),
            step("POST", "/launch-42/exec", {"error": "guest booting"}, 503, 1),
        ]
    )
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.TimeoutError, match="launch-42") as caught,
    ):
        client.computers.launch(timeout=1, poll=0)
    assert isinstance(caught.value.__cause__, mc.UnavailableError)
    assert "guest did not respond" in str(caught.value)
    assert not scenario.steps


@pytest.mark.parametrize("stage", ["build", "start"])
def test_exhausted_budget_does_not_enter_next_stage(monkeypatch, stage):
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
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.TimeoutError, match="launch-42"),
    ):
        client.computers.launch(timeout=1, poll=0)
    assert not scenario.steps


@pytest.mark.parametrize(
    "result, reason",
    [
        ({**state("build-failed", 0), "build_error": "disk copy failed"}, "disk copy failed"),
        ({**state("stopped", 0), "start_error": "boot refused"}, "boot refused"),
    ],
)
def test_failed_create_stage_is_not_retried(result, reason):
    scenario = Scenario([step("POST", "", result)])
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.MandalaError, match=reason) as caught,
    ):
        client.computers.launch()
    assert not isinstance(caught.value, mc.TimeoutError)
    assert "launch-42" in str(caught.value)
    assert not scenario.steps


@pytest.mark.parametrize("stage", ["create", "start", "running", "guest"])
def test_permanent_refusal_retains_its_type_and_body(stage):
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
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.AuthenticationError) as caught,
    ):
        client.computers.launch()
    assert caught.value.status == 401
    assert caught.value.body == {"error": "key revoked"}
    if stage != "create":
        assert "launch-42" in str(caught.value)
    assert not scenario.steps


def test_start_transport_timeout_names_the_created_computer():
    original = mc.TimeoutError("request expired")
    scenario = Scenario(
        [
            step("POST", "", state("stopped", 0)),
            step("POST", "/launch-42/start", original),
        ]
    )
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.TimeoutError, match="launch-42") as caught,
    ):
        client.computers.launch()
    assert caught.value is original
    assert not scenario.steps


@pytest.mark.parametrize("stage", ["build", "start", "running", "guest"])
def test_interruption_propagates_without_later_requests(stage):
    original = KeyboardInterrupt("interrupted")
    initial = (
        state("building", 0)
        if stage == "build"
        else state("stopped", 0)
        if stage == "start"
        else COMPUTER
    )
    steps = [step("POST", "", initial)]
    if stage == "guest":
        steps.append(step("GET", "/launch-42", COMPUTER))
    method, suffix = (
        ("POST", "/launch-42/start")
        if stage == "start"
        else ("POST", "/launch-42/exec")
        if stage == "guest"
        else ("GET", "/launch-42")
    )
    steps.append(step(method, suffix, original))
    scenario = Scenario(steps)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle), timeout=60) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(KeyboardInterrupt) as caught,
    ):
        client.computers.launch(poll=0)
    assert caught.value is original
    assert not scenario.steps


def test_zero_budget_preserves_id_without_starting():
    scenario = Scenario([step("POST", "", state("stopped", 0))])
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.TimeoutError, match="launch-42"),
    ):
        client.computers.launch(timeout=0)
    assert not scenario.steps


def test_explicitly_deferred_create_starts_without_reservation_field():
    scenario = Scenario(
        [
            step("POST", "", {"id": "launch-42", "status": "stopped"}),
            step("POST", "/launch-42/start", {"ok": True}),
            step("GET", "/launch-42", COMPUTER),
            step("GET", "/launch-42", COMPUTER),
            step("POST", "/launch-42/exec", GUEST),
        ]
    )
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        client.computers.launch(start=False)
    assert json.loads(scenario.requests[0].content) == {"start": False}
    assert not scenario.steps


@pytest.mark.parametrize("initial", [True, False])
def test_admitted_start_is_not_replayed_after_disk_preparation(initial):
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

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.MandalaError, match="launch-42"),
    ):
        client.computers.launch(poll=0)
    assert not replayed
    assert all(not r.url.path.endswith("/exec") and r.method != "DELETE" for r in requests)


@pytest.mark.parametrize("status", [None, "", ["stopped"], "unrecognized"])
def test_unknown_build_status_does_not_enter_another_stage(monkeypatch, status):
    row = {**COMPUTER, "status": status, "running_ram_mb": 0}
    scenario = Scenario([step("POST", "", row), step("GET", "/launch-42", row, elapsed=1)])
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.TimeoutError, match="launch-42") as caught,
    ):
        client.computers.launch(timeout=1, poll=0)
    assert "still building" not in str(caught.value)
    assert not scenario.steps


@pytest.mark.parametrize(
    "status, expected", [(401, mc.AuthenticationError), (503, mc.TimeoutError)]
)
def test_build_refresh_failure_at_deadline(monkeypatch, status, expected):
    scenario = Scenario(
        [
            step("POST", "", state("building", 0)),
            step("GET", "/launch-42", {"error": "refresh failed"}, status, 1),
        ]
    )
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(expected, match="launch-42") as caught,
    ):
        client.computers.launch(timeout=1, poll=0)
    assert "still building" not in str(caught.value)
    assert not scenario.steps


def test_build_transient_failures_honour_retry_after(monkeypatch):
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

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.launch(start=False, timeout=2, poll=0.25)
        assert c.id == "launch-42"
    assert json.loads(requests[0].content) == {"start": False}
    assert len([r for r in requests if r.url.path.endswith("/start")]) == 1
