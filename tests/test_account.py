"""Account quota decoding and real sync/async transport bindings, without sockets."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import FrozenInstanceError, asdict
from typing import Any

import httpx
import pytest

import mandala_computer as mc

BASE = "https://api.test/api/v1"


def account_report() -> dict[str, Any]:
    return {
        "scope": "account",
        "advisory": True,
        "observed_at": "2026-09-16T12:34:56.123Z",
        "plan": {"id": "standard", "label": "Standard"},
        "limits": {
            "max_computers": 5,
            "vcpu_pool": 24,
            "ram_pool_mb": 32768,
            "disk_pool_gb": 400,
            "snapshot_storage_bytes": 107374182400,
        },
        "per_computer": {"max_vcpu": 16, "max_ram_mb": 16384, "max_disk_gb": 200},
        "capabilities": {"windows": False},
        "complete": {"computers": True, "snapshots": True},
        "usage": {
            "kept_computers": 3,
            "configured_vcpu": 14,
            "configured_disk_gb": 120,
            "running_or_reserved_computers": 2,
            "running_or_reserved_vcpu": 6,
            "running_or_reserved_ram_mb": 8192,
            "snapshot_storage_bytes": 1073741825,
        },
        "remaining": {
            "kept_computers": 2,
            "configured_vcpu": 10,
            "configured_disk_gb": 280,
            "running_or_reserved_ram_mb": 24576,
            "snapshot_storage_bytes": 106300440575,
        },
    }


def partial(computers: bool, snapshots: bool) -> dict[str, Any]:
    data = account_report()
    data["complete"] = {"computers": computers, "snapshots": snapshots}
    for section in ("usage", "remaining"):
        for key in data[section]:
            if not (snapshots if key == "snapshot_storage_bytes" else computers):
                data[section][key] = None
    return data


async def read(data: object, asynchronous: bool) -> tuple[mc.AccountQuota, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(handle)
    if asynchronous:
        async with httpx.AsyncClient(transport=transport) as http:
            async with mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client:
                assert isinstance(client.account, mc.AsyncAccount)
                result = await client.account.read()
            assert not http.is_closed  # A supplied transport retains its caller-owned lifetime.
    else:
        with httpx.Client(transport=transport) as http:
            with mc.Client("com_test", base_url=BASE, http_client=http) as client:
                assert isinstance(client.account, mc.Account)
                result = client.account.read()
            assert not http.is_closed
    return result, calls


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_named_resource_sends_one_authenticated_get_without_selectors(
    asynchronous: bool,
) -> None:
    data = account_report()
    quota, calls = await read(data, asynchronous)
    assert len(calls) == 1
    assert calls[0].method == "GET"
    assert str(calls[0].url) == BASE + "/account"
    assert calls[0].content == b""
    assert calls[0].headers["Authorization"] == "Bearer com_test"
    assert isinstance(quota, mc.AccountQuota)
    projected = asdict(quota)
    assert projected.pop("raw") == data
    assert projected == data


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("computers,snapshots", [(False, True), (True, False), (False, False)])
async def test_independent_incomplete_groups(
    asynchronous: bool, computers: bool, snapshots: bool
) -> None:
    data = partial(computers, snapshots)
    quota, _ = await read(data, asynchronous)
    assert asdict(quota.complete) == data["complete"]
    assert asdict(quota.usage) == data["usage"]
    assert asdict(quota.remaining) == data["remaining"]
    assert quota.limits.vcpu_pool == 24


def test_frozen_models_raw_copy_and_unknown_fields() -> None:
    data = account_report()
    data["future_field"] = {"answer": 7}
    data["usage"]["future_usage"] = 9
    before = copy.deepcopy(data)
    quota = mc.AccountQuota.from_api(data)
    assert data == before
    assert quota.raw == data
    assert quota.raw is not data
    assert quota.raw["future_field"] is data["future_field"]
    assert not hasattr(quota, "future_field")
    assert not hasattr(quota.usage, "future_usage")
    for model in [
        quota,
        quota.plan,
        quota.limits,
        quota.per_computer,
        quota.capabilities,
        quota.complete,
        quota.usage,
        quota.remaining,
    ]:
        with pytest.raises(FrozenInstanceError):
            setattr(model, next(iter(asdict(model))), None)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_zero_no_plan_and_overage(asynchronous: bool) -> None:
    data = account_report()
    data["usage"] = dict.fromkeys(data["usage"], 0)
    data["remaining"] = dict(zip(data["remaining"], data["limits"].values()))
    quota, _ = await read(data, asynchronous)
    assert quota.usage.kept_computers == 0
    assert quota.remaining.snapshot_storage_bytes == 107374182400
    retained = account_report()
    retained["plan"] = {"id": "none", "label": "No plan"}
    for section in ("limits", "per_computer", "remaining"):
        retained[section] = dict.fromkeys(retained[section], 0)
    quota, _ = await read(retained, asynchronous)
    assert asdict(quota.usage) == account_report()["usage"]
    assert set(asdict(quota.remaining).values()) == {0}
    overage = account_report()
    overage["limits"]["vcpu_pool"] = 4
    overage["remaining"]["configured_vcpu"] = 0
    quota, _ = await read(overage, asynchronous)
    assert quota.usage.configured_vcpu == 14
    assert quota.remaining.configured_vcpu == 0


def test_stopped_resources_zero_cpu_and_equal_active_and_kept() -> None:
    data = account_report()
    data["usage"].update(
        running_or_reserved_computers=0, running_or_reserved_vcpu=0, running_or_reserved_ram_mb=0
    )
    data["remaining"]["running_or_reserved_ram_mb"] = data["limits"]["ram_pool_mb"]
    assert mc.AccountQuota.from_api(data).usage.configured_vcpu == 14
    data["usage"].update(
        running_or_reserved_computers=3, running_or_reserved_ram_mb=8192, configured_vcpu=0
    )
    data["remaining"].update(running_or_reserved_ram_mb=24576, configured_vcpu=24)
    assert mc.AccountQuota.from_api(data).usage.running_or_reserved_vcpu == 0
    data["usage"].update(configured_vcpu=14, running_or_reserved_vcpu=14)
    data["remaining"]["configured_vcpu"] = 10
    assert mc.AccountQuota.from_api(data).usage.running_or_reserved_vcpu == 14


FIELDS = [
    (section, key)
    for section, value in account_report().items()
    for key in (value if isinstance(value, dict) else [None])
]
NUMERIC_FIELDS = [
    pair for pair in FIELDS if pair[0] in ("limits", "per_computer", "usage", "remaining")
]


@pytest.mark.parametrize("section,key", FIELDS)
def test_requires_every_field(section: str, key: str | None) -> None:
    data = account_report()
    if key:
        del data[section][key]
    else:
        del data[section]
    with pytest.raises(mc.MandalaError, match="expected an account quota report"):
        mc.AccountQuota.from_api(data)


@pytest.mark.parametrize("section,key", NUMERIC_FIELDS)
def test_numeric_domains(section: str, key: str) -> None:
    for bad in [None, True, False, -1, 1.5, "0", float("nan"), float("inf"), 2**53, 10**100]:
        data = account_report()
        data[section][key] = bad
        with pytest.raises(mc.MandalaError, match="nonnegative safe integer"):
            mc.AccountQuota.from_api(data)
    for good in [0, 1.0, 2**53 - 1]:
        data = account_report()
        data[section][key] = good
        quota = mc.AccountQuota.from_api(data)
        assert getattr(getattr(quota, section), key) == good
        assert type(getattr(getattr(quota, section), key)) is int


def test_incomplete_fields_require_explicit_null() -> None:
    for section in ("usage", "remaining"):
        for key in account_report()[section]:
            for bad in [0, "unknown", False]:
                data = partial(False, False)
                data[section][key] = bad
                with pytest.raises(mc.MandalaError, match="null when incomplete"):
                    mc.AccountQuota.from_api(data)
            data = partial(False, False)
            del data[section][key]
            with pytest.raises(mc.MandalaError, match="null when incomplete"):
                mc.AccountQuota.from_api(data)


def test_real_booleans_scope_advisory_and_observation() -> None:
    invalid = [
        ("scope", None, "workspace"),
        ("advisory", None, False),
        ("advisory", None, 1),
        ("plan", "id", ""),
        ("plan", "label", 0),
        ("capabilities", "windows", "false"),
        ("complete", "computers", 1),
        ("complete", "snapshots", None),
    ] + [
        ("observed_at", None, date)
        for date in ["yesterday", "2026-09-16", "2026-09-16T12:00:00", "2026-02-30T00:00:00Z"]
    ]
    for section, key, bad in invalid:
        data = account_report()
        if key:
            data[section][key] = bad
        else:
            data[section] = bad
        with pytest.raises(mc.MandalaError, match="expected an account quota report"):
            mc.AccountQuota.from_api(data)
    data = account_report()
    data["plan"]["label"] = {"secret": "response detail"}
    with pytest.raises(mc.MandalaError) as caught:
        mc.AccountQuota.from_api(data)
    assert (
        str(caught.value)
        == "expected an account quota report: plan.label must be a nonempty string"
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("invalid", [None, [], 7, "response detail", {}, {"scope": "account"}])
async def test_malformed_transport_reports(asynchronous: bool, invalid: object) -> None:
    with pytest.raises(mc.MandalaError):
        await read(invalid, asynchronous)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "status,error_type",
    [
        (401, mc.AuthenticationError),
        (402, mc.PlanLimitError),
        (403, mc.PermissionDeniedError),
        (429, mc.RateLimitError),
        (503, mc.UnavailableError),
    ],
)
async def test_http_errors_keep_metadata(
    asynchronous: bool, status: int, error_type: type[mc.APIError]
) -> None:
    calls = []
    body = {"error": "Quota read refused", "reason": "account_suspended", "request_id": "body-id"}

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status,
            json=body,
            headers={
                "X-Request-ID": "header-id",
                "WWW-Authenticate": "Bearer",
                "Retry-After": "2",
            },
        )

    with pytest.raises(error_type) as caught:
        if asynchronous:
            async with (
                httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
                mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
            ):
                await client.account.read()
        else:
            with (
                httpx.Client(transport=httpx.MockTransport(handle)) as http,
                mc.Client("com_test", base_url=BASE, http_client=http) as client,
            ):
                client.account.read()
    error = caught.value
    assert error.status == status
    assert error.body == body
    assert error.reason == "account_suspended"
    assert error.request_id == "header-id"
    assert error.www_authenticate == "Bearer"
    assert error.retry_after == 2
    assert len(calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_each_read_gets_a_fresh_observation(asynchronous: bool) -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        data = account_report()
        data["observed_at"] = f"2026-09-16T12:34:0{len(calls)}Z"
        return httpx.Response(200, json=data)

    if asynchronous:
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
            mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
        ):
            first = await client.account.read()
            second = await client.account.read()
    else:
        with (
            httpx.Client(transport=httpx.MockTransport(handle)) as http,
            mc.Client("com_test", base_url=BASE, http_client=http) as client,
        ):
            first = client.account.read()
            second = client.account.read()
    assert len(calls) == 2
    assert first.observed_at != second.observed_at


async def test_async_cancellation_reaches_the_transport() -> None:
    ready = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == BASE + "/account"
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        task = asyncio.create_task(client.account.read())
        await asyncio.wait_for(ready.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
