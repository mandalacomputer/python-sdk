"""Opt-in transport retries, complete bodies, and terminal failures."""

import httpx
import pytest

from mandala_computer import (
    APIError,
    AsyncClient,
    ConnectionError,
    ConnectionInterruptedError,
    RateLimitError,
    TimeoutError,
    _client,
)
from mandala_computer._client import AsyncTransport


class Broken(httpx.AsyncByteStream):
    def __init__(self, prefix=b"lost", error=None):
        self.prefix = prefix
        self.error = error or httpx.ReadError("connection dropped")
        self.closed = False

    async def __aiter__(self):
        yield self.prefix
        raise self.error

    async def aclose(self):
        self.closed = True


@pytest.fixture
def delays(monkeypatch):
    seen = []

    async def sleep(delay):
        seen.append(delay)

    monkeypatch.setattr(_client, "_async_retry_sleep", sleep)
    return seen


def make(handler, retries=None):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AsyncTransport(
        "test-key", base_url="https://example.test/api/v1", retries=retries, client=client
    )


async def test_opt_in_recovers_503(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json={"recovered": True})

    t = make(handler, {"idempotent": 1})
    assert await t.json("GET", "/computers") == {"recovered": True}
    assert len(calls) == 2
    assert delays == [0.25]


@pytest.mark.parametrize("policy", [None, {"idempotent": 0}])
async def test_default_one_attempt(policy, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={})

    with pytest.raises(APIError):
        await make(handler, policy).json("GET", "/computers")
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize(
    "policy",
    [
        {},
        [],
        "2",
        2,
        {"idempotent": -1},
        {"idempotent": 1.5},
        {"idempotent": float("inf")},
        {"idempotent": float("nan")},
        {"idempotent": True},
        {"idempotent": "2"},
        {"idempotent": 1, "extra": 2},
    ],
)
def test_invalid_policy_refused_at_construction(policy):
    with pytest.raises(ValueError, match="retries"):
        AsyncClient("test", retries=policy)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_methods_statuses_metadata_and_request_identity(method, status, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": f"failure {len(calls)}"}, headers={"Retry-After": "3"}
        )

    t = make(handler, {"idempotent": 2})
    with pytest.raises(APIError) as caught:
        await t.request(
            method, "/computers", params={"tag": "two words"}, headers={"X-Test": "kept"}
        )
    assert caught.value.status == status
    assert caught.value.body == {"error": "failure 3"}
    assert caught.value.retry_after == 3
    assert len(calls) == 3
    assert delays == [3, 3]
    assert str(calls[0].url) == "https://example.test/api/v1/computers?tag=two+words"
    assert calls[0].headers["Authorization"] == "Bearer test-key"
    for call in calls[1:]:
        assert call.method == calls[0].method
        assert call.url == calls[0].url
        assert call.headers == calls[0].headers


@pytest.mark.parametrize(
    "error,expected",
    [
        (httpx.ConnectError, ConnectionError),
        (httpx.ReadError, ConnectionInterruptedError),
        (httpx.RemoteProtocolError, ConnectionInterruptedError),
    ],
)
async def test_connection_errors_preserve_final_class(error, expected, delays):
    calls = []

    def handler(request):
        calls.append(request)
        raise error("failed")

    with pytest.raises(expected):
        await make(handler, {"idempotent": 2}).json("GET", "/computers")
    assert len(calls) == 3
    assert delays == [0.25, 0.5]


@pytest.mark.parametrize("status", [401, 402, 403, 408, 409, 429, 500, 501, 520, 522])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
async def test_known_nonretryable_status_survives_interrupted_body(status, kind, delays):
    bodies = []

    def handler(request):
        body = Broken()
        bodies.append(body)
        return httpx.Response(status, stream=body, headers={"Retry-After": "9"})

    t = make(handler, {"idempotent": 3})
    with pytest.raises(RateLimitError if status == 429 else APIError) as caught:
        if kind == "ordinary":
            await t.json("GET", "/computers")
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            await anext(t.sse("GET", "/builds/b/events"))
    assert caught.value.status == status
    assert caught.value.retry_after == 9
    assert len(bodies) == 1 and bodies[0].closed
    assert delays == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("shape", ["status", "connect", "body"])
