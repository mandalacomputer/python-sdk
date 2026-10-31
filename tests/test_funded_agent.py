"""Agent runs on the account's AI credits, and the ledger they spend (OPL-5866).

A run started without a ``model_key`` is FUNDED: no ``X-Model-Key`` header, the
platform runs it on the account's Mandala AI credits, and every step reports
what the run has cost so far. Pinned here:

- what goes out: no header, ``max_cost_usd`` in the body, checked before it is
  sent the way the platform checks it
- what comes back: the cost fields on steps, results and failures, absent (not
  zero) on a run with the caller's own key
- the 402 that carries ``top_up_url``: the account's credits, raised as
  :class:`~mandala_computer.InsufficientCreditsError` from every entry point,
  never a :class:`~mandala_computer.PlanLimitError` and never the model
  provider's billing refusal
- ``GET credits``, strictly decoded

Both halves of the client, because :mod:`tests.test_parity` proves only that
the signatures match.
"""

from __future__ import annotations

import json
import math
from contextlib import aclosing, closing
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer._agent import to_agent_event
from mandala_computer._client import MODEL_KEY_HEADER

BASE = "https://api.test/api/v1"
AGENT = f"{BASE}/computers/vm-1/agent"
CREDITS_URL = f"{BASE}/credits"
KEY = "sk-ant-test"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux", "cpu": 2}

#: The platform's refusal of a funded run the credits cannot start, before the
#: stream opens: an ordinary JSON error with the top-up link beside it.
INSUFFICIENT = {"error": "Insufficient AI credits", "top_up_url": "/billing"}


def stream(*frames: str) -> httpx.Response:
    return httpx.Response(
        200, content="".join(frames).encode(), headers={"Content-Type": "text/event-stream"}
    )


def frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


FUNDED_STEP = {
    "n": 1,
    "tool": "computer",
    "action": "left_click",
    "detail": "clicked",
    "cost_usd": 0.0123,
    "credits_remaining_usd": 4.98,
}
FUNDED_DONE = {
    "steps": 1,
    "stop": "max_cost",
    "text": "stopped at the cap",
    "usage": {"input_tokens": 10, "output_tokens": 2},
    "cost_usd": 0.51,
}


@pytest.fixture
def computer() -> mc.Computer:
    return mc.Computer(mc.Client("gck_test", base_url=BASE)._t, COMPUTER)


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


