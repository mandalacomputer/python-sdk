"""A call that starts an agent run, refused during a platform update (OPL-6094).

While the platform is about to restart for an update, the calls that start an
agent run answer 503 with ``reason: "draining"`` and a ``Retry-After``, before
anything starts: nothing ran, nothing was held or charged. So
:func:`mandala_computer.is_transient` answers ``True`` for it whatever the
method, and a client that opted into ``retries`` sends the same call again
after the wait — on those calls only, for that answer only, within the same
attempts and under the same cap on one wait. Sync and async both.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _client
from mandala_computer._client import MODEL_KEY_HEADER, Transport
from mandala_computer._exceptions import _refused_while_draining

BASE = "https://api.test/api/v1"
AGENT = f"{BASE}/computers/vm-1/agent"
START = f"{BASE}/computers/vm-1/agent/runs"
KEY = "sk-ant-test"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux", "cpu": 2}
RUN: dict[str, Any] = {
    "id": "run_0123456789abcdef01234567",
    "computer_id": "vm-1",
    "thread_id": None,
    "status": "running",
    "prompt": "rename the screenshots",
    "model": "claude-sonnet-5-5",
    "max_steps": 20,
    "steps": 0,
    "steps_taken": [],
    "created_at": "2026-10-18T12:00:00.000Z",
    "updated_at": "2026-10-18T12:00:00.000Z",
    "ended_at": None,
}
DRAINING: dict[str, Any] = {
    "error": "New agent runs are paused for a few minutes while the platform updates; runs "
    "already going are not affected. Send the same request again after the Retry-After wait.",
    "reason": "draining",
}


def draining(retry_after: str | None = "30") -> httpx.Response:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return httpx.Response(503, json=DRAINING, headers=headers)


def done_stream() -> httpx.Response:
    return httpx.Response(
        200,
        content=b'event: done\ndata: {"steps": 2, "stop": "end_turn", "text": "all set"}\n\n',
        headers={"Content-Type": "text/event-stream"},
    )


def done_body() -> httpx.Response:
    return httpx.Response(200, json={"steps": 2, "stop": "end_turn", "text": "all set"})


def started() -> httpx.Response:
    return httpx.Response(202, json=RUN)


@pytest.fixture
def delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every wait the transport would have slept, slept as nothing."""
    seen: list[float] = []

    def sleep(delay: float) -> None:
        seen.append(delay)

    async def asleep(delay: float) -> None:
        seen.append(delay)

    monkeypatch.setattr(_client, "_retry_sleep", sleep)
    monkeypatch.setattr(_client, "_async_retry_sleep", asleep)
    return seen


def client(retries: dict[str, int] | None = None) -> mc.Client:
    return mc.Client("gck_test", base_url=BASE, retries=retries)


def aclient(retries: dict[str, int] | None = None) -> mc.AsyncClient:
    return mc.AsyncClient("gck_test", base_url=BASE, retries=retries)


def ok(answer: object) -> bool:
    """Whether a call's answer is the success its route was mocked with."""
    if isinstance(answer, mc.AgentResult):
        return answer.finished
    if isinstance(answer, mc.AgentRun):
        return answer.id == RUN["id"]
    if isinstance(answer, list):
        return any(isinstance(e, mc.AgentDone) and e.result.finished for e in answer)
    return False


SyncCall = Callable[[mc.Client], object]
AsyncCall = Callable[[mc.AsyncClient], Awaitable[object]]


async def _collect(stream: Any) -> list[Any]:
    return [event async for event in stream]


#: (name, route, the success it answers, the call). Every call that starts a run.
SYNC_CALLS: list[tuple[str, str, Callable[[], httpx.Response], SyncCall]] = [
    ("agent", AGENT, done_stream, lambda c: mc.Computer(c._t, COMPUTER).agent("go", model_key=KEY)),
    (
        "agent_stream",
        AGENT,
        done_stream,
        lambda c: list(mc.Computer(c._t, COMPUTER).agent_stream("go", model_key=KEY)),
    ),
    (
        "agent_once",
        AGENT,
        done_body,
        lambda c: mc.Computer(c._t, COMPUTER).agent_once("go", model_key=KEY),
    ),
    (
        "start_agent_run",
        START,
        started,
        lambda c: mc.Computer(c._t, COMPUTER).start_agent_run("go", model_key=KEY),
    ),
    ("agent_runs.start", START, started, lambda c: c.agent_runs.start("vm-1", "go", model_key=KEY)),
    # Funded: no key of the caller's, and the 503 keeps its class all the same.
    ("agent, funded", AGENT, done_stream, lambda c: mc.Computer(c._t, COMPUTER).agent("go")),
    ("agent_runs.start, funded", START, started, lambda c: c.agent_runs.start("vm-1", "go")),
]

