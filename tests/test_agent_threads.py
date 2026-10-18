"""Agent threads (OPL-6084): continuing a run by ``thread_id``, keeping none
with ``store=False``, a caller's own ``messages``, the ``thread_id`` a run
answers with, and ``client.agent_threads`` — sync and async."""

from __future__ import annotations

import json
from contextlib import closing
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer._agent import to_agent_event

BASE = "https://api.test/api/v1"
AGENT = f"{BASE}/computers/vm-1/agent"
THREADS = f"{BASE}/agent/threads"
KEY = "sk-ant-test"
THREAD_ID = "thr_0123456789abcdef01234567"
OTHER_THREAD = "thr_fedcba9876543210fedcba98"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux", "cpu": 2}
HISTORY: list[mc.AgentMessage] = [
    {"role": "user", "content": "open the settings"},
    {"role": "assistant", "content": "the settings are open"},
]

SUMMARY: dict[str, Any] = {
    "id": THREAD_ID,
    "computer_id": "vm-1",
    "title": "open the settings",
    "turns": 4,
    "created_at": "2026-10-18T12:00:00.000Z",
    "updated_at": "2026-10-18T12:05:00.000Z",
}
THREAD: dict[str, Any] = {
    **SUMMARY,
    "messages": [
        {"role": "user", "text": "open the settings", "created_at": "2026-10-18T12:00:00.000Z"},
        {
            "role": "assistant",
            "text": "the settings are open",
            "stop": "end_turn",
            "steps": 3,
            "cost_usd": 0.12,
            "created_at": "2026-10-18T12:01:00.000Z",
        },
        {"role": "user", "text": "turn on dark mode", "created_at": "2026-10-18T12:04:00.000Z"},
        {
            "role": "assistant",
            "text": "",
            "stop": "error",
            "error": "model API: overloaded",
            "steps": 1,
            "created_at": "2026-10-18T12:05:00.000Z",
        },
    ],
}


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


@pytest.fixture
def computer(client: mc.Client) -> mc.Computer:
    return mc.Computer(client._t, COMPUTER)


def stream(*frames: str) -> httpx.Response:
    return httpx.Response(
        200, content="".join(frames).encode(), headers={"Content-Type": "text/event-stream"}
    )


