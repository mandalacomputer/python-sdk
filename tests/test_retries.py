"""Opt-in transport retries, complete bodies, and terminal failures."""

import httpcore
import httpx
import pytest

from mandala_computer import (
    APIError,
    Client,
    ConnectionError,
    ConnectionInterruptedError,
    RateLimitError,
    TimeoutError,
    _client,
)
from mandala_computer._client import Transport


class Broken(httpx.SyncByteStream):
    def __init__(self, prefix=b"lost", error=None):
        self.prefix = prefix
        self.error = error or httpx.ReadError("connection dropped")
        self.closed = False

    def __iter__(self):
        yield self.prefix
        raise self.error

    def close(self):
        self.closed = True


@pytest.fixture
def delays(monkeypatch):
    seen = []

    def sleep(delay):
        seen.append(delay)

    monkeypatch.setattr(_client, "_retry_sleep", sleep)
    return seen


def make(handler, retries=None):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Transport(
        "test-key", base_url="https://example.test/api/v1", retries=retries, client=client
    )


def test_opt_in_recovers_503(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json={"recovered": True})

    t = make(handler, {"idempotent": 1})
    assert t.json("GET", "/computers") == {"recovered": True}
    assert len(calls) == 2
    assert delays == [0.25]


@pytest.mark.parametrize("policy", [None, {"idempotent": 0}])
def test_default_one_attempt(policy, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={})

    with pytest.raises(APIError):
        make(handler, policy).json("GET", "/computers")
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
        Client("test", retries=policy)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("status", [502, 503, 504])
def test_methods_statuses_metadata_and_request_identity(method, status, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": f"failure {len(calls)}"}, headers={"Retry-After": "3"}
        )

    t = make(handler, {"idempotent": 2})
    with pytest.raises(APIError) as caught:
        t.request(method, "/computers", params={"tag": "two words"}, headers={"X-Test": "kept"})
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
def test_connection_errors_preserve_final_class(error, expected, delays):
    calls = []

    def handler(request):
        calls.append(request)
        raise error("failed")

    with pytest.raises(expected):
        make(handler, {"idempotent": 2}).json("GET", "/computers")
    assert len(calls) == 3
    assert delays == [0.25, 0.5]


@pytest.mark.parametrize("status", [401, 402, 403, 408, 409, 429, 500, 501, 520, 522])
@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
def test_known_nonretryable_status_survives_interrupted_body(status, kind, delays):
    bodies = []

    def handler(request):
        body = Broken()
        bodies.append(body)
        return httpx.Response(status, stream=body, headers={"Retry-After": "9"})

    t = make(handler, {"idempotent": 3})
    with pytest.raises(RateLimitError if status == 429 else APIError) as caught:
        if kind == "ordinary":
            t.json("GET", "/computers")
        elif kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            next(t.sse("GET", "/builds/b/events"))
    assert caught.value.status == status
    assert caught.value.retry_after == 9
    assert len(bodies) == 1 and bodies[0].closed
    assert delays == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("shape", ["status", "connect", "body"])
def test_mutations_are_never_retried(method, shape, delays):
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
        make(handler, {"idempotent": 3}).json(method, "/computers", json={"name": "once"})
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("suffix", ["12", "12/", "12?offset=0"])
def test_consuming_legacy_reads_are_excluded(method, suffix, delays):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadError("lost answer")

    with pytest.raises(ConnectionInterruptedError):
        make(handler, {"idempotent": 3}).json(method, "/computers/vm/exec/" + suffix)
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["json", "listing", "binary", "bounded_json", "bounded_binary"])
def test_dropped_finite_body_restarts_without_prefix(kind, delays):
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
        result = t.json("GET", "/computers")
        assert result == {"id": "complete"}
    elif kind == "listing":
        result = t.listing("/computers")
        assert result == ([{"id": "complete"}], None)
    elif kind == "binary":
        assert (
            t.binary(
                "GET",
                "/computers/vm/files",
                accept="application/octet-stream",
                content_types=("application/octet-stream",),
            )
            == complete
        )
    elif kind == "bounded_json":
        assert t.bounded_json_object("GET", "/computers/vm/results/r", max_bytes=100) == {
            "id": "complete"
        }
    else:
        result, _ = t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
        assert result == complete
    assert len(calls) == 2
    assert broken.closed