ASYNC_CALLS: list[tuple[str, str, Callable[[], httpx.Response], AsyncCall]] = [
    (
        "agent",
        AGENT,
        done_stream,
        lambda c: mc.AsyncComputer(c._t, COMPUTER).agent("go", model_key=KEY),
    ),
    (
        "agent_stream",
        AGENT,
        done_stream,
        lambda c: _collect(mc.AsyncComputer(c._t, COMPUTER).agent_stream("go", model_key=KEY)),
    ),
    (
        "agent_once",
        AGENT,
        done_body,
        lambda c: mc.AsyncComputer(c._t, COMPUTER).agent_once("go", model_key=KEY),
    ),
    (
        "start_agent_run",
        START,
        started,
        lambda c: mc.AsyncComputer(c._t, COMPUTER).start_agent_run("go", model_key=KEY),
    ),
    ("agent_runs.start", START, started, lambda c: c.agent_runs.start("vm-1", "go", model_key=KEY)),
    ("agent, funded", AGENT, done_stream, lambda c: mc.AsyncComputer(c._t, COMPUTER).agent("go")),
]


def sync_ids(calls: list[Any]) -> list[str]:
    return [name for name, *_ in calls]


def assert_same_request_twice(route: respx.Route, funded: bool) -> None:
    first, second = (call.request for call in route.calls)
    assert first.method == second.method == "POST"
    assert first.url == second.url
    assert json.loads(first.content) == json.loads(second.content)
    if funded:
        assert MODEL_KEY_HEADER not in first.headers and MODEL_KEY_HEADER not in second.headers
    else:
        assert first.headers[MODEL_KEY_HEADER] == second.headers[MODEL_KEY_HEADER] == KEY


# --- what is_transient says ------------------------------------------------------


#: (status, method, body, transient). Only the top-level word on a 503.
TABLE: list[tuple[int, str, object, bool]] = [
    (503, "POST", DRAINING, True),
    (503, "GET", DRAINING, True),
    (503, "DELETE", DRAINING, True),
    # Every other 503 on a change is an outcome nobody knows.
    (503, "POST", {"error": "the host could not be reached"}, False),
    (503, "POST", {"error": "busy", "reason": "contention"}, False),
    # Nested under ``error``: whatever the envelope wraps, not this request.
    (503, "POST", {"error": {"message": "paused", "reason": "draining"}}, False),
    # The word is read on a 503 alone.
    (409, "POST", DRAINING, False),
    (400, "POST", DRAINING, False),
    (502, "POST", DRAINING, False),
    (504, "POST", DRAINING, False),
    # A body that did not arrive, or did not parse.
    (503, "POST", None, False),
    (503, "POST", "draining", False),
    # A body that accounts for a run is about a run that started.
    (503, "POST", {**DRAINING, "usage": {"input_tokens": 900}}, False),
    (503, "POST", {**DRAINING, "steps": []}, False),
    (503, "POST", {**DRAINING, "steps_taken": [{"n": 1, "tool": "computer"}]}, False),
    (503, "POST", {**DRAINING, "cost_usd": 0.05}, False),
    (503, "POST", {**DRAINING, "thread_id": None}, False),
]


@pytest.mark.parametrize(("status", "method", "body", "transient"), TABLE)
def test_is_transient_reads_draining_on_a_503_whatever_the_method(
    status: int, method: str, body: object, transient: bool, delays: list[float]
) -> None:
    content = json.dumps(body).encode() if isinstance(body, dict) else (body or "").encode()
    with respx.mock:
        respx.route(method=method, url=f"{BASE}/computers/vm-1/agent/runs").mock(
            return_value=httpx.Response(status, content=content, headers={"Retry-After": "30"})
        )
        with client() as c, pytest.raises(mc.APIError) as raised:
            c._t.json(method, "computers/vm-1/agent/runs")
    assert mc.is_transient(raised.value) is transient
    if isinstance(body, dict) and body.get("reason") == "draining":
        assert raised.value.reason == "draining"


