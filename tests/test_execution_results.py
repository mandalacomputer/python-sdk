"""Stable execution reads must be independent of legacy PID polling."""

from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, computer, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414 - shared pytest fixture

import mandala_computer as mc

EXECUTION_ID = "exec_0123456789abcdef0123456789abcdef"
METADATA = {
    "execution_id": EXECUTION_ID,
    "computer_id": "vm-1",
    "pid": 4242,
    "status": "running",
    "started_at": "2026-09-15T12:00:00.123456789Z",
    "output_source": "volatile_guest_files",
}


@respx.mock
async def test_metadata_is_a_single_identity_bound_read(client: Any) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
        httpx.Response(200, json=METADATA)
    )
    result = await resolved(computer(client).execution(EXECUTION_ID))
    assert result.execution_id == EXECUTION_ID
    assert result.status == "running"
    assert result.exit_code is None
    assert route.call_count == 1


def test_background_handle_exposes_start_identity_without_polling() -> None:
    with mc.Client("gck_test", base_url=BASE) as api:
        job = mc.BackgroundCommand(api._t, "vm-1", {"pid": 4242, "execution_id": EXECUTION_ID})
        assert job.execution_id == EXECUTION_ID


def output(
    stdout: bytes = b"", stderr: bytes = b"", *, out: int = 0, err: int = 0, **extra: Any
) -> dict[str, Any]:
    import base64

    return {
        "execution_id": EXECUTION_ID,
        "stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "stdout_offset": out + len(stdout),
        "stderr_offset": err + len(stderr),
        "stdout_more": False,
        "stderr_more": False,
        "diagnostic_b64": "",
        "diagnostic_truncated": False,
        **extra,
    }


@respx.mock
async def test_readers_interleave_with_consuming_legacy_poll_without_sharing_offsets(
    client: Any,
) -> None:
    import base64

    c = computer(client)
    stdout, stderr = b"\x00\xff\xe2\x82\xacend", b"\x00error"
    diagnostic = b"wrapper stderr\x00wrapper stdout\xff"
    log: list[tuple[int, int]] = []

    def read(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert set(request.url.params) == {"stdout_offset", "stderr_offset", "limit"}
        out, err = (
            int(request.url.params["stdout_offset"]),
            int(request.url.params["stderr_offset"]),
        )
        limit = int(request.url.params["limit"])
        log.append((out, err))
        out_bytes, err_bytes = stdout[out : out + limit], stderr[err : err + limit]
        return httpx.Response(
            200,
            json=output(
                out_bytes,
                err_bytes,
                out=out,
                err=err,
                stdout_more=len(out_bytes) == limit,
                stderr_more=len(err_bytes) == limit,
                diagnostic_b64=base64.b64encode(diagnostic).decode(),
                diagnostic_truncated=True,
            ),
        )

    respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}/output").mock(side_effect=read)
    legacy = respx.get(f"{BASE}/computers/vm-1/exec/4242").mock(
        httpx.Response(
            200,
            json={
                "pid": 4242,
                "running": False,
                "exited": True,
                "exit_code": -9,
                "stdout_b64": "bGVnYWN5",
                "stderr_b64": "d3JhcHBlcg==",
                "more": False,
                "stdout_offset": 900,
                "stderr_offset": 99,
            },
        )
    )
    a = await resolved(c.execution_output(EXECUTION_ID, stdout_offset=0, stderr_offset=0, limit=3))
    status = await resolved(c.background_command(4242).poll())
    b = await resolved(c.execution_output(EXECUTION_ID, stdout_offset=0, stderr_offset=0, limit=3))
    a2 = await resolved(
        c.execution_output(
            EXECUTION_ID, stdout_offset=a.stdout_offset, stderr_offset=a.stderr_offset, limit=3
        )
    )
    b2 = await resolved(
        c.execution_output(
            EXECUTION_ID, stdout_offset=b.stdout_offset, stderr_offset=b.stderr_offset, limit=3
        )
    )
    assert a == b and a2 == b2
    assert a.stdout == b"\x00\xff\xe2" and a2.stdout == b"\x82\xace"
    assert (a.stdout + a2.stdout)[2:5].decode() == "€"
    assert a.stderr == b"\x00er" and a2.stderr == b"ror"
    assert status.stdout == b"legacy" and status.stderr == b"wrapper"
    assert status.exit_code == -9 and status.drained
    assert legacy.call_count == 1
    assert log == [(0, 0), (0, 0), (3, 3), (3, 3)]
    assert all(
        part.diagnostic == diagnostic and part.diagnostic_truncated for part in (a, b, a2, b2)
    )