# --- what goes out ------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
def test_a_funded_run_sends_no_key_and_its_cost_cap(computer: mc.Computer, method: str) -> None:
    route = respx.post(AGENT).mock(
        httpx.Response(200, json=FUNDED_DONE)
        if method == "agent_once"
        else stream(frame("done", FUNDED_DONE))
    )
    result = getattr(computer, method)("go", max_cost_usd=0.5)
    if method == "agent_stream":
        list(result)
    request = route.calls.last.request
    assert MODEL_KEY_HEADER not in request.headers
    assert sent(route)["max_cost_usd"] == 0.5


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
async def test_an_async_funded_run_sends_no_key_and_its_cost_cap(method: str) -> None:
    route = respx.post(AGENT).mock(
        httpx.Response(200, json=FUNDED_DONE)
        if method == "agent_once"
        else stream(frame("done", FUNDED_DONE))
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        result = getattr(c, method)("go", max_cost_usd=20)
        if method == "agent_stream":
            _ = [event async for event in result]
        else:
            await result
    assert MODEL_KEY_HEADER not in route.calls.last.request.headers
    assert sent(route)["max_cost_usd"] == 20


@respx.mock
def test_the_cost_cap_rides_beside_a_key_too(computer: mc.Computer) -> None:
    """A run on the caller's own key ignores it; that is the platform's rule to
    apply, so it is sent rather than silently dropped."""
    route = respx.post(AGENT).mock(stream(frame("done", FUNDED_DONE)))
    computer.agent("go", model_key=KEY, max_cost_usd=1)
    assert route.calls.last.request.headers[MODEL_KEY_HEADER] == KEY
    assert sent(route)["max_cost_usd"] == 1


@respx.mock
def test_no_cost_cap_is_sent_unless_asked_for(computer: mc.Computer) -> None:
    route = respx.post(AGENT).mock(stream(frame("done", FUNDED_DONE)))
    computer.agent("go")
    assert "max_cost_usd" not in sent(route)


@respx.mock
@pytest.mark.parametrize("cap", [0.25, 2, 20])
def test_the_cost_cap_bounds_are_inclusive(computer: mc.Computer, cap: float) -> None:
    route = respx.post(AGENT).mock(stream(frame("done", FUNDED_DONE)))
    computer.agent("go", max_cost_usd=cap)
    assert sent(route)["max_cost_usd"] == cap


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
@pytest.mark.parametrize(
    ("cap", "match"),
    [
        (0.24, "between 0.25 and 20"),
        (20.01, "between 0.25 and 20"),
        (0, "between 0.25 and 20"),
        (-1, "between 0.25 and 20"),
        (math.nan, "finite"),
        (math.inf, "finite"),
        (True, "must be a number"),
        ("2", "must be a number"),
    ],
)
def test_a_cost_cap_out_of_range_is_refused_before_it_is_sent(
    computer: mc.Computer, method: str, cap: object, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        result = getattr(computer, method)("go", max_cost_usd=cap)
        if method == "agent_stream":
            list(result)
    assert not respx.calls


# --- what comes back ----------------------------------------------------------


@respx.mock
def test_a_funded_stream_reports_the_cost_on_each_step_and_the_total(
    computer: mc.Computer,
) -> None:
    respx.post(AGENT).mock(stream(frame("step", FUNDED_STEP), frame("done", FUNDED_DONE)))
    events = list(computer.agent_stream("go"))
    step = events[0]
    assert isinstance(step, mc.AgentStepEvent)
    assert step.step.cost_usd == 0.0123
    assert step.step.credits_remaining_usd == 4.98
    done = events[-1]
    assert isinstance(done, mc.AgentDone)
    assert done.result.cost_usd == 0.51
    assert done.result.stop == "max_cost"
    assert not done.result.finished


@respx.mock
def test_agent_once_reads_the_cost_off_the_result_and_its_steps(computer: mc.Computer) -> None:
    respx.post(AGENT).mock(httpx.Response(200, json={**FUNDED_DONE, "steps_taken": [FUNDED_STEP]}))
    result = computer.agent_once("go")
    assert result.cost_usd == 0.51
    assert result.steps_taken[0].cost_usd == 0.0123
    assert result.steps_taken[0].credits_remaining_usd == 4.98


@respx.mock
def test_a_run_on_your_own_key_reports_no_cost_rather_than_zero(computer: mc.Computer) -> None:
    """Absence is the answer: a zero would claim a charge on credits the run
    never touched."""
    respx.post(AGENT).mock(
        stream(
            frame("step", {"n": 1, "tool": "computer"}),
            frame("done", {"steps": 1, "stop": "end_turn", "text": "ok"}),
        )
    )
    events = list(computer.agent_stream("go", model_key=KEY))
    assert isinstance(events[0], mc.AgentStepEvent)
    assert events[0].step.cost_usd is None
    assert events[0].step.credits_remaining_usd is None
    assert isinstance(events[-1], mc.AgentDone)
    assert events[-1].result.cost_usd is None


@pytest.mark.parametrize("bad", [True, "0.5", None, math.nan, math.inf, [1], 10**400])
def test_a_cost_figure_that_is_not_a_number_reads_as_none(bad: object) -> None:
    step = mc.AgentStep.from_api({"n": 1, "cost_usd": bad, "credits_remaining_usd": bad}, 1)
    assert step.cost_usd is None
    assert step.credits_remaining_usd is None
    assert mc.AgentResult.from_api({"stop": "end_turn", "cost_usd": bad}).cost_usd is None


def test_a_negative_remaining_balance_is_kept() -> None:
    """The platform documents it: a completed call can overrun the balance."""
    step = mc.AgentStep.from_api({"n": 1, "credits_remaining_usd": -0.07}, 1)
    assert step.credits_remaining_usd == -0.07


def test_a_failure_frame_carries_its_cost_and_top_up_link() -> None:
    """The billing page's path, resolved against the client's base URL into a
    link a person can open; the value as sent stays on ``raw``."""
    frame_data = {
        "error": "Insufficient AI credits",
        "status": 402,
        "cost_usd": 0,
        "top_up_url": "/billing",
        "usage": {},
        "steps": [],
    }
    failure = to_agent_event("error", frame_data, 0, base_url=BASE)
    assert isinstance(failure, mc.AgentFailed)
    assert failure.cost_usd == 0
    assert failure.top_up_url == "https://api.test/billing"
    assert failure.raw["top_up_url"] == "/billing"
    # No base to resolve a path against: no link, rather than one nobody can follow.
    unresolved = to_agent_event("error", frame_data, 0)
    assert isinstance(unresolved, mc.AgentFailed) and unresolved.top_up_url is None
    plain = to_agent_event("error", {"error": "model API: boom", "status": 500}, 0)
    assert isinstance(plain, mc.AgentFailed)
    assert plain.cost_usd is None and plain.top_up_url is None


# --- the 402 that is the account's credits ------------------------------------


def _assert_credits_refusal(error: BaseException) -> None:
    assert isinstance(error, mc.InsufficientCreditsError)
    assert not isinstance(error, (mc.PlanLimitError, mc.ModelProviderError))
    assert error.status == 402
    assert error.top_up_url == "https://api.test/billing"
    assert "add credits at https://api.test/billing" in str(error)
    assert isinstance(error.body, dict) and error.body["top_up_url"] == "/billing"
    assert mc.is_transient(error) is False


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
def test_a_funded_run_the_credits_cannot_start_is_insufficient_credits(
    computer: mc.Computer, method: str
) -> None:
    """Refused before the stream opens, with a status — which the transport's
    table would otherwise call a PlanLimitError, on all three entry points."""
    respx.post(AGENT).mock(httpx.Response(402, json=INSUFFICIENT))
    with pytest.raises(mc.InsufficientCreditsError) as error:
        result = getattr(computer, method)("go")
        if method == "agent_stream":
            list(result)
    _assert_credits_refusal(error.value)
    assert error.value.method == "POST"


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
async def test_the_async_halves_raise_insufficient_credits_too(method: str) -> None:
    respx.post(AGENT).mock(httpx.Response(402, json=INSUFFICIENT))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.InsufficientCreditsError) as error:
            result = getattr(c, method)("go")
            if method == "agent_stream":
                _ = [event async for event in result]
            else:
                await result
    _assert_credits_refusal(error.value)


@respx.mock
def test_a_credits_refusal_reported_on_the_stream_is_insufficient_credits(
    computer: mc.Computer,
) -> None:
    """The same refusal found after the stream opened arrives as an ``error``
    frame, with the status it would have had."""
    respx.post(AGENT).mock(
        stream(
            frame(
                "error",
                {
                    "request_id": "req-1",
                    "error": "Insufficient AI credits",
                    "status": 402,
                    "cost_usd": 0,
                    "top_up_url": "/billing",
                    "usage": {"input_tokens": 0},
                    "steps": [],
                },
            )
        )
    )
    with pytest.raises(mc.InsufficientCreditsError) as error:
        computer.agent("go")
    _assert_credits_refusal(error.value)
    assert error.value.cost_usd == 0
    assert error.value.agent is not None
    assert error.value.agent.top_up_url == "https://api.test/billing"


@respx.mock
def test_agent_stream_hands_the_refusal_frame_over_as_an_event(computer: mc.Computer) -> None:
    """The stream itself raises nothing for a frame: the caller reading events
    reads the failure, top-up link and all."""
    respx.post(AGENT).mock(
        stream(frame("error", {**INSUFFICIENT, "status": 402, "usage": {}, "steps": []}))
    )
    with closing(computer.agent_stream("go")) as events:
        failure = next(iter(events))
    assert isinstance(failure, mc.AgentFailed)
    assert failure.status == 402 and failure.top_up_url == "https://api.test/billing"


@respx.mock
def test_a_non_streaming_credits_refusal_is_insufficient_credits(computer: mc.Computer) -> None:
    body = {**INSUFFICIENT, "cost_usd": 0, "usage": {"input_tokens": 0}, "steps_taken": []}
    respx.post(AGENT).mock(httpx.Response(402, json=body))
    with pytest.raises(mc.InsufficientCreditsError) as error:
        computer.agent_once("go")
    _assert_credits_refusal(error.value)
    assert error.value.agent is not None
    assert error.value.agent.top_up_url == "https://api.test/billing"


@respx.mock
@pytest.mark.parametrize("url", [None, "", "   ", 7])
def test_a_402_without_a_top_up_link_stays_the_model_provider(
    computer: mc.Computer, url: object
) -> None:
    """The model API's own billing refusal on a run with your key carries no
    link, and keeps the class that says to fix the model account."""
    body: dict[str, Any] = {"error": "model API: 402 billing_error", "usage": {}}
    if url is not None:
        body["top_up_url"] = url
    respx.post(AGENT).mock(httpx.Response(402, json=body))
    with pytest.raises(mc.ModelProviderError) as error:
        computer.agent_once("go", model_key=KEY)
    assert not isinstance(error.value, mc.InsufficientCreditsError)


@respx.mock
def test_a_402_at_stream_open_without_a_link_is_not_the_plan_either(
    computer: mc.Computer,
) -> None:
    """Nothing on the agent routes is the Mandala plan, the stream's opening
    included."""
    respx.post(AGENT).mock(httpx.Response(402, json={"error": "payment required"}))
    with pytest.raises(mc.ModelProviderError) as error:
        computer.agent("go", model_key=KEY)
    assert not isinstance(error.value, mc.PlanLimitError)


@respx.mock
def test_the_stream_open_keeps_every_other_status_as_it_was(computer: mc.Computer) -> None:
    respx.post(AGENT).mock(httpx.Response(400, json={"error": "send X-Model-Key"}))
    with pytest.raises(mc.APIError) as error:
        computer.agent("go")
    assert type(error.value) is mc.APIError
    assert error.value.status == 400


@pytest.mark.parametrize(
    ("sent", "base", "link"),
    [
        ("/billing", BASE, "https://api.test/billing"),
        ("/billing", "http://localhost:3000/api/v1", "http://localhost:3000/billing"),
        ("https://pay.example.com/top-up", BASE, "https://pay.example.com/top-up"),
        ("/billing", None, None),
        ("javascript:alert(1)", BASE, None),
        ("mailto:billing@example.com", BASE, None),
        (" ", BASE, None),
        (7, BASE, None),
        (None, BASE, None),
    ],
)
def test_the_top_up_link_is_an_http_link_or_nothing(
    sent: object, base: str | None, link: str | None
) -> None:
    error = mc.InsufficientCreditsError("no", status=402, body={"top_up_url": sent}, base_url=base)
    assert error.top_up_url == link


def test_the_cost_on_a_hand_built_error() -> None:
    bare = mc.InsufficientCreditsError("no", status=402)
    assert bare.top_up_url is None and bare.cost_usd is None
    odd = mc.InsufficientCreditsError("no", status=402, body={"cost_usd": True})
    assert odd.cost_usd is None
    assert mc.InsufficientCreditsError("no", status=402, body={"cost_usd": 0.5}).cost_usd == 0.5


# --- activity rows ------------------------------------------------------------


def test_an_agent_run_summary_says_whether_it_was_funded_and_what_it_cost() -> None:
    funded = mc.Activity.from_api({"activity_id": "a", "funded": True, "cost_usd": 0.42})
    assert funded.funded is True and funded.cost_usd == 0.42
    byok = mc.Activity.from_api({"activity_id": "b", "funded": False, "cost_usd": 0})
    assert byok.funded is False and byok.cost_usd == 0
    other = mc.Activity.from_api({"activity_id": "c"})
    assert other.funded is None and other.cost_usd is None


# --- GET credits --------------------------------------------------------------

CREDITS: dict[str, Any] = {
    "balance_micros": 4_750_000,
    "available_micros": -120_000,
    "held_micros": 250_000,
    "debt_micros": 4_620_000,
    "frozen": False,
    "grant": {
        "period_start": "2026-10-01T00:00:00.000Z",
        "period_end": "2026-11-01T00:00:00.000Z",
        "granted_micros": 5_000_000,
        "used_micros": 250_000,
    },
    "lots": [
        {
            "id": "lot-1",
            "kind": "plan_grant",
            "amount_micros": 5_000_000,
            "remaining_micros": 4_750_000,
            "expires_at": "2026-11-01T00:00:00.000Z",
            "created_at": "2026-10-01T00:00:00.000Z",
        }
    ],
    "entries": [
        {
            "id": 9,
            "lot_id": None,
            "kind": "debit",
            "amount_micros": 250_000,
            "debt_micros": 0,
            "model": "claude-sonnet-5-5",
            "run_id": "run-1",
            "step": 2,
            "created_at": "2026-10-09T12:00:00.000Z",
        },
        {
            "id": 8,
            "lot_id": "lot-1",
            "kind": "credit",
            "amount_micros": 5_000_000,
            "debt_micros": 0,
            "model": None,
            "run_id": None,
            "step": None,
            "created_at": "2026-10-01T00:00:00.000Z",
        },
    ],
    "next_before": 8,
}


def _check_report(report: mc.CreditReport) -> None:
    assert report.balance_micros == 4_750_000
    assert report.available_micros == -120_000
    assert report.held_micros == 250_000 and report.debt_micros == 4_620_000
    assert report.frozen is False
    assert report.grant == mc.CreditGrant(
        period_start="2026-10-01T00:00:00.000Z",
        period_end="2026-11-01T00:00:00.000Z",
        granted_micros=5_000_000,
        used_micros=250_000,
    )
    assert [lot.id for lot in report.lots] == ["lot-1"]
    assert report.lots[0].remaining_micros == 4_750_000
    assert [(e.id, e.kind) for e in report.entries] == [(9, "debit"), (8, "credit")]
    assert report.entries[0].run_id == "run-1" and report.entries[0].step == 2
    assert report.entries[1].lot_id == "lot-1" and report.entries[1].step is None
    assert report.next_before == 8


@respx.mock
def test_credits_reads_the_ledger_and_sends_only_what_was_asked(client_sync: mc.Client) -> None:
    route = respx.get(CREDITS_URL).mock(httpx.Response(200, json=CREDITS))
    _check_report(client_sync.credits.read())
    assert route.calls.last.request.url.params.multi_items() == []
    client_sync.credits.read(before=8, limit=100)
    assert route.calls.last.request.url.params.multi_items() == [
        ("before", "8"),
        ("limit", "100"),
    ]


@respx.mock
async def test_async_credits_reads_the_same_ledger() -> None:
    route = respx.get(CREDITS_URL).mock(httpx.Response(200, json=CREDITS))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        _check_report(await client.credits.read())
        await client.credits.read(before=1, limit=1)
    assert route.calls.last.request.url.params.multi_items() == [("before", "1"), ("limit", "1")]


@pytest.fixture
def client_sync() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


@respx.mock
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"limit": 0}, "between 1 and 100"),
        ({"limit": 101}, "between 1 and 100"),
        ({"limit": True}, "whole number"),
        ({"limit": 2.0}, "whole number"),
        ({"before": 0}, "next_before"),
        ({"before": 2**53}, "next_before"),
        ({"before": "8"}, "whole number"),
    ],
)
def test_credits_refuses_a_page_the_platform_would(
    client_sync: mc.Client, kwargs: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        client_sync.credits.read(**kwargs)
    assert not respx.calls


@respx.mock
def test_the_last_page_has_no_next_before_and_no_grant(client_sync: mc.Client) -> None:
    respx.get(CREDITS_URL).mock(
        httpx.Response(200, json={**CREDITS, "grant": None, "next_before": None, "frozen": True})
    )
    report = client_sync.credits.read()
    assert report.grant is None and report.next_before is None and report.frozen is True


def _without(key: str) -> dict[str, Any]:
    return {k: v for k, v in CREDITS.items() if k != key}


@respx.mock
@pytest.mark.parametrize(
    ("body", "match"),
    [
        (_without("balance_micros"), "balance_micros"),
        ({**CREDITS, "available_micros": 1.5}, "available_micros"),
        ({**CREDITS, "held_micros": True}, "held_micros"),
        ({**CREDITS, "debt_micros": "0"}, "debt_micros"),
        (_without("frozen"), "frozen"),
        ({**CREDITS, "frozen": "false"}, "frozen"),
        ({**CREDITS, "grant": []}, "grant"),
        ({**CREDITS, "grant": {**CREDITS["grant"], "used_micros": None}}, "used_micros"),
        (_without("lots"), "lots"),
        ({**CREDITS, "lots": ["lot-1"]}, "lots"),
        ({**CREDITS, "lots": [{**CREDITS["lots"][0], "id": ""}]}, "id"),
        ({**CREDITS, "entries": None}, "entries"),
        ({**CREDITS, "entries": [{**CREDITS["entries"][0], "step": "2"}]}, "step"),
        ({**CREDITS, "entries": [{**CREDITS["entries"][0], "run_id": 5}]}, "run_id"),
        ({**CREDITS, "next_before": 0}, "next_before"),
        ({**CREDITS, "next_before": "8"}, "next_before"),
        ([], "not a JSON object"),
    ],
)
def test_a_ledger_this_cannot_read_is_refused_not_reported_as_nothing(
    client_sync: mc.Client, body: object, match: str
) -> None:
    respx.get(CREDITS_URL).mock(httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError, match=match):
        client_sync.credits.read()


def _entry_without(key: str) -> dict[str, Any]:
    entry = {k: v for k, v in CREDITS["entries"][0].items() if k != key}
    return {**CREDITS, "entries": [entry]}


@respx.mock
@pytest.mark.parametrize(
    ("body", "missing"),
    [
        (_without("next_before"), "next_before"),
        (_without("grant"), "grant"),
        *((_entry_without(key), key) for key in ("lot_id", "model", "run_id", "step")),
    ],
)
def test_a_nullable_ledger_field_that_is_missing_is_refused_not_read_as_null(
    client_sync: mc.Client, body: dict[str, Any], missing: str
) -> None:
    """Every field of the ledger is required, the nullable ones included. A
    missing ``next_before`` read as ``None`` would end the documented page walk
    at the first page and silently drop the older journal."""
    respx.get(CREDITS_URL).mock(httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError, match=f"no {missing}"):
        client_sync.credits.read()


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
def test_a_key_on_your_own_http_client_is_not_mistaken_for_a_funded_run(method: str) -> None:
    """httpx merges a client's default headers into every request, so a funded
    call through an http_client carrying X-Model-Key would quietly run on that
    key instead. Ambiguous, so refused before anything is sent."""
    with httpx.Client(headers={"x-model-key": "sk-inherited"}) as http:
        c = mc.Computer(mc.Client("gck_test", base_url=BASE, http_client=http)._t, COMPUTER)
        with pytest.raises(mc.MandalaError, match="http_client sends X-Model-Key"):
            result = getattr(c, method)("go", max_cost_usd=1)
            if method == "agent_stream":
                list(result)
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
async def test_the_async_halves_refuse_an_inherited_key_too(method: str) -> None:
    async with httpx.AsyncClient(headers={"X-Model-Key": "sk-inherited"}) as http:
        client = mc.AsyncClient("gck_test", base_url=BASE, http_client=http)
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.MandalaError, match="http_client sends X-Model-Key"):
            result = getattr(c, method)("go")
            if method == "agent_stream":
                _ = [event async for event in result]
            else:
                await result
    assert not respx.calls


