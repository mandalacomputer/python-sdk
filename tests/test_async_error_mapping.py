"""Async transport diagnostics and stream ownership match the sync public API."""

import asyncio

import httpx
import pytest
from tests.test_error_mapping import (
    AUTH_CASES,
    BASE,
    BRANCHES,
    COMPUTER,
    CORRELATION,
    HEADERS,
    assert_metadata,
    stream_response,
)

import mandala_computer as mc
from mandala_computer import _client


class Broken(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        yield b'{"error":'
        raise httpx.ReadError("response interrupted")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("allow", ["GET, HEAD, POST, OPTIONS", "GET, HEAD, OPTIONS", None])
async def test_async_405_and_specific_allow(allow):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            405,
            json={
                "error": "method not allowed",
                "request_id": "body-id",
                "allow": "ignored",
                "www_authenticate": "ignored",
            },
            headers={"X-Request-ID": "method-id", **({"Allow": allow} if allow else {})},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        async with mc.AsyncClient(
            "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
        ) as client:
            with pytest.raises(mc.MethodNotAllowedError) as caught:
                await client._t.json("PUT", "/computers")
        assert not http.is_closed
    assert caught.value.status == 405 and caught.value.allow == allow
    assert caught.value.request_id == "method-id" and caught.value.www_authenticate is None
    assert not mc.is_transient(caught.value) and len(calls) == 1


@pytest.mark.parametrize("payload,reason,challenge,message", AUTH_CASES)
async def test_async_auth_matrix(payload, reason, challenge, message):
    body = {
        **payload,
        "request_id": "body-auth",
        "usage": {"input_tokens": 17},
        "steps": [{"action": "click"}],
        "extra": "retained",
    }
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            401,
            json=body,
            headers={
                "X-Request-ID": "header-auth",
                **({"WWW-Authenticate": challenge} if challenge else {}),
            },
        )

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        mc.AsyncClient(
            "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
        ) as client,
    ):
        with pytest.raises(mc.AuthenticationError) as caught:
            await client.computers.list()
    error = caught.value
    assert error.status == 401 and str(error) == message and error.body == body
    assert error.reason == reason and error.request_id == "header-auth"
    assert error.www_authenticate == challenge and not mc.is_transient(error)
    assert len(calls) == 1


@pytest.mark.parametrize("header,body_id,expected", CORRELATION)
async def test_async_header_first_top_level_correlation(header, body_id, expected):
    body = {"error": {"message": "refused", "request_id": "nested-id"}, "request_id": body_id}
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401, json=body, headers={} if header is None else {"X-Request-ID": header}
                )
            )
        ) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.AuthenticationError) as caught:
            await client.computers.list()
    assert caught.value.request_id == expected


@pytest.mark.parametrize(
    "method,content",
    [("HEAD", b""), ("GET", b""), ("GET", b"<html>refused</html>"), ("GET", b'{"error":')],
)
async def test_async_head_and_unreadable_bodies_keep_headers(method, content):
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(405, content=content, headers=HEADERS)
            )
        ) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.MethodNotAllowedError) as caught:
            await client._t.json(method, "/computers")
    assert_metadata(caught.value)


@pytest.mark.parametrize("status", [401, 405, 429])
async def test_async_known_refusal_keeps_headers_and_closes_broken_response(status):
    body = Broken()
    response = httpx.Response(status, stream=body, headers={**HEADERS, "Retry-After": "7"})
    calls = []

    def handler(request):
        calls.append(request)
        return response

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        mc.AsyncClient(
            "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
        ) as client,
    ):
        with pytest.raises(mc.APIError) as caught:
            await client.computers.list()
    assert_metadata(caught.value)
    assert caught.value.status == status and caught.value.retry_after == 7
    assert len(calls) == 1 and response.is_closed and body.closed


@pytest.mark.parametrize("status,extra,cls", BRANCHES)
async def test_async_constructor_branches(status, extra, cls):
    body = {**extra, "request_id": "body-branch"}
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status,
                    json=body,
                    headers={**HEADERS, "Retry-After": "13", "Content-Range": "bytes */12345"},
                )
            )
        ) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(cls) as caught:
            await client.computers.list()
    error = caught.value
    assert_metadata(error)
    assert error.retry_after == 13 and error.status == status and error.body == body
    if isinstance(error, mc.RangeNotSatisfiableError):
        assert error.size == 12345
    if isinstance(error, mc.MoveRequiredError):
        assert error.move_possible is extra["move"]["possible"]


@pytest.mark.parametrize("status", [401, 402, 403, 404, 405])
async def test_async_clearing_reason_cannot_make_auth_and_request_errors_retry(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"reason": "starting"})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        mc.AsyncClient(
            "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
        ) as client,
    ):
        with pytest.raises(mc.APIError) as caught:
            await client.computers.list()
    assert len(calls) == 1 and not mc.is_transient(caught.value)


@pytest.mark.parametrize(
    "status,message", [(404, "no such file in the guest"), (400, "permission denied")]
)
async def test_async_actual_guest_file_resource_keeps_status(status, message):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": message}, headers=HEADERS)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.APIError) as caught:
            await mc.AsyncComputer(client._t, COMPUTER).read_file("/tmp/missing")
    assert type(caught.value) is (mc.NotFoundError if status == 404 else mc.APIError)
    assert str(caught.value) == message
    assert_metadata(caught.value)
    assert calls[0].url.path == "/api/v1/computers/vm-1/files"


