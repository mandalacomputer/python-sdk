"""Launch waits for bound secrets to land, and write_file reports what it wrote (OPL-5048)."""

import json

import httpx
import pytest
from tests.test_launch import BASE, COMPUTER, GUEST, Scenario, step

import mandala_computer as mc
import mandala_computer._async_computer as async_computers
import mandala_computer._async_resources as async_resources
import mandala_computer._computer as computers
import mandala_computer._resources as resources

BINDING = {"secret_id": "csec-0123456789abcdef", "revision_id": "csr-1", "env": "TOKEN"}
RECEIPT = {"generation": 1, "applied_at": "2026-09-25T00:00:00Z", "revisions": {}}
FAILED = "a secret could not be read"


def bound(delivering, **extra):
    row = {**COMPUTER, "secrets": [BINDING], "secrets_generation": 1, **extra}
    if delivering is not None:
        row["secrets_delivering"] = delivering
    return row


def launch_steps():
    return [
        step("POST", "", bound(True)),
        step("GET", "/launch-42", bound(True)),
        step("POST", "/launch-42/exec", GUEST),
        step("GET", "/launch-42", bound(True)),
        step("GET", "/launch-42", bound(False, secrets_applied=RECEIPT)),
    ]


def failed_steps():
    return [
        step("POST", "", bound(True)),
        step("GET", "/launch-42", bound(True)),
        step("POST", "/launch-42/exec", GUEST),
        step(
            "GET",
            "/launch-42",
            bound(False, secrets_error=FAILED, status="stopped", running_ram_mb=0),
        ),
    ]


FAILED_MESSAGE = (
    f"launch of launch-42 failed: launch-42's secrets were not delivered: {FAILED}. "
    "The platform stopped it; call start() to try again"
)


# --- sync ---------------------------------------------------------------------


def run_sync(monkeypatch, steps, **launch):
    scenario = Scenario(steps)
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        return scenario, client.computers.launch(poll=0.5, **launch)


def test_launch_waits_for_the_secrets_to_land(monkeypatch):
    scenario, c = run_sync(
        monkeypatch, launch_steps(), secrets=[{"secret_id": BINDING["secret_id"], "env": "TOKEN"}]
    )
    assert not scenario.steps
    assert c.secrets_delivering is False
    assert json.loads(scenario.requests[0].content)["secrets"] == [
        {"secret_id": BINDING["secret_id"], "env": "TOKEN"}
    ]


def test_launch_waits_on_a_binding_the_record_reports(monkeypatch):
    scenario, _ = run_sync(monkeypatch, launch_steps())
    assert not scenario.steps


def test_launch_raises_naming_why_when_the_delivery_failed(monkeypatch):
    with pytest.raises(mc.MandalaError) as caught:
        run_sync(monkeypatch, failed_steps())
    assert not isinstance(caught.value, mc.TimeoutError)
    assert str(caught.value) == FAILED_MESSAGE


def test_launch_adds_no_request_with_nothing_bound(monkeypatch):
    scenario, _ = run_sync(
        monkeypatch,
        [
            step("POST", "", COMPUTER),
            step("GET", "/launch-42", COMPUTER),
            step("POST", "/launch-42/exec", GUEST),
        ],
    )
    assert not scenario.steps


def wait_sync(monkeypatch, steps, **wait):
    scenario = Scenario(steps)
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.get("launch-42")
        return scenario, c.wait_for_secrets(**wait)


def test_wait_reads_again_even_when_the_handle_says_delivered(monkeypatch):
    with pytest.raises(mc.TimeoutError, match="secrets were still being delivered after 2s"):
        wait_sync(
            monkeypatch,
            [
                step("GET", "/launch-42", bound(False)),
                step("GET", "/launch-42", bound(True)),
                step("GET", "/launch-42", bound(True)),
            ],
            timeout=2,
            poll=1,
        )


