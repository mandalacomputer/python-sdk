"""Opt-in transport retries, complete bodies, and terminal failures."""

import h2.config
import h2.connection
import h2.events
import httpcore
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


@pytest.mark.parametrize("status", [200, 429, 502, 503, 504])
@pytest.mark.parametrize("kind", ["json", "sse"])
@pytest.mark.parametrize("policy", [None, {"idempotent": 1}])
async def test_decoding_error_is_terminal_for_every_sdk_error_class(status, kind, policy, delays):
    bodies = []

    class GzipBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"not a gzip body"

        async def aclose(self):
            self.closed = True

    def handler(request):
        body = GzipBody()
        bodies.append(body)
        return httpx.Response(
            status,
            stream=body,
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "text/event-stream" if kind == "sse" else "application/json",
                "Retry-After": "9",
            },
        )

    t = make(handler, policy)
    with pytest.raises(ConnectionInterruptedError if status == 200 else APIError) as caught:
        if kind == "sse":
            await anext(t.sse("GET", "/builds/b/events"))
        else:
            await t.json("GET", "/computers")
    assert isinstance(caught.value.__cause__, httpx.DecodingError)
    if status != 200:
        assert caught.value.status == status
        assert caught.value.retry_after == 9
    assert len(bodies) == 1 and bodies[0].closed
    assert delays == []


