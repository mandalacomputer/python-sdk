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
