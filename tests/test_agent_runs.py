"""Background agent runs (OPL-6084): ``client.agent_runs`` and
``Computer.start_agent_run`` — the start, the read, the page, the cancel and the
wait, sync and async."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer._client import MODEL_KEY_HEADER

BASE = "https://api.test/api/v1"
START = f"{BASE}/computers/vm-1/agent/runs"
RUNS = f"{BASE}/agent/runs"
KEY = "sk-ant-test"
RUN_ID = "run_0123456789abcdef01234567"
OLDER_RUN = "run_fedcba9876543210fedcba98"
THREAD_ID = "thr_0123456789abcdef01234567"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux", "cpu": 2}

RUNNING: dict[str, Any] = {
    "id": RUN_ID,
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
STEPPING: dict[str, Any] = {
    **RUNNING,
    "thread_id": THREAD_ID,
    "steps": 1,
    "cost_usd": 0.05,
    "steps_taken": [{"n": 1, "tool": "computer", "action": "screenshot", "cost_usd": 0.05}],
    "updated_at": "2026-10-18T12:00:10.000Z",
}
COMPLETED: dict[str, Any] = {
    **STEPPING,
    "status": "completed",
    "steps": 2,
    "cost_usd": 0.11,
    "result": {
        "steps": 2,
        "stop": "end_turn",
        "text": "renamed 4 files",
        "usage": {"input_tokens": 900, "output_tokens": 40},
        "cost_usd": 0.11,
    },
    "steps_taken": [
        {"n": 1, "tool": "computer", "action": "screenshot"},
        {"n": 2, "tool": "bash", "detail": "mv … → exit 0"},
    ],
    "updated_at": "2026-10-18T12:01:00.000Z",
    "ended_at": "2026-10-18T12:01:00.000Z",
}
FAILED: dict[str, Any] = {
    **STEPPING,
    "status": "failed",
    "error": {
        "error": "Insufficient AI credits",
        "status": 402,
        "reason": "credits",
        "top_up_url": "/billing",
    },
    "ended_at": "2026-10-18T12:00:20.000Z",
}
INTERRUPTED: dict[str, Any] = {
    **STEPPING,
    "status": "interrupted",
    "error": {"error": "the platform restarted while the run was going", "status": 503},
    "ended_at": "2026-10-18T12:00:30.000Z",
}
CANCELLED: dict[str, Any] = {
    **STEPPING,
    "status": "cancelled",
    "ended_at": "2026-10-18T12:00:40.000Z",
}


def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


def aclient() -> mc.AsyncClient:
    return mc.AsyncClient("gck_test", base_url=BASE)


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


def sequence(*bodies: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Answers each read with the next body in turn, and the last one forever."""
    answers = list(bodies)

    def answer(_request: httpx.Request) -> httpx.Response:
        body = answers.pop(0) if len(answers) > 1 else answers[0]
        return httpx.Response(200, json=body)

    return answer


# --- start ----------------------------------------------------------------------


@respx.mock
def test_start_sends_the_agent_body_without_stream_and_reads_the_202() -> None:
    route = respx.post(START).mock(return_value=httpx.Response(202, json=RUNNING))
    run = client().agent_runs.start(
        "vm-1",
        "rename the screenshots",
        model_key=KEY,
        system="be brief",
        max_steps=5,
        model="claude-opus-5-5",
        provider="anthropic",
        max_cost_usd=1.0,
        thread_id=THREAD_ID,
    )
    assert sent(route) == {
        "prompt": "rename the screenshots",
        "system": "be brief",
        "max_steps": 5,
        "model": "claude-opus-5-5",
        "provider": "anthropic",
        "max_cost_usd": 1.0,
        "thread_id": THREAD_ID,
    }
    assert route.calls.last.request.headers[MODEL_KEY_HEADER] == KEY
    assert run.id == RUN_ID and run.status == "running"
    assert run.thread_id is None and run.ended_at is None
    assert not run.ended and not run.finished
    assert run.result is None and run.error is None and run.steps_taken == ()


