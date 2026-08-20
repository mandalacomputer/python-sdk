"""The agent loop, against a mocked stream.

The route is unlike every other one here: it answers 200 and then reports its
own outcome — including its own failures — inside the body. So most of what is
worth testing is about what the SDK does with events rather than with statuses,
and the cases that matter are the ones where "the run ended" and "the run went
well" come apart.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer._client import MODEL_KEY_HEADER

BASE = "https://api.test/api/v1"
AGENT = f"{BASE}/computers/vm-1/agent"
KEY = "sk-ant-test"

COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux", "cpu": 2}

DONE = {"steps": 2, "stop": "end_turn", "text": "all set", "usage": {"input_tokens": 900}}


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


@pytest.fixture
def computer(client: mc.Client) -> mc.Computer:
    return mc.Computer(client._t, COMPUTER)


def stream(*frames: str) -> httpx.Response:
    """An event-stream response carrying these frames, already framed."""
    return httpx.Response(
        200,
        content="".join(frames).encode(),
        headers={"Content-Type": "text/event-stream"},
    )


def frame(event: str, data: str) -> str:
    return f"event: {event}\ndata: {data}\n\n"


def json_body(route: respx.Route) -> object:
    """What the last call actually put on the wire."""
    return json.loads(route.calls.last.request.content)


DONE_FRAME = frame("done", '{"steps": 2, "stop": "end_turn", "text": "all set"}')


# --- the happy path -------------------------------------------------------


@respx.mock
def test_a_run_that_finishes_comes_back_as_a_result(computer: mc.Computer) -> None:
    respx.post(AGENT).mock(stream(frame("done", '{"steps": 2, "stop": "end_turn"}')))
    result = computer.agent("do the thing", model_key=KEY)
    assert result.finished and result.stop == "end_turn" and result.steps == 2


@respx.mock
def test_the_stream_reports_each_step_as_it_happens(computer: mc.Computer) -> None:
    respx.post(AGENT).mock(
        stream(
            frame("step", '{"n": 1, "tool": "computer", "action": "left_click"}'),
            frame("text", '{"text": "clicking the menu"}'),
            frame("step", '{"n": 2, "tool": "bash"}'),
            DONE_FRAME,
        )
    )
    events = list(computer.agent_stream("do the thing", model_key=KEY))
    assert [type(e).__name__ for e in events] == [
        "AgentStepEvent",
        "AgentText",
        "AgentStepEvent",
        "AgentDone",
    ]
    assert events[0].step.action == "left_click"
    assert events[1].text == "clicking the menu"


@respx.mock
def test_usage_is_carried_off_the_result(computer: mc.Computer) -> None:
    """It is spent on the caller's own key, so it is the one number they cannot
    look up on their Mandala bill afterwards."""
    respx.post(AGENT).mock(
        stream(
            frame(
                "done",
                '{"stop": "end_turn", "usage": {"input_tokens": 900, '
                '"output_tokens": 40, "cache_read_tokens": 800}}',
            )
        )
    )
    usage = computer.agent("do the thing", model_key=KEY).usage
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (900, 40, 800)


# --- endings that are not failures ---------------------------------------


@respx.mock
@pytest.mark.parametrize("stop", ["max_steps", "rate_limited", "refusal"])
def test_a_run_that_did_not_finish_is_returned_not_raised(computer: mc.Computer, stop: str) -> None:
    """The steps already taken are real, and what they did to the desktop stands.

    Raising would discard the only account of what was done to somebody's
    machine — so these come back as a result whose `finished` is False, which is
    the check callers are told to write.
    """
    respx.post(AGENT).mock(stream(frame("done", f'{{"steps": 9, "stop": "{stop}"}}')))
    result = computer.agent("do the thing", model_key=KEY)
    assert not result.finished
    assert result.stop == stop and result.steps == 9


# --- endings that are ----------------------------------------------------


@respx.mock
def test_a_failure_reported_mid_stream_is_raised_as_its_status_deserves(
    computer: mc.Computer,
) -> None:
    """A 401 arriving inside a 200 is still a 401.

    The response was a success and the run went wrong afterwards, so nothing
    maps this for us. Raised bare, the one failure that reaches a caller from
    inside a stream would be the one their `except AuthenticationError` misses.
    """
    respx.post(AGENT).mock(stream(frame("error", '{"error": "bad model key", "status": 401}')))
    with pytest.raises(mc.AuthenticationError, match="bad model key") as e:
        computer.agent("do the thing", model_key=KEY)
    assert e.value.status == 401


@respx.mock
def test_a_failure_with_no_status_is_still_a_mandala_error(computer: mc.Computer) -> None:
    respx.post(AGENT).mock(stream(frame("error", '{"error": "the model went away"}')))
    with pytest.raises(mc.MandalaError, match="the model went away"):
        computer.agent("do the thing", model_key=KEY)


@respx.mock
def test_a_failure_after_a_result_still_wins(computer: mc.Computer) -> None:
    """The last word about a run that went wrong is not a result that predates it."""
    respx.post(AGENT).mock(stream(DONE_FRAME, frame("error", '{"error": "lost the host"}')))
    with pytest.raises(mc.MandalaError, match="lost the host"):
        computer.agent("do the thing", model_key=KEY)


@respx.mock
def test_a_stream_that_says_nothing_about_the_outcome_raises(computer: mc.Computer) -> None:
    """Steps and no ending. Returning a default result would report a run that
    was cut off as one that completed with no text."""
    respx.post(AGENT).mock(stream(frame("step", '{"n": 1}')))
    with pytest.raises(mc.MandalaError, match="ended without a result"):
        computer.agent("do the thing", model_key=KEY)


@respx.mock
def test_a_status_on_the_response_itself_is_raised_as_usual(computer: mc.Computer) -> None:
    """A run refused before it started — a stopped computer, or one already
    being driven — never becomes a stream at all."""
    respx.post(AGENT).mock(httpx.Response(409, json={"error": "computer is not running"}))
    with pytest.raises(mc.ConflictError, match="not running"):
        computer.agent("do the thing", model_key=KEY)


@respx.mock
def test_something_that_is_not_a_stream_says_so(computer: mc.Computer) -> None:
    """The captive-portal case: a proxy answering 200 with an HTML page.

    It contains no `data:` lines, so without the content-type check it frames to
    a stream of no events and surfaces as "ended without a result" — a sentence
    about the platform, describing something that never reached it.
    """
    respx.post(AGENT).mock(
        httpx.Response(200, html="<html>Sign in to the wifi</html>"),
    )
    with pytest.raises(mc.MandalaError, match="not an event stream"):
        computer.agent("do the thing", model_key=KEY)


# --- what goes out --------------------------------------------------------


@respx.mock
def test_the_model_key_travels_on_the_request_and_not_on_the_client(
    computer: mc.Computer,
) -> None:
    route = respx.post(AGENT).mock(stream(DONE_FRAME))
    computer.agent("do the thing", model_key=KEY)
    sent = route.calls.last.request
    assert sent.headers[MODEL_KEY_HEADER] == KEY
    assert sent.headers["accept"] == "text/event-stream"
    # Not kept: the client's own headers are what every other call carries, and
    # a model key on those is a model key on every request the SDK ever makes.
    assert MODEL_KEY_HEADER not in computer._t._headers


@respx.mock
def test_the_options_reach_the_body(computer: mc.Computer) -> None:
    route = respx.post(AGENT).mock(stream(DONE_FRAME))
    computer.agent(
        "do the thing", model_key=KEY, system="be brief", max_steps=5, model="claude-opus-5"
    )
    body = json_body(route)
    assert body == {
        "prompt": "do the thing",
        "stream": True,
        "system": "be brief",
        "max_steps": 5,
        "model": "claude-opus-5",
    }


@respx.mock
def test_what_was_not_asked_for_is_not_sent(computer: mc.Computer) -> None:
    """An omitted option must not become an explicit null the platform then
    reads as a choice."""
    route = respx.post(AGENT).mock(stream(DONE_FRAME))
    computer.agent("do the thing", model_key=KEY)
    assert json_body(route) == {"prompt": "do the thing", "stream": True}


# --- what is refused before it is sent ------------------------------------


def test_a_run_without_a_model_key_is_refused_here(computer: mc.Computer) -> None:
    """Not sent to be 401'd. On this route a 401 reads as "your Mandala key is
    wrong", which is the one thing it does not mean."""
    with pytest.raises(mc.MandalaError, match="your own Anthropic API key"):
        computer.agent("do the thing", model_key="")