@pytest.mark.parametrize("status", [0, 400, 401, 429, 504, 520])
async def test_async_stream_keeps_frame_and_partial_work_with_isolated_metadata(status):
    body = {
        "error": "run stopped",
        "status": status,
        "reason": "starting",
        "request_id": "frame-id",
        "usage": {"input_tokens": 31},
        "steps": [{"action": "click"}],
    }
    responses = []

    def handler(request):
        response = stream_response(body)
        responses.append(response)
        return response

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        mc.AsyncClient(
            "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
        ) as client,
    ):
        computer = mc.AsyncComputer(client._t, COMPUTER)
        events = [event async for event in computer.agent_stream("task", model_key="sk-test")]
        assert len(events) == 1 and events[0].raw == body
        with pytest.raises(mc.MandalaError) as caught:
            await computer.agent("task", model_key="sk-test")
    error = caught.value
    assert (
        error.agent.raw == body
        and error.agent.usage.input_tokens == 31
        and len(error.agent.steps) == 1
    )
    if status:
        assert_metadata(error, {"request_id": "frame-id", "allow": None, "www_authenticate": None})
        assert error.reason is None and error.retry_after is None
    if status in (504, 520):
        assert type(error) is mc.APIError
    if status == 0:
        assert type(error) is mc.MandalaError
    assert len(responses) == 2 and all(response.is_closed for response in responses)


async def test_async_cancellation_remains_cancellation_and_closes_response():
    entered = asyncio.Event()

    class Waiting(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            self.closed = True

    body = Waiting()
    response = httpx.Response(405, stream=body, headers=HEADERS)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
        async with mc.AsyncClient(
            "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
        ) as client:
            task = asyncio.create_task(client.computers.list())
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert response.is_closed and body.closed
        assert not http.is_closed


async def test_async_final_attempt_and_concurrent_ids_are_response_local(monkeypatch):
    async def no_sleep(delay):
        pass

    monkeypatch.setattr(_client, "_async_retry_sleep", no_sleep)
    attempts = {}
    responses = []

    def handler(request):
        path = request.url.path
        attempts[path] = attempts.get(path, 0) + 1
        response = httpx.Response(
            503,
            json={"error": "unavailable", "request_id": "body-id"},
            headers={"X-Request-ID": f"{path}-{attempts[path]}"},
        )
        responses.append(response)
        return response

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        mc.AsyncClient(
            "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
        ) as client,
    ):
        results = await asyncio.gather(
            client._t.json("GET", "/sizes"),
            client._t.json("GET", "/computers"),
            return_exceptions=True,
        )
    assert [error.request_id for error in results] == ["/api/v1/sizes-3", "/api/v1/computers-3"]
    assert len(responses) == 6 and all(response.is_closed for response in responses)


@pytest.mark.parametrize(
    "mode", ["json", "binary", "listing", "bounded_json_object", "bounded_binary", "sse"]
)
async def test_async_all_response_readers_preserve_headers(mode):
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "refused"}, headers=HEADERS)
            )
        ) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        with pytest.raises(mc.AuthenticationError) as caught:
            if mode == "listing":
                await client._t.listing("/computers")
            elif mode == "sse":
                await client._t.sse("POST", "/computers/vm-1/agent").__anext__()
            elif mode == "binary":
                await client._t.binary(
                    "GET",
                    "/computers",
                    accept="application/octet-stream",
                    content_types=("application/octet-stream",),
                )
            else:
                await getattr(client._t, mode)(
                    "GET",
                    "/computers",
                    **({"max_bytes": 4096} if mode.startswith("bounded") else {}),
                )
    assert_metadata(caught.value)


async def test_async_create_only_409_with_an_interrupted_body_is_never_transient():
    """The async half of the sync test of the same name (OPL-4994, Codex review)."""
    computer_row = {"id": "vm-1", "name": "dev", "status": "running"}

    def handler(request):
        if request.method == "PUT":
            return httpx.Response(409, stream=Broken())
        return httpx.Response(200, json=computer_row)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        computer = mc.AsyncComputer(client._t, computer_row)
        with pytest.raises(mc.FileExistsError) as caught:
            await computer.write_file("/tmp/a", b"hi", overwrite=False)
    assert caught.value.reason is None and not mc.is_transient(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        {"error": "conflict"},
        {"reason": 5},
        {"error": "conflict", "reason": None},
        {"reason": ""},
        {"reason": "   "},
        {},
        {"error": "a file already exists at that path"},
    ],
    ids=[
        "missing",
        "numeric",
        "null",
        "empty-string",
        "blank-string",
        "empty-object",
        "existence-text",
    ],
)
async def test_async_create_only_409_with_no_usable_reason_is_never_transient(body):
    """The async half of the sync test of the same name (OPL-4994, Codex review)."""
    computer_row = {"id": "vm-1", "name": "dev", "status": "running"}

    def handler(request):
        if request.method == "PUT":
            return httpx.Response(409, json=body)
        return httpx.Response(200, json=computer_row)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        computer = mc.AsyncComputer(client._t, computer_row)
        with pytest.raises(mc.FileExistsError) as caught:
            await computer.write_file("/tmp/a", b"hi", overwrite=False)
    assert caught.value.reason is None
    assert "refused as a conflict, reason unknown" in str(caught.value)
    assert "already exists" not in str(caught.value)
    assert not mc.is_transient(caught.value)
