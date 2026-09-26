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


def unreported(**extra):
    """A running record that leaves the ``secrets`` group out, as one served
    without its host's answer does."""
    row = bound(False, **extra)
    del row["secrets"]
    return row


def unreported_steps():
    return [
        step("POST", "", bound(True)),
        step("GET", "/launch-42", bound(True)),
        step("POST", "/launch-42/exec", GUEST),
        step("GET", "/launch-42", unreported()),
        step("GET", "/launch-42", unreported()),
        step("GET", "/launch-42", bound(False, secrets_applied=RECEIPT)),
    ]


def silent_admission_steps():
    """Stopped, with no ``running_ram_mb``: a host that did not say whether a
    start is admitted — then running."""
    silent = bound(False, status="stopped")
    del silent["running_ram_mb"]
    return [
        step("GET", "/launch-42", silent),
        step("GET", "/launch-42", silent),
        step("GET", "/launch-42", bound(False)),
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


def test_wait_rides_through_a_host_that_does_not_say_what_it_admitted(monkeypatch):
    # Absent is "cannot tell", not zero: refusing would tell the caller to
    # start a computer that may already be starting.
    scenario, c = wait_sync(monkeypatch, silent_admission_steps(), poll=0.5)
    assert c.status == "running"
    assert not scenario.steps


def test_launch_waits_past_a_read_that_leaves_the_bindings_out(monkeypatch):
    scenario, c = run_sync(
        monkeypatch,
        unreported_steps(),
        secrets=[{"secret_id": BINDING["secret_id"], "env": "TOKEN"}],
    )
    assert not scenario.steps
    assert c.secrets_applied is not None


def test_wait_times_out_saying_the_bindings_went_unreported(monkeypatch):
    with pytest.raises(mc.TimeoutError) as caught:
        wait_sync(
            monkeypatch,
            [step("GET", "/launch-42", unreported()) for _ in range(4)],
            timeout=2,
            poll=1,
            expect_secrets=True,
        )
    assert str(caught.value) == (
        "launch-42 was read for 2s without reporting its bindings, so whether its secrets "
        "arrived is unknown"
    )


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


def test_wait_refuses_nothing_admitted_even_when_the_bindings_are_left_out(monkeypatch):
    # expect_secrets waits past a read that omits the bindings, but not one
    # that says outright nothing is starting: that is an answer whatever is
    # bound. The long timeout is the test: the scenario would run dry first.
    stopped = unreported(status="stopped", running_ram_mb=0)
    with pytest.raises(mc.MandalaError) as caught:
        wait_sync(
            monkeypatch,
            [step("GET", "/launch-42", stopped), step("GET", "/launch-42", stopped)],
            timeout=60,
            expect_secrets=True,
        )
    assert not isinstance(caught.value, mc.TimeoutError)
    assert str(caught.value) == (
        "launch-42 is 'stopped', and secrets are delivered only as it starts: call start()"
    )


def test_wait_still_waits_past_left_out_bindings_when_admission_is_unsaid(monkeypatch):
    silent = unreported(status="stopped")
    del silent["running_ram_mb"]
    with pytest.raises(mc.TimeoutError, match="without reporting its bindings"):
        wait_sync(
            monkeypatch,
            [step("GET", "/launch-42", silent) for _ in range(4)],
            timeout=2,
            poll=1,
            expect_secrets=True,
        )


START_ERROR = "no host had room"
START_FAILED_MESSAGE = (
    "launch-42 is stopped after it failed to start, so its secrets were not delivered: "
    f"{START_ERROR}. Call start() to try again"
)


def silent_stopped(**extra):
    """Stopped with bindings and no ``running_ram_mb``: the reads after a
    create whose first start failed, from a host that does not report the
    pool."""
    row = bound(False, status="stopped", **extra)
    if "running_ram_mb" not in extra:
        del row["running_ram_mb"]
    return row


def created_with_start_error():
    return step("POST", "", {"computer": silent_stopped(), "start_error": START_ERROR})


def create_then_wait_sync(monkeypatch, steps, **wait):
    scenario = Scenario([created_with_start_error(), *steps])
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.create(secrets=[{"secret_id": BINDING["secret_id"], "env": "TOKEN"}])
        assert c.start_error == START_ERROR
        return scenario, c.wait_for_secrets(**wait)


def test_wait_refuses_a_create_whose_start_failed_rather_than_timing_out(monkeypatch):
    with pytest.raises(mc.MandalaError) as caught:
        create_then_wait_sync(
            monkeypatch, [step("GET", "/launch-42", silent_stopped())], timeout=60
        )
    assert not isinstance(caught.value, mc.TimeoutError)
    assert str(caught.value) == START_FAILED_MESSAGE


def test_wait_refuses_a_failed_create_when_the_bindings_are_left_out_too(monkeypatch):
    row = silent_stopped()
    del row["secrets"]
    with pytest.raises(mc.MandalaError) as caught:
        create_then_wait_sync(
            monkeypatch, [step("GET", "/launch-42", row)], timeout=60, expect_secrets=True
        )
    assert str(caught.value) == START_FAILED_MESSAGE


def test_wait_retires_a_create_failure_once_a_start_is_admitted(monkeypatch):
    # A reservation, then a read that does not report the pool: the old
    # failure belongs to an earlier attempt and must not refuse this one.
    scenario, c = create_then_wait_sync(
        monkeypatch,
        [
            step("GET", "/launch-42", silent_stopped(running_ram_mb=1024)),
            step("GET", "/launch-42", silent_stopped()),
            step("GET", "/launch-42", bound(False)),
        ],
        poll=0.5,
    )
    assert c.status == "running"
    assert not scenario.steps


def restart_steps():
    """A restart comes back running before its secrets are delivered again;
    ``secrets_delivering`` reads true until they are."""
    return [
        step("GET", "/launch-42", bound(False, secrets_applied=RECEIPT)),
        step("POST", "/launch-42/restart", {}),
        step("GET", "/launch-42", bound(True)),
        step("GET", "/launch-42", bound(True)),
        step("GET", "/launch-42", bound(True)),
        step("GET", "/launch-42", bound(False, secrets_applied=RECEIPT)),
    ]


def test_wait_after_restart_waits_until_the_secrets_are_applied_again(monkeypatch):
    scenario = Scenario(restart_steps())
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.get("launch-42")
        c.restart()
        assert c.secrets_delivering is True
        c.wait_for_secrets(poll=0.5)
    assert c.secrets_delivering is False
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


async def test_async_launch_waits_past_a_read_that_leaves_the_bindings_out(monkeypatch):
    scenario, _ = await run_async(
        monkeypatch,
        unreported_steps(),
        secrets=[{"secret_id": BINDING["secret_id"], "env": "TOKEN"}],
    )
    assert not scenario.steps


async def test_async_wait_rides_through_a_host_that_does_not_say_what_it_admitted(monkeypatch):
    scenario = Scenario(silent_admission_steps())
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.get("launch-42")
        await c.wait_for_secrets(poll=0.5)
    assert c.status == "running"
    assert not scenario.steps


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


async def test_async_wait_refuses_nothing_admitted_even_when_the_bindings_are_left_out(
    monkeypatch,
):
    stopped = unreported(status="stopped", running_ram_mb=0)
    scenario = Scenario([step("GET", "/launch-42", stopped), step("GET", "/launch-42", stopped)])
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.get("launch-42")
        with pytest.raises(mc.MandalaError, match="secrets are delivered only as it starts"):
            await c.wait_for_secrets(timeout=60, expect_secrets=True)


async def create_then_wait_async(monkeypatch, steps, **wait):
    scenario = Scenario([created_with_start_error(), *steps])
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.create(
            secrets=[{"secret_id": BINDING["secret_id"], "env": "TOKEN"}]
        )
        return scenario, await c.wait_for_secrets(**wait)


async def test_async_wait_refuses_a_create_whose_start_failed(monkeypatch):
    with pytest.raises(mc.MandalaError) as caught:
        await create_then_wait_async(
            monkeypatch, [step("GET", "/launch-42", silent_stopped())], timeout=60
        )
    assert not isinstance(caught.value, mc.TimeoutError)
    assert str(caught.value) == START_FAILED_MESSAGE


async def test_async_wait_retires_a_create_failure_once_a_start_is_admitted(monkeypatch):
    scenario, c = await create_then_wait_async(
        monkeypatch,
        [
            step("GET", "/launch-42", silent_stopped(running_ram_mb=1024)),
            step("GET", "/launch-42", silent_stopped()),
            step("GET", "/launch-42", bound(False)),
        ],
        poll=0.5,
    )
    assert c.status == "running"
    assert not scenario.steps


async def test_async_wait_after_restart_waits_until_the_secrets_are_applied_again(monkeypatch):
    scenario = Scenario(restart_steps())
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.get("launch-42")
        await c.restart()
        assert c.secrets_delivering is True
        await c.wait_for_secrets(poll=0.5)
    assert c.secrets_delivering is False
    assert not scenario.steps


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