def test_a_draining_refusal_built_by_hand_is_transient_with_no_method() -> None:
    """A change with no ``method`` is treated as a change; this one is safe
    whatever the method, so that does not matter."""
    err = mc.UnavailableError("paused", status=503, body=dict(DRAINING), retry_after=30)
    assert err.method is None and err.reason == "draining"
    assert mc.is_transient(err) is True


def test_an_error_carrying_a_run_is_never_the_refusal() -> None:
    """The refusal comes before the stream opens. A failure reported from
    inside a run that had begun is about a run that started."""
    err = mc.UnavailableError("paused", status=503, body=dict(DRAINING), method="POST")
    err.agent = mc.AgentFailed(error="paused", status=503)
    assert mc.is_transient(err) is False


@respx.mock
def test_a_draining_word_in_a_runs_own_error_event_is_not_resent(delays: list[float]) -> None:
    """A stream that opened carries a run, whatever its ``error`` event says:
    raised once, never sent again, and not transient."""
    route = respx.post(AGENT).mock(
        return_value=httpx.Response(
            200,
            content=(
                b'event: step\ndata: {"n": 1, "tool": "computer", "action": "screenshot"}\n\n'
                b'event: error\ndata: {"error": "paused", "status": 503, "reason": "draining", '
                b'"steps": [{"n": 1, "tool": "computer", "action": "screenshot"}]}\n\n'
            ),
            headers={"Content-Type": "text/event-stream"},
        )
    )
    with client({"idempotent": 3}) as c, pytest.raises(mc.APIError) as raised:
        mc.Computer(c._t, COMPUTER).agent("go", model_key=KEY)
    assert raised.value.status == 503 and raised.value.agent is not None
    assert mc.is_transient(raised.value) is False
    assert route.call_count == 1 and delays == []


# --- resent, when the caller asked for retries ------------------------------------


@pytest.mark.parametrize(("name", "url", "success", "call"), SYNC_CALLS, ids=sync_ids(SYNC_CALLS))
def test_each_call_that_starts_a_run_resends_the_refusal_after_its_wait(
    name: str,
    url: str,
    success: Callable[[], httpx.Response],
    call: SyncCall,
    delays: list[float],
) -> None:
    with respx.mock:
        route = respx.post(url).mock(side_effect=[draining(), success()])
        with client({"idempotent": 2}) as c:
            answer = call(c)
    assert ok(answer), name
    assert route.call_count == 2
    assert delays == [30]
    assert_same_request_twice(route, funded="funded" in name)


@pytest.mark.parametrize(("name", "url", "success", "call"), ASYNC_CALLS, ids=sync_ids(ASYNC_CALLS))
async def test_each_async_call_that_starts_a_run_resends_the_refusal_after_its_wait(
    name: str,
    url: str,
    success: Callable[[], httpx.Response],
    call: AsyncCall,
    delays: list[float],
) -> None:
    with respx.mock:
        route = respx.post(url).mock(side_effect=[draining(), success()])
        async with aclient({"idempotent": 2}) as c:
            answer = await call(c)
    assert ok(answer), name
    assert route.call_count == 2
    assert delays == [30]
    assert_same_request_twice(route, funded="funded" in name)


@pytest.mark.parametrize(("name", "url", "success", "call"), SYNC_CALLS, ids=sync_ids(SYNC_CALLS))
@pytest.mark.parametrize("retries", [None, {"idempotent": 0}])
def test_without_retries_the_refusal_is_raised_once_and_is_transient(
    name: str,
    url: str,
    success: Callable[[], httpx.Response],
    call: SyncCall,
    retries: dict[str, int] | None,
    delays: list[float],
) -> None:
    with respx.mock:
        route = respx.post(url).mock(side_effect=[draining(), success()])
        with client(retries) as c, pytest.raises(mc.UnavailableError) as raised:
            call(c)
    err = raised.value
    assert err.status == 503 and err.reason == "draining" and err.retry_after == 30
    assert err.method == "POST" and err.agent is None
    assert mc.is_transient(err) is True
    assert route.call_count == 1 and delays == []