@respx.mock
def test_the_handle_starts_the_same_run_and_a_funded_one_sends_no_key() -> None:
    route = respx.post(START).mock(return_value=httpx.Response(202, json=RUNNING))
    c = mc.Computer(client()._t, COMPUTER)
    run = c.start_agent_run("rename the screenshots", messages=[{"role": "user", "content": "hi"}])
    assert run.id == RUN_ID
    assert MODEL_KEY_HEADER not in route.calls.last.request.headers
    assert sent(route) == {
        "prompt": "rename the screenshots",
        "messages": [{"role": "user", "content": "hi"}],
    }


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"store": False}, TypeError),
        ({"stream": True}, TypeError),
        ({"messages": [{"role": "user", "content": "hi"}], "thread_id": THREAD_ID}, ValueError),
        ({"thread_id": RUN_ID}, ValueError),
        ({"max_steps": 0}, ValueError),
        ({"max_cost_usd": 50}, ValueError),
        ({"model_key": " "}, mc.MandalaError),
    ],
)
def test_start_refuses_what_the_platform_would_before_sending(
    kwargs: dict[str, Any], error: type[Exception]
) -> None:
    """``store`` and ``stream`` are not arguments at all: the platform refuses
    ``store: false`` and any ``stream`` on this route, and ``store: true`` is
    its default. The rest are the agent route's own checks."""
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(START).mock(return_value=httpx.Response(202, json=RUNNING))
        with pytest.raises(error):
            client().agent_runs.start("vm-1", "rename the screenshots", **kwargs)
        with pytest.raises(error):
            mc.Computer(client()._t, COMPUTER).start_agent_run("rename", **kwargs)
        assert not route.called


@respx.mock
def test_a_run_the_credits_cannot_start_is_insufficient_credits_not_the_plan() -> None:
    respx.post(START).mock(
        return_value=httpx.Response(
            402, json={"error": "Insufficient AI credits", "top_up_url": "/billing"}
        )
    )
    with pytest.raises(mc.InsufficientCreditsError) as caught:
        client().agent_runs.start("vm-1", "rename the screenshots")
    assert caught.value.top_up_url == "https://api.test/billing"


@respx.mock
def test_a_refused_start_keeps_the_class_its_status_deserves() -> None:
    respx.post(START).mock(
        side_effect=[
            httpx.Response(409, json={"error": "already driven", "reason": "contention"}),
            httpx.Response(404, json={"error": "computer not found"}),
        ]
    )
    with pytest.raises(mc.ConflictError) as conflict:
        client().agent_runs.start("vm-1", "rename the screenshots", model_key=KEY)
    assert conflict.value.reason == "contention" and mc.is_transient(conflict.value)
    with pytest.raises(mc.NotFoundError):
        client().agent_runs.start("vm-1", "rename the screenshots", model_key=KEY)


@respx.mock
def test_a_start_is_not_sent_twice() -> None:
    """A start whose answer was lost may have started a run, and a second POST
    would start another: no retry, even on a client that asked for them."""
    route = respx.post(START).mock(return_value=httpx.Response(503, json={"error": "busy"}))
    retrying = mc.Client("gck_test", base_url=BASE, retries={"idempotent": 3})
    with pytest.raises(mc.UnavailableError):
        retrying.agent_runs.start("vm-1", "rename the screenshots", model_key=KEY)
    assert route.call_count == 1


# --- get, list, cancel ----------------------------------------------------------


@respx.mock
def test_get_reads_a_completed_run_whole() -> None:
    route = respx.get(f"{RUNS}/{RUN_ID}").mock(return_value=httpx.Response(200, json=COMPLETED))
    run = client().agent_runs.get(RUN_ID)
    assert route.called
    assert run.status == "completed" and run.ended and run.finished
    assert run.thread_id == THREAD_ID and run.cost_usd == 0.11
    assert run.result is not None and run.result.text == "renamed 4 files"
    assert run.result.usage.input_tokens == 900 and run.result.thread_id == THREAD_ID
    assert [s.tool for s in run.steps_taken] == ["computer", "bash"]
    assert run.ended_at == COMPLETED["ended_at"] and run.raw == COMPLETED