async def test_mutations_are_never_retried(method, shape, delays):
    calls = []

    def handler(request):
        calls.append(request)
        if shape == "connect":
            raise httpx.ConnectError("refused")
        return (
            httpx.Response(503, json={})
            if shape == "status"
            else httpx.Response(200, stream=Broken())
        )

    with pytest.raises((APIError, ConnectionError)):
        await make(handler, {"idempotent": 3}).json(method, "/computers", json={"name": "once"})
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("suffix", ["12", "12/", "12?offset=0"])
async def test_consuming_legacy_reads_are_excluded(method, suffix, delays):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadError("lost answer")

    with pytest.raises(ConnectionInterruptedError):
        await make(handler, {"idempotent": 3}).json(method, "/computers/vm/exec/" + suffix)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["json", "listing", "binary", "bounded_json", "bounded_binary"])
async def test_dropped_finite_body_restarts_without_prefix(kind, delays):
    binary = kind in ("binary", "bounded_binary")
    complete = (
        b"complete"
        if binary
        else b'[{"id":"complete"}]'
        if kind == "listing"
        else b'{"id":"complete"}'
    )
    broken = Broken()
    calls = []

    def handler(request):
        calls.append(request)
        headers = {
            "content-type": "application/octet-stream" if binary else "application/json",
            "content-length": str(len(complete)),
        }
        if len(calls) == 1:
            return httpx.Response(200, stream=broken, headers=headers)
        assert broken.closed
        return httpx.Response(200, content=complete, headers=headers)

    t = make(handler, {"idempotent": 2})
    if kind == "json":
        result = await t.json("GET", "/computers")
        assert result == {"id": "complete"}
    elif kind == "listing":
        result = await t.listing("/computers")
        assert result == ([{"id": "complete"}], None)
    elif kind == "binary":
        assert (
            await t.binary(
                "GET",
                "/computers/vm/files",
                accept="application/octet-stream",
                content_types=("application/octet-stream",),
            )
            == complete
        )
    elif kind == "bounded_json":
        assert await t.bounded_json_object("GET", "/computers/vm/results/r", max_bytes=100) == {
            "id": "complete"
        }
    else:
        result, _ = await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
        assert result == complete
    assert len(calls) == 2
    assert broken.closed


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout]
)
async def test_timeouts_are_terminal(error, delays):
    calls = []

    def handler(request):
        calls.append(request)
        raise error("timed out")

    with pytest.raises(TimeoutError):
        await make(handler, {"idempotent": 2}).json("GET", "/computers")
    assert len(calls) == 1
    assert delays == []


async def test_invalid_json_and_retained_integrity_are_terminal(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            content=b"invalid",
            headers={"content-type": "application/octet-stream", "content-length": "10"},
        )

    t = make(handler, {"idempotent": 3})
    with pytest.raises(Exception, match="JSON"):
        await t.json_object("GET", "/computers")
    with pytest.raises(Exception, match="declared size"):
        await t.bounded_binary("GET", "/computers/vm/artifacts/a/download", max_bytes=20)
    assert len(calls) == 2
    assert delays == []


async def test_backoff_doubles_to_cap(delays):
    with pytest.raises(APIError):
        await make(lambda request: httpx.Response(503), {"idempotent": 9}).json("GET", "/computers")
    assert delays == [0.25, 0.5, 1, 2, 4, 8, 16, 30, 30]


@pytest.mark.parametrize(
    "header,expected",
    [
        ("0", 0.25),
        ("3", 3),
        ("-3", 0.25),
        ("1.5", 0.25),
        ("bad", 0.25),
        ("Wed, 01 Jan 2031 00:00:04 GMT", 4),
    ],
)
async def test_retry_after_lower_bound(header, expected, delays, monkeypatch):
    monkeypatch.setattr(_client.time, "time", lambda: 1924992000)
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(503, headers={"Retry-After": header})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    await make(handler, {"idempotent": 1}).json("GET", "/computers")
    assert delays == [expected]


async def test_timeout_cap_deducts_monotonic_elapsed_time(delays, monkeypatch):
    now = [100.0]
    caps = []
    monkeypatch.setattr(_client.time, "monotonic", lambda: now[0])

    async def sleep(delay):
        now[0] += delay

    monkeypatch.setattr(_client, "_async_retry_sleep", sleep)

    def handler(request):
        caps.append(request.extensions["timeout"]["read"])
        now[0] += 0.6
        return httpx.Response(503, json={})

    with pytest.raises(TimeoutError):
        await make(handler, {"idempotent": 5}).json("GET", "/computers", timeout_cap=1.6)
    assert caps == pytest.approx([1.6, 0.75])