@pytest.mark.parametrize(
    "error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout]
)
def test_timeouts_are_terminal(error, delays):
    calls = []

    def handler(request):
        calls.append(request)
        raise error("timed out")

    with pytest.raises(TimeoutError):
        make(handler, {"idempotent": 2}).json("GET", "/computers")
    assert len(calls) == 1
    assert delays == []


def test_invalid_json_and_retained_integrity_are_terminal(delays):
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
        t.json_object("GET", "/computers")
    with pytest.raises(Exception, match="declared size"):
        t.bounded_binary("GET", "/computers/vm/artifacts/a/download", max_bytes=20)
    assert len(calls) == 2
    assert delays == []


def test_backoff_doubles_to_cap(delays):
    with pytest.raises(APIError):
        make(lambda request: httpx.Response(503), {"idempotent": 9}).json("GET", "/computers")
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
def test_retry_after_lower_bound(header, expected, delays, monkeypatch):
    monkeypatch.setattr(_client.time, "time", lambda: 1924992000)
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(503, headers={"Retry-After": header})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    make(handler, {"idempotent": 1}).json("GET", "/computers")
    assert delays == [expected]


def test_timeout_cap_deducts_monotonic_elapsed_time(delays, monkeypatch):
    now = [100.0]
    caps = []
    monkeypatch.setattr(_client.time, "monotonic", lambda: now[0])

    def sleep(delay):
        now[0] += delay

    monkeypatch.setattr(_client, "_retry_sleep", sleep)

    def handler(request):
        caps.append(request.extensions["timeout"]["read"])
        now[0] += 0.6
        return httpx.Response(503, json={})

    with pytest.raises(TimeoutError):
        make(handler, {"idempotent": 5}).json("GET", "/computers", timeout_cap=1.6)
    assert caps == pytest.approx([1.6, 0.75])


def test_huge_retry_after_cannot_exceed_explicit_budget(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "9999999999999999999999999"})

    with pytest.raises(TimeoutError):
        make(handler, {"idempotent": 2}).json("GET", "/computers", timeout_cap=1)
    assert len(calls) == 1
    assert delays == []


def test_caller_mutation_does_not_change_policy(delays):
    policy = {"idempotent": 1}
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json={})

    t = make(handler, policy)
    policy["idempotent"] = 0
    t.json("GET", "/computers")
    assert len(calls) == 2


def test_sse_retries_before_first_event_only(delays):
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
    events = [event for event in t.sse("GET", "/builds/b/events")]
    assert len(events) == 1 and events[0].data == {"id": 2}
    assert len(calls) == 2


def test_sse_never_replays_exposed_event(delays):
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
        for event in make(handler, {"idempotent": 2}).sse("GET", "/builds/b/events"):
            events.append(event)  # noqa: PERF402 - retain the prefix before the failure
    assert len(events) == 1
    assert len(calls) == 1


def test_post_agent_stream_is_not_retried(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={})

    t = make(handler, {"idempotent": 3})
    with pytest.raises(APIError):
        next(t.sse("POST", "/computers/vm/agent", json={"task": "once"}))
    assert len(calls) == 1