def frame(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


DONE = frame("done", {"steps": 1, "stop": "end_turn", "text": "done", "thread_id": THREAD_ID})


# --- the options on the agent route --------------------------------------------


@respx.mock
def test_thread_id_and_store_reach_the_body_of_every_entry_point(computer: mc.Computer) -> None:
    route = respx.post(AGENT).mock(stream(DONE))
    computer.agent("dark mode", model_key=KEY, thread_id=THREAD_ID, store=True)
    assert sent(route) == {
        "prompt": "dark mode",
        "stream": True,
        "thread_id": THREAD_ID,
        "store": True,
    }
    with closing(computer.agent_stream("dark mode", model_key=KEY, store=False)) as events:
        list(events)
    assert sent(route) == {"prompt": "dark mode", "stream": True, "store": False}

    once = respx.post(AGENT).mock(
        return_value=httpx.Response(200, json={"steps": 1, "stop": "end_turn", "text": "done"})
    )
    computer.agent_once("dark mode", model_key=KEY, thread_id=THREAD_ID)
    assert sent(once) == {"prompt": "dark mode", "stream": False, "thread_id": THREAD_ID}


@respx.mock
def test_messages_are_sent_as_role_and_content_only(computer: mc.Computer) -> None:
    """Only the two fields the platform reads, copied: a caller's dict that
    carries more (an OpenAI ``name``, say) is not forwarded as if it meant
    something here."""
    route = respx.post(AGENT).mock(stream(DONE))
    computer.agent(
        "dark mode",
        model_key=KEY,
        messages=[{"role": "user", "content": "hi", "name": "dana"}, HISTORY[1]],  # type: ignore[list-item]
    )
    assert sent(route)["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "the settings are open"},
    ]


@respx.mock
def test_options_left_out_are_not_sent(computer: mc.Computer) -> None:
    route = respx.post(AGENT).mock(stream(DONE))
    computer.agent("dark mode", model_key=KEY)
    assert sent(route) == {"prompt": "dark mode", "stream": True}


@respx.mock
def test_an_empty_history_is_sent_and_may_go_with_a_thread(computer: mc.Computer) -> None:
    """The platform refuses ``messages`` beside ``thread_id`` only when there is
    a turn in it, so an empty list is no conflict here either."""
    route = respx.post(AGENT).mock(stream(DONE))
    computer.agent("dark mode", model_key=KEY, messages=[], thread_id=THREAD_ID)
    assert sent(route)["messages"] == [] and sent(route)["thread_id"] == THREAD_ID


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"store": False, "thread_id": THREAD_ID}, "store=False cannot continue a thread"),
        ({"messages": HISTORY, "thread_id": THREAD_ID}, "messages or thread_id, not both"),
        ({"thread_id": "run_0123456789abcdef01234567"}, "thr_ followed by 24"),
        ({"thread_id": "thr_0123456789ABCDEF01234567"}, "thr_ followed by 24"),
        ({"thread_id": ""}, "thr_ followed by 24"),
        ({"thread_id": 7}, "thread_id must be a string"),
        ({"store": "false"}, "store must be True or False"),
        ({"store": 0}, "store must be True or False"),
        ({"messages": "hi"}, "messages must be a list"),
        ({"messages": {"role": "user", "content": "hi"}}, "messages must be a list"),
        ({"messages": ["hi"]}, r"messages\[0\] must be"),
        ({"messages": [{"role": "system", "content": "be brief"}]}, r"messages\[0\] role"),
        ({"messages": [{"content": "hi"}]}, r"messages\[0\] role"),
        ({"messages": [HISTORY[0], {"role": "assistant"}]}, r"messages\[1\] content"),
        ({"messages": [{"role": "user", "content": [{"type": "text"}]}]}, r"messages\[0\] content"),
    ],
)
def test_what_the_platform_would_refuse_is_refused_before_the_request(
    computer: mc.Computer, kwargs: dict[str, Any], match: str
) -> None:
    """Every one of these is a 400 or a 404 the platform answers only after the
    run has been admitted, and its rate cost spent."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(AGENT).mock(stream(DONE))
        for call in (computer.agent, computer.agent_once):
            with pytest.raises(ValueError, match=match):
                call("dark mode", model_key=KEY, **kwargs)
        with pytest.raises(ValueError, match=match):
            next(computer.agent_stream("dark mode", model_key=KEY, **kwargs))
        assert not route.called


@respx.mock
def test_a_thread_the_platform_refuses_keeps_its_class(computer: mc.Computer) -> None:
    """Another computer's thread, or a full one, is a 409 before the stream
    opens, and one this key cannot see a 404: neither is the model provider's."""
    respx.post(AGENT).mock(
        side_effect=[
            httpx.Response(409, json={"error": "thread thr_x belongs to computer vm-2"}),
            httpx.Response(404, json={"error": "thread not found"}),
        ]
    )
    with pytest.raises(mc.ConflictError, match="belongs to computer vm-2"):
        computer.agent("dark mode", model_key=KEY, thread_id=THREAD_ID)
    with pytest.raises(mc.NotFoundError, match="thread not found"):
        computer.agent("dark mode", model_key=KEY, thread_id=THREAD_ID)


# --- the thread a run answers with ---------------------------------------------


@respx.mock
def test_the_result_names_its_thread_streamed_and_not(computer: mc.Computer) -> None:
    respx.post(AGENT).mock(stream(DONE))
    assert computer.agent("dark mode", model_key=KEY).thread_id == THREAD_ID
    respx.post(AGENT).mock(
        return_value=httpx.Response(
            200, json={"steps": 1, "stop": "end_turn", "text": "ok", "thread_id": OTHER_THREAD}
        )
    )
    assert computer.agent_once("dark mode", model_key=KEY).thread_id == OTHER_THREAD


@pytest.mark.parametrize("value", [None, "", 7, ["thr_x"]])
def test_a_result_without_a_usable_thread_names_none(value: object) -> None:
    """``store=False`` keeps nothing, and a malformed id costs the result
    nothing: the run's answer is still the answer."""
    result = mc.AgentResult.from_api({"stop": "end_turn", "text": "done", "thread_id": value})
    assert result.thread_id is None and result.text == "done"


@respx.mock
def test_a_failed_run_names_its_thread_on_the_event_and_the_exception(
    computer: mc.Computer,
) -> None:
    error = frame(
        "error",
        {
            "error": "model API: overloaded",
            "status": 529,
            "usage": {"input_tokens": 10},
            "steps": [{"n": 1, "tool": "computer", "action": "left_click"}],
            "thread_id": THREAD_ID,
        },
    )
    respx.post(AGENT).mock(stream(error))
    (event,) = list(computer.agent_stream("dark mode", model_key=KEY))
    assert isinstance(event, mc.AgentFailed) and event.thread_id == THREAD_ID
    with pytest.raises(mc.APIError) as caught:
        computer.agent("dark mode", model_key=KEY)
    assert caught.value.thread_id == THREAD_ID
    assert caught.value.agent is not None and caught.value.agent.thread_id == THREAD_ID