@pytest.mark.parametrize(("name", "url", "success", "call"), ASYNC_CALLS, ids=sync_ids(ASYNC_CALLS))
async def test_without_retries_the_async_refusal_is_raised_once_and_is_transient(
    name: str,
    url: str,
    success: Callable[[], httpx.Response],
    call: AsyncCall,
    delays: list[float],
) -> None:
    with respx.mock:
        route = respx.post(url).mock(side_effect=[draining(), success()])
        async with aclient() as c:
            with pytest.raises(mc.UnavailableError) as raised:
                await call(c)
    assert raised.value.reason == "draining" and mc.is_transient(raised.value) is True
    assert route.call_count == 1 and delays == []


@pytest.mark.parametrize(("name", "url", "success", "call"), SYNC_CALLS, ids=sync_ids(SYNC_CALLS))
def test_the_attempts_run_out_and_the_refusal_is_raised(
    name: str,
    url: str,
    success: Callable[[], httpx.Response],
    call: SyncCall,
    delays: list[float],
) -> None:
    with respx.mock:
        route = respx.post(url).mock(return_value=draining())
        with client({"idempotent": 2}) as c, pytest.raises(mc.UnavailableError) as raised:
            call(c)
    assert raised.value.reason == "draining" and mc.is_transient(raised.value) is True
    assert route.call_count == 3
    assert delays == [30, 30]


async def test_the_async_attempts_run_out_and_the_refusal_is_raised(delays: list[float]) -> None:
    with respx.mock:
        route = respx.post(START).mock(return_value=draining())
        async with aclient({"idempotent": 1}) as c:
            with pytest.raises(mc.UnavailableError) as raised:
                await c.agent_runs.start("vm-1", "go", model_key=KEY)
    assert raised.value.reason == "draining"
    assert route.call_count == 2 and delays == [30]


def test_without_a_retry_after_the_ordinary_backoff_is_waited(delays: list[float]) -> None:
    with respx.mock:
        route = respx.post(START).mock(side_effect=[draining(None), draining(None), started()])
        with client({"idempotent": 2}) as c:
            c.agent_runs.start("vm-1", "go", model_key=KEY)
    assert route.call_count == 3 and delays == [0.25, 0.5]


@pytest.mark.parametrize(("after", "slept"), [("3600", None), ("45", 45)])
def test_one_wait_is_capped_as_for_a_read(
    after: str, slept: float | None, delays: list[float]
) -> None:
    """With no budget, a wait past the ordinary request deadline is not slept:
    the refusal is raised with the interval the platform asked for."""
    with respx.mock:
        route = respx.post(AGENT).mock(side_effect=[draining(after), done_stream()])
        with client({"idempotent": 3}) as c:
            computer = mc.Computer(c._t, COMPUTER)
            if slept is None:
                with pytest.raises(mc.UnavailableError) as raised:
                    computer.agent("go", model_key=KEY)
                assert raised.value.retry_after == 3600
                assert not isinstance(raised.value, mc.TimeoutError)
            else:
                assert computer.agent("go", model_key=KEY).finished
    assert route.call_count == (1 if slept is None else 2)
    assert delays == ([] if slept is None else [slept])


# --- and nothing else is ----------------------------------------------------------


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _read_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadError("connection dropped", request=request)


#: What the calls that start a run meet, and must not send again for.
NOT_RESENT: list[tuple[str, Callable[[httpx.Request], httpx.Response]]] = [
    ("503 without a word", lambda r: httpx.Response(503, json={"error": "host unreachable"})),
    (
        "503 nested draining",
        lambda r: httpx.Response(
            503,
            json={"error": {"message": "paused", "type": "server_error", "reason": "draining"}},
            headers={"Retry-After": "30"},
        ),
    ),
    (
        "503 contention",
        lambda r: httpx.Response(503, json={"error": "busy", "reason": "contention"}),
    ),
    ("502 draining", lambda r: httpx.Response(502, json=DRAINING)),
    ("504", lambda r: httpx.Response(504, json={"error": "timed out"})),
    (
        "409 contention",
        lambda r: httpx.Response(409, json={"error": "driven", "reason": "contention"}),
    ),
    ("429", lambda r: httpx.Response(429, json={"error": "slow"}, headers={"Retry-After": "1"})),
    ("503 draining, no body", lambda r: httpx.Response(503, headers={"Retry-After": "30"})),
    ("connect error", _connect_error),
    ("read error", _read_error),
]