@respx.mock
def test_an_explicit_key_still_wins_over_one_on_the_http_client() -> None:
    route = respx.post(AGENT).mock(stream(frame("done", FUNDED_DONE)))
    with httpx.Client(headers={"X-Model-Key": "sk-inherited"}) as http:
        c = mc.Computer(mc.Client("gck_test", base_url=BASE, http_client=http)._t, COMPUTER)
        c.agent("go", model_key=KEY)
    assert route.calls.last.request.headers[MODEL_KEY_HEADER] == KEY


@respx.mock
def test_a_workspace_key_is_refused_the_account_ledger(client_sync: mc.Client) -> None:
    respx.get(CREDITS_URL).mock(httpx.Response(403, json={"error": "use an account-wide key"}))
    with pytest.raises(mc.PermissionDeniedError):
        client_sync.credits.read()


def test_an_unknown_lot_or_entry_kind_is_kept_as_sent() -> None:
    report = mc.CreditReport.from_api(
        {
            **CREDITS,
            "lots": [{**CREDITS["lots"][0], "kind": "promo"}],
            "entries": [{**CREDITS["entries"][0], "kind": "refund"}],
        }
    )
    assert report.lots[0].kind == "promo" and report.entries[0].kind == "refund"


async def test_async_stream_closes_cleanly_on_a_credits_refusal() -> None:
    """``aclosing`` over a stream that raised on open must not mask the error."""
    with respx.mock:
        respx.post(AGENT).mock(httpx.Response(402, json=INSUFFICIENT))
        async with mc.AsyncClient("gck_test", base_url=BASE) as client:
            c = mc.AsyncComputer(client._t, COMPUTER)
            with pytest.raises(mc.InsufficientCreditsError):
                async with aclosing(c.agent_stream("go")) as events:
                    async for _ in events:
                        pass