@pytest.mark.parametrize(
    "status, fields",
    [
        ("running", {}),
        ("lost", {}),
        ("exited", {"ended_at": "2026-09-15T07:00:01-05:00", "exit_code": -9}),
        ("exited", {"ended_at": "2026-09-15T12:00:01Z", "exit_code": 0}),
    ],
)
@respx.mock
async def test_metadata_projects_only_observed_evidence(
    client: Any, status: str, fields: dict
) -> None:
    body = {**METADATA, "status": status, **fields, "command": "private", "new_field": {"x": 1}}
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
        httpx.Response(200, json=body)
    )
    result = await resolved(computer(client).execution(EXECUTION_ID))
    assert isinstance(result, mc.ExecutionMetadata)
    assert result.status == status
    assert result.started_at == METADATA["started_at"]
    assert result.ended_at == fields.get("ended_at")
    assert result.exit_code == fields.get("exit_code")
    assert result.output_source == "volatile_guest_files"
    assert not hasattr(result, "raw") and not hasattr(result, "command")
    assert route.call_count == 1


@pytest.mark.parametrize("fraction_length", range(10))
@pytest.mark.parametrize("zone", ["Z", "+05:30", "-07:00"])
@pytest.mark.parametrize("field", ["started_at", "ended_at"])
@respx.mock
async def test_metadata_preserves_every_fraction_length_in_both_timestamps(
    client: Any, fraction_length: int, zone: str, field: str
) -> None:
    fraction = "." + "123456789"[:fraction_length] if fraction_length else ""
    timestamp = f"2024-02-29T12:30:59{fraction}{zone}"
    body = {**METADATA, field: timestamp}
    if field == "ended_at":
        body.update(status="exited", exit_code=0)
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
        httpx.Response(200, json=body)
    )
    result = await resolved(computer(client).execution(EXECUTION_ID))
    assert getattr(result, field) == timestamp
    assert route.call_count == 1


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
@pytest.mark.parametrize(
    "timestamp, valid",
    [
        ("2026-09-30T23:59:59.1Z", True),
        ("2024-02-29T00:00:00.12345+00:00", True),
        ("2026-09-31T12:30:59.1Z", False),
        ("2026-02-29T12:30:59.12345Z", False),
        ("2026-02-30T12:30:59.123456789Z", False),
        ("2026-09-15T24:00:00.1Z", False),
        ("2026-09-15T12:60:00.1Z", False),
        ("2026-09-15T12:30:60.1234Z", False),
        ("2026-09-15T12:30:59.1+24:00", False),
        ("2026-09-15T12:30:59.12345-00:60", False),
        ("2026-09-15T12:30:59.1", False),
        ("2026-09-15T12:30:59.1234567890Z", False),
        ("2026-09-15T12:30:59.Z", False),
    ],
)
@respx.mock
async def test_fraction_normalization_preserves_calendar_and_timezone_restrictions(
    client: Any, field: str, timestamp: str, valid: bool
) -> None:
    body = {**METADATA, field: timestamp}
    if field == "ended_at":
        body.update(status="exited", exit_code=0)
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
        httpx.Response(200, json=body)
    )
    if valid:
        result = await resolved(computer(client).execution(EXECUTION_ID))
        assert getattr(result, field) == timestamp
    else:
        with pytest.raises(mc.MandalaError, match=field):
            await resolved(computer(client).execution(EXECUTION_ID))
    assert route.call_count == 1


