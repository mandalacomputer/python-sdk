"""Exercise real httpx streaming ownership through both public clients."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from tests.test_api_contract import computer, resolved
from tests.test_artifacts import ARTIFACT_ID, artifact_manifest
from tests.test_retained_results import EXECUTION_ID, RESULT_ID, result_manifest

import mandala_computer as mc

BASE = "https://api.test/prefix/api/v1"
# Task.cancel(msg) propagates its message to the awaiting task only on Python 3.11+.
# Cancellation, response closure and request-count assertions remain unconditional.
CANCEL_MESSAGE_PROPAGATES = sys.version_info >= (3, 11)


class TrackedBody(httpx.SyncByteStream, httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], error: Exception | None = None) -> None:
        self.chunks = chunks
        self.error = error
        self.consumed = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk
        if self.error:
            raise self.error

    async def __aiter__(self):
        for chunk in self:
            yield chunk

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


@asynccontextmanager
async def connected(asynchronous: bool, handler: Any, **options: Any):
    transport = httpx.MockTransport(handler)
    if asynchronous:
        async with httpx.AsyncClient(transport=transport, follow_redirects=True, **options) as raw:
            async with mc.AsyncClient("key", base_url=BASE, http_client=raw) as client:
                yield computer(client), raw
            assert not raw.is_closed
    else:
        with httpx.Client(transport=transport, follow_redirects=True, **options) as raw:
            with mc.Client("key", base_url=BASE, http_client=raw) as client:
                yield computer(client), raw
            assert not raw.is_closed


@pytest.fixture(params=[False, True], ids=["sync", "async"])
def asynchronous(request: pytest.FixtureRequest) -> bool:
    return request.param


@pytest.mark.parametrize("size", [8192, 8193, 20000])
async def test_metadata_body_limit_enforced_during_read(asynchronous: bool, size: int) -> None:
    manifest = result_manifest(padding="")
    original = json.dumps(manifest).encode()
    manifest["padding"] = "x" * (size - len(original))
    data = json.dumps(manifest).encode()
    assert len(data) == size
    body = TrackedBody([bytes([byte]) for byte in data])
    requests = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return httpx.Response(200, stream=body, headers={"Content-Type": "application/json"})

    async with connected(asynchronous, handler) as (c, _):
        if size == 8192:
            got = await resolved(c.result(RESULT_ID))
            assert got.result_id == RESULT_ID and not hasattr(got, "padding")
        else:
            with pytest.raises(mc.MandalaError, match="byte limit"):
                await resolved(c.result(RESULT_ID))
    assert body.closed and body.consumed == min(size, 8193)
    assert len(requests) == 1
    assert requests[0].url.path == f"/prefix/api/v1/computers/vm-1/results/{RESULT_ID}"
    assert requests[0].headers["authorization"] == "Bearer key"


@pytest.mark.parametrize(
    "body_bytes", [b"{}", b'{"version":1,"version":1}', b"[]", b'{"x":NaN}', b"\xff"]
)
async def test_complete_json_still_requires_finite_valid_metadata(
    asynchronous: bool, body_bytes: bytes
) -> None:
    body = TrackedBody([body_bytes])
    async with connected(
        asynchronous,
        lambda req: httpx.Response(200, stream=body, headers={"Content-Type": "application/json"}),
    ) as (c, _):
        with pytest.raises(mc.MandalaError):
            await resolved(c.result(RESULT_ID))
    assert body.closed


@pytest.mark.parametrize(
    "status,cls",
    [
        (401, mc.AuthenticationError),
        (403, mc.PermissionDeniedError),
        (404, mc.NotFoundError),
        (409, mc.ConflictError),
        (429, mc.RateLimitError),
        (503, mc.UnavailableError),
    ],
)
async def test_oversized_error_bodies_preserve_status_and_close(
    asynchronous: bool, status: int, cls: type
) -> None:
    body = TrackedBody([b"x"] * 20000)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(
            status, stream=body, headers={"Content-Type": "application/json", "Retry-After": "60"}
        )

    async with connected(asynchronous, handler) as (c, _):
        with pytest.raises(cls) as error:
            await resolved(c.retain_execution_output(EXECUTION_ID))
    assert error.value.status == status
    assert len(str(error.value)) < 1000
    assert body.closed and body.consumed == 8193 and len(calls) == 1


@pytest.mark.parametrize(
    "operation", ["result", "capture", "artifact", "delete_result", "delete_artifact"]
)
async def test_injected_redirect_enabled_clients_never_follow_location(
    asynchronous: bool, operation: str
) -> None:
    calls = []
    body = TrackedBody([b""])

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(307, stream=body, headers={"Location": "https://other.test/secret"})

    async with connected(asynchronous, handler) as (c, _):
        action = {
            "result": lambda: c.result(RESULT_ID),
            "capture": lambda: c.retain_execution_output(EXECUTION_ID),
            "artifact": lambda: c.download_artifact(ARTIFACT_ID),
            "delete_result": lambda: c.delete_result(RESULT_ID),
            "delete_artifact": lambda: c.delete_artifact(ARTIFACT_ID),
        }[operation]
        with pytest.raises(mc.APIError) as error:
            await resolved(action())
    assert error.value.status == 307 and len(calls) == 1 and body.closed


@pytest.mark.parametrize(
    "headers",
    [
        [
            ("Content-Type", "application/octet-stream"),
            ("Content-Length", "3"),
            ("Content-Length", "3"),
        ],
        [
            ("Content-Type", "application/octet-stream"),
            ("Content-Length", "3"),
            ("Content-Encoding", "gzip"),
        ],
        [
            ("Content-Type", "application/octet-stream"),
            ("Content-Length", "3"),
            ("Content-Range", "bytes 0-2/3"),
        ],
        [("Content-Type", "application/octet-stream")],
    ],
)
async def test_artifact_header_refusal_precedes_body_read(
    asynchronous: bool, headers: list[tuple[str, str]]
) -> None:
    body = TrackedBody([b"abc"])

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/download"):
            return httpx.Response(200, stream=body, headers=headers)
        return httpx.Response(200, json=artifact_manifest())

    async with connected(asynchronous, handler) as (c, _):
        with pytest.raises(mc.MandalaError):
            await resolved(c.download_artifact(ARTIFACT_ID))
    assert body.closed and body.consumed == 0


@pytest.mark.parametrize("kind", ["reset", "timeout", "extra", "short"])
async def test_body_failures_close_without_partial_success(asynchronous: bool, kind: str) -> None:
    error = (
        httpx.ReadError("connection reset")
        if kind == "reset"
        else httpx.ReadTimeout("read timeout")
        if kind == "timeout"
        else None
    )
    chunks = [b"a"] if error or kind == "short" else [b"abc", b"d", b"unread"]
    body = TrackedBody(chunks, error)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/download"):
            return httpx.Response(
                200,
                stream=body,
                headers={"Content-Type": "application/octet-stream", "Content-Length": "3"},
            )
        return httpx.Response(200, json=artifact_manifest())

    async with connected(asynchronous, handler) as (c, _):
        with pytest.raises(mc.MandalaError):
            await resolved(c.download_artifact(ARTIFACT_ID))
    assert body.closed
    if kind == "extra":
        assert body.consumed == 4


@pytest.mark.parametrize("timeout", [10, 120, None])
async def test_capture_and_download_only_widen_read_write_phases(
    asynchronous: bool, timeout: int | None
) -> None:
    phases = []

    def handler(req: httpx.Request) -> httpx.Response:
        phases.append(req.extensions["timeout"])
        if req.method == "POST":
            return httpx.Response(201, json=result_manifest())
        if req.url.path.endswith("/download"):
            return httpx.Response(
                200, content=b"abc", headers={"Content-Type": "application/octet-stream"}
            )
        return httpx.Response(200, json=artifact_manifest())

    async with connected(
        asynchronous, handler, timeout=httpx.Timeout(timeout, connect=2, pool=3)
    ) as (c, _):
        await resolved(c.retain_execution_output(EXECUTION_ID))
        assert await resolved(c.download_artifact(ARTIFACT_ID)) == b"abc"
    widened = None if timeout is None else max(90, timeout)
    assert phases == [
        {"connect": 2, "read": widened, "write": widened, "pool": 3},
        {"connect": 2, "read": timeout, "write": timeout, "pool": 3},
        {"connect": 2, "read": widened, "write": widened, "pool": 3},
    ]


async def test_async_cancellation_during_body_closes_and_never_returns_partial_bytes() -> None:
    started = asyncio.Event()

    class HeldBody(TrackedBody):
        async def __aiter__(self):
            yield b"a"
            started.set()
            await asyncio.Event().wait()

    body = HeldBody([])
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith("/download"):
            return httpx.Response(
                200,
                stream=body,
                headers={"Content-Type": "application/octet-stream", "Content-Length": "3"},
            )
        return httpx.Response(200, json=artifact_manifest())

    async with connected(True, handler) as (c, _):
        task = asyncio.create_task(c.download_artifact(ARTIFACT_ID))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel("cancel body")
        with pytest.raises(
            asyncio.CancelledError,
            match="cancel body" if CANCEL_MESSAGE_PROPAGATES else None,
        ):
            await task
    assert body.closed and len(calls) == 2


async def test_cancellation_after_metadata_closure_prevents_binary_request() -> None:
    calls = []

    class CancellingMetadata(TrackedBody):
        async def aclose(self) -> None:
            await super().aclose()
            asyncio.current_task().cancel("between requests")

    body = CancellingMetadata([json.dumps(artifact_manifest()).encode()])

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(200, stream=body, headers={"Content-Type": "application/json"})

    async with connected(True, handler) as (c, _):
        task = asyncio.create_task(c.download_artifact(ARTIFACT_ID))
        with pytest.raises(
            asyncio.CancelledError,
            match="between requests" if CANCEL_MESSAGE_PROPAGATES else None,
        ):
            await task
    assert body.closed and len(calls) == 1


async def test_cancellation_while_waiting_for_headers_propagates() -> None:
    started = asyncio.Event()
    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async with connected(True, handler) as (c, _):
        task = asyncio.create_task(c.result(RESULT_ID))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel("headers")
        with pytest.raises(
            asyncio.CancelledError,
            match="headers" if CANCEL_MESSAGE_PROPAGATES else None,
        ):
            await task
    assert len(calls) == 1


async def test_cancellation_queued_by_hash_verification_prevents_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mandala_computer import _async_computer

    original = _async_computer.verify_download
    body = TrackedBody([b"abc"])
    calls = []

    def cancel_after_hash(artifact, content):
        value = original(artifact, content)
        asyncio.current_task().cancel("verified cancellation")
        return value

    monkeypatch.setattr(_async_computer, "verify_download", cancel_after_hash)

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.endswith("/download"):
            return httpx.Response(
                200,
                stream=body,
                headers={"Content-Type": "application/octet-stream", "Content-Length": "3"},
            )
        return httpx.Response(200, json=artifact_manifest())

    async with connected(True, handler) as (c, _):
        task = asyncio.create_task(c.download_artifact(ARTIFACT_ID))
        with pytest.raises(
            asyncio.CancelledError,
            match="verified cancellation" if CANCEL_MESSAGE_PROPAGATES else None,
        ):
            await task

    assert body.closed and len(calls) == 2
    assert task.cancelled()


@pytest.mark.parametrize(
    "headers",
    [
        [("X-Result-Offset", "0"), ("X-Result-Offset", "0")],
        [("X-Result-Next-Offset", "1"), ("X-Result-Next-Offset", "1")],
        [("X-Result-EOF", "true"), ("X-Result-EOF", "true")],
    ],
)
async def test_duplicate_result_evidence_is_refused_before_bytes(
    asynchronous: bool, headers
) -> None:
    body = TrackedBody([b"a"])
    metadata = [("Content-Type", "application/octet-stream"), ("Content-Length", "1")]
    async with connected(
        asynchronous, lambda req: httpx.Response(200, stream=body, headers=metadata + headers)
    ) as (c, _):
        with pytest.raises(mc.MandalaError, match="duplicate"):
            await resolved(c.result_output(RESULT_ID, stream="stdout", offset=0))
    assert body.closed and body.consumed == 0


async def test_concurrent_readers_keep_their_own_identity_and_offset() -> None:
    other = "res_" + "c" * 32
    arrived = asyncio.Event()
    calls = []

    async def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 2:
            arrived.set()
        await arrived.wait()
        offset = int(req.url.params["offset"])
        body = b"a" if RESULT_ID in req.url.path else b"b"
        return httpx.Response(
            200,
            content=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Result-Offset": str(offset),
                "X-Result-Next-Offset": str(offset + 1),
                "X-Result-EOF": "true",
            },
        )

    async with connected(True, handler) as (c, _):
        first, second = await asyncio.wait_for(
            asyncio.gather(
                c.result_output(RESULT_ID, stream="stdout", offset=7),
                c.result_output(other, stream="stderr", offset=14),
            ),
            1,
        )
    assert (first.result_id, first.offset, first.next_offset, first.data) == (RESULT_ID, 7, 8, b"a")
    assert (second.result_id, second.offset, second.next_offset, second.data) == (
        other,
        14,
        15,
        b"b",
    )


async def test_opted_in_exec_cannot_replay_via_a_redirect(asynchronous: bool) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(307, headers={"Location": "https://other.test/exec"})

    async with connected(asynchronous, handler) as (c, raw):
        with pytest.raises(mc.APIError) as error:
            await resolved(c.exec("true", retain_output=True))
        assert error.value.status == 307 and not raw.is_closed
    assert len(calls) == 1 and calls[0].method == "POST"


async def test_default_exec_keeps_the_injected_clients_existing_redirect_policy(
    asynchronous: bool,
) -> None:
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(307, headers={"Location": "/existing-policy"})
        return httpx.Response(200, json={"exit_code": 0})

    async with connected(asynchronous, handler) as (c, raw):
        result = await resolved(c.exec("true", retain_output=False))
        assert result.exit_code == 0 and not raw.is_closed
    assert len(calls) == 2