# --- a funded run's provider failure after steps ------------------------------

#: How the platform reports a funded run's model provider failure: the
#: provider's status, its own fixed sentence and no ``model API: `` prefix.
FUNDED_429 = {
    "error": "Mandala AI is unavailable",
    "usage": {"input_tokens": 3},
    "steps_taken": [{"n": 1, "tool": "computer", "cost_usd": 0.02}],
    "cost_usd": 0.02,
}


@respx.mock
def test_a_funded_429_after_steps_is_not_transient(computer: mc.Computer) -> None:
    """Without the prefix the relayed-429 rule never saw these, so a funded run
    that had clicked was called safe to send again — repeating the clicks and
    spending more credits."""
    respx.post(AGENT).mock(
        side_effect=[
            httpx.Response(429, json=FUNDED_429),
            stream(
                frame("step", FUNDED_STEP),
                frame("error", {"error": "Mandala AI is unavailable", "status": 429, "steps": []}),
            ),
            httpx.Response(429, json={**FUNDED_429, "steps_taken": []}),
            stream(frame("error", {"error": "Mandala AI is unavailable", "status": 429})),
        ]
    )
    with pytest.raises(mc.RateLimitError) as once_after:
        computer.agent_once("go")
    with pytest.raises(mc.RateLimitError) as streamed_after:
        computer.agent("go")
    with pytest.raises(mc.RateLimitError) as once_before:
        computer.agent_once("go")
    with pytest.raises(mc.RateLimitError) as streamed_before:
        computer.agent("go")
    assert mc.is_transient(once_after.value) is False
    assert mc.is_transient(streamed_after.value) is False
    # Before any step nothing ran, and the wait is still worth honouring.
    assert mc.is_transient(once_before.value) is True
    assert mc.is_transient(streamed_before.value) is True