NOT_RESENT_CALLS: list[tuple[str, str, SyncCall]] = [
    ("agent", AGENT, lambda c: mc.Computer(c._t, COMPUTER).agent("go", model_key=KEY)),
    ("agent_once", AGENT, lambda c: mc.Computer(c._t, COMPUTER).agent_once("go", model_key=KEY)),
    ("agent_runs.start", START, lambda c: c.agent_runs.start("vm-1", "go", model_key=KEY)),
]


@pytest.mark.parametrize(("case", "answer"), NOT_RESENT, ids=[c for c, _ in NOT_RESENT])
@pytest.mark.parametrize(
    ("name", "url", "call"), NOT_RESENT_CALLS, ids=[n for n, _, _ in NOT_RESENT_CALLS]
)
def test_nothing_else_the_calls_meet_is_resent(
    case: str,
    answer: Callable[[httpx.Request], httpx.Response],
    name: str,
    url: str,
    call: SyncCall,
    delays: list[float],
) -> None:
    with respx.mock:
        route = respx.post(url).mock(side_effect=answer)
        with client({"idempotent": 3}) as c, pytest.raises(mc.MandalaError) as raised:
            call(c)
    assert route.call_count == 1, case
    assert delays == []
    # Their classification is what it was: the SDK resending none of them is
    # not the same as calling them all final.
    transient = case in ("connect error", "409 contention", "429")
    assert mc.is_transient(raised.value) is transient, case


@pytest.mark.parametrize(("case", "answer"), NOT_RESENT, ids=[c for c, _ in NOT_RESENT])
async def test_nothing_else_the_async_calls_meet_is_resent(
    case: str, answer: Callable[[httpx.Request], httpx.Response], delays: list[float]
) -> None:
    with respx.mock:
        route = respx.post(AGENT).mock(side_effect=answer)
        async with aclient({"idempotent": 3}) as c:
            with pytest.raises(mc.MandalaError):
                await mc.AsyncComputer(c._t, COMPUTER).agent("go", model_key=KEY)
    assert route.call_count == 1, case
    assert delays == []


# --- the transport ----------------------------------------------------------------


def transport(handler: Callable[[httpx.Request], httpx.Response], **http: Any) -> Transport:
    return Transport(
        "test-key",
        base_url="https://example.test/api/v1",
        retries={"idempotent": 3},
        client=httpx.Client(transport=httpx.MockTransport(handler), **http),
    )


def test_only_a_call_that_asks_resends_a_change(delays: list[float]) -> None:
    """Any other POST — a create, an exec — is never sent twice, whatever it
    answered: the platform says draining only on the calls that start a run,
    and the SDK resends only where it was written to expect it."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return draining()

    with pytest.raises(mc.UnavailableError):
        transport(handler).json("POST", "/computers", json={"name": "once"})
    assert len(calls) == 1 and delays == []
    with pytest.raises(mc.UnavailableError):
        next(transport(handler).sse("POST", "/computers/vm/agent", json={"prompt": "once"}))
    assert len(calls) == 2 and delays == []


def test_a_read_that_asks_keeps_the_rules_of_a_read(delays: list[float]) -> None:
    """The flag narrows a change's retries to one answer; on a read it changes
    nothing, so a read's 502 is still ridden out."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(502 if len(calls) == 1 else 200, json={})

    assert transport(handler).json("GET", "/agent/runs", resend_draining=True) == {}
    assert len(calls) == 2 and delays == [0.25]