@pytest.mark.parametrize(
    "field, value",
    [
        ("execution_id", None),
        ("execution_id", "exec_" + "f" * 32),
        ("execution_id", "4242"),
        ("computer_id", "vm-2"),
        ("computer_id", 1),
        ("pid", True),
        ("pid", "4242"),
        ("pid", 1.5),
        ("pid", 0),
        ("pid", 2**53),
        ("status", "done"),
        ("status", None),
        ("started_at", "yesterday"),
        ("started_at", "2026-02-30T00:00:00Z"),
        ("started_at", "2026-09-15T00:00:00"),
        ("started_at", "2026-09-15T00:00:00+00:99"),
        ("started_at", "2026-09-15T24:00:00Z"),
        ("started_at", "2026-09-15T00:00:00.1234567890Z"),
        ("output_source", "retained"),
        ("exit_code", 0),
        ("ended_at", None),
    ],
)
@respx.mock
async def test_metadata_refuses_malformed_or_contradictory_evidence(
    client: Any, field: str, value: Any
) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
        httpx.Response(200, json={**METADATA, field: value})
    )
    with pytest.raises(mc.MandalaError, match="execution response"):
        await resolved(computer(client).execution(EXECUTION_ID))
    assert route.call_count == 1


@pytest.mark.parametrize("field", list(METADATA))
@respx.mock
async def test_metadata_requires_each_wire_field(client: Any, field: str) -> None:
    body = dict(METADATA)
    del body[field]
    respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
        httpx.Response(200, json=body)
    )
    with pytest.raises(mc.MandalaError):
        await resolved(computer(client).execution(EXECUTION_ID))


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"exit_code": 0},
        {"ended_at": "2026-09-15T12:00:01Z"},
        {"exit_code": True, "ended_at": "2026-09-15T12:00:01Z"},
        {"exit_code": "0", "ended_at": "2026-09-15T12:00:01Z"},
        {"exit_code": 0.0, "ended_at": "2026-09-15T12:00:01Z"},
        {"exit_code": 2**53, "ended_at": "2026-09-15T12:00:01Z"},
        {"exit_code": -(2**53), "ended_at": "2026-09-15T12:00:01Z"},
        {"exit_code": 0, "ended_at": None},
    ],
)
@respx.mock
async def test_exited_requires_real_signed_code_and_time(client: Any, fields: dict) -> None:
    respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
        httpx.Response(200, json={**METADATA, "status": "exited", **fields})
    )
    with pytest.raises(mc.MandalaError):
        await resolved(computer(client).execution(EXECUTION_ID))


@pytest.mark.parametrize(
    "field, value",
    [
        ("execution_id", "exec_" + "f" * 32),
        ("execution_id", None),
        ("stdout_b64", "Zh=="),
        ("stderr_b64", "Zg=== "),
        ("diagnostic_b64", "Zg==\n"),
        ("stdout_b64", "_w=="),
        ("stdout_b64", "Zg"),
        ("stdout_b64", "é"),
        ("stdout_b64", False),
        ("stderr_b64", None),
        ("diagnostic_b64", "!!!!"),
        ("stdout_b64", "AAAAAA=="),
        ("stdout_offset", 1),
        ("stdout_offset", -1),
        ("stdout_offset", True),
        ("stdout_offset", "0"),
        ("stderr_offset", 0.0),
        ("stderr_offset", 2**53),
        ("stdout_more", 1),
        ("stderr_more", "false"),
        ("stdout_more", True),
        ("diagnostic_truncated", None),
        ("diagnostic_truncated", 1),
    ],
)
@respx.mock
async def test_output_refuses_bad_binary_and_cursor_evidence(
    client: Any, field: str, value: Any
) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}/output").mock(
        httpx.Response(200, json=output(**{field: value}))
    )
    with pytest.raises(mc.MandalaError, match="execution response"):
        await resolved(
            computer(client).execution_output(
                EXECUTION_ID, stdout_offset=0, stderr_offset=0, limit=3
            )
        )
    assert route.call_count == 1