@respx.mock
async def test_the_async_halves_call_a_funded_429_after_steps_not_transient() -> None:
    respx.post(AGENT).mock(
        side_effect=[
            httpx.Response(429, json=FUNDED_429),
            stream(
                frame("step", FUNDED_STEP),
                frame("error", {"error": "Mandala AI is unavailable", "status": 429}),
            ),
        ]
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.RateLimitError) as once_after:
            await c.agent_once("go")
        with pytest.raises(mc.RateLimitError) as streamed_after:
            await c.agent("go")
    assert mc.is_transient(once_after.value) is False
    assert mc.is_transient(streamed_after.value) is False


# --- a funded run's provider 404/413/429, which carry no prefix ---------------

BUDGET = {"RateLimit-Limit": "100", "RateLimit-Remaining": "0", "RateLimit-Reset": "30"}


@respx.mock
@pytest.mark.parametrize("status", [401, 402, 403, 404, 413])
def test_a_funded_providers_failure_is_a_plain_api_error(
    computer: mc.Computer, status: int
) -> None:
    """The platform keeps the provider's status and replaces its words with its
    own fixed sentence. Read by the status table, a funded run's provider
    failure would blame the caller's API key (401), their role (403), the
    computer (404) or the request (413) — and the model-provider class would
    blame a model account the caller does not have. None is the cause."""
    failure = {"error": "Mandala AI is unavailable", "usage": {}, "steps_taken": []}
    respx.post(AGENT).mock(
        side_effect=[
            httpx.Response(status, json=failure),
            stream(frame("error", {**failure, "status": status, "steps": []})),
        ]
    )
    with pytest.raises(mc.APIError) as once:
        computer.agent_once("go")
    with pytest.raises(mc.APIError) as streamed:
        computer.agent("go")
    for error in (once.value, streamed.value):
        assert type(error) is mc.APIError
        assert error.status == status
        assert "Mandala AI is unavailable" in str(error)
        assert mc.is_transient(error) is False


