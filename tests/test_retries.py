"""Opt-in transport retries, complete bodies, and terminal failures."""

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