@pytest.mark.parametrize("field", list(output()))
@respx.mock
async def test_output_requires_all_wire_fields(client: Any, field: str) -> None:
    body = output()
    del body[field]
    respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}/output").mock(
        httpx.Response(200, json=body)
    )
    with pytest.raises(mc.MandalaError):
        await resolved(
            computer(client).execution_output(EXECUTION_ID, stdout_offset=0, stderr_offset=0)
        )


@respx.mock
async def test_empty_current_eof_preserves_beyond_end_positions_and_has_no_completion_claim(
    client: Any,
) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}/output").mock(
        httpx.Response(200, json=output(out=100, err=900))
    )
    c = computer(client)
    result = await resolved(c.execution_output(EXECUTION_ID, stdout_offset=100, stderr_offset=900))
    assert isinstance(result, mc.ExecutionOutput)
    assert result.stdout == result.stderr == b""
    assert (result.stdout_offset, result.stderr_offset) == (100, 900)
    assert not result.stdout_more and not result.stderr_more
    assert not hasattr(result, "drained") and not hasattr(result, "exit_code")
    assert route.calls.last.request.url.params["limit"] == "65536"
    route.mock(httpx.Response(200, json=output(b"later", out=100, err=900)))
    later = await resolved(c.execution_output(EXECUTION_ID, stdout_offset=100, stderr_offset=900))
    assert later.stdout == b"later" and later.stdout_offset == 105


@pytest.mark.parametrize(
    "identity",
    [
        None,
        False,
        4242,
        "",
        "../exec/4242",
        "exec_" + "a" * 31,
        "exec_" + "a" * 33,
        "exec_" + "A" * 32,
        EXECUTION_ID + "\n",
        EXECUTION_ID + "?limit=1",
    ],
)
@pytest.mark.parametrize("method", ["execution", "execution_output"])
@respx.mock
async def test_bad_identity_refused_before_io(client: Any, identity: Any, method: str) -> None:
    args = {"stdout_offset": 0, "stderr_offset": 0} if method.endswith("output") else {}
    with pytest.raises(ValueError, match="execution_id"):
        await resolved(getattr(computer(client), method)(identity, **args))
    assert not respx.calls


@pytest.mark.parametrize(
    "field, value",
    [
        ("stdout_offset", True),
        ("stderr_offset", False),
        ("stdout_offset", "0"),
        ("stdout_offset", 0.0),
        ("stderr_offset", -1),
        ("stderr_offset", None),
        ("stdout_offset", 2**53 - 1),
        ("stderr_offset", 2**53),
        ("limit", True),
        ("limit", "1"),
        ("limit", 0),
        ("limit", -1),
        ("limit", 1_048_577),
        ("limit", 1.0),
        ("limit", None),
    ],
)
@respx.mock
async def test_invalid_read_parameters_never_dispatch(client: Any, field: str, value: Any) -> None:
    params = {"stdout_offset": 0, "stderr_offset": 0, "limit": 1}
    params[field] = value
    with pytest.raises(ValueError, match=field):
        await resolved(computer(client).execution_output(EXECUTION_ID, **params))
    assert not respx.calls


@pytest.mark.parametrize("params", [{}, {"stdout_offset": 0}, {"stderr_offset": 0}])
@respx.mock
async def test_offsets_are_required_keyword_arguments(client: Any, params: dict) -> None:
    with pytest.raises(TypeError):
        await resolved(computer(client).execution_output(EXECUTION_ID, **params))
    with pytest.raises(TypeError):
        await resolved(computer(client).execution_output(EXECUTION_ID, 0, 0))
    assert not respx.calls