@respx.mock
@pytest.mark.parametrize("status", [401, 402, 403, 404, 413])
async def test_the_async_halves_read_a_funded_providers_failure_the_same_way(
    status: int,
) -> None:
    failure = {"error": "Mandala AI is unavailable", "usage": {}, "steps_taken": []}
    respx.post(AGENT).mock(
        side_effect=[
            httpx.Response(status, json=failure),
            stream(frame("error", {**failure, "status": status})),
        ]
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.APIError) as once:
            await c.agent_once("go")
        with pytest.raises(mc.APIError) as streamed:
            await c.agent("go")
    assert type(once.value) is mc.APIError and type(streamed.value) is mc.APIError


@respx.mock
def test_a_funded_402_without_a_link_is_not_the_callers_model_account(
    computer: mc.Computer,
) -> None:
    """A funded run has no key of the caller's for the provider to bill, so a
    402 without top_up_url, whatever it says, is not a ModelProviderError."""
    respx.post(AGENT).mock(httpx.Response(402, json={"error": "payment required"}))
    with pytest.raises(mc.APIError) as raised:
        computer.agent("go")
    assert type(raised.value) is mc.APIError


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_the_same_sentence_on_a_run_with_your_key_keeps_the_tables_class(
    computer: mc.Computer, status: int
) -> None:
    """Only a funded run is read this way; with your own key a 401 or 403 is
    about a credential and keeps its class."""
    respx.post(AGENT).mock(
        httpx.Response(status, json={"error": "Mandala AI is unavailable", "usage": {}})
    )
    with pytest.raises((mc.AuthenticationError, mc.PermissionDeniedError)):
        computer.agent_once("go", model_key=KEY)