@respx.mock
def test_building_the_stream_spends_nothing_until_it_is_iterated(
    computer: mc.Computer,
) -> None:
    """Ordinary generator semantics, worth pinning because of what a step costs.

    Nothing is sent, and nothing is checked, until the first event is asked
    for — so a run that is set up and then abandoned never starts.
    """
    route = respx.post(AGENT).mock(stream(DONE_FRAME))
    events = computer.agent_stream("do the thing", model_key=KEY)
    assert not route.called
    next(events)
    assert route.called


def test_the_stream_checks_its_arguments_when_it_starts_not_when_it_is_built(
    computer: mc.Computer,
) -> None:
    """The other half of the same fact: the refusal arrives at the first step.

    `agent()` and `agent_once()` raise on the call itself, because they iterate.
    """
    events = computer.agent_stream("do the thing", model_key="")
    with pytest.raises(mc.MandalaError, match="your own Anthropic API key"):
        next(events)


def test_an_empty_prompt_is_refused_before_it_costs_anything(computer: mc.Computer) -> None:
    """The one call where a round trip is not the whole cost of getting it
    wrong: what comes back is billed to the caller's own model key."""
    with pytest.raises(ValueError, match="prompt must not be empty"):
        computer.agent("   ", model_key=KEY)


def test_a_step_cap_of_nothing_is_refused(computer: mc.Computer) -> None:
    with pytest.raises(ValueError, match="max_steps must be at least 1"):
        computer.agent("do the thing", model_key=KEY, max_steps=0)