@pytest.mark.parametrize("limit", [1, 65_536, 1_048_576])
@respx.mock
async def test_safe_boundary_is_inclusive_and_per_stream(client: Any, limit: int) -> None:
    start = 2**53 - 1 - limit
    payload = b"x" * limit
    respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}/output").mock(
        httpx.Response(
            200,
            json=output(payload, payload, out=start, err=start, stdout_more=True, stderr_more=True),
        )
    )
    result = await resolved(
        computer(client).execution_output(
            EXECUTION_ID, stdout_offset=start, stderr_offset=start, limit=limit
        )
    )
    assert result.stdout_offset == result.stderr_offset == 2**53 - 1
    assert result.stdout == result.stderr == payload
    with pytest.raises(ValueError):
        await resolved(
            computer(client).execution_output(
                EXECUTION_ID, stdout_offset=start + 1, stderr_offset=start, limit=limit
            )
        )


@pytest.mark.parametrize("size, accepted", [(65_536, True), (65_537, False)])
@respx.mock
async def test_diagnostic_has_its_own_bound_independent_of_stream_limit(
    client: Any, size: int, accepted: bool
) -> None:
    import base64

    diagnostic = b"x" * size
    respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}/output").mock(
        httpx.Response(
            200,
            json=output(
                diagnostic_b64=base64.b64encode(diagnostic).decode(), diagnostic_truncated=True
            ),
        )
    )
    if accepted:
        result = await resolved(
            computer(client).execution_output(
                EXECUTION_ID, stdout_offset=0, stderr_offset=0, limit=1
            )
        )
        assert result.diagnostic == diagnostic and result.diagnostic_truncated
        assert result.stdout_offset == result.stderr_offset == 0
    else:
        with pytest.raises(mc.MandalaError):
            await resolved(
                computer(client).execution_output(
                    EXECUTION_ID, stdout_offset=0, stderr_offset=0, limit=1
                )
            )


@pytest.mark.parametrize(
    "status, error_type",
    [
        (401, mc.AuthenticationError),
        (403, mc.PermissionDeniedError),
        (404, mc.NotFoundError),
        (409, mc.ConflictError),
        (429, mc.RateLimitError),
        (503, mc.UnavailableError),
    ],
)
@pytest.mark.parametrize("method", ["execution", "execution_output"])
@respx.mock
async def test_server_errors_preserve_transport_behavior_with_no_fallback(
    client: Any, status: int, error_type: type, method: str
) -> None:
    suffix = "/output" if method.endswith("output") else ""
    body = {
        "error": "not available",
        "code": "output_unavailable" if status == 409 else "execution_unavailable",
    }
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}{suffix}").mock(
        httpx.Response(status, json=body)
    )
    kwargs = {"stdout_offset": 0, "stderr_offset": 0} if suffix else {}
    with pytest.raises(error_type) as exc:
        await resolved(getattr(computer(client), method)(EXECUTION_ID, **kwargs))
    assert exc.value.status == status and exc.value.body == body
    assert route.call_count == len(respx.calls) == 1


@pytest.mark.parametrize("body", [None, [], "login", 42])
@pytest.mark.parametrize("method", ["execution", "execution_output"])
@respx.mock
async def test_nonobject_success_is_not_a_result(client: Any, method: str, body: Any) -> None:
    suffix = "/output" if method.endswith("output") else ""
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}{suffix}").mock(
        httpx.Response(200, json=body)
    )
    kwargs = {"stdout_offset": 0, "stderr_offset": 0} if suffix else {}
    with pytest.raises(mc.MandalaError):
        await resolved(getattr(computer(client), method)(EXECUTION_ID, **kwargs))
    assert route.call_count == 1