@respx.mock
def test_a_funded_providers_429_does_not_describe_the_mandala_budget(
    computer: mc.Computer,
) -> None:
    respx.post(AGENT).mock(
        httpx.Response(
            429,
            json={"error": "Mandala AI is unavailable", "usage": {}, "steps_taken": []},
            headers={**BUDGET, "Retry-After": "7"},
        )
    )
    with pytest.raises(mc.RateLimitError) as raised:
        computer.agent_once("go")
    assert raised.value.limit is None and raised.value.remaining is None
    assert raised.value.retry_after == 7
    assert mc.is_transient(raised.value) is True


@respx.mock
@pytest.mark.parametrize("status", [404, 413])
def test_the_platforms_own_404_or_413_keeps_its_class_on_a_funded_run(
    computer: mc.Computer, status: int
) -> None:
    respx.post(AGENT).mock(
        httpx.Response(status, json={"error": "no such computer", "usage": {}, "steps_taken": []})
    )
    with pytest.raises((mc.NotFoundError, mc.FileTooLargeError)) as raised:
        computer.agent_once("go")
    assert not isinstance(raised.value, mc.ModelProviderError)


# --- strictness the TypeScript SDK shares -------------------------------------


@pytest.mark.parametrize("bad", [-0.01, -1])
def test_a_negative_charge_is_not_a_charge(bad: float) -> None:
    """A charge is never negative; only the balance left after an overrun is."""
    step = mc.AgentStep.from_api({"n": 1, "cost_usd": bad, "credits_remaining_usd": bad}, 1)
    assert step.cost_usd is None and step.credits_remaining_usd == bad
    assert mc.AgentResult.from_api({"stop": "max_cost", "cost_usd": bad}).cost_usd is None
    error = mc.InsufficientCreditsError("no", status=402, body={"cost_usd": bad})
    assert error.cost_usd is None


@pytest.mark.parametrize(
    ("funded", "cost"), [("true", "0.4"), (1, True), (None, -0.2), ("yes", math.nan)]
)
def test_an_activity_summary_never_coerces_its_charge(funded: object, cost: object) -> None:
    row = mc.Activity.from_api({"activity_id": "a", "funded": funded, "cost_usd": cost})
    assert row.funded is None and row.cost_usd is None