@respx.mock
def test_a_non_streaming_failure_names_its_thread_on_the_exception(
    computer: mc.Computer,
) -> None:
    respx.post(AGENT).mock(
        return_value=httpx.Response(
            529,
            json={
                "error": "model API: overloaded",
                "usage": {"input_tokens": 10},
                "steps_taken": [{"n": 1, "tool": "bash"}],
                "thread_id": THREAD_ID,
            },
        )
    )
    with pytest.raises(mc.APIError) as caught:
        computer.agent_once("dark mode", model_key=KEY)
    assert caught.value.thread_id == THREAD_ID


def test_an_error_that_is_not_a_run_names_no_thread() -> None:
    assert mc.MandalaError("nothing to do with the agent").thread_id is None
    failed = to_agent_event("error", {"error": "refused", "status": 409}, 0)
    assert isinstance(failed, mc.AgentFailed) and failed.thread_id is None


# --- client.agent_threads --------------------------------------------------------


@respx.mock
def test_list_sends_every_parameter_and_reads_the_page(client: mc.Client) -> None:
    route = respx.get(THREADS).mock(
        return_value=httpx.Response(200, json={"threads": [SUMMARY], "has_more": True})
    )
    page = client.agent_threads.list(computer_id="vm-1", limit=1, before=OTHER_THREAD)
    assert dict(route.calls.last.request.url.params) == {
        "computer_id": "vm-1",
        "limit": "1",
        "before": OTHER_THREAD,
    }
    assert page.threads == [
        mc.AgentThreadSummary(
            id=THREAD_ID,
            computer_id="vm-1",
            title="open the settings",
            turns=4,
            created_at=SUMMARY["created_at"],
            updated_at=SUMMARY["updated_at"],
        )
    ]
    assert page.has_more is True and page.next_before == THREAD_ID


@respx.mock
def test_a_walk_follows_next_before_to_the_last_page(client: mc.Client) -> None:
    older = {**SUMMARY, "id": OTHER_THREAD}
    route = respx.get(THREADS).mock(
        side_effect=[
            httpx.Response(200, json={"threads": [SUMMARY], "has_more": True}),
            httpx.Response(200, json={"threads": [older], "has_more": False}),
        ]
    )
    seen, before = [], None
    while True:
        page = client.agent_threads.list(limit=1, before=before)
        seen += [t.id for t in page.threads]
        before = page.next_before
        if before is None:
            break
    assert seen == [THREAD_ID, OTHER_THREAD]
    assert route.calls[0].request.url.query == b"limit=1"
    assert route.calls[1].request.url.params["before"] == THREAD_ID


@respx.mock
def test_list_sends_nothing_it_was_not_given(client: mc.Client) -> None:
    route = respx.get(THREADS).mock(
        return_value=httpx.Response(200, json={"threads": [], "has_more": False})
    )
    page = client.agent_threads.list()
    assert route.calls.last.request.url.query == b""
    assert page.threads == [] and page.next_before is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": 2.5},
        {"limit": True},
        {"computer_id": ""},
        {"computer_id": " vm-1"},
        {"before": ""},
        {"before": 7},
    ],
)
def test_list_refuses_what_the_platform_would_400_before_sending(
    client: mc.Client, kwargs: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            client.agent_threads.list(**kwargs)
        assert not mock.calls


@pytest.mark.parametrize(
    "body",
    [
        {"threads": [SUMMARY]},
        {"threads": [SUMMARY], "has_more": "false"},
        {"threads": [], "has_more": True},
        {"threads": {}, "has_more": False},
        {"threads": ["thr_x"], "has_more": False},
        {"threads": [{**SUMMARY, "id": ""}], "has_more": False},
        {"threads": [{**SUMMARY, "id": None}], "has_more": False},
    ],
)
@respx.mock
def test_list_refuses_a_page_that_could_not_be_walked(client: mc.Client, body: Any) -> None:
    respx.get(THREADS).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError):
        client.agent_threads.list()