async def test_huge_retry_after_cannot_exceed_explicit_budget(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "9999999999999999999999999"})

    with pytest.raises(TimeoutError):
        await make(handler, {"idempotent": 2}).json("GET", "/computers", timeout_cap=1)
    assert len(calls) == 1
    assert delays == []


async def test_caller_mutation_does_not_change_policy(delays):
    policy = {"idempotent": 1}
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={})

    t = make(handler, policy)
    policy["idempotent"] = 0
    await t.json("GET", "/computers")
    assert len(calls) == 2


async def test_sse_retries_before_first_event_only(delays):
    broken = Broken(b": heartbeat\n\n")
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, stream=broken, headers={"content-type": "text/event-stream"})
        assert broken.closed
        return httpx.Response(
            200,
            content=b'event: ready\ndata: {"id":2}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    t = make(handler, {"idempotent": 2})
    events = [event async for event in t.sse("GET", "/builds/b/events")]
    assert len(events) == 1 and events[0].data == {"id": 2}
    assert len(calls) == 2


async def test_sse_never_replays_exposed_event(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            stream=Broken(b'event: ready\ndata: {"id":1}\n\n'),
            headers={"content-type": "text/event-stream"},
        )

    events = []
    with pytest.raises(ConnectionInterruptedError):
        async for event in make(handler, {"idempotent": 2}).sse("GET", "/builds/b/events"):
            events.append(event)
    assert len(events) == 1
    assert len(calls) == 1


async def test_post_agent_stream_is_not_retried(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={})

    t = make(handler, {"idempotent": 3})
    with pytest.raises(APIError):
        await anext(t.sse("POST", "/computers/vm/agent", json={"task": "once"}))
    assert len(calls) == 1


async def test_public_constructor_forwards_policy(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=[])

    c = AsyncClient(
        "test",
        retries={"idempotent": 1},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await c.sizes.list()
    assert result == [] and len(calls) == 2


async def test_cancellation_during_backoff_prevents_later_attempt(monkeypatch):
    import asyncio

    waiting = asyncio.Event()
    calls = []

    async def sleep(delay):
        waiting.set()
        await asyncio.Future()

    monkeypatch.setattr(_client, "_async_retry_sleep", sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    task = asyncio.create_task(make(handler, {"idempotent": 3}).json("GET", "/computers"))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
async def test_cancellation_closes_inflight_body_without_retry(kind, delays):
    import asyncio

    reading = asyncio.Event()

    class Hanging(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            reading.set()
            await asyncio.Future()
            yield b""

        async def aclose(self):
            self.closed = True

    body = Hanging()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            stream=body,
            headers={
                "content-type": "text/event-stream"
                if kind == "sse"
                else "application/octet-stream",
                "content-length": "1",
            },
        )

    t = make(handler, {"idempotent": 3})

    async def read():
        if kind == "ordinary":
            await t.json("GET", "/computers")
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=1)
        else:
            await anext(t.sse("GET", "/builds/b/events"))

    task = asyncio.create_task(read())
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed and len(calls) == 1
    assert delays == []


async def test_concurrent_calls_own_independent_counters(delays):
    import asyncio

    calls = {}

    def handler(request):
        key = str(request.url)
        calls[key] = calls.get(key, 0) + 1
        return httpx.Response(503) if calls[key] == 1 else httpx.Response(200, json={})

    t = make(handler, {"idempotent": 1})
    await asyncio.gather(t.json("GET", "/computers/a"), t.json("GET", "/computers/b"))
    assert list(calls.values()) == [2, 2]


async def test_huge_delay_uses_bounded_cancelable_timer_chunks(monkeypatch):
    import asyncio

    observed = []

    async def sleep(delay):
        observed.append(delay)
        raise asyncio.CancelledError()

    monkeypatch.setattr(_client.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await _client._async_retry_sleep(1e100)
    assert observed == [86400.0]


async def test_non_event_stream_cannot_gain_retry_permission_from_diagnostic_read(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Broken(), headers={"content-type": "text/html"})

    with pytest.raises(Exception, match="event stream"):
        await anext(make(handler, {"idempotent": 2}).sse("GET", "/builds/b/events"))
    assert len(calls) == 1
    assert delays == []


async def test_public_template_preparation_stays_single_attempt(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={})

    c = AsyncClient(
        "test",
        retries={"idempotent": 3},
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(APIError):
        await c.computers.create(template="base", template_transfer="opaque-token")
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("shape", ["status", "connect"])
async def test_followed_redirect_to_consuming_get_cannot_be_retried(shape, delays):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/computers":
            return httpx.Response(307, headers={"Location": "/computers/vm/exec/12"})
        if shape == "connect":
            raise httpx.ReadError("lost answer", request=request)
        return httpx.Response(503)

    t = AsyncTransport(
        "test",
        base_url="https://example.test",
        retries={"idempotent": 2},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True),
    )
    with pytest.raises((APIError, ConnectionError)):
        await t.json("GET", "/computers")
    assert len(calls) == 2
    assert delays == []


@pytest.mark.parametrize(
    "method,path,policy",
    [
        ("GET", "/computers", None),
        ("GET", "/computers", {"idempotent": 0}),
        ("POST", "/computers", {"idempotent": 3}),
        ("GET", "/computers/vm/exec/12", {"idempotent": 3}),
    ],
)
@pytest.mark.parametrize("cap,expected", [(0.25, 0.25), (0, 0.001)])
async def test_non_retry_operation_preserves_exact_cap_without_reading_clock(
    method, path, policy, cap, expected, monkeypatch
):
    from types import SimpleNamespace

    def forbidden_clock():
        raise AssertionError("single-attempt operation must not read a retry clock")

    monkeypatch.setattr(_client, "time", SimpleNamespace(monotonic=forbidden_clock))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={})

    await make(handler, policy).json(method, path, timeout_cap=cap)
    assert len(calls) == 1
    assert calls[0].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), expected
    )