def _lot(**changes: Any) -> dict[str, Any]:
    return {**CREDITS, "lots": [{**CREDITS["lots"][0], **changes}]}


def _entry(**changes: Any) -> dict[str, Any]:
    return {**CREDITS, "entries": [{**CREDITS["entries"][0], **changes}]}


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({**CREDITS, "balance_micros": -1}, "balance_micros"),
        ({**CREDITS, "held_micros": 2**53}, "held_micros"),
        ({**CREDITS, "available_micros": -(2**53)}, "available_micros"),
        (_lot(remaining_micros=-5), "remaining_micros"),
        (_entry(id=0), "id"),
        (_entry(step=-1), "step"),
        ({**CREDITS, "next_before": 2**53}, "next_before"),
        (_lot(expires_at="2026-11-01T00:00:00+00:00"), "expires_at"),
        (_lot(created_at="2026-10-01 00:00:00Z"), "created_at"),
        (_lot(created_at="2026-02-30T00:00:00Z"), "created_at"),
        (_lot(created_at="2026-10-01T24:00:00Z"), "created_at"),
        (_entry(created_at="2026-10-09"), "created_at"),
        (_entry(created_at="\u0662026-10-09T12:00:00Z"), "created_at"),
        ({**CREDITS, "grant": {**CREDITS["grant"], "period_end": "soon"}}, "period_end"),
    ],
)
def test_the_ledger_is_read_as_strictly_as_the_typescript_sdk_reads_it(
    body: dict[str, Any], match: str
) -> None:
    """Micros are safe integers, nonnegative but for ``available_micros``;
    journal positions are positive; timestamps are RFC 3339 instants in UTC."""
    with pytest.raises(mc.MandalaError, match=match):
        mc.CreditReport.from_api(body)


def test_the_ledger_takes_what_the_platform_actually_sends() -> None:
    report = mc.CreditReport.from_api(
        {
            **_lot(created_at="2026-10-01T00:00:00Z", expires_at="2026-11-01T00:00:00.5Z"),
            "available_micros": -(2**53 - 1),
        }
    )
    assert report.available_micros == -(2**53 - 1)
    assert report.lots[0].expires_at == "2026-11-01T00:00:00.5Z"


# OPL-6261: how the account pays, and on pay as you go its burn and runway.
def test_credits_decode_the_billing_mode_burn_and_runway() -> None:
    payg = mc.CreditReport.from_api(
        {
            **CREDITS,
            "billing_mode": "payg",
            "burn_micros_per_hour": 122_738,
            "runway_seconds": 272_189,
        }
    )
    assert (payg.billing_mode, payg.burn_micros_per_hour, payg.runway_seconds) == (
        "payg",
        122_738,
        272_189,
    )
    idle = mc.CreditReport.from_api(
        {**CREDITS, "billing_mode": "payg", "burn_micros_per_hour": 0, "runway_seconds": None}
    )
    assert (idle.burn_micros_per_hour, idle.runway_seconds) == (0, None)
    # A platform that predates pay as you go sends none of them: unknown, not refused.
    old = mc.CreditReport.from_api(CREDITS)
    assert (old.billing_mode, old.burn_micros_per_hour, old.runway_seconds) == (None, None, None)
    with pytest.raises(mc.MandalaError, match="burn_micros_per_hour"):
        mc.CreditReport.from_api({**CREDITS, "billing_mode": "payg", "burn_micros_per_hour": -1})
    with pytest.raises(mc.MandalaError, match="billing_mode"):
        mc.CreditReport.from_api({**CREDITS, "billing_mode": 7})


@respx.mock
def test_a_short_balance_is_an_insufficient_balance_error_and_still_a_plan_limit(
    client_sync: mc.Client,
) -> None:
    respx.post(f"{BASE}/computers").mock(
        httpx.Response(
            402,
            json={
                "error": "Your credit balance is too low to start this.",
                "code": "INSUFFICIENT_BALANCE",
                "projected_micros": 120_000,
                "required_micros": 250_000,
            },
        )
    )
    with pytest.raises(mc.InsufficientBalanceError) as caught:
        client_sync.computers.create()
    err = caught.value
    assert isinstance(err, mc.PlanLimitError)
    assert (err.projected_micros, err.required_micros) == (120_000, 250_000)
    assert not mc.is_transient(err)


@respx.mock
def test_any_other_402_stays_a_plain_plan_limit(client_sync: mc.Client) -> None:
    respx.post(f"{BASE}/computers").mock(
        httpx.Response(402, json={"error": "Not enough RAM on your plan."})
    )
    with pytest.raises(mc.PlanLimitError) as caught:
        client_sync.computers.create()
    assert not isinstance(caught.value, mc.InsufficientBalanceError)