@pytest.mark.parametrize("has_id", [False, True])
@respx.mock
async def test_start_id_remains_original_after_pid_reuse_and_reconstruction(
    client: Any, has_id: bool
) -> None:
    data = {"pid": 4242, **({"execution_id": EXECUTION_ID} if has_id else {})}
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(202, json=data))
    replacement = "exec_" + "f" * 32
    response = {
        "pid": 4242,
        "execution_id": replacement,
        "running": False,
        "exited": True,
        "exit_code": 0,
        "stdout_b64": "",
        "stderr_b64": "",
        "more": False,
    }
    respx.get(f"{BASE}/computers/vm-1/exec/4242").mock(httpx.Response(200, json=response))
    respx.delete(f"{BASE}/computers/vm-1/exec/4242").mock(
        httpx.Response(200, json={**response, "killed": True})
    )
    unavailable = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
        httpx.Response(404, json={"code": "execution_unavailable"})
    )
    c = computer(client)
    job = await resolved(c.start_exec("work"))
    assert job.execution_id == (EXECUTION_ID if has_id else None)
    rebuilt = c.background_command(4242)
    assert rebuilt.execution_id is None
    status = await resolved(job.poll())
    assert status.exit_code == 0 and status.raw["execution_id"] == replacement
    assert (await resolved(rebuilt.kill())).killed
    assert job.execution_id == (EXECUTION_ID if has_id else None)
    assert rebuilt.execution_id is None
    with pytest.raises(mc.NotFoundError):
        await resolved(c.execution(EXECUTION_ID))
    assert unavailable.call_count == 1
    assert len(respx.calls) == 4


@pytest.mark.parametrize("bad", [None, False, 4242, "", "exec_" + "G" * 32])
@respx.mock
async def test_malformed_optional_id_does_not_break_legacy_start(client: Any, bad: Any) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(202, json={"pid": 4242, "execution_id": bad})
    )
    job = await resolved(computer(client).start_exec("work"))
    assert job.pid == 4242
    with pytest.raises(mc.MandalaError, match="execution_id"):
        _ = job.execution_id
    assert len(respx.calls) == 1


@pytest.mark.parametrize("method", ["execution", "execution_output"])
@respx.mock
async def test_supported_prefix_auth_and_encoded_computer_segment(client: Any, method: str) -> None:
    prefix = "https://api.test/customer/service/api/v1"
    client._t.base_url = prefix
    c = computer(client)
    c._data["id"] = "vm /?雪"
    suffix = "/output" if method.endswith("output") else ""
    response = output() if suffix else {**METADATA, "computer_id": c.id}
    route = respx.route(host="api.test").mock(httpx.Response(200, json=response))
    kwargs = {"stdout_offset": 0, "stderr_offset": 0} if suffix else {}
    await resolved(getattr(c, method)(EXECUTION_ID, **kwargs))
    request = route.calls.last.request
    assert request.method == "GET"
    assert (
        request.url.raw_path.split(b"?", 1)[0]
        == (
            "/customer/service/api/v1/computers/vm%20%2F%3F%E9%9B%AA/executions/"
            + EXECUTION_ID
            + suffix
        ).encode()
    )
    assert request.headers["authorization"] == "Bearer gck_test"
    assert not request.content
    assert route.call_count == 1


@pytest.mark.parametrize("method", ["execution", "execution_output"])
@pytest.mark.parametrize(
    "transport_error, expected",
    [
        (httpx.ConnectError, mc.ConnectionError),
        (httpx.ReadTimeout, mc.TimeoutError),
    ],
)
@respx.mock
async def test_network_errors_are_not_retried_or_replaced(
    client: Any, method: str, transport_error: type, expected: type
) -> None:
    suffix = "/output" if method.endswith("output") else ""
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}{suffix}").mock(
        side_effect=transport_error("test transport failure")
    )
    kwargs = {"stdout_offset": 0, "stderr_offset": 0} if suffix else {}
    with pytest.raises(expected):
        await resolved(getattr(computer(client), method)(EXECUTION_ID, **kwargs))
    assert route.call_count == len(respx.calls) == 1