async def test_terminal_timeout_preserves_httpx_cause_after_budget_expiry(monkeypatch, delays):
    from types import SimpleNamespace

    now = [100.0]
    monkeypatch.setattr(_client, "time", SimpleNamespace(monotonic=lambda: now[0]))
    native = httpx.ReadTimeout("original phase timeout")

    def handler(request):
        now[0] = 102.0
        raise native

    t = make(handler, {"idempotent": 2})
    with pytest.raises(TimeoutError) as caught:
        await t.json("GET", "/computers", timeout_cap=1)
    assert caught.value.__cause__ is native
    assert t.phase_ceiling(caught.value) == 5.0
    assert delays == []


async def test_exhausted_http_failure_is_not_reclassified_after_budget_expiry(monkeypatch, delays):
    from types import SimpleNamespace

    now = [100.0]
    monkeypatch.setattr(_client, "time", SimpleNamespace(monotonic=lambda: now[0]))
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            now[0] = 102.0
        return httpx.Response(503, json={"error": f"failure {len(calls)}"})

    with pytest.raises(APIError) as caught:
        await make(handler, {"idempotent": 1}).json("GET", "/computers", timeout_cap=1)
    assert caught.value.status == 503
    assert caught.value.body == {"error": "failure 2"}
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["json", "binary", "sse"])
@pytest.mark.parametrize("policy", [None, {"idempotent": 2}])
async def test_content_decoding_failure_is_terminal_with_original_class(kind, policy, delays):
    bodies = []

    class EncodedBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"not a gzip body"

        async def aclose(self):
            self.closed = True

    def handler(request):
        body = EncodedBody()
        bodies.append(body)
        return httpx.Response(
            200,
            stream=body,
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "text/event-stream" if kind == "sse" else "application/json",
            },
        )

    t = make(handler, policy)
    with pytest.raises(ConnectionInterruptedError) as caught:
        if kind == "sse":
            await anext(t.sse("GET", "/builds/b/events"))
        elif kind == "binary":
            await t.binary(
                "GET",
                "/computers/vm/files",
                accept="application/octet-stream",
                content_types=("application/octet-stream",),
            )
        else:
            await t.json("GET", "/computers")
    assert isinstance(caught.value.__cause__, httpx.DecodingError)
    assert len(bodies) == 1 and bodies[0].closed
    assert delays == []