@pytest.mark.parametrize("status", [307, 308, 303])
def test_a_refusal_reached_through_a_redirect_is_not_resent(
    status: int, delays: list[float]
) -> None:
    """A 303 after a POST says the POST was carried out, and any redirect means
    the 503 answered a request other than the one sent."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/agent/runs"):
            return httpx.Response(status, headers={"Location": "/api/v1/elsewhere"})
        return draining()

    t = transport(handler, follow_redirects=True)
    with pytest.raises(mc.UnavailableError) as raised:
        t.json("POST", "/computers/vm/agent/runs", json={"prompt": "once"}, resend_draining=True)
    assert raised.value.reason == "draining"
    assert len(calls) == 2 and delays == []
    # Nor may a caller's own loop be told to send it again, for the word or for
    # the method: after a 303 the last hop was a GET, but the request sent was
    # the POST, and the 303 says it was carried out.
    assert raised.value._redirected and not _refused_while_draining(raised.value)
    assert raised.value.method == "POST"
    assert mc.is_transient(raised.value) is False


@pytest.mark.parametrize("status", [307, 303])
def test_a_stream_refused_through_a_redirect_is_not_resent_or_transient(
    status: int, delays: list[float]
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/agent"):
            return httpx.Response(status, headers={"Location": "/api/v1/elsewhere"})
        return draining()

    t = transport(handler, follow_redirects=True)
    with pytest.raises(mc.UnavailableError) as raised:
        next(t.sse("POST", "/computers/vm/agent", json={"prompt": "once"}, resend_draining=True))
    assert len(calls) == 2 and delays == []
    assert not _refused_while_draining(raised.value)
    assert raised.value.method == "POST"
    assert mc.is_transient(raised.value) is False


def test_a_post_answered_303_keeps_the_method_it_was_sent_with() -> None:
    """Any 503 after a POST's 303, draining or not: the 303 says the POST was
    carried out, so the error names the POST and no caller loop resends it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(303, headers={"Location": "/api/v1/elsewhere"})
        return httpx.Response(503, json={"error": "busy"})

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with (
        mc.Client("gck_test", base_url=BASE, http_client=http) as c,
        pytest.raises(mc.UnavailableError) as raised,
    ):
        c.agent_runs.start("vm-1", "go", model_key=KEY)
    assert raised.value.method == "POST" and raised.value._redirected
    assert mc.is_transient(raised.value) is False