@respx.mock
def test_a_failure_and_an_interruption_carry_their_error() -> None:
    respx.get(f"{RUNS}/{RUN_ID}").mock(side_effect=sequence(FAILED, INTERRUPTED))
    failed = client().agent_runs.get(RUN_ID)
    assert failed.ended and not failed.finished
    assert failed.error == mc.AgentRunError(
        error="Insufficient AI credits",
        status=402,
        reason="credits",
        top_up_url="https://api.test/billing",
    )
    assert failed.error.raw["top_up_url"] == "/billing"
    interrupted = client().agent_runs.get(RUN_ID)
    assert interrupted.ended and interrupted.error is not None
    assert interrupted.error.status == 503 and interrupted.error.reason is None


@pytest.mark.parametrize("nested", [None, "", "thr_aaaaaaaaaaaaaaaaaaaaaaaa"])
def test_the_result_names_the_runs_own_thread_whatever_it_carried(nested: object) -> None:
    """The run's ``thread_id`` and its result's cannot disagree: a caller
    reading ``run.result.thread_id`` to continue the conversation gets the
    thread the run says it is kept in, not one the result happened to carry."""
    body = {**COMPLETED, "result": {**COMPLETED["result"], "thread_id": nested}}
    run = mc.AgentRun.from_api(body)
    assert run.thread_id == THREAD_ID
    assert run.result is not None and run.result.thread_id == THREAD_ID
    unkept = mc.AgentRun.from_api({**body, "thread_id": None})
    assert unkept.result is not None and unkept.result.thread_id is None


def test_completed_is_not_finished_unless_the_model_ended_its_turn() -> None:
    capped = {**COMPLETED, "result": {**COMPLETED["result"], "stop": "max_steps"}}
    run = mc.AgentRun.from_api(capped)
    assert run.ended and not run.finished


@pytest.mark.parametrize(
    "bad",
    [
        {**RUNNING, "id": ""},
        {**RUNNING, "id": None},
        {**RUNNING, "status": ""},
        {**RUNNING, "status": 3},
        {**RUNNING, "thread_id": 7},
        {**COMPLETED, "result": "end_turn"},
        {**FAILED, "error": "Insufficient AI credits"},
        {**RUNNING, "steps_taken": {"n": 1}},
        ["not", "a", "run"],
    ],
)
@respx.mock
def test_get_refuses_a_run_a_wait_could_not_decide_on(bad: Any) -> None:
    respx.get(f"{RUNS}/{RUN_ID}").mock(return_value=httpx.Response(200, json=bad))
    with pytest.raises(mc.MandalaError):
        client().agent_runs.get(RUN_ID)


@respx.mock
def test_list_sends_every_parameter_and_reads_the_page() -> None:
    older = {**COMPLETED, "id": OLDER_RUN, "steps_taken": []}
    route = respx.get(RUNS).mock(
        return_value=httpx.Response(
            200, json={"runs": [{**COMPLETED, "steps_taken": []}, older], "has_more": True}
        )
    )
    page = client().agent_runs.list(computer_id="vm-1", status="completed", limit=2, before=RUN_ID)
    assert dict(route.calls.last.request.url.params) == {
        "computer_id": "vm-1",
        "status": "completed",
        "limit": "2",
        "before": RUN_ID,
    }
    assert [r.id for r in page.runs] == [RUN_ID, OLDER_RUN]
    assert page.has_more and page.next_before == OLDER_RUN


@respx.mock
def test_list_sends_nothing_it_was_not_given() -> None:
    route = respx.get(RUNS).mock(
        return_value=httpx.Response(200, json={"runs": [], "has_more": False})
    )
    page = client().agent_runs.list()
    assert route.calls.last.request.url.query == b""
    assert page.runs == [] and page.next_before is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "done"},
        {"status": "Running"},
        {"limit": 0},
        {"limit": 101},
        {"computer_id": ""},
        {"before": ""},
    ],
)
def test_list_refuses_what_the_platform_would_400_before_sending(kwargs: dict[str, Any]) -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            client().agent_runs.list(**kwargs)
        assert not mock.calls