@pytest.mark.parametrize("digits", [309, 5000])
async def test_overflowing_numeric_retry_after_cannot_bypass_budget(digits, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "9" * digits})

    with pytest.raises(TimeoutError):
        await make(handler, {"idempotent": 2}).json("GET", "/computers", timeout_cap=1)
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("kind", ["json", "bounded", "sse"])
async def test_overflowing_header_uses_interruptible_long_wait_without_cap(kind, monkeypatch):
    import asyncio

    calls, waits = [], []

    async def sleep(delay):
        if delay == 0:
            return
        waits.append(delay)
        raise asyncio.CancelledError()

    monkeypatch.setattr(_client.asyncio, "sleep", sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "9" * 309})

    t = make(handler, {"idempotent": 2})
    with pytest.raises(asyncio.CancelledError):
        if kind == "sse":
            await anext(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            await t.json("GET", "/computers")
    assert waits == [86400.0]
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["json", "sse"])
@pytest.mark.parametrize("failure", ["status", "connection", "redirect_body"])
async def test_intermediate_consuming_redirect_is_never_replayed(kind, failure, delays):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/computers":
            return httpx.Response(307, headers={"Location": "/computers/vm/exec/12"})
        if request.url.path == "/computers/vm/exec/12":
            return httpx.Response(
                307,
                headers={"Location": "/safe-final"},
                **({"stream": Broken()} if failure == "redirect_body" else {}),
            )
        if failure == "connection":
            raise httpx.ConnectError("final connection failed", request=request)
        return httpx.Response(503)

    t = AsyncTransport(
        "test",
        base_url="https://example.test",
        retries={"idempotent": 2},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True),
    )
    with pytest.raises((APIError, ConnectionError)):
        if kind == "sse":
            await anext(t.sse("GET", "/computers"))
        else:
            await t.json("GET", "/computers")
    assert calls == ["/computers", "/computers/vm/exec/12"] + (
        [] if failure == "redirect_body" else ["/safe-final"]
    )
    assert delays == []


@pytest.mark.parametrize("kind", ["json", "sse"])
async def test_complete_safe_redirect_chain_can_retry_without_changing_client_auth_or_hooks(
    kind, delays
):
    calls, auth_calls, hook_calls = [], [], []

    class Auth(httpx.Auth):
        def auth_flow(self, request):
            auth_calls.append(request.url.path)
            request.headers["X-Custom-Auth"] = "kept"
            yield request

    async def hook(request):
        hook_calls.append(request.url.path)

    def handler(request):
        calls.append(request.url.path)
        assert request.headers["X-Custom-Auth"] == "kept"
        if request.url.path == "/computers":
            return httpx.Response(307, headers={"Location": "/safe-final"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(
            200,
            content=b"event: ready\ndata: {}\n\n" if kind == "sse" else b"{}",
            headers={"Content-Type": "text/event-stream" if kind == "sse" else "application/json"},
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        auth=Auth(),
        event_hooks={"request": [hook]},
    )
    hooks = list(http.event_hooks["request"])
    t = AsyncTransport(
        "test", base_url="https://example.test", retries={"idempotent": 1}, client=http
    )
    if kind == "sse":
        events = [event async for event in t.sse("GET", "/computers")]
        assert len(events) == 1
    else:
        assert await t.json("GET", "/computers") == {}
    assert calls == hook_calls == ["/computers", "/safe-final", "/computers", "/safe-final"]
    assert auth_calls == ["/computers", "/computers"]
    assert http.event_hooks["request"] == hooks and http.follow_redirects is True
    assert delays == [0.25]


@pytest.mark.parametrize("follows,override", [(True, None), (False, True), (True, False)])
async def test_connection_failure_without_response_obeys_effective_redirect_policy(
    follows, override, delays
):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("no final response", request=request)

    t = AsyncTransport(
        "test",
        retries={"idempotent": 1},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=follows),
    )
    with pytest.raises(ConnectionError):
        await t.json("GET", "/computers", follow_redirects=override)
    assert len(calls) == (2 if override is False else 1)
    assert delays == ([0.25] if override is False else [])


async def test_previous_retry_after_does_not_leak_into_next_connection_failure(delays):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "7"})
        if len(calls) == 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={})

    assert await make(handler, {"idempotent": 2}).json("GET", "/computers") == {}
    assert delays == [7, 0.5]


async def test_http_date_delay_is_evaluated_after_error_body_consumption(monkeypatch, delays):
    from types import SimpleNamespace

    now = [1924992000.0]
    monkeypatch.setattr(_client, "time", SimpleNamespace(time=lambda: now[0]))

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            now[0] += 5
            yield b"{}"

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                503, stream=SlowBody(), headers={"Retry-After": "Wed, 01 Jan 2031 00:00:04 GMT"}
            )
        return httpx.Response(200, json={})

    assert await make(handler, {"idempotent": 1}).json("GET", "/computers") == {}
    assert delays == [0.25]