# --- the non-streaming form -----------------------------------------------


@respx.mock
def test_agent_once_asks_for_one_body_and_reads_it(computer: mc.Computer) -> None:
    route = respx.post(AGENT).mock(httpx.Response(200, json=DONE))
    result = computer.agent_once("do the thing", model_key=KEY)
    assert json_body(route) == {"prompt": "do the thing", "stream": False}
    assert result.finished and result.text == "all set"


@respx.mock
def test_agent_once_still_needs_the_model_key(computer: mc.Computer) -> None:
    with pytest.raises(mc.MandalaError, match="your own Anthropic API key"):
        computer.agent_once("do the thing", model_key="")


# --- forward compatibility -------------------------------------------------


@respx.mock
def test_an_event_this_sdk_does_not_know_is_skipped(computer: mc.Computer) -> None:
    """The platform is free to add types. Falling over on the first unrecognised
    one would turn a forward-compatible addition into an outage."""
    respx.post(AGENT).mock(
        stream(frame("thinking", '{"tokens": 12}'), frame("step", '{"n": 1}'), DONE_FRAME)
    )
    events = list(computer.agent_stream("do the thing", model_key=KEY))
    assert [type(e).__name__ for e in events] == ["AgentStepEvent", "AgentDone"]


@respx.mock
def test_a_step_with_no_number_is_counted_rather_than_left_at_zero(
    computer: mc.Computer,
) -> None:
    """The count is the SDK's, so a server that stops sending `n` does not turn
    every step in a caller's progress line into "0.".
    """
    respx.post(AGENT).mock(
        stream(frame("step", '{"tool": "computer"}'), frame("step", '{"tool": "bash"}'), DONE_FRAME)
    )
    steps = [
        e.step.n
        for e in computer.agent_stream("do the thing", model_key=KEY)
        if isinstance(e, mc.AgentStepEvent)
    ]
    assert steps == [1, 2]


@respx.mock
def test_an_unrecognised_stop_reason_is_not_finished(computer: mc.Computer) -> None:
    """`finished` is `end_turn` and nothing else. A stop reason added later must
    default to "did not finish" rather than to success."""
    respx.post(AGENT).mock(stream(frame("done", '{"stop": "interrupted_by_operator"}')))
    result = computer.agent("do the thing", model_key=KEY)
    assert not result.finished and result.stop == "interrupted_by_operator"


@respx.mock
def test_a_done_frame_with_nothing_in_it_is_survivable(computer: mc.Computer) -> None:
    """Every field is optional as far as this SDK is concerned — see the models."""
    respx.post(AGENT).mock(stream(frame("done", "{}")))
    result = computer.agent("do the thing", model_key=KEY)
    assert not result.finished and result.stop == "unknown" and result.text == ""


# --- the async half --------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_the_async_client_streams_the_same_events() -> None:
    respx.post(AGENT).mock(stream(frame("step", '{"n": 1}'), DONE_FRAME))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        events = [e async for e in c.agent_stream("do the thing", model_key=KEY)]
    assert [type(e).__name__ for e in events] == ["AgentStepEvent", "AgentDone"]


@respx.mock
@pytest.mark.asyncio
async def test_the_async_client_raises_the_same_mid_stream_failure() -> None:
    respx.post(AGENT).mock(stream(frame("error", '{"error": "bad model key", "status": 401}')))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.AuthenticationError, match="bad model key"):
            await c.agent("do the thing", model_key=KEY)


@respx.mock
@pytest.mark.asyncio
async def test_the_async_non_streaming_form_reads_one_body() -> None:
    respx.post(AGENT).mock(httpx.Response(200, json=DONE))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        assert (await c.agent_once("do the thing", model_key=KEY)).text == "all set"