@respx.mock
def test_get_reads_every_turn(client: mc.Client) -> None:
    route = respx.get(f"{THREADS}/{THREAD_ID}").mock(return_value=httpx.Response(200, json=THREAD))
    thread = client.agent_threads.get(THREAD_ID)
    assert route.called
    assert isinstance(thread, mc.AgentThreadSummary) and thread.turns == 4
    assert [(t.role, t.stop) for t in thread.messages] == [
        ("user", None),
        ("assistant", "end_turn"),
        ("user", None),
        ("assistant", "error"),
    ]
    assert thread.messages[1] == mc.AgentThreadTurn(
        role="assistant",
        text="the settings are open",
        created_at="2026-10-18T12:01:00.000Z",
        stop="end_turn",
        steps=3,
        cost_usd=0.12,
    )
    assert thread.messages[3].error == "model API: overloaded"
    assert thread.messages[0].steps is None and thread.messages[0].cost_usd is None
    assert thread.raw == THREAD


@pytest.mark.parametrize(
    "body",
    [
        {**SUMMARY},
        {**SUMMARY, "messages": "none"},
        {**SUMMARY, "messages": [{"text": "no role"}]},
        {**SUMMARY, "messages": ["a turn"]},
    ],
)
@respx.mock
def test_get_refuses_a_thread_whose_turns_cannot_be_read(client: mc.Client, body: Any) -> None:
    """A thread read without its turns is one that reads as empty."""
    respx.get(f"{THREADS}/{THREAD_ID}").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError):
        client.agent_threads.get(THREAD_ID)


@respx.mock
def test_delete_and_a_thread_this_key_cannot_see(client: mc.Client) -> None:
    gone = respx.delete(f"{THREADS}/{THREAD_ID}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert client.agent_threads.delete(THREAD_ID) is None
    assert gone.called
    respx.get(f"{THREADS}/{OTHER_THREAD}").mock(
        return_value=httpx.Response(404, json={"error": "thread not found"})
    )
    with pytest.raises(mc.NotFoundError):
        client.agent_threads.get(OTHER_THREAD)


@pytest.mark.parametrize("bad", ["", "..", "thr/../x"])
def test_an_id_that_would_address_another_route_is_refused_or_escaped(
    client: mc.Client, bad: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(host="api.test").mock(return_value=httpx.Response(404, json={}))
        if bad in ("", ".."):
            with pytest.raises(ValueError):
                client.agent_threads.get(bad)
            assert not route.called
        else:
            with pytest.raises(mc.NotFoundError):
                client.agent_threads.delete(bad)
            assert route.calls.last.request.url.raw_path.endswith(b"/agent/threads/thr%2F..%2Fx")


# --- the async half -------------------------------------------------------------


async def test_async_options_result_and_threads() -> None:
    with respx.mock:
        route = respx.post(AGENT).mock(stream(DONE))
        listing = respx.get(THREADS).mock(
            return_value=httpx.Response(200, json={"threads": [SUMMARY], "has_more": False})
        )
        respx.get(f"{THREADS}/{THREAD_ID}").mock(return_value=httpx.Response(200, json=THREAD))
        gone = respx.delete(f"{THREADS}/{THREAD_ID}").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        async with mc.AsyncClient("gck_test", base_url=BASE) as client:
            c = mc.AsyncComputer(client._t, COMPUTER)
            result = await c.agent("dark mode", model_key=KEY, messages=HISTORY, store=True)
            assert result.thread_id == THREAD_ID
            assert sent(route)["messages"] == list(HISTORY) and sent(route)["store"] is True
            with pytest.raises(ValueError, match="not both"):
                await c.agent_once(
                    "dark mode", model_key=KEY, messages=HISTORY, thread_id=THREAD_ID
                )
            page = await client.agent_threads.list(computer_id="vm-1", limit=5)
            assert page.threads[0].id == THREAD_ID and page.next_before is None
            assert dict(listing.calls.last.request.url.params) == {
                "computer_id": "vm-1",
                "limit": "5",
            }
            thread = await client.agent_threads.get(THREAD_ID)
            assert len(thread.messages) == 4
            assert await client.agent_threads.delete(THREAD_ID) is None
            assert gone.called


async def test_async_failure_names_its_thread() -> None:
    with respx.mock:
        respx.post(AGENT).mock(
            stream(
                frame(
                    "error",
                    {"error": "revoked", "status": 401, "steps": [], "thread_id": THREAD_ID},
                )
            )
        )
        async with mc.AsyncClient("gck_test", base_url=BASE) as client:
            c = mc.AsyncComputer(client._t, COMPUTER)
            with pytest.raises(mc.AuthenticationError) as caught:
                await c.agent("dark mode", model_key=KEY, thread_id=THREAD_ID)
            assert caught.value.thread_id == THREAD_ID