class _CutShort(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A 503 body that stops partway: the connection drops or the read times out."""

    def __init__(self, error: type[httpx.TransportError]) -> None:
        self.error = error

    def __iter__(self) -> Any:
        yield b'{"error": "busy", "reason": "dr'
        raise self.error("body cut short")

    async def __aiter__(self) -> Any:
        yield b'{"error": "busy", "reason": "dr'
        raise self.error("body cut short")


def _post_303_then_cut_short(error: type[httpx.TransportError]) -> Callable[..., httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(303, headers={"Location": "/api/v1/elsewhere"})
        return httpx.Response(503, stream=_CutShort(error))

    return handler


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.ReadTimeout])
def test_a_303_whose_503_body_is_cut_short_still_names_the_post(
    error: type[httpx.TransportError],
) -> None:
    """The error rebuilt for a body that never arrived keeps the redirect, so
    it still names the POST that was sent and no caller loop resends it."""
    http = httpx.Client(
        transport=httpx.MockTransport(_post_303_then_cut_short(error)), follow_redirects=True
    )
    with (
        mc.Client("gck_test", base_url=BASE, http_client=http) as c,
        pytest.raises(mc.UnavailableError) as raised,
    ):
        c.agent_runs.start("vm-1", "go", model_key=KEY)
    assert raised.value.method == "POST" and raised.value._redirected
    assert mc.is_transient(raised.value) is False


@pytest.mark.parametrize("error", [httpx.ReadError, httpx.ReadTimeout])
async def test_async_a_303_whose_503_body_is_cut_short_still_names_the_post(
    error: type[httpx.TransportError],
) -> None:
    handler = _post_303_then_cut_short(error)

    async def ahandler(request: httpx.Request) -> httpx.Response:
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(ahandler), follow_redirects=True)
    async with mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as c:
        with pytest.raises(mc.UnavailableError) as raised:
            await c.agent_runs.start("vm-1", "go", model_key=KEY)
    assert raised.value.method == "POST" and raised.value._redirected
    assert mc.is_transient(raised.value) is False


def test_a_public_call_refused_through_a_redirect_is_not_resent_or_transient(
    delays: list[float],
) -> None:
    """The same through ``agent_runs.start`` on a caller's own httpx client
    that follows redirects: the mark survives the agent route's own reading."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/agent/runs"):
            return httpx.Response(307, headers={"Location": "/api/v1/elsewhere"})
        return draining()

    http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    with (
        mc.Client("gck_test", base_url=BASE, http_client=http, retries={"idempotent": 3}) as c,
        pytest.raises(mc.UnavailableError) as raised,
    ):
        c.agent_runs.start("vm-1", "go", model_key=KEY)
    assert len(calls) == 2 and delays == []
    assert mc.is_transient(raised.value) is False


def test_the_same_client_resends_a_refusal_that_was_not_redirected(delays: list[float]) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return draining() if len(calls) == 1 else httpx.Response(202, json=RUN)

    t = transport(handler, follow_redirects=True)
    answer = t.json(
        "POST", "/computers/vm/agent/runs", json={"prompt": "once"}, resend_draining=True
    )
    assert answer == RUN
    assert len(calls) == 2 and delays == [30]


def test_a_stream_is_resent_only_before_its_first_event(delays: list[float]) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return draining() if len(calls) == 1 else done_stream()

    events = list(
        transport(handler).sse(
            "POST", "/computers/vm/agent", json={"prompt": "once"}, resend_draining=True
        )
    )
    assert [e.event for e in events] == ["done"]
    assert len(calls) == 2 and delays == [30]


# --- a body that accounts for a run -------------------------------------------------


#: A 503 that says draining and also reports a run: steps taken and tokens
#: spent. Not the refusal, which comes before anything starts.
ACCOUNTED: dict[str, Any] = {
    **DRAINING,
    "usage": {"input_tokens": 900, "output_tokens": 40},
    "steps_taken": [{"n": 1, "tool": "computer", "action": "left_click"}],
}


def test_agent_once_never_resends_a_refusal_that_accounts_for_a_run(delays: list[float]) -> None:
    """The transport decides before ``agent_once`` attaches the run to the
    error, so the account in the body is what has to stop it."""
    with respx.mock:
        route = respx.post(AGENT).mock(
            side_effect=[httpx.Response(503, json=ACCOUNTED, headers={"Retry-After": "30"})]
        )
        with client({"idempotent": 3}) as c, pytest.raises(mc.UnavailableError) as raised:
            mc.Computer(c._t, COMPUTER).agent_once("go", model_key=KEY)
    assert route.call_count == 1 and delays == []
    err = raised.value
    assert err.agent is not None and len(err.agent.steps) == 1
    assert err.agent.usage.input_tokens == 900
    assert mc.is_transient(err) is False


async def test_async_agent_once_never_resends_a_refusal_that_accounts_for_a_run(
    delays: list[float],
) -> None:
    with respx.mock:
        route = respx.post(AGENT).mock(
            side_effect=[httpx.Response(503, json=ACCOUNTED, headers={"Retry-After": "30"})]
        )
        async with aclient({"idempotent": 3}) as c:
            with pytest.raises(mc.UnavailableError) as raised:
                await mc.AsyncComputer(c._t, COMPUTER).agent_once("go", model_key=KEY)
    assert route.call_count == 1 and delays == []
    assert raised.value.agent is not None and len(raised.value.agent.steps) == 1
    assert mc.is_transient(raised.value) is False


@pytest.mark.parametrize(
    "extra",
    [{"steps": []}, {"thread_id": "thr_0123456789abcdef01234567"}, {"cost_usd": 0.05}],
    ids=["steps", "thread_id", "cost_usd"],
)
@pytest.mark.parametrize(
    ("name", "url", "call"), NOT_RESENT_CALLS, ids=[n for n, _, _ in NOT_RESENT_CALLS]
)
def test_no_call_resends_a_refusal_that_accounts_for_a_run(
    extra: dict[str, Any], name: str, url: str, call: SyncCall, delays: list[float]
) -> None:
    with respx.mock:
        route = respx.post(url).mock(
            return_value=httpx.Response(
                503, json={**DRAINING, **extra}, headers={"Retry-After": "30"}
            )
        )
        with client({"idempotent": 3}) as c, pytest.raises(mc.UnavailableError) as raised:
            call(c)
    assert route.call_count == 1 and delays == []
    assert mc.is_transient(raised.value) is False


def test_the_agent_routes_rebuilt_error_keeps_the_redirect_mark() -> None:
    """The agent routes rebuild some statuses as another class (a 402 as the
    account's credits); the rebuilt error answers the same redirected request."""
    from mandala_computer._computer import _agent_route_error

    sent = mc.PlanLimitError(
        "Insufficient AI credits",
        status=402,
        body={"error": "Insufficient AI credits", "top_up_url": "/billing"},
        method="POST",
    )
    sent._redirected = True
    rebuilt = _agent_route_error(sent, funded=True, base_url=BASE)
    assert isinstance(rebuilt, mc.InsufficientCreditsError) and rebuilt is not sent
    assert rebuilt._redirected is True