def test_public_constructor_forwards_policy(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=[])

    c = Client(
        "test",
        retries={"idempotent": 1},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = c.sizes.list()
    assert result == [] and len(calls) == 2


def test_keyboard_interrupt_is_never_swallowed(monkeypatch):
    calls = []

    def sleep(delay):
        raise KeyboardInterrupt()

    monkeypatch.setattr(_client.time, "sleep", sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    with pytest.raises(KeyboardInterrupt):
        make(handler, {"idempotent": 3}).json("GET", "/computers")
    assert len(calls) == 1


def test_huge_delay_uses_bounded_interruptible_timer_chunks(monkeypatch):
    observed = []

    def sleep(delay):
        observed.append(delay)
        raise KeyboardInterrupt()

    monkeypatch.setattr(_client.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        _client._retry_sleep(1e100)
    assert observed == [86400.0]


def test_non_event_stream_cannot_gain_retry_permission_from_diagnostic_read(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=Broken(), headers={"content-type": "text/html"})

    with pytest.raises(Exception, match="event stream"):
        next(make(handler, {"idempotent": 2}).sse("GET", "/builds/b/events"))
    assert len(calls) == 1
    assert delays == []


def test_public_template_preparation_stays_single_attempt(delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={})

    c = Client(
        "test",
        retries={"idempotent": 3},
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(APIError):
        c.computers.create(template="base", template_transfer="opaque-token")
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("shape", ["status", "connect"])
def test_followed_redirect_to_consuming_get_cannot_be_retried(shape, delays):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/computers":
            return httpx.Response(307, headers={"Location": "/computers/vm/exec/12"})
        if shape == "connect":
            raise httpx.ReadError("lost answer", request=request)
        return httpx.Response(503)

    t = Transport(
        "test",
        base_url="https://example.test",
        retries={"idempotent": 2},
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )
    with pytest.raises((APIError, ConnectionError)):
        t.json("GET", "/computers")
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
def test_non_retry_operation_preserves_exact_cap_without_reading_clock(
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

    make(handler, policy).json(method, path, timeout_cap=cap)
    assert len(calls) == 1
    assert calls[0].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), expected
    )


def test_terminal_timeout_preserves_httpx_cause_after_budget_expiry(monkeypatch, delays):
    from types import SimpleNamespace

    now = [100.0]
    monkeypatch.setattr(_client, "time", SimpleNamespace(monotonic=lambda: now[0]))
    native = httpx.ReadTimeout("original phase timeout")

    def handler(request):
        now[0] = 102.0
        raise native

    t = make(handler, {"idempotent": 2})
    with pytest.raises(TimeoutError) as caught:
        t.json("GET", "/computers", timeout_cap=1)
    assert caught.value.__cause__ is native
    assert t.phase_ceiling(caught.value) == 5.0
    assert delays == []


def test_exhausted_http_failure_is_not_reclassified_after_budget_expiry(monkeypatch, delays):
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
        make(handler, {"idempotent": 1}).json("GET", "/computers", timeout_cap=1)
    assert caught.value.status == 503
    assert caught.value.body == {"error": "failure 2"}
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["json", "binary", "sse"])
@pytest.mark.parametrize("policy", [None, {"idempotent": 2}])
def test_content_decoding_failure_is_terminal_with_original_class(kind, policy, delays):
    bodies = []

    class EncodedBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b"not a gzip body"

        def close(self):
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
            next(t.sse("GET", "/builds/b/events"))
        elif kind == "binary":
            t.binary(
                "GET",
                "/computers/vm/files",
                accept="application/octet-stream",
                content_types=("application/octet-stream",),
            )
        else:
            t.json("GET", "/computers")
    assert isinstance(caught.value.__cause__, httpx.DecodingError)
    assert len(bodies) == 1 and bodies[0].closed
    assert delays == []


@pytest.mark.parametrize("digits", [309, 5000])
def test_overflowing_numeric_retry_after_cannot_bypass_budget(digits, delays):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "9" * digits})

    with pytest.raises(TimeoutError):
        make(handler, {"idempotent": 2}).json("GET", "/computers", timeout_cap=1)
    assert len(calls) == 1
    assert delays == []


