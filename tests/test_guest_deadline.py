"""Guest readiness preserves permanent refusals even at the wait deadline."""

from types import SimpleNamespace

import httpx
import pytest

import mandala_computer as mc
import mandala_computer._async_computer as async_computer
import mandala_computer._computer as sync_computer

BASE = "https://api.test/api/v1"
COMPUTER = {"id": "vm-1", "status": "running"}
REFUSALS = [
    (401, mc.AuthenticationError),
    (402, mc.PlanLimitError),
    (403, mc.PermissionDeniedError),
    (404, mc.NotFoundError),
    (526, mc.OriginTLSError),
]


class Probe:
    def __init__(self, status: int, answered_at: float, *, refresh_failure: bool = False) -> None:
        self.now = 0.0
        self.status = status
        self.answered_at = answered_at
        self.refresh_failure = refresh_failure
        self.requests: list[httpx.Request] = []
        self.errors: list[mc.APIError] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if len(self.requests) == 1:
            assert request.method == "GET"
            return httpx.Response(200, json=COMPUTER)
        if self.refresh_failure and len(self.requests) == 3:
            assert request.method == "GET"
            self.now = 1.0
            return httpx.Response(400, json={"error": "not running"})
        assert request.method == "POST"
        assert request.url.path == "/api/v1/computers/vm-1/exec"
        self.now = self.answered_at
        if self.status == 0:
            raise httpx.ConnectError("connection lost", request=request)
        return httpx.Response(
            self.status,
            json={"error": "not running" if self.status == 400 else "refused"},
            headers={"Retry-After": "12"} if self.status == 429 else {},
        )

    def install(self, monkeypatch: pytest.MonkeyPatch, client, module) -> None:
        # Patch only this loop's clock, leaving asyncio and HTTP timing alone.
        monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: self.now))
        decode = client._t._error

        def record_error(response: httpx.Response) -> mc.APIError:
            error = decode(response)
            self.errors.append(error)
            return error

        monkeypatch.setattr(client._t, "_error", record_error)

    def assert_one_probe(self) -> None:
        # The first GET constructs the public handle. No refresh or retry may
        # follow a probe that has already spent the budget.
        assert len(self.requests) == 2
        assert max(self.requests[-1].extensions["timeout"].values()) <= 1


@pytest.mark.parametrize("answered_at", [1.0, 1.01], ids=["at-deadline", "after-deadline"])
@pytest.mark.parametrize("status, expected", REFUSALS)
def test_guest_deadline_preserves_permanent_refusal(monkeypatch, answered_at, status, expected):
    probe = Probe(status, answered_at)
    with (
        httpx.Client(transport=httpx.MockTransport(probe.handle)) as http,
        mc.Client("gck_test", base_url=BASE, http_client=http) as client,
    ):
        computer = client.computers.get("vm-1")
        probe.install(monkeypatch, client, sync_computer)
        with pytest.raises(expected) as caught:
            computer.wait_for_guest(timeout=1, poll=0)
    assert caught.value is probe.errors[0]
    assert caught.value.status == status
    assert caught.value.body == {"error": "refused"}
    probe.assert_one_probe()


def test_guest_refresh_400_remains_fatal_at_deadline(monkeypatch):
    # Identical 400 bodies have different policies: the guest POST may be
    # booting, but a failed control-plane GET is a request refusal.
    probe = Probe(400, 0.25, refresh_failure=True)
    with (
        httpx.Client(transport=httpx.MockTransport(probe.handle)) as http,
        mc.Client("gck_test", base_url=BASE, http_client=http) as client,
    ):
        computer = client.computers.get("vm-1")
        probe.install(monkeypatch, client, sync_computer)
        with pytest.raises(mc.APIError) as caught:
            computer.wait_for_guest(timeout=1, poll=0)
    assert caught.value is probe.errors[1]
    assert caught.value.status == 400
    assert [request.method for request in probe.requests] == ["GET", "POST", "GET"]


async def test_async_guest_refresh_400_remains_fatal_at_deadline(monkeypatch):
    probe = Probe(400, 0.25, refresh_failure=True)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(probe.handle)) as http,
        mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
    ):
        computer = await client.computers.get("vm-1")
        probe.install(monkeypatch, client, async_computer)
        with pytest.raises(mc.APIError) as caught:
            await computer.wait_for_guest(timeout=1, poll=0)
    assert caught.value is probe.errors[1]
    assert caught.value.status == 400
    assert [request.method for request in probe.requests] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("answered_at", [1.0, 1.01], ids=["at-deadline", "after-deadline"])
@pytest.mark.parametrize("status, expected", REFUSALS)
async def test_async_guest_deadline_preserves_permanent_refusal(
    monkeypatch, answered_at, status, expected
):
    probe = Probe(status, answered_at)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(probe.handle)) as http,
        mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
    ):
        computer = await client.computers.get("vm-1")
        probe.install(monkeypatch, client, async_computer)
        with pytest.raises(expected) as caught:
            await computer.wait_for_guest(timeout=1, poll=0)
    assert caught.value is probe.errors[0]
    assert caught.value.status == status
    assert caught.value.body == {"error": "refused"}
    probe.assert_one_probe()


@pytest.mark.parametrize("answered_at", [1.0, 1.01], ids=["at-deadline", "after-deadline"])
@pytest.mark.parametrize(
    "status, expected",
    [
        (400, mc.APIError),
        (429, mc.RateLimitError),
        (503, mc.UnavailableError),
        (0, mc.ConnectionError),
    ],
    ids=["guest-not-running", "rate-limit", "server-failure", "transport-failure"],
)
def test_guest_deadline_still_wraps_transient_failure(monkeypatch, answered_at, status, expected):
    probe = Probe(status, answered_at)
    with (
        httpx.Client(transport=httpx.MockTransport(probe.handle)) as http,
        mc.Client("gck_test", base_url=BASE, http_client=http) as client,
    ):
        computer = client.computers.get("vm-1")
        probe.install(monkeypatch, client, sync_computer)
        with pytest.raises(mc.TimeoutError) as caught:
            computer.wait_for_guest(timeout=1, poll=0)
    assert isinstance(caught.value.__cause__, expected)
    if status:
        assert caught.value.__cause__ is probe.errors[0]
    probe.assert_one_probe()


@pytest.mark.parametrize("answered_at", [1.0, 1.01], ids=["at-deadline", "after-deadline"])
@pytest.mark.parametrize(
    "status, expected",
    [
        (400, mc.APIError),
        (429, mc.RateLimitError),
        (503, mc.UnavailableError),
        (0, mc.ConnectionError),
    ],
    ids=["guest-not-running", "rate-limit", "server-failure", "transport-failure"],
)
async def test_async_guest_deadline_still_wraps_transient_failure(
    monkeypatch, answered_at, status, expected
):
    probe = Probe(status, answered_at)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(probe.handle)) as http,
        mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
    ):
        computer = await client.computers.get("vm-1")
        probe.install(monkeypatch, client, async_computer)
        with pytest.raises(mc.TimeoutError) as caught:
            await computer.wait_for_guest(timeout=1, poll=0)
    assert isinstance(caught.value.__cause__, expected)
    if status:
        assert caught.value.__cause__ is probe.errors[0]
    probe.assert_one_probe()
