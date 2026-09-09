"""Start serialization and refresh against a resume-aware platform fake (OPL-4624)."""

from __future__ import annotations

import httpx
import pytest
import respx
from tests.test_client import BASE, COMPUTER

import mandala_computer as mc


@respx.mock
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("options", [{}, {"resume_only": False}, {"resume_only": True}])
@pytest.mark.parametrize("before", ["suspended", "stopped", "running"])
async def test_start_query_and_refreshed_state(asynchronous, options, before):
    # Model the supported server contract, not evidence of deployed support.
    # Read the actual request so dropping the flag also changes the observed
    # state: a stopped computer cold-boots unless resume_only=true was sent.
    state = {**COMPUTER, "status": before}

    def start_computer(request):
        if request.url.params.get("resume_only") != "true" or state["status"] == "suspended":
            state["status"] = "running"
        return httpx.Response(200, json={"ok": True})

    start = respx.post(f"{BASE}/computers/vm-1/start").mock(side_effect=start_computer)
    refresh = respx.get(f"{BASE}/computers/vm-1").mock(
        side_effect=lambda request: httpx.Response(200, json=state)
    )
    if asynchronous:
        async with mc.AsyncClient("gck_test", base_url=BASE) as client:
            computer = mc.AsyncComputer(client._t, {**COMPUTER, "status": before})
            result = await computer.start(**options)
    else:
        with mc.Client("gck_test", base_url=BASE) as client:
            computer = mc.Computer(client._t, {**COMPUTER, "status": before})
            result = computer.start(**options)

    assert result is computer
    assert computer.status == (
        "stopped" if before == "stopped" and options.get("resume_only") else "running"
    )
    assert start.call_count == refresh.call_count == 1
    request = start.calls.last.request
    assert dict(request.url.params) == (
        {"resume_only": "true"} if options.get("resume_only") else {}
    )
    assert request.content == b""
    assert [call.request.method for call in respx.calls] == ["POST", "GET"]


@respx.mock
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("value", ["true", "false", "", 0, 1, None, [], {}])
async def test_start_rejects_non_boolean_resume_only_before_dispatch(asynchronous, value):
    with pytest.raises(ValueError, match="resume_only must be True or False"):
        if asynchronous:
            async with mc.AsyncClient("gck_test", base_url=BASE) as client:
                await mc.AsyncComputer(client._t, COMPUTER).start(resume_only=value)
        else:
            with mc.Client("gck_test", base_url=BASE) as client:
                mc.Computer(client._t, COMPUTER).start(resume_only=value)
    assert len(respx.calls) == 0


@respx.mock
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_resume_failure_does_not_refresh_or_change_cached_state(asynchronous):
    start = respx.post(f"{BASE}/computers/vm-1/start").mock(
        httpx.Response(409, json={"error": "capture in progress"})
    )
    with pytest.raises(mc.ConflictError):
        if asynchronous:
            async with mc.AsyncClient("gck_test", base_url=BASE) as client:
                computer = mc.AsyncComputer(client._t, {**COMPUTER, "status": "suspended"})
                await computer.start(resume_only=True)
        else:
            with mc.Client("gck_test", base_url=BASE) as client:
                computer = mc.Computer(client._t, {**COMPUTER, "status": "suspended"})
                computer.start(resume_only=True)
    assert computer.status == "suspended"
    assert start.call_count == len(respx.calls) == 1


@respx.mock
@pytest.mark.parametrize("before", ["stopped", "suspended"])
async def test_wait_until_running_waits_for_a_start_already_admitted(before) -> None:
    """OPL-4629. A start that has been admitted holds its memory before QEMU
    exists, and `status` reports what the computer WAS for the whole of that
    load — stopped for a cold boot, suspended for a resume, whose session record
    is spent only on the way out of a start that worked.

    Refusing there tells a caller to make a call they have already made. The
    long timeout is the test: it must not be reached either, since the machine
    does come up.
    """
    polls = {"n": 0}

    def read(request):
        polls["n"] += 1
        if polls["n"] < 3:
            return httpx.Response(
                200,
                json={**COMPUTER, "status": before, "running_ram_mb": COMPUTER["ram_mb"]},
            )
        return httpx.Response(200, json=COMPUTER)

    respx.get(f"{BASE}/computers/vm-1").mock(side_effect=read)
    with mc.Client("gck_test", base_url=BASE) as client:
        computer = mc.Computer(client._t, {**COMPUTER, "status": before})
        assert computer.wait_until_running(timeout=30, poll=0).status == "running"


@respx.mock
async def test_wait_until_running_waits_when_the_platform_did_not_say() -> None:
    """A host that could not be reached, or one too old to report the field, has
    not said nothing is coming — it has said nothing. Refusing on that would be
    inventing the sentence, so it waits, which costs a timeout rather than a
    machine.
    """
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "stopped"})
    )
    with mc.Client("gck_test", base_url=BASE) as client:
        computer = mc.Computer(client._t, {**COMPUTER, "status": "stopped"})
        with pytest.raises(mc.TimeoutError):
            computer.wait_until_running(timeout=0.05, poll=0)


@respx.mock
async def test_wait_for_guest_probes_a_start_already_admitted() -> None:
    """The guest of a machine that is coming up answers shortly; the refusal is
    only for one the platform says it is holding nothing for.
    """
    probes = {"n": 0}

    def probe(request):
        probes["n"] += 1
        if probes["n"] < 2:
            return httpx.Response(409, json={"error": "the agent is not up yet"})
        return httpx.Response(200, json={"exit_code": 0, "stdout_b64": "", "stderr_b64": ""})

    respx.post(f"{BASE}/computers/vm-1/exec").mock(side_effect=probe)
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(
            200, json={**COMPUTER, "status": "stopped", "running_ram_mb": COMPUTER["ram_mb"]}
        )
    )
    with mc.Client("gck_test", base_url=BASE) as client:
        computer = mc.Computer(
            client._t, {**COMPUTER, "status": "stopped", "running_ram_mb": COMPUTER["ram_mb"]}
        )
        assert computer.wait_for_guest(timeout=30, poll=0) is computer
        assert probes["n"] == 2


@respx.mock
async def test_wait_until_running_refuses_a_computer_nobody_is_starting() -> None:
    """The change OPL-4629 is actually for.

    A stopped computer the platform says it is holding nothing for will not
    become running on its own, and this wait used to spend its whole budget
    discovering that before reporting "still 'stopped'" — a sentence that names
    the state the caller already passed in. The long timeout is the test: it
    must not be reached.
    """
    route = respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "stopped", "running_ram_mb": 0})
    )
    with mc.Client("gck_test", base_url=BASE) as client:
        computer = mc.Computer(client._t, {**COMPUTER, "status": "stopped", "running_ram_mb": 0})
        with pytest.raises(mc.MandalaError, match=r"stopped and will not start on its own"):
            computer.wait_until_running(timeout=300, poll=0)
    # One read, not a budget's worth: the refusal comes off the first fresh
    # state rather than after repeated confirmation of the same one.
    assert route.call_count == 1