@pytest.mark.parametrize("kind", ["json", "bounded", "sse"])
def test_overflowing_header_uses_interruptible_long_wait_without_cap(kind, monkeypatch):

    calls, waits = [], []

    def sleep(delay):
        if delay == 0:
            return
        waits.append(delay)
        raise KeyboardInterrupt()

    monkeypatch.setattr(_client.time, "sleep", sleep)

    def handler(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "9" * 309})

    t = make(handler, {"idempotent": 2})
    with pytest.raises(KeyboardInterrupt):
        if kind == "sse":
            next(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            t.json("GET", "/computers")
    assert waits == [86400.0]
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["json", "sse"])
@pytest.mark.parametrize("failure", ["status", "connection", "redirect_body"])
def test_intermediate_consuming_redirect_is_never_replayed(kind, failure, delays):
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

    t = Transport(
        "test",
        base_url="https://example.test",
        retries={"idempotent": 2},
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )
    with pytest.raises((APIError, ConnectionError)):
        if kind == "sse":
            next(t.sse("GET", "/computers"))
        else:
            t.json("GET", "/computers")
    assert calls == ["/computers", "/computers/vm/exec/12"] + (
        [] if failure == "redirect_body" else ["/safe-final"]
    )
    assert delays == []


@pytest.mark.parametrize("kind", ["json", "sse"])
def test_complete_safe_redirect_chain_can_retry_without_changing_client_auth_or_hooks(kind, delays):
    calls, auth_calls, hook_calls = [], [], []

    class Auth(httpx.Auth):
        def auth_flow(self, request):
            auth_calls.append(request.url.path)
            request.headers["X-Custom-Auth"] = "kept"
            yield request

    def hook(request):
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

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        auth=Auth(),
        event_hooks={"request": [hook]},
    )
    hooks = list(http.event_hooks["request"])
    t = Transport("test", base_url="https://example.test", retries={"idempotent": 1}, client=http)
    if kind == "sse":
        events = [event for event in t.sse("GET", "/computers")]
        assert len(events) == 1
    else:
        assert t.json("GET", "/computers") == {}
    assert calls == hook_calls == ["/computers", "/safe-final", "/computers", "/safe-final"]
    assert auth_calls == ["/computers", "/computers"]
    assert http.event_hooks["request"] == hooks and http.follow_redirects is True
    assert delays == [0.25]


@pytest.mark.parametrize("follows,override", [(True, None), (False, True), (True, False)])
def test_connection_failure_without_response_obeys_effective_redirect_policy(
    follows, override, delays
):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("no final response", request=request)

    t = Transport(
        "test",
        retries={"idempotent": 1},
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=follows),
    )
    with pytest.raises(ConnectionError):
        t.json("GET", "/computers", follow_redirects=override)
    assert len(calls) == (2 if override is False else 1)
    assert delays == ([0.25] if override is False else [])


def test_previous_retry_after_does_not_leak_into_next_connection_failure(delays):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "7"})
        if len(calls) == 2:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={})

    assert make(handler, {"idempotent": 2}).json("GET", "/computers") == {}
    assert delays == [7, 0.5]


def test_http_date_delay_is_evaluated_after_error_body_consumption(monkeypatch, delays):
    from types import SimpleNamespace

    now = [1924992000.0]
    monkeypatch.setattr(_client, "time", SimpleNamespace(time=lambda: now[0]))

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
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

    assert make(handler, {"idempotent": 1}).json("GET", "/computers") == {}
    assert delays == [0.25]