@pytest.mark.parametrize(
    "body",
    [
        {"runs": [RUNNING]},
        {"runs": [], "has_more": True},
        {"runs": [RUNNING], "has_more": 1},
        {"runs": None, "has_more": False},
        {"runs": [{**RUNNING, "status": None}], "has_more": False},
    ],
)
@respx.mock
def test_list_refuses_a_page_that_could_not_be_walked(body: Any) -> None:
    respx.get(RUNS).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError):
        client().agent_runs.list()


@respx.mock
def test_cancel_answers_the_run_and_is_sent_once() -> None:
    route = respx.post(f"{RUNS}/{RUN_ID}/cancel").mock(
        return_value=httpx.Response(200, json=CANCELLED)
    )
    run = client().agent_runs.cancel(RUN_ID)
    assert run.status == "cancelled" and run.ended and not run.finished
    assert route.call_count == 1 and route.calls.last.request.content == b""


@respx.mock
def test_a_run_this_key_cannot_see_is_not_found() -> None:
    respx.get(f"{RUNS}/{RUN_ID}").mock(
        return_value=httpx.Response(404, json={"error": "run not found"})
    )
    respx.post(f"{RUNS}/{RUN_ID}/cancel").mock(
        return_value=httpx.Response(404, json={"error": "run not found"})
    )
    with pytest.raises(mc.NotFoundError):
        client().agent_runs.get(RUN_ID)
    with pytest.raises(mc.NotFoundError):
        client().agent_runs.cancel(RUN_ID)


# --- wait -----------------------------------------------------------------------


@respx.mock
def test_wait_polls_a_running_run_to_its_end() -> None:
    route = respx.get(f"{RUNS}/{RUN_ID}").mock(
        side_effect=sequence(RUNNING, STEPPING, STEPPING, COMPLETED)
    )
    run = client().agent_runs.wait(RUN_ID, timeout=5, poll=0.001)
    assert run.finished and route.call_count == 4


@pytest.mark.parametrize("ending", [FAILED, CANCELLED, INTERRUPTED])
@respx.mock
def test_wait_answers_every_ending_rather_than_raising(ending: dict[str, Any]) -> None:
    """The run is the account of what was done to the desktop, however it
    ended; raising would put it somewhere a caller has to dig for."""
    respx.get(f"{RUNS}/{RUN_ID}").mock(side_effect=sequence(STEPPING, ending))
    run = client().agent_runs.wait(RUN_ID, timeout=5, poll=0.001)
    assert run.status == ending["status"] and run.ended and not run.finished


@respx.mock
def test_wait_takes_the_run_itself() -> None:
    respx.post(START).mock(return_value=httpx.Response(202, json=RUNNING))
    route = respx.get(f"{RUNS}/{RUN_ID}").mock(return_value=httpx.Response(200, json=COMPLETED))
    c = client()
    started = c.agent_runs.start("vm-1", "rename the screenshots", model_key=KEY)
    assert c.agent_runs.wait(started, poll=0.001).finished
    assert route.call_count == 1


@respx.mock
def test_wait_ends_on_a_status_it_does_not_know_once_the_run_has_ended() -> None:
    later = {**COMPLETED, "status": "expired", "result": None}
    respx.get(f"{RUNS}/{RUN_ID}").mock(side_effect=sequence(RUNNING, later))
    run = client().agent_runs.wait(RUN_ID, timeout=5, poll=0.001)
    assert run.status == "expired" and run.ended and not run.finished


@respx.mock
def test_wait_polls_through_a_status_it_does_not_know_while_the_run_goes_on() -> None:
    queued = {**RUNNING, "status": "queued"}
    route = respx.get(f"{RUNS}/{RUN_ID}").mock(side_effect=sequence(queued, queued, COMPLETED))
    assert client().agent_runs.wait(RUN_ID, timeout=5, poll=0.001).finished
    assert route.call_count == 3