def test_wait_answers_at_once_for_nothing_bound(monkeypatch):
    scenario, _ = wait_sync(
        monkeypatch, [step("GET", "/launch-42", COMPUTER), step("GET", "/launch-42", COMPUTER)]
    )
    assert not scenario.steps


def test_wait_refuses_a_stopped_computer_nobody_is_starting(monkeypatch):
    stopped = bound(False, status="stopped", running_ram_mb=0)
    with pytest.raises(mc.MandalaError, match="secrets are delivered only as it starts"):
        wait_sync(
            monkeypatch, [step("GET", "/launch-42", stopped), step("GET", "/launch-42", stopped)]
        )


def test_wait_rides_through_an_admitted_start(monkeypatch):
    admitted = bound(False, status="stopped", running_ram_mb=1024)
    scenario, c = wait_sync(
        monkeypatch,
        [
            step("GET", "/launch-42", admitted),
            step("GET", "/launch-42", admitted),
            step("GET", "/launch-42", bound(False)),
        ],
        poll=0.5,
    )
    assert c.status == "running"
    assert not scenario.steps


def test_wait_reads_the_receipt_on_a_platform_without_the_field(monkeypatch):
    scenario, c = wait_sync(
        monkeypatch,
        [
            step("GET", "/launch-42", bound(None)),
            step("GET", "/launch-42", bound(None)),
            step("GET", "/launch-42", bound(None, secrets_applied=RECEIPT)),
        ],
        poll=0.5,
    )
    assert c.secrets_delivering is None
    assert not scenario.steps


def test_wait_rides_out_a_host_that_cannot_be_reached(monkeypatch):
    scenario, _ = wait_sync(
        monkeypatch,
        [
            step("GET", "/launch-42", bound(True)),
            step("GET", "/launch-42", {"error": "host unreachable"}, status=503),
            step("GET", "/launch-42", bound(False)),
        ],
        poll=0.5,
    )
    assert not scenario.steps


# --- async --------------------------------------------------------------------


async def run_async(monkeypatch, steps, **launch):
    scenario = Scenario(steps)
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        return scenario, await client.computers.launch(poll=0.5, **launch)


async def test_async_launch_waits_for_the_secrets_to_land(monkeypatch):
    scenario, c = await run_async(
        monkeypatch, launch_steps(), secrets=[{"secret_id": BINDING["secret_id"], "env": "TOKEN"}]
    )
    assert not scenario.steps
    assert c.secrets_delivering is False


async def test_async_launch_raises_naming_why_when_the_delivery_failed(monkeypatch):
    with pytest.raises(mc.MandalaError) as caught:
        await run_async(monkeypatch, failed_steps())
    assert str(caught.value) == FAILED_MESSAGE


async def test_async_wait_refuses_a_stopped_computer_nobody_is_starting(monkeypatch):
    stopped = bound(False, status="stopped", running_ram_mb=0)
    scenario = Scenario([step("GET", "/launch-42", stopped), step("GET", "/launch-42", stopped)])
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.get("launch-42")
        with pytest.raises(mc.MandalaError, match="secrets are delivered only as it starts"):
            await c.wait_for_secrets()


# --- write_file ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [({"bytes": 5}, 5), ({"bytes": 5.0}, 5), ({}, None), ({"bytes": "5"}, None), ([], None)],
)
def test_write_file_returns_what_the_platform_says_it_wrote(answer, expected):
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=COMPUTER)
        return httpx.Response(200, json=answer)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.get("launch-42")
        assert c.write_file("/tmp/a.txt", "hello") == expected


async def test_async_write_file_returns_what_the_platform_says_it_wrote():
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=COMPUTER)
        return httpx.Response(200, json={"bytes": 5})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.get("launch-42")
        assert await c.write_file("/tmp/a.txt", "hello") == 5


def test_write_file_says_nothing_for_an_empty_answer():
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=COMPUTER)
        return httpx.Response(204)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.get("launch-42")
        assert c.write_file("/tmp/a.txt", b"hello") is None