@pytest.mark.parametrize("status", [200, 429, 502, 503, 504])
@pytest.mark.parametrize("kind", ["json", "sse"])
@pytest.mark.parametrize("policy", [None, {"idempotent": 1}])
def test_decoding_error_is_terminal_for_every_sdk_error_class(status, kind, policy, delays):
    bodies = []

    class GzipBody(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b"not a gzip body"

        def close(self):
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
            next(t.sse("GET", "/builds/b/events"))
        else:
            t.json("GET", "/computers")
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
def test_pre_return_body_processing_failure_is_terminal(status, kind, observer, policy, delays):
    bodies, calls, inspected = [], [], []

    def hook(response):
        inspected.append(response.status_code)
        response.read()

    class BodyAuth(httpx.Auth):
        requires_response_body = True

        def auth_flow(self, request):
            inspected.append("auth started")
            yield request

    class FlowAuth(httpx.Auth):
        def sync_auth_flow(self, request):
            response = yield request
            inspected.append(response.status_code)
            response.read()

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

    http = httpx.Client(transport=httpx.MockTransport(handler), auth=auth, event_hooks=hooks)
    original_hooks = {key: list(value) for key, value in http.event_hooks.items()}
    t = Transport("test", client=http, retries=policy)
    with pytest.raises(ConnectionInterruptedError) as caught:
        if kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        elif kind == "sse":
            next(t.sse("GET", "/builds/b/events"))
        else:
            t.json("GET", "/computers", timeout_cap=1)
    assert isinstance(caught.value.__cause__, httpx.ReadError)
    assert len(calls) == len(bodies) == 1 and bodies[0].closed
    assert inspected == (["auth started"] if observer == "auth_body" else [status])
    assert delays == []
    assert http.auth is auth and http.event_hooks == original_hooks


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
@pytest.mark.parametrize("observer", ["response_hook", "auth_body", "auth_flow"])
def test_returned_responses_can_retry_after_caller_body_processing(kind, observer, delays):
    calls, inspected = [], []

    def hook(response):
        inspected.append(response.status_code)
        response.read()

    class BodyAuth(httpx.Auth):
        requires_response_body = True

    class FlowAuth(httpx.Auth):
        def sync_auth_flow(self, request):
            response = yield request
            inspected.append(response.status_code)
            response.read()

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

    http = httpx.Client(transport=httpx.MockTransport(handler), auth=auth, event_hooks=hooks)
    original_hooks = {key: list(value) for key, value in http.event_hooks.items()}
    t = Transport("test", client=http, retries={"idempotent": 1})
    if kind == "sse":
        events = [event for event in t.sse("GET", "/builds/b/events")]
        assert len(events) == 1
    elif kind == "bounded":
        result, _ = t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        assert result == b"ok"
    else:
        assert t.json("GET", "/computers") == {}
    assert len(calls) == 2 and delays == [0.25]
    assert inspected == ([] if observer == "auth_body" else [503, 200])
    assert http.auth is auth and http.event_hooks == original_hooks


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
@pytest.mark.parametrize("observer", ["response_hook", "auth_flow"])
def test_callback_cannot_clear_captured_response_uncertainty(kind, observer, delays):
    calls, bodies = [], []

    def hook(response):
        http.event_hooks["response"].clear()
        response.read()

    class Auth(httpx.Auth):
        def sync_auth_flow(self, request):
            response = yield request
            http.auth = None
            response.read()

    auth = Auth() if observer == "auth_flow" else None
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: handler(request)),
        auth=auth,
        event_hooks={"response": [hook]} if observer == "response_hook" else {},
    )

    def handler(request):
        calls.append(request)
        body = Broken()
        bodies.append(body)
        return httpx.Response(429, stream=body, headers={"Retry-After": "9"})

    t = Transport("test", client=http, retries={"idempotent": 3})
    with pytest.raises(ConnectionInterruptedError):
        if kind == "sse":
            next(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            t.json("GET", "/computers")
    assert len(calls) == len(bodies) == 1 and bodies[0].closed
    assert delays == []
    assert http.event_hooks["response"] == [] and http.auth is None


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
def test_response_boundary_configuration_is_captured_for_each_attempt(kind, monkeypatch):
    calls, delays = [], []

    def hook(response):
        response.read()

    def sleep(delay):
        delays.append(delay)
        http.event_hooks["response"] = [hook]

    monkeypatch.setattr(_client, "_retry_sleep", sleep)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={})
        return httpx.Response(429, stream=Broken(), headers={"Retry-After": "9"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    t = Transport("test", client=http, retries={"idempotent": 3})
    with pytest.raises(ConnectionInterruptedError):
        if kind == "sse":
            next(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            t.json("GET", "/computers")
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
def test_effective_observation_policy_across_all_read_paths(
    kind, follows, override, expected, processor, delays
):
    calls = []

    def hook(response):
        response.read()

    def handler(request):
        calls.append(request)
        if processor != "auth":
            assert request.headers["Authorization"] == "Bearer test"
        raise httpx.ConnectError("no returned response", request=request)

    http = httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=follows,
        auth=httpx.BasicAuth("user", "password") if processor == "auth" else None,
        event_hooks={"response": [hook]} if processor == "response_hook" else {},
    )
    t = Transport("test", client=http, retries={"idempotent": 1})
    with pytest.raises(ConnectionError):
        if kind == "sse":
            next(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            t.json("GET", "/computers", follow_redirects=override)
    assert len(calls) == (expected if processor == "none" else 1)
    assert delays == ([0.25] if expected == 2 and processor == "none" else [])


@pytest.mark.parametrize("kind", ["ordinary", "bounded", "sse"])
def test_auth_challenge_body_failure_is_terminal_without_response_body_flag(kind, delays):
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
    t = Transport(
        "test",
        client=httpx.Client(transport=httpx.MockTransport(handler), auth=auth),
        retries={"idempotent": 2},
    )
    with pytest.raises(ConnectionInterruptedError):
        if kind == "sse":
            next(t.sse("GET", "/builds/b/events"))
        elif kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=10)
        else:
            t.json("GET", "/computers")
    assert inspected == [429] and len(calls) == 1 and delays == []


class InMemoryWire(httpx.HTTPTransport):
    """Use the native HTTP parser and connection pool with no socket backend."""

    def __init__(self):
        self.calls = 0
        self._pool = httpcore.ConnectionPool(network_backend=httpcore.MockBackend([]))

    def handle_request(self, request):
        self.calls += 1
        return super().handle_request(request)


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
def test_native_protocol_errors_preserve_classes_without_replaying_local_failures(
    policy, kind, key, base_url, native, expected, retryable, delays
):
    wire = InMemoryWire()
    with (
        httpx.Client(transport=wire, trust_env=False) as http,
        pytest.raises(expected) as caught,
    ):
        if kind == "public":
            with Client(key, base_url=base_url, http_client=http, retries=policy) as sdk:
                sdk.sizes.list()
        else:
            t = Transport(key, base_url=base_url, client=http, retries=policy)
            if kind == "ordinary":
                t.json("GET", "/computers")
            elif kind == "bounded":
                t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
            else:
                next(t.sse("GET", "/builds/b/events"))
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
def test_local_protocol_failure_keeps_its_cause_with_a_short_budget(
    policy, key, base_url, native, expected, delays
):
    wire = InMemoryWire()
    with httpx.Client(transport=wire, trust_env=False) as http:
        t = Transport(key, base_url=base_url, client=http, retries=policy)
        with pytest.raises(expected) as caught:
            t.json("GET", "/computers", timeout_cap=0.1)
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
        (httpx.ProxyError, True),
        (httpx.UnsupportedProtocol, False),
        (httpx.DecodingError, False),
        (httpx.TooManyRedirects, False),
    ],
)
def test_native_exception_family_controls_replay_before_and_after_response(
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
            t.json("GET", "/computers")
        elif kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
        else:
            next(t.sse("GET", "/builds/b/events"))
    assert caught.value.__cause__ is failures[-1]
    if status is not None and not issubclass(family, httpx.TimeoutException):
        assert caught.value.status == status
        assert caught.value.retry_after == 9
    allowed = retryable and status != 429 and policy is not None
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
        (httpx.RemoteProtocolError, True),
    ],
)
def test_local_failure_cannot_gain_replay_permission_through_a_network_wrapper(
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
            t.json("GET", "/computers")
        elif kind == "bounded":
            t.bounded_binary("GET", "/computers/vm/results/r/output", max_bytes=100)
        else:
            next(t.sse("GET", "/builds/b/events"))
    assert caught.value.__cause__ is failures[-1]
    assert type(caught.value.__cause__.__cause__) is nested
    if status:
        assert caught.value.status == status
        assert caught.value.retry_after == 9
    allowed = retryable and policy is not None
    assert len(calls) == (2 if allowed else 1)
    assert delays == ([9 if status else 0.25] if allowed else [])
    assert all(body.closed for body in bodies)
