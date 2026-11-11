"""Signals the platform sends that a caller acts on: a computer its host holds
paused, why its proxied connections fail, why a retained capture was not
kept, cancelling a build, and the refusal codes macOS and storage holds
answer with — sync and async."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, computer, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414

import mandala_computer as mc
from mandala_computer._client import error_for_status

RUNNING: dict[str, Any] = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux"}
PAUSED = {"reason": "disk_emergency", "since": "2026-11-09T10:00:00.000Z"}
PROXY_ERROR = {"reason": "credentials_refused", "at": "2026-11-09T10:01:00.000Z", "count": 3}


@pytest.mark.parametrize("cls", [mc.Computer, mc.AsyncComputer])
def test_host_paused_reads_why_and_since(cls: Any) -> None:
    c = cls(None, {**RUNNING, "host_paused": PAUSED})
    assert c.host_paused == mc.HostPaused("disk_emergency", "2026-11-09T10:00:00.000Z")
    assert c.status == "running"


@pytest.mark.parametrize("cls", [mc.Computer, mc.AsyncComputer])
def test_egress_proxy_last_error_reads_reason_when_and_count(cls: Any) -> None:
    c = cls(None, {**RUNNING, "egress_proxy_last_error": PROXY_ERROR})
    assert c.egress_proxy_last_error == mc.EgressProxyLastError(
        "credentials_refused", "2026-11-09T10:01:00.000Z", 3
    )


@pytest.mark.parametrize("cls", [mc.Computer, mc.AsyncComputer])
def test_both_are_none_when_the_platform_leaves_them_out(cls: Any) -> None:
    c = cls(None, RUNNING)
    assert c.host_paused is None and c.egress_proxy_last_error is None
    c = cls(None, {**RUNNING, "host_paused": None, "egress_proxy_last_error": None})
    assert c.host_paused is None and c.egress_proxy_last_error is None


@pytest.mark.parametrize("field", ["host_paused", "egress_proxy_last_error"])
def test_one_that_is_not_an_object_is_refused_rather_than_read_as_absent(field: str) -> None:
    """Read as absent, a pause would leave a caller treating stalls as faults."""
    with pytest.raises(mc.MandalaError, match=field):
        getattr(mc.Computer(None, {**RUNNING, field: "yes"}), field)  # type: ignore[arg-type]


@respx.mock
async def test_refresh_brings_a_host_pause_in_and_out(client: Any) -> None:
    route = respx.get(f"{BASE}/computers/vm-1").mock(
        return_value=httpx.Response(200, json={**RUNNING, "host_paused": PAUSED})
    )
    c = computer(client)
    await resolved(c.refresh())
    assert c.host_paused is not None and c.host_paused.reason == "disk_emergency"
    route.return_value = httpx.Response(200, json=RUNNING)
    await resolved(c.refresh())
    assert c.host_paused is None


# --- retained output reason ----------------------------------------------------


@pytest.mark.parametrize("reason", ["computer_quota", "account_quota", "capacity"])
@respx.mock
async def test_exec_reports_why_a_capture_was_not_kept(client: Any, reason: str) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        return_value=httpx.Response(
            200,
            headers={"X-GC-Retained-Output": "unavailable", "X-GC-Retained-Output-Reason": reason},
            json={"exit_code": 0, "stdout_b64": "", "stderr_b64": ""},
        )
    )
    done = await resolved(computer(client).exec("true", retain_output=True))
    assert (done.retained_output, done.retained_output_reason) == ("unavailable", reason)


@respx.mock
async def test_a_failed_exec_carries_the_reason_too(client: Any) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        return_value=httpx.Response(
            409,
            headers={
                "X-GC-Retained-Output": "unavailable",
                "X-GC-Retained-Output-Reason": "account_quota",
            },
            json={"error": "busy", "reason": "contention"},
        )
    )
    with pytest.raises(mc.ConflictError) as caught:
        await resolved(computer(client).exec("true", retain_output=True))
    assert caught.value.retained_output_reason == "account_quota"


@respx.mock
async def test_no_reason_without_the_unavailable_word(client: Any) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        return_value=httpx.Response(
            200,
            headers={"X-GC-Retained-Output-Reason": "capacity"},
            json={"exit_code": 0, "stdout_b64": "", "stderr_b64": ""},
        )
    )
    done = await resolved(computer(client).exec("true"))
    assert done.retained_output is None and done.retained_output_reason is None


# --- build cancel --------------------------------------------------------------


@respx.mock
async def test_cancel_posts_and_answers_the_build_as_it_stands(client: Any) -> None:
    route = respx.post(f"{BASE}/builds/bld-1/cancel").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "bld-1",
                "ref": "acc-1/devbox@1.0.0",
                "status": "running",
                "started_at": "2026-11-10T12:00:00.000Z",
            },
        )
    )
    build = await resolved(client.builds.cancel("bld-1"))
    assert route.calls.last.request.content == b""
    assert (build.id, build.status) == ("bld-1", "running")


@respx.mock
async def test_cancelling_a_finished_build_is_a_conflict(client: Any) -> None:
    respx.post(f"{BASE}/builds/bld-1/cancel").mock(
        return_value=httpx.Response(409, json={"error": "The build has already finished."})
    )
    with pytest.raises(mc.ConflictError):
        await resolved(client.builds.cancel("bld-1"))


# --- refusal codes ---------------------------------------------------------------


def test_unsupported_on_macos_is_final() -> None:
    err = error_for_status(
        409,
        "macOS does not support SSH",
        body={"code": "UNSUPPORTED_ON_MACOS", "reason": "unsupported_on_macos"},
    )
    assert isinstance(err, mc.ConflictError)
    assert mc.is_transient(err) is False


def test_unsupported_on_macos_beats_a_code_that_would_clear() -> None:
    """Read before a passing code, as every permanent word is."""
    err = mc.ConflictError(
        "x", status=409, body={"code": "WORKSPACE_MOVING", "reason": "unsupported_on_macos"}
    )
    assert mc.is_transient(err) is False


@pytest.mark.parametrize(
    ("status", "code", "cls"),
    [
        (402, "STORAGE_OVER_PLAN", mc.PlanLimitError),
        (403, "MACOS_LICENCE_REQUIRED", mc.PermissionDeniedError),
    ],
)
def test_the_storage_hold_and_macos_licence_codes_are_final(
    status: int, code: str, cls: type[mc.APIError]
) -> None:
    err = error_for_status(status, "refused", body={"error": "refused", "code": code})
    assert type(err) is cls and err.code == code
    assert mc.is_transient(err) is False