@respx.mock
def test_running_with_an_end_time_is_still_running() -> None:
    """The status decides when it is one this version knows; ``ended_at`` is
    only read for one it does not."""
    contradictory = {**RUNNING, "ended_at": "2026-10-18T12:00:30.000Z"}
    assert not mc.AgentRun.from_api(contradictory).ended


@respx.mock
def test_wait_rides_out_a_transient_failure() -> None:
    respx.get(f"{RUNS}/{RUN_ID}").mock(
        side_effect=[
            httpx.Response(503, json={"error": "down"}),
            httpx.Response(200, json=COMPLETED),
        ]
    )
    assert client().agent_runs.wait(RUN_ID, poll=0.001).finished


@respx.mock
def test_wait_stops_at_once_on_a_run_it_cannot_see() -> None:
    route = respx.get(f"{RUNS}/{RUN_ID}").mock(
        return_value=httpx.Response(404, json={"error": "run not found"})
    )
    with pytest.raises(mc.NotFoundError):
        client().agent_runs.wait(RUN_ID, poll=0.001)
    assert route.call_count == 1


@respx.mock
def test_wait_times_out_on_a_run_that_keeps_going_and_says_where_it_got_to() -> None:
    respx.get(f"{RUNS}/{RUN_ID}").mock(return_value=httpx.Response(200, json=STEPPING))
    with pytest.raises(mc.TimeoutError, match=r"still running .* at step 1.*only this wait has"):
        client().agent_runs.wait(RUN_ID, timeout=0.05, poll=0.01)


def test_wait_refuses_what_it_cannot_wait_on_before_sending() -> None:
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            client().agent_runs.wait("")
        with pytest.raises(ValueError):
            client().agent_runs.wait(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            client().agent_runs.wait(RUN_ID, timeout=float("nan"))
        with pytest.raises(ValueError):
            client().agent_runs.wait(RUN_ID, poll=-1)
        assert not mock.calls


# --- the async half -------------------------------------------------------------


async def test_async_start_get_list_cancel_and_wait() -> None:
    with respx.mock:
        start = respx.post(START).mock(return_value=httpx.Response(202, json=RUNNING))
        respx.get(RUNS).mock(
            return_value=httpx.Response(200, json={"runs": [RUNNING], "has_more": False})
        )
        read = respx.get(f"{RUNS}/{RUN_ID}").mock(
            side_effect=sequence(RUNNING, STEPPING, COMPLETED)
        )
        respx.post(f"{RUNS}/{RUN_ID}/cancel").mock(return_value=httpx.Response(200, json=COMPLETED))
        async with aclient() as c:
            run = await c.agent_runs.start("vm-1", "rename the screenshots", model_key=KEY)
            assert run.status == "running"
            assert start.calls.last.request.headers[MODEL_KEY_HEADER] == KEY
            handle = mc.AsyncComputer(c._t, COMPUTER)
            again = await handle.start_agent_run("rename", thread_id=THREAD_ID)
            assert again.id == RUN_ID and sent(start)["thread_id"] == THREAD_ID
            page = await c.agent_runs.list(status="running")
            assert page.runs[0].id == RUN_ID and page.next_before is None
            assert (await c.agent_runs.get(RUN_ID)).status == "running"
            done = await c.agent_runs.wait(run, poll=0.001)
            assert done.finished and read.call_count == 3
            assert (await c.agent_runs.cancel(RUN_ID)).status == "completed"


async def test_async_wait_times_out_and_refuses_store() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{RUNS}/{RUN_ID}").mock(return_value=httpx.Response(200, json=STEPPING))
        async with aclient() as c:
            with pytest.raises(mc.TimeoutError, match="only this wait has"):
                await c.agent_runs.wait(RUN_ID, timeout=0.05, poll=0.01)
            with pytest.raises(TypeError):
                await c.agent_runs.start("vm-1", "rename", store=False)  # type: ignore[call-arg]