@pytest.mark.parametrize("status", [200, 429, 502, 503, 504])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
@pytest.mark.parametrize("observer", ["response_hook", "auth_body", "auth_flow"])
@pytest.mark.parametrize("policy", [None, {"idempotent": 1}])
async def test_pre_return_body_processing_failure_is_terminal(
    status, kind, observer, policy, delays
):
    bodies, calls, inspected = [], [], []

    async def hook(response):
        inspected.append(response.status_code)
        await response.aread()

    class BodyAuth(httpx.Auth):
        requires_response_body = True

        def auth_flow(self, request):
            inspected.append("auth started")
            yield request

    class FlowAuth(httpx.Auth):
        async def async_auth_flow(self, request):
            response = yield request
            inspected.append(response.status_code)
            await response.aread()

    auth = (
        BodyAuth() if observer == "auth_body" else FlowAuth() if observer == "auth_flow" else None
    )
    hooks = {"response": [hook]} if observer == "response_hook" else {}

    def handler(request):
        calls.append(request)
        body = Broken()
        bodies.append(body)
        return httpx.Response(
            status, stream=body, headers={"Retry-After": "9" * 1000 if status == 503 else "9"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth, event_hooks=hooks)
    original_hooks = {key: list(value) for key, value in http.event_hooks.items()}
    t = AsyncTransport("test", client=http, retries=policy)
    with pytest.raises(ConnectionInterruptedError) as caught:
        if kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        elif kind == "sse":
            await anext(t.sse("GET", "/builds/b/events"))
        else:
            await t.json("GET", "/computers", timeout_cap=1)
    assert isinstance(caught.value.__cause__, httpx.ReadError)
    assert len(calls) == len(bodies) == 1 and bodies[0].closed
    assert inspected == (["auth started"] if observer == "auth_body" else [status])
    assert delays == []
    assert http.auth is auth and http.event_hooks == original_hooks


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
@pytest.mark.parametrize("observer", ["response_hook", "auth_body", "auth_flow"])
async def test_returned_responses_can_retry_after_caller_body_processing(kind, observer, delays):
    calls, inspected = [], []

    async def hook(response):
        inspected.append(response.status_code)
        await response.aread()

    class BodyAuth(httpx.Auth):
        requires_response_body = True

    class FlowAuth(httpx.Auth):
        async def async_auth_flow(self, request):
            response = yield request
            inspected.append(response.status_code)
            await response.aread()

    auth = (
        BodyAuth() if observer == "auth_body" else FlowAuth() if observer == "auth_flow" else None
    )
    hooks = {"response": [hook]} if observer == "response_hook" else {}

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "try again"})
        content = (
            b"event: ready\ndata: {}\n\n"
            if kind == "sse"
            else b"ok"
            if kind == "bounded"
            else b"{}"
        )
        media = (
            "text/event-stream"
            if kind == "sse"
            else "application/octet-stream"
            if kind == "bounded"
            else "application/json"
        )
        return httpx.Response(
            200,
            content=content,
            headers={"Content-Type": media, "Content-Length": str(len(content))},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth, event_hooks=hooks)
    original_hooks = {key: list(value) for key, value in http.event_hooks.items()}
    t = AsyncTransport("test", client=http, retries={"idempotent": 1})
    if kind == "sse":
        events = [event async for event in t.sse("GET", "/builds/b/events")]
        assert len(events) == 1
    elif kind == "bounded":
        result, _ = await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        assert result == b"ok"
    else:
        assert await t.json("GET", "/computers") == {}
    assert len(calls) == 2 and delays == [0.25]
    assert inspected == ([] if observer == "auth_body" else [503, 200])
    assert http.auth is auth and http.event_hooks == original_hooks


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
@pytest.mark.parametrize("observer", ["response_hook", "auth_flow"])
async def test_callback_cannot_clear_captured_response_uncertainty(kind, observer, delays):
    calls, bodies = [], []

    async def hook(response):
        http.event_hooks["response"].clear()
        await response.aread()

    class Auth(httpx.Auth):
        async def async_auth_flow(self, request):
            response = yield request
            http.auth = None
            await response.aread()

    auth = Auth() if observer == "auth_flow" else None
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: handler(request)),
        auth=auth,
        event_hooks={"response": [hook]} if observer == "response_hook" else {},
    )

    def handler(request):
        calls.append(request)
        body = Broken()
        bodies.append(body)
        return httpx.Response(429, stream=body, headers={"Retry-After": "9"})

    t = AsyncTransport("test", client=http, retries={"idempotent": 3})
    with pytest.raises(ConnectionInterruptedError):
        if kind == "sse":
            await anext(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            await t.json("GET", "/computers")
    assert len(calls) == len(bodies) == 1 and bodies[0].closed
    assert delays == []
    assert http.event_hooks["response"] == [] and http.auth is None


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
async def test_response_boundary_configuration_is_captured_for_each_attempt(kind, monkeypatch):
    calls, delays = [], []

    async def hook(response):
        await response.aread()

    async def sleep(delay):
        delays.append(delay)
        http.event_hooks["response"] = [hook]

    monkeypatch.setattr(_client, "_async_retry_sleep", sleep)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={})
        return httpx.Response(429, stream=Broken(), headers={"Retry-After": "9"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    t = AsyncTransport("test", client=http, retries={"idempotent": 3})
    with pytest.raises(ConnectionInterruptedError):
        if kind == "sse":
            await anext(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            await t.json("GET", "/computers")
    assert len(calls) == 2 and delays == [0.25]


@pytest.mark.parametrize(
    "kind,follows,override,expected",
    [
        ("ordinary", True, False, 2),
        ("ordinary", False, True, 1),
        ("bounded", True, None, 2),
        ("sse", True, None, 1),
    ],
)
@pytest.mark.parametrize("processor", ["none", "response_hook", "auth"])
async def test_effective_observation_policy_across_all_read_paths(
    kind, follows, override, expected, processor, delays
):
    calls = []

    async def hook(response):
        await response.aread()

    def handler(request):
        calls.append(request)
        if processor != "auth":
            assert request.headers["Authorization"] == "Bearer test"
        raise httpx.ConnectError("no returned response", request=request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=follows,
        auth=httpx.BasicAuth("user", "password") if processor == "auth" else None,
        event_hooks={"response": [hook]} if processor == "response_hook" else {},
    )
    t = AsyncTransport("test", client=http, retries={"idempotent": 1})
    with pytest.raises(ConnectionError):
        if kind == "sse":
            await anext(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            await t.json("GET", "/computers", follow_redirects=override)
    assert len(calls) == (expected if processor == "none" else 1)
    assert delays == ([0.25] if expected == 2 and processor == "none" else [])


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
async def test_auth_challenge_body_failure_is_terminal_without_response_body_flag(kind, delays):
    calls, inspected = [], []

    class Auth(httpx.Auth):
        def auth_flow(self, request):
            response = yield request
            inspected.append(response.status_code)
            # httpx reads this intermediate body before sending the next auth request.
            yield request

    def handler(request):
        calls.append(request)
        return httpx.Response(429, stream=Broken(), headers={"Retry-After": "9"})

    auth = Auth()
    assert auth.requires_response_body is False
    t = AsyncTransport(
        "test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth),
        retries={"idempotent": 2},
    )
    with pytest.raises(ConnectionInterruptedError):
        if kind == "sse":
            await anext(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            await t.json("GET", "/computers")
    assert inspected == [429] and len(calls) == 1 and delays == []


class InMemoryWire(httpx.AsyncHTTPTransport):
    """Use the native HTTP parser and connection pool with no socket backend."""

    def __init__(self):
        self.calls = 0
        self._pool = httpcore.AsyncConnectionPool(network_backend=httpcore.AsyncMockBackend([]))

    async def handle_async_request(self, request):
        self.calls += 1
        return await super().handle_async_request(request)


@pytest.mark.parametrize("policy", [None, {"idempotent": 0}, {"idempotent": 2}])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse", "public"])
@pytest.mark.parametrize(
    "key,base_url,native,expected,retryable",
    [
        (
            "pasted-token\n",
            "http://example.test",
            httpx.LocalProtocolError,
            ConnectionInterruptedError,
            False,
        ),
        ("test-key", "ftp://example.test", httpx.UnsupportedProtocol, ConnectionError, False),
        (
            "test-key",
            "http://example.test",
            httpx.RemoteProtocolError,
            ConnectionInterruptedError,
            True,
        ),
        ("test-key", "http://[invalid]", httpx.InvalidURL, httpx.InvalidURL, False),
    ],
)
async def test_native_protocol_errors_preserve_classes_without_replaying_local_failures(
    policy, kind, key, base_url, native, expected, retryable, delays
):
    wire = InMemoryWire()
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        with pytest.raises(expected) as caught:
            if kind == "public":
                async with AsyncClient(
                    key, base_url=base_url, http_client=http, retries=policy
                ) as sdk:
                    await sdk.sizes.list()
            else:
                t = AsyncTransport(key, base_url=base_url, client=http, retries=policy)
                if kind == "ordinary":
                    await t.json("GET", "/computers")
                elif kind == "bounded":
                    await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
                else:
                    await anext(t.sse("GET", "/builds/b/events"))
    assert type(caught.value) is expected
    if native is httpx.InvalidURL:
        assert wire.calls == 0
    else:
        assert type(caught.value.__cause__) is native
        assert wire.calls == (3 if retryable and policy and policy["idempotent"] else 1)
    assert delays == ([0.25, 0.5] if retryable and policy and policy["idempotent"] else [])


@pytest.mark.parametrize("policy", [None, {"idempotent": 0}, {"idempotent": 2}])
@pytest.mark.parametrize(
    "key,base_url,native,expected",
    [
        (
            "pasted-token\n",
            "http://example.test",
            httpx.LocalProtocolError,
            ConnectionInterruptedError,
        ),
        ("test-key", "ftp://example.test", httpx.UnsupportedProtocol, ConnectionError),
    ],
)
async def test_local_protocol_failure_keeps_its_cause_with_a_short_budget(
    policy, key, base_url, native, expected, delays
):
    wire = InMemoryWire()
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        t = AsyncTransport(key, base_url=base_url, client=http, retries=policy)
        with pytest.raises(expected) as caught:
            await t.json("GET", "/computers", timeout_cap=0.1)
    assert type(caught.value) is expected
    assert type(caught.value.__cause__) is native
    assert wire.calls == 1
    assert delays == []


@pytest.mark.parametrize("policy", [None, {"idempotent": 1}])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
@pytest.mark.parametrize("status", [None, 503, 429])
@pytest.mark.parametrize(
    "family,retryable",
    [
        (httpx.RequestError, False),
        (httpx.TransportError, False),
        (httpx.TimeoutException, False),
        (httpx.ConnectTimeout, False),
        (httpx.ReadTimeout, False),
        (httpx.WriteTimeout, False),
        (httpx.PoolTimeout, False),
        (httpx.NetworkError, True),
        (httpx.ConnectError, True),
        (httpx.ReadError, True),
        (httpx.WriteError, True),
        (httpx.CloseError, True),
        (httpx.ProtocolError, False),
        (httpx.LocalProtocolError, False),
        (httpx.RemoteProtocolError, True),
        (httpx.ProxyError, False),
        (httpx.UnsupportedProtocol, False),
        (httpx.DecodingError, False),
        (httpx.TooManyRedirects, False),
    ],
)
async def test_native_exception_family_controls_replay_before_and_after_response(
    policy, kind, status, family, retryable, delays
):
    calls = []
    bodies = []
    failures = []

    def handler(request):
        calls.append(request)
        failure = family("native request failure")
        failures.append(failure)
        if status is None:
            raise failure
        body = Broken(error=failure)
        bodies.append(body)
        return httpx.Response(status, stream=body, headers={"Retry-After": "9"})

    if issubclass(family, httpx.TimeoutException):
        expected = TimeoutError
    elif status is not None:
        expected = RateLimitError if status == 429 else APIError
    elif family in (httpx.ConnectError, httpx.ProxyError, httpx.UnsupportedProtocol):
        expected = ConnectionError
    else:
        expected = ConnectionInterruptedError
    t = make(handler, policy)
    with pytest.raises(expected) as caught:
        if kind == "ordinary":
            await t.json("GET", "/computers")
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
        else:
            await anext(t.sse("GET", "/builds/b/events"))
    assert caught.value.__cause__ is failures[-1]
    if status is not None and not issubclass(family, httpx.TimeoutException):
        assert caught.value.status == status
        assert caught.value.retry_after == 9
    allowed = (
        retryable
        and status != 429
        and policy is not None
        and not (family is httpx.WriteError and status is None)
    )
    assert len(calls) == (2 if allowed else 1)
    assert delays == ([9 if status else 0.25] if allowed else [])
    assert all(body.closed for body in bodies)


@pytest.mark.parametrize("policy", [None, {"idempotent": 1}])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
@pytest.mark.parametrize("status", [None, 503])
@pytest.mark.parametrize(
    "nested,retryable",
    [
        (httpx.LocalProtocolError, False),
        (httpx.UnsupportedProtocol, False),
        (httpx.ProxyError, False),
        (httpx.WriteError, True),
        (httpx.RemoteProtocolError, True),
    ],
)
async def test_local_failure_cannot_gain_replay_permission_through_a_network_wrapper(
    policy, kind, status, nested, retryable, delays
):
    calls = []
    bodies = []
    failures = []

    def handler(request):
        calls.append(request)
        failure = httpx.ReadError("wrapped request failure")
        failure.__cause__ = nested("underlying protocol failure")
        failures.append(failure)
        if status is None:
            raise failure
        body = Broken(error=failure)
        bodies.append(body)
        return httpx.Response(status, stream=body, headers={"Retry-After": "9"})

    t = make(handler, policy)
    with pytest.raises(APIError if status else ConnectionInterruptedError) as caught:
        if kind == "ordinary":
            await t.json("GET", "/computers")
        elif kind == "bounded":
            await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
        else:
            await anext(t.sse("GET", "/builds/b/events"))
    assert caught.value.__cause__ is failures[-1]
    assert type(caught.value.__cause__.__cause__) is nested
    if status:
        assert caught.value.status == status
        assert caught.value.retry_after == 9
    allowed = (
        retryable and policy is not None and not (nested is httpx.WriteError and status is None)
    )
    assert len(calls) == (2 if allowed else 1)
    assert delays == ([9 if status else 0.25] if allowed else [])
    assert all(body.closed for body in bodies)


class InMemoryProxyWire(httpx.AsyncHTTPTransport):
    """Native CONNECT parsing and TLS upgrade with tracked in-memory streams."""

    def __init__(self, responses, first_failure=None):
        self.calls = 0
        self.streams = []
        self.connects = 0
        wire = self

        class Stream(httpcore.AsyncMockStream):
            def __init__(self, buffer):
                super().__init__(buffer)
                self.closed = False
                self.written = []

            async def write(self, buffer, timeout=None):
                self.written.append(buffer)
                await super().write(buffer, timeout)

            async def aclose(self):
                self.closed = True
                await super().aclose()

        class Backend(httpcore.AsyncMockBackend):
            async def connect_tcp(self, *args, **kwargs):
                wire.connects += 1
                if wire.connects == 1 and first_failure == "connect":
                    raise httpcore.ConnectError("proxy connection refused")
                buffer = [] if wire.connects == 1 and first_failure == "remote" else responses
                stream = Stream(list(buffer))
                wire.streams.append(stream)
                return stream

        self._pool = httpcore.AsyncHTTPProxy(
            "http://proxy.test",
            proxy_headers=[(b"X-Proxy-Context", b"kept")],
            network_backend=Backend([]),
        )

    async def handle_async_request(self, request):
        self.calls += 1
        return await super().handle_async_request(request)


def proxy_refusal(status):
    return (
        f"HTTP/1.1 {status} Refused\r\nRetry-After: {'9' * 1000}\r\nContent-Length: 4\r\n\r\nlost"
    ).encode()


@pytest.mark.parametrize("policy", [None, {"idempotent": 0}, {"idempotent": 2}])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse", "public"])
@pytest.mark.parametrize("status", [407, 429, 503])
async def test_native_proxy_refusal_is_terminal_and_closed(status, kind, policy, delays):
    wire = InMemoryProxyWire([proxy_refusal(status)])
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        with pytest.raises(ConnectionError) as caught:
            if kind == "public":
                sdk = AsyncClient(
                    "test", base_url="https://example.test", http_client=http, retries=policy
                )
                await sdk.sizes.list()
            else:
                t = AsyncTransport(
                    "test", base_url="https://example.test", client=http, retries=policy
                )
                if kind == "ordinary":
                    await t.json("GET", "/computers")
                elif kind == "bounded":
                    await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
                else:
                    await anext(t.sse("GET", "/builds/b/events"))
        # Check before closing the caller-owned client: the failed exchange
        # itself must release the CONNECT response and its connection.
        assert wire.streams and all(stream.closed for stream in wire.streams)
        assert type(caught.value) is ConnectionError
        assert type(caught.value.__cause__) is httpx.ProxyError
        assert wire.calls == wire.connects == 1
        assert delays == []
        sent = b"".join(wire.streams[0].written)
        assert sent.startswith(b"CONNECT example.test:443 HTTP/1.1\r\n")
        assert b"X-Proxy-Context: kept\r\n" in sent
        assert b"GET " not in sent


@pytest.mark.parametrize("policy", [None, {"idempotent": 0}, {"idempotent": 2}])
@pytest.mark.parametrize("status", [407, 429, 503])
async def test_native_proxy_refusal_keeps_original_error_under_short_cap(status, policy, delays):
    wire = InMemoryProxyWire([proxy_refusal(status)])
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        t = AsyncTransport("test", base_url="https://example.test", client=http, retries=policy)
        with pytest.raises(ConnectionError) as caught:
            await t.json("GET", "/computers", timeout_cap=0.1)
        assert type(caught.value) is ConnectionError
        assert type(caught.value.__cause__) is httpx.ProxyError
        assert wire.calls == wire.connects == 1
        assert wire.streams and all(stream.closed for stream in wire.streams)
        assert delays == []


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse", "public"])
@pytest.mark.parametrize("failure", ["connect", "remote"])
async def test_native_proxy_connection_failure_can_retry_without_a_refusal(failure, kind, delays):
    content = b"data: {}\n\n" if kind == "sse" else b"[]" if kind == "public" else b"{}"
    media = (
        "text/event-stream"
        if kind == "sse"
        else "application/octet-stream"
        if kind == "bounded"
        else "application/json"
    )
    response = (
        f"HTTP/1.1 200 OK\r\nContent-Type: {media}\r\n"
        f"Content-Length: {len(content)}\r\nConnection: close\r\n\r\n"
    ).encode() + content
    wire = InMemoryProxyWire([b"HTTP/1.1 200 Established\r\n\r\n", response], failure)
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        if kind == "public":
            sdk = AsyncClient(
                "test", base_url="https://example.test", http_client=http, retries={"idempotent": 1}
            )
            assert await sdk.sizes.list() == []
        else:
            t = AsyncTransport(
                "test", base_url="https://example.test", client=http, retries={"idempotent": 1}
            )
            if kind == "ordinary":
                assert await t.json("GET", "/computers") == {}
            elif kind == "bounded":
                result, _ = await t.bounded_binary(
                    "GET", "/computers/vm/results/r/output", max_bytes=100
                )
                assert result == content
            else:
                events = t.sse("GET", "/builds/b/events")
                event = await anext(events)
                assert event.data == {}
                await events.aclose()
        assert wire.calls == wire.connects == 2
        assert delays == [0.25]
        assert all(stream.closed for stream in wire.streams)


class InMemoryHTTP2Wire(httpx.AsyncHTTPTransport):
    """Native HTTP/2 frames with an I/O failure before or after headers return."""

    def __init__(self, status, mode, after="9"):
        self.calls = 0
        self.streams = []
        wire = self

        class Stream(httpcore.AsyncMockStream):
            def __init__(self):
                super().__init__([], http2=True)
                self.server = h2.connection.H2Connection(
                    h2.config.H2Configuration(client_side=False)
                )
                self.server.initiate_connection()
                self.stream_id = None
                self.response_sent = False
                self.body_sent = False
                self.failed_writes = 0
                self.closed = False

            async def write(self, data, timeout=None):
                if data and self.response_sent:
                    if mode == "headers_ack_failure" or (
                        mode == "body_ack_failure" and self.body_sent
                    ):
                        self.failed_writes += 1
                        raise httpcore.WriteError(
                            "connection closed while acknowledging received frames"
                        )
                    if mode in ("goaway", "goaway_error"):
                        return  # The server has already closed its HTTP/2 state.
                for event in self.server.receive_data(data):
                    if isinstance(event, h2.events.RequestReceived):
                        self.stream_id = event.stream_id
                        self.server.send_headers(
                            event.stream_id,
                            [(":status", str(status)), ("retry-after", after)],
                            end_stream=mode != "body_ack_failure",
                        )
                        if mode in ("goaway", "goaway_error"):
                            self.server.close_connection(
                                error_code=1 if mode == "goaway_error" else 0,
                                last_stream_id=event.stream_id,
                            )

            async def read(self, max_bytes, timeout=None):
                if mode == "disconnect":
                    return b""
                if mode == "body_ack_failure" and self.response_sent and not self.body_sent:
                    # A PING arrives after the SDK has received the headers;
                    # writing its ACK fails during body consumption.
                    self.server.ping(b"12345678")
                    self.server.send_data(self.stream_id, b"lost", end_stream=True)
                    self.body_sent = True
                data = self.server.data_to_send()
                self.response_sent = self.stream_id is not None
                return data

            async def aclose(self):
                self.closed = True
                await super().aclose()

        class Backend(httpcore.AsyncMockBackend):
            async def connect_tcp(self, *args, **kwargs):
                stream = Stream()
                wire.streams.append(stream)
                return stream

        self._pool = httpcore.AsyncConnectionPool(http2=True, network_backend=Backend([]))

    async def handle_async_request(self, request):
        self.calls += 1
        return await super().handle_async_request(request)


@pytest.mark.parametrize("policy", [None, {"idempotent": 0}, {"idempotent": 2}])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse", "public"])
@pytest.mark.parametrize("status", [200, 407, 429, 503])
async def test_native_http2_write_failure_before_response_return_is_terminal(
    status, kind, policy, delays
):
    wire = InMemoryHTTP2Wire(status, "headers_ack_failure", "9" * 1000)
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        with pytest.raises(ConnectionInterruptedError) as caught:
            if kind == "public":
                sdk = AsyncClient(
                    "test", base_url="https://example.test", http_client=http, retries=policy
                )
                await sdk.sizes.list()
            else:
                t = AsyncTransport(
                    "test", base_url="https://example.test", client=http, retries=policy
                )
                if kind == "ordinary":
                    await t.json("GET", "/computers")
                elif kind == "bounded":
                    await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
                else:
                    await anext(t.sse("GET", "/builds/b/events"))
        assert type(caught.value) is ConnectionInterruptedError
        assert type(caught.value.__cause__) is httpx.WriteError
        assert not http.is_closed  # The SDK does not close its caller's client.
        assert wire.calls == 1 and delays == []
        assert wire.streams[0].response_sent and wire.streams[0].failed_writes == 1
    assert all(stream.closed for stream in wire.streams)


@pytest.mark.parametrize("policy", [None, {"idempotent": 0}, {"idempotent": 2}])
@pytest.mark.parametrize("status", [407, 429, 503])
async def test_native_http2_opaque_write_failure_preserves_error_under_short_cap(
    status, policy, delays
):
    wire = InMemoryHTTP2Wire(status, "headers_ack_failure", "9" * 1000)
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        t = AsyncTransport("test", base_url="https://example.test", client=http, retries=policy)
        with pytest.raises(ConnectionInterruptedError) as caught:
            await t.json("GET", "/computers", timeout_cap=0.1)
        assert type(caught.value) is ConnectionInterruptedError
        assert type(caught.value.__cause__) is httpx.WriteError
        assert wire.calls == 1 and delays == []
    assert all(stream.closed for stream in wire.streams)


@pytest.mark.parametrize("policy", [None, {"idempotent": 2}])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse", "public"])
@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("mode", ["goaway", "goaway_error", "body_ack_failure", "disconnect"])
async def test_native_http2_returned_headers_and_remote_disconnect_keep_retry_rules(
    policy, kind, status, mode, delays
):
    wire = InMemoryHTTP2Wire(status, mode)
    expected = (
        ConnectionInterruptedError
        if mode == "disconnect"
        else RateLimitError
        if status == 429
        else APIError
    )
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        with pytest.raises(expected) as caught:
            if kind == "public":
                sdk = AsyncClient(
                    "test", base_url="https://example.test", http_client=http, retries=policy
                )
                await sdk.sizes.list()
            else:
                t = AsyncTransport(
                    "test", base_url="https://example.test", client=http, retries=policy
                )
                if kind == "ordinary":
                    await t.json("GET", "/computers")
                elif kind == "bounded":
                    await t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
                else:
                    await anext(t.sse("GET", "/builds/b/events"))
        allowed = policy is not None and (mode == "disconnect" or status == 503)
        assert wire.calls == (3 if allowed else 1)
        assert delays == (([0.25, 0.5] if mode == "disconnect" else [9, 9]) if allowed else [])
        if mode == "disconnect":
            assert type(caught.value.__cause__) is httpx.RemoteProtocolError
            assert not any(stream.response_sent for stream in wire.streams)
        else:
            assert caught.value.status == status and caught.value.retry_after == 9
            assert all(stream.response_sent for stream in wire.streams)
            if mode == "body_ack_failure":
                assert type(caught.value.__cause__) is httpx.WriteError
                assert all(
                    stream.body_sent and stream.failed_writes == 1 for stream in wire.streams
                )
    assert all(stream.closed for stream in wire.streams)


@pytest.mark.parametrize("status", [429, 503])
async def test_native_http2_goaway_keeps_huge_retry_after_and_status(status, delays):
    wire = InMemoryHTTP2Wire(status, "goaway", "9" * 1000)
    async with httpx.AsyncClient(transport=wire, trust_env=False) as http:
        t = AsyncTransport(
            "test", base_url="https://example.test", client=http, retries={"idempotent": 2}
        )
        with pytest.raises(RateLimitError if status == 429 else TimeoutError):
            await t.json("GET", "/computers", timeout_cap=1)
        assert wire.calls == 1 and delays == []
        assert wire.streams[0].response_sent and wire.streams[0].closed