@pytest.mark.parametrize("method", ["execution", "execution_output"])
@respx.mock
async def test_async_cancellation_propagates_without_fallback_or_replay(method: str) -> None:
    import asyncio

    entered = asyncio.Event()
    cancelled = asyncio.Event()
    count = 0

    async def held(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    respx.route(host="api.test").mock(side_effect=held)
    async with mc.AsyncClient("gck_test", base_url=BASE) as api:
        c = mc.AsyncComputer(api._t, {"id": "vm-1", "status": "suspended"})
        kwargs = {"stdout_offset": 0, "stderr_offset": 0} if method.endswith("output") else {}
        task = asyncio.create_task(getattr(c, method)(EXECUTION_ID, **kwargs))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and count == 1


@respx.mock
async def test_metadata_keeps_original_computer_expectation_across_await() -> None:
    async with mc.AsyncClient("gck_test", base_url=BASE) as api:
        c = mc.AsyncComputer(api._t, {"id": "vm-1"})

        async def reply(request: httpx.Request) -> httpx.Response:
            c._data["id"] = "vm-2"
            return httpx.Response(200, json={**METADATA, "computer_id": "vm-2"})

        route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}").mock(
            side_effect=reply
        )
        with pytest.raises(mc.MandalaError, match="computer_id"):
            await c.execution(EXECUTION_ID)
        assert route.call_count == 1


class UntrustworthyInt(int):
    def __str__(self) -> str:
        return "-1"

    def __format__(self, spec: str) -> str:
        return "../stop"

    def __lt__(self, other: object) -> bool:
        return False

    def __le__(self, other: object) -> bool:
        return True


class UntrustworthyString(str):
    def __str__(self) -> str:
        return "../stop"

    def encode(self, *args: Any, **kwargs: Any) -> bytes:
        return b"../stop"


@respx.mock
async def test_validated_request_values_are_the_exact_values_sent(client: Any) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}/output").mock(
        httpx.Response(200, json=output(out=7, err=13))
    )
    result = await resolved(
        computer(client).execution_output(
            UntrustworthyString(EXECUTION_ID),
            stdout_offset=UntrustworthyInt(7),
            stderr_offset=UntrustworthyInt(13),
            limit=UntrustworthyInt(1),
        )
    )
    assert dict(route.calls.last.request.url.params) == {
        "stdout_offset": "7",
        "stderr_offset": "13",
        "limit": "1",
    }
    assert (result.stdout_offset, result.stderr_offset) == (7, 13)
    with pytest.raises(ValueError):
        await resolved(
            computer(client).execution_output(
                EXECUTION_ID, stdout_offset=UntrustworthyInt(-1), stderr_offset=0
            )
        )
    assert route.call_count == 1


def test_existing_execution_dataclasses_keep_positional_construction_and_matching() -> None:
    import dataclasses

    expected_status = (
        "pid",
        "command",
        "running",
        "exited",
        "exit_code",
        "stdout",
        "stderr",
        "stdout_offset",
        "stderr_offset",
        "more",
        "killed",
        "started_at",
        "raw",
    )
    assert mc.ExecStatus.__match_args__ == expected_status
    assert mc.ExecResult.__match_args__ == (
        "exit_code",
        "stdout",
        "stderr",
        "timed_out",
        "out_truncated",
        "err_truncated",
        "raw",
    )
    status = mc.ExecStatus(4242, "true", False, True, -9, b"a", b"b", 1, 1, False, False)
    result = mc.ExecResult(-9, b"a", b"b", False)
    assert status == dataclasses.replace(status, raw={"execution_id": EXECUTION_ID})
    assert result == dataclasses.replace(result, raw={"execution_id": EXECUTION_ID})
    assert hash(status) == hash(dataclasses.replace(status, raw={"unknown": True}))
