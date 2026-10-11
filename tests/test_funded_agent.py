"""Agent runs on the account's AI credits, and the ledger they spend (OPL-5866).

A run started without a ``model_key`` is FUNDED: no ``X-Model-Key`` header, the
platform runs it on the account's Mandala AI credits, and every step reports
what the run has cost so far. Pinned here:

- what goes out: no header, ``max_cost_usd`` in the body, checked before it is
  sent the way the platform checks it
- what comes back: the cost fields on steps, results and failures, absent (not
  zero) on a run with the caller's own key
- the 402 that carries ``top_up_url``: the account's credits, raised as
  :class:`~mandala_computer.CreditsExhaustedError` from every entry point,
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
    failure = to_agent_event(
        "error",
        {
            "error": "Insufficient AI credits",
            "status": 402,
            "cost_usd": 0,
            "top_up_url": "/billing",
            "usage": {},
            "steps": [],
        },
        0,
    )
    assert isinstance(failure, mc.AgentFailed)
    assert failure.cost_usd == 0
    assert failure.top_up_url == "/billing"
    plain = to_agent_event("error", {"error": "model API: boom", "status": 500}, 0)
    assert isinstance(plain, mc.AgentFailed)
    assert plain.cost_usd is None and plain.top_up_url is None


# --- the 402 that is the account's credits ------------------------------------


def _assert_credits_refusal(error: BaseException) -> None:
    assert isinstance(error, mc.CreditsExhaustedError)
    assert not isinstance(error, (mc.PlanLimitError, mc.ModelProviderError))
    assert error.status == 402
    assert error.top_up_url == "/billing"
    assert mc.is_transient(error) is False


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
def test_a_funded_run_the_credits_cannot_start_is_credits_exhausted(
    computer: mc.Computer, method: str
) -> None:
    """Refused before the stream opens, with a status — which the transport's
    table would otherwise call a PlanLimitError, on all three entry points."""
    respx.post(AGENT).mock(httpx.Response(402, json=INSUFFICIENT))
    with pytest.raises(mc.CreditsExhaustedError) as error:
        result = getattr(computer, method)("go")
        if method == "agent_stream":
            list(result)
    _assert_credits_refusal(error.value)
    assert error.value.method == "POST"


@respx.mock
@pytest.mark.parametrize("method", ["agent", "agent_once", "agent_stream"])
async def test_the_async_halves_raise_credits_exhausted_too(method: str) -> None:
    respx.post(AGENT).mock(httpx.Response(402, json=INSUFFICIENT))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.CreditsExhaustedError) as error:
            result = getattr(c, method)("go")
            if method == "agent_stream":
                _ = [event async for event in result]
            else:
                await result
    _assert_credits_refusal(error.value)


@respx.mock
def test_a_credits_refusal_reported_on_the_stream_is_credits_exhausted(
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
    with pytest.raises(mc.CreditsExhaustedError) as error:
        computer.agent("go")
    _assert_credits_refusal(error.value)
    assert error.value.cost_usd == 0
    assert error.value.agent is not None and error.value.agent.top_up_url == "/billing"


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
    assert failure.status == 402 and failure.top_up_url == "/billing"


@respx.mock
def test_a_non_streaming_credits_refusal_is_credits_exhausted(computer: mc.Computer) -> None:
    body = {**INSUFFICIENT, "cost_usd": 0, "usage": {"input_tokens": 0}, "steps_taken": []}
    respx.post(AGENT).mock(httpx.Response(402, json=body))
    with pytest.raises(mc.CreditsExhaustedError) as error:
        computer.agent_once("go")
    _assert_credits_refusal(error.value)
    assert error.value.agent is not None and error.value.agent.top_up_url == "/billing"


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
    assert not isinstance(error.value, mc.CreditsExhaustedError)


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


def test_the_top_up_link_on_a_hand_built_error() -> None:
    bare = mc.CreditsExhaustedError("no", status=402)
    assert bare.top_up_url is None and bare.cost_usd is None
    odd = mc.CreditsExhaustedError("no", status=402, body={"top_up_url": " ", "cost_usd": True})
    assert odd.top_up_url is None and odd.cost_usd is None


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
            with pytest.raises(mc.CreditsExhaustedError):
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
