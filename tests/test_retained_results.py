"""Public retained reads preserve finite evidence and independent binary pages."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, computer, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414

import mandala_computer as mc

RESULT_ID = "res_" + "a" * 32
EXECUTION_ID = "exec_" + "b" * 32
STAMP = "2026-09-16T12:00:00.123456789Z"


def result_manifest(synchronous: bool = False, **extra: Any) -> dict[str, Any]:
    prefix = {
        "bytes": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
        "source_offset": 0,
        "next_source_offset": 0,
        "end_reason": "observed_eof",
    }
    if synchronous:
        prefix.update(source_response_bytes=0, end_reason="response_end", upstream_truncated=True)
    return {
        "version": 1,
        "result_id": RESULT_ID,
        "kind": "synchronous-output" if synchronous else "background-output",
        "state": "ready",
        "account_id": "acc-1",
        "computer_id": "vm-1",
        "workspace_id": None,
        "execution_id": None if synchronous else EXECUTION_ID,
        "capture_started_at": STAMP,
        "captured_at": STAMP,
        "expires_at": "2026-09-17T12:00:00.123456789Z",
        "source": "exec_response" if synchronous else "volatile_guest_files",
        "execution_observation": {"status": "exited", "exit_code": -9, "observed_at": STAMP},
        "stdout": prefix.copy(),
        "stderr": prefix.copy(),
        "diagnostic": None
        if synchronous
        else {
            "bytes": 0,
            "sha256": hashlib.sha256(b"").hexdigest(),
            "source": "wrapper",
            "diagnostic_truncated": False,
        },
        **extra,
    }


def page(data: bytes, offset: int = 0, eof: bool = True, **headers: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=data,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(data)),
            "X-Result-Offset": str(offset),
            "X-Result-Next-Offset": str(offset + len(data)),
            "X-Result-EOF": str(eof).lower(),
            **headers,
        },
    )


@respx.mock
async def test_capture_is_one_explicit_post_and_checks_execution_identity(client: Any) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/executions/{EXECUTION_ID}/retained-output").mock(
        httpx.Response(201, json=result_manifest())
    )
    c = computer(client)
    got = await resolved(
        c.retain_execution_output(EXECUTION_ID, max_bytes_per_stream=123, retention_seconds=60)
    )
    assert isinstance(got, mc.BackgroundResult)
    assert got.execution_id == EXECUTION_ID and got.execution_observation.exit_code == -9
    assert json.loads(route.calls[0].request.content) == {
        "max_bytes_per_stream": 123,
        "retention_seconds": 60,
    }
    assert route.call_count == 1
    route.return_value = httpx.Response(201, json=result_manifest(execution_id="exec_" + "c" * 32))
    with pytest.raises(mc.MandalaError):
        await resolved(c.retain_execution_output(EXECUTION_ID))
    assert route.call_count == 2
    assert json.loads(route.calls[1].request.content) == {}


@pytest.mark.parametrize("synchronous", [False, True])
@respx.mock
async def test_both_kinds_are_finite_and_do_not_infer_success(
    client: Any, synchronous: bool
) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/results/{RESULT_ID}").mock(
        httpx.Response(200, json=result_manifest(synchronous, private_secret="omit"))
    )
    got = await resolved(computer(client).result(RESULT_ID))
    assert got.captured_at == STAMP
    assert got.execution_observation.exit_code == -9
    assert "private_secret" not in dataclasses.asdict(got)
    assert not hasattr(got, "raw")
    assert (got.diagnostic is None) == synchronous
    if synchronous:
        assert got.stdout.upstream_truncated is True
        assert got.stdout.end_reason == "response_end"
        assert got.execution_id is None
    assert route.call_count == 1


@pytest.mark.parametrize("kind", [False, True])
@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("version", True),
        ("kind", "future"),
        ("state", "pending"),
        ("result_id", "res_" + "c" * 32),
        ("computer_id", "vm-2"),
        ("account_id", "a/secret"),
        ("workspace_id", 3),
        ("captured_at", "2026-02-30T00:00:00Z"),
        ("expires_at", STAMP),
        ("capture_started_at", "2026-09-16T12:00:00.123456790Z"),
        ("source", "guessed"),
        ("stdout", {}),
        ("execution_observation", {"status": "lost"}),
    ],
)
@respx.mock
async def test_bad_metadata_is_not_empty_success(
    client: Any, kind: bool, field: str, value: Any
) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/results/{RESULT_ID}").mock(
        httpx.Response(200, json=result_manifest(kind, **{field: value}))
    )
    with pytest.raises(mc.MandalaError):
        await resolved(computer(client).result(RESULT_ID))
    assert route.call_count == 1


@pytest.mark.parametrize("digits", range(10))
@respx.mock
async def test_nanoseconds_survive_calendar_validation_on_both_clients(
    client: Any, digits: int
) -> None:
    stamp = "2024-02-29T12:00:00" + ("." + "123456789"[:digits] if digits else "") + "Z"
    manifest = result_manifest()
    for field in ("capture_started_at", "captured_at"):
        manifest[field] = stamp
    manifest["execution_observation"]["observed_at"] = stamp
    manifest["expires_at"] = "2024-03-01T12:00:00Z"
    respx.get(f"{BASE}/computers/vm-1/results/{RESULT_ID}").mock(httpx.Response(200, json=manifest))
    got = await resolved(computer(client).result(RESULT_ID))
    assert (
        got.capture_started_at == got.captured_at == got.execution_observation.observed_at == stamp
    )


@pytest.mark.parametrize(
    "stamp",
    [
        "2023-02-29T00:00:00Z",
        "2024-02-29T24:00:00Z",
        "2024-02-29T00:60:00Z",
        "2024-02-29T00:00:60Z",
        "2024-02-29T00:00:00+00:00",
        "2024-02-29T00:00:00.1234567890Z",
        "0000-01-01T00:00:00Z",
    ],
)
@respx.mock
async def test_invalid_calendar_and_non_utc_forms_are_refused(client: Any, stamp: str) -> None:
    respx.get(f"{BASE}/computers/vm-1/results/{RESULT_ID}").mock(
        httpx.Response(200, json=result_manifest(capture_started_at=stamp))
    )
    with pytest.raises(mc.MandalaError):
        await resolved(computer(client).result(RESULT_ID))


@respx.mock
async def test_independent_result_pages_keep_all_bytes_and_offsets(client: Any) -> None:
    data = bytes(range(256)) + b"\xef\xbb\xbf\xe2\x82\xac\xff\x00"
    log = []

    def respond(req: httpx.Request) -> httpx.Response:
        assert set(req.url.params) == {"stream", "offset", "limit"}
        stream, start, count = (
            req.url.params["stream"],
            int(req.url.params["offset"]),
            int(req.url.params["limit"]),
        )
        assert "range" not in req.headers
        log.append((stream, start))
        return page(data[start : start + count], start, start + count >= len(data))

    respx.get(f"{BASE}/computers/vm-1/results/{RESULT_ID}/output").mock(side_effect=respond)
    c = computer(client)
    a = await resolved(c.result_output(RESULT_ID, stream="stdout", offset=256, limit=5))
    b = await resolved(c.result_output(RESULT_ID, stream="diagnostic", offset=0, limit=256))
    tail = await resolved(
        c.result_output(RESULT_ID, stream="stdout", offset=a.next_offset, limit=5)
    )
    past = await resolved(c.result_output(RESULT_ID, stream="stderr", offset=999, limit=1))
    assert a.data + tail.data == data[256:]
    assert b.data == bytes(range(256)) and b.offset == 0
    assert past.data == b"" and past.offset == past.next_offset == 999 and past.eof
    assert log == [("stdout", 256), ("diagnostic", 0), ("stdout", 261), ("stderr", 999)]


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Result-Offset": "1"},
        {"X-Result-Next-Offset": "2"},
        {"X-Result-EOF": "TRUE"},
        {"X-Result-Next-Offset": "NaN"},
        {"Content-Range": "bytes 0-0/1"},
        {"Content-Length": "2"},
        {"Content-Type": "text/html"},
        {"Content-Encoding": "gzip"},
    ],
)
@respx.mock
async def test_output_header_evidence_is_checked(client: Any, headers: dict[str, str]) -> None:
    respx.get(f"{BASE}/computers/vm-1/results/{RESULT_ID}/output").mock(page(b"a", **headers))
    with pytest.raises(mc.MandalaError):
        await resolved(
            computer(client).result_output(RESULT_ID, stream="stdout", offset=0, limit=1)
        )


@pytest.mark.parametrize(
    "options",
    [
        {"stream": "other", "offset": 0},
        {"stream": "stdout", "offset": True},
        {"stream": "stdout", "offset": -1},
        {"stream": "stdout", "offset": 0, "limit": 65537},
        {"stream": "stdout", "offset": 9_007_199_254_740_991},
        {"stream": "stdout", "offset": 0.5},
    ],
)
@respx.mock
async def test_invalid_page_options_never_dispatch(client: Any, options: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        await resolved(computer(client).result_output(RESULT_ID, **options))
    assert not respx.calls


@respx.mock
async def test_result_delete_and_unavailable_stream_never_retry(client: Any) -> None:
    route = respx.delete(f"{BASE}/computers/vm-1/results/{RESULT_ID}").mock(
        side_effect=[
            httpx.Response(204),
            httpx.Response(404, json={"code": "result_unavailable", "error": "result unavailable"}),
        ]
    )
    c = computer(client)
    assert await resolved(c.delete_result(RESULT_ID)) is None
    with pytest.raises(mc.NotFoundError):
        await resolved(c.delete_result(RESULT_ID))
    assert route.call_count == 2
    output = respx.get(f"{BASE}/computers/vm-1/results/{RESULT_ID}/output").mock(
        httpx.Response(
            409, json={"code": "result_stream_unavailable", "error": "result stream unavailable"}
        )
    )
    with pytest.raises(mc.ConflictError) as error:
        await resolved(c.result_output(RESULT_ID, stream="diagnostic", offset=0))
    assert error.value.body["code"] == "result_stream_unavailable"
    assert output.call_count == 1


@pytest.mark.parametrize(
    "method,identity",
    [
        ("result", "res_" + "A" * 32),
        ("delete_result", "../other"),
        ("artifact", "art_" + "G" * 32),
        ("delete_artifact", "../other"),
        ("retain_execution_output", "42"),
        ("download_artifact", "res_" + "a" * 32),
    ],
)
@respx.mock
async def test_invalid_id_families_are_refused_before_io(
    client: Any, method: str, identity: str
) -> None:
    with pytest.raises((ValueError, TypeError)):
        await resolved(getattr(computer(client), method)(identity))
    assert not respx.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("bytes", True),
        ("bytes", 4194305),
        ("source_offset", True),
        ("next_source_offset", 1),
        ("source_response_bytes", -1),
        ("source_response_bytes", 16777217),
        ("end_reason", "byte_limit"),
        ("upstream_truncated", None),
    ],
)
@respx.mock
async def test_synchronous_prefix_evidence_is_not_coerced(
    client: Any, field: str, value: Any
) -> None:
    manifest = result_manifest(True)
    manifest["stdout"][field] = value
    respx.get(f"{BASE}/computers/vm-1/results/{RESULT_ID}").mock(httpx.Response(200, json=manifest))
    with pytest.raises(mc.MandalaError):
        await resolved(computer(client).result(RESULT_ID))
