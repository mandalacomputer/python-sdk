"""A computer's end time: set at create, read back, moved and cleared."""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _api

BASE = "https://api.test/api/v1"
COMPUTER: dict[str, Any] = {"id": "vm-1", "name": "demo", "status": "running", "os": "linux"}
ENDS = "2026-11-01T12:00:00Z"
ENDING: dict[str, Any] = {**COMPUTER, "ends_at": ENDS}
#: An end time already behind us, for a create resent after it fired.
PAST = "2026-01-01T00:00:00Z"
STOPPED: dict[str, Any] = {**COMPUTER, "status": "stopped", "running_ram_mb": 0}


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


def echo(request: httpx.Request) -> httpx.Response:
    """A computer answering as a server that applied the end time does: the
    time asked for, hours turned into a time, and none after a clear."""
    sent = json.loads(request.content or b"{}")
    answer = dict(COMPUTER)
    if isinstance(sent.get("ends_at"), str):
        answer["ends_at"] = sent["ends_at"]
    elif sent.get("ends_in_hours") is not None:
        ends = datetime.now(timezone.utc) + timedelta(hours=sent["ends_in_hours"])
        answer["ends_at"] = ends.isoformat()
    return httpx.Response(201 if request.method == "POST" else 200, json=answer)


@respx.mock
def test_create_sends_a_time_or_hours_and_nothing_when_neither() -> None:
    route = respx.post(f"{BASE}/computers").mock(side_effect=echo)
    client = mc.Client("com_test", base_url=BASE)
    assert client.computers.create(template="base", ends_at=ENDS).ends_at == ENDS
    assert sent(route)["ends_at"] == ENDS
    assert "ends_in_hours" not in sent(route)

    assert client.computers.create(template="base", ends_in_hours=1.5).ends_at is not None
    assert sent(route)["ends_in_hours"] == 1.5
    assert "ends_at" not in sent(route)

    when = datetime(2026, 11, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    client.computers.create(template="base", ends_at=when)
    assert sent(route)["ends_at"] == "2026-11-01T12:00:00+02:00"

    client.computers.create(template="base")
    assert not {"ends_at", "ends_in_hours"} & set(sent(route))


@respx.mock
def test_the_end_time_is_set_moved_and_cleared_with_null() -> None:
    """Neither given clears it: null is SENT, since dropped it would mean "change nothing"."""
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    route = respx.patch(f"{BASE}/computers/vm-1").mock(side_effect=echo)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    assert c.ends_at is None
    c.set_end_time(ENDS)
    assert sent(route) == {"ends_at": ENDS}
    assert c.ends_at == ENDS

    c.set_end_time(ends_in_hours=2)
    assert sent(route) == {"ends_in_hours": 2}

    c.set_end_time(None)
    assert sent(route) == {"ends_at": None}
    assert c.ends_at is None


@respx.mock
async def test_the_async_half_sends_the_same() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    create = respx.post(f"{BASE}/computers").mock(side_effect=echo)
    patch = respx.patch(f"{BASE}/computers/vm-1").mock(side_effect=echo)
    client = mc.AsyncClient("com_test", base_url=BASE)
    await client.computers.create(template="base", ends_in_hours=3)
    assert sent(create)["ends_in_hours"] == 3
    c = await client.computers.get("vm-1")
    await c.set_end_time(ENDS)
    assert sent(patch) == {"ends_at": ENDS}
    assert c.ends_at == ENDS
    await c.set_end_time()
    assert sent(patch) == {"ends_at": None}


# --- the answer must carry the time asked for, not merely a time ------------

#: Answers that carry an end time, but not the one asked for: the computer's
#: previous one left unchanged, a garbled one, one off by more than the second
#: the platform's precision allows, and one that is not a time at all.
WRONG_TIMES = [
    pytest.param({"ends_at": ENDS}, "2026-10-31T09:00:00Z", "not the time asked for", id="stale"),
    pytest.param({"ends_at": ENDS}, "2026-11-01T12:00:02Z", "not the time asked for", id="off-2s"),
    pytest.param({"ends_at": ENDS}, "soon", "unreadable", id="malformed"),
    pytest.param({"ends_at": ENDS}, 1767225600, "unreadable", id="number"),
    pytest.param({"ends_in_hours": 2}, ENDS, "not the time asked for", id="hours-stale"),
    pytest.param({"ends_in_hours": 2}, "", "unreadable", id="hours-empty"),
]


@pytest.mark.parametrize(("kwargs", "answered", "problem"), WRONG_TIMES)
@respx.mock
def test_a_wrong_end_time_in_the_answer_raises(
    kwargs: dict[str, Any], answered: object, problem: str
) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.post(f"{BASE}/computers").mock(
        httpx.Response(201, json={**COMPUTER, "ends_at": answered})
    )
    respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "ends_at": answered})
    )
    client = mc.Client("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match=rf"^vm-1: the end time was not applied.*{problem}"):
        client.computers.create(template="base", **kwargs)
    c = client.computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match=rf"^vm-1: the end time was not applied.*{problem}"):
        c.set_end_time(**kwargs)


@pytest.mark.parametrize(("kwargs", "answered", "problem"), WRONG_TIMES)
@respx.mock
async def test_the_async_half_refuses_a_wrong_end_time_alike(
    kwargs: dict[str, Any], answered: object, problem: str
) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.post(f"{BASE}/computers").mock(
        httpx.Response(201, json={**COMPUTER, "ends_at": answered})
    )
    respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "ends_at": answered})
    )
    client = mc.AsyncClient("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match=rf"^vm-1: the end time was not applied.*{problem}"):
        await client.computers.launch(template="base", **kwargs)
    c = await client.computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match=rf"^vm-1: the end time was not applied.*{problem}"):
        await c.set_end_time(**kwargs)


def test_a_time_stored_to_the_second_is_the_time_asked_for() -> None:
    """The platform keeps whole seconds; a request with a fraction comes back without it."""
    now = datetime.now(timezone.utc)
    ex = _api.Exchange(began=now, ended=now, seconds=0.0)
    body = {"ends_at": "2026-11-01T12:00:00.750Z"}
    for answered in (
        "2026-11-01T12:00:00Z",
        "2026-11-01T12:00:00.000Z",
        "2026-11-01T13:00:00+01:00",
    ):
        assert _api.end_time_refusal("vm-1", body, answered, ex) is None


def test_a_time_and_hours_together_are_refused_before_sending() -> None:
    with pytest.raises(ValueError, match="not both"):
        _api.end_time_body(ENDS, 2)
    with pytest.raises(ValueError, match="not both"):
        _api.create_body(
            name=None,
            template="base",
            cpu=None,
            ram_mb=None,
            disk_gb=None,
            start=True,
            ends_at=ENDS,
            ends_in_hours=2,
        )


def test_a_naive_datetime_is_refused_rather_than_given_a_guessed_zone() -> None:
    with pytest.raises(ValueError, match="aware datetime"):
        _api.end_time_body(datetime(2026, 11, 1, 12), None)


@pytest.mark.parametrize("stamp", ["2026-11-01", "2026-11-01T12:00:00", "tomorrow", 1700000000])
def test_a_time_without_a_zone_or_not_a_time_is_refused(stamp: object) -> None:
    with pytest.raises(ValueError, match="RFC 3339"):
        _api.end_time_body(stamp, None)


@pytest.mark.parametrize("hours", [0, -1, math.nan, math.inf, True, "2"])
def test_hours_must_be_a_positive_finite_number(hours: object) -> None:
    with pytest.raises(ValueError, match="ends_in_hours"):
        _api.end_time_body(None, hours)


def test_an_unreadable_end_time_is_an_error_not_none() -> None:
    computer = mc.Computer(None, {**COMPUTER, "ends_at": 17})  # type: ignore[arg-type]
    with pytest.raises(mc.MandalaError, match="ends_at"):
        _ = computer.ends_at


# --- an answer that does not confirm the end time is a failure ---------------
#
# A server that predates end times ignores the fields and answers as if they
# were never sent. The computer is left alone — never stopped or deleted — and
# the caller is told, by id.


@pytest.mark.parametrize("answer", [COMPUTER, {**COMPUTER, "ends_at": None}])
@pytest.mark.parametrize("kwargs", [{"ends_at": ENDS}, {"ends_in_hours": 2}])
@respx.mock
def test_a_create_answered_without_the_end_time_raises_and_leaves_it(
    answer: dict[str, Any], kwargs: dict[str, Any]
) -> None:
    create = respx.post(f"{BASE}/computers").mock(httpx.Response(201, json=answer))
    client = mc.Client("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not applied"):
        client.computers.create(template="base", **kwargs)
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not applied.*launch"):
        client.computers.launch(template="base", **kwargs)
    # The creates, and nothing after them: no stop, no delete, no start.
    assert [c.request.method for c in respx.calls] == ["POST", "POST"]
    assert create.call_count == 2


@pytest.mark.parametrize("answer", [COMPUTER, {**COMPUTER, "ends_at": None}])
@respx.mock
async def test_the_async_create_refuses_the_same(answer: dict[str, Any]) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(201, json=answer))
    client = mc.AsyncClient("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not applied"):
        await client.computers.create(template="base", ends_in_hours=2)
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not applied"):
        await client.computers.launch(template="base", ends_at=ENDS)
    assert [c.request.method for c in respx.calls] == ["POST", "POST"]


@pytest.mark.parametrize("answer", [COMPUTER, {**COMPUTER, "ends_at": None}])
@respx.mock
def test_setting_an_end_time_the_answer_lacks_raises(answer: dict[str, Any]) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.patch(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=answer))
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not applied"):
        c.set_end_time(ends_in_hours=2)
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not applied"):
        c.set_end_time(ENDS)


@respx.mock
def test_clearing_an_end_time_the_answer_still_has_raises() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=ENDING))
    respx.patch(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=ENDING))
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not cleared"):
        c.set_end_time(None)


@respx.mock
async def test_the_async_set_and_clear_refuse_the_same() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    patch = respx.patch(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    c = await mc.AsyncClient("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not applied"):
        await c.set_end_time(ends_in_hours=2)
    patch.mock(httpx.Response(200, json=ENDING))
    with pytest.raises(mc.MandalaError, match=r"^vm-1: the end time was not cleared"):
        await c.set_end_time()


# --- a launch resent after its end time does not start the computer again ----
#
# The resend is answered from the idempotency record: the FIRST call's answer,
# end time and all. The host has since stopped the computer at that time and
# cleared it, so a fresh read finds it stopped with none — and starting it would
# leave it running with no end time at all.


def replayed_launch(ends_at: str) -> tuple[respx.Route, respx.Route]:
    respx.post(f"{BASE}/computers").mock(
        httpx.Response(
            200,
            json={**COMPUTER, "ends_at": ends_at},
            headers={"Idempotent-Replayed": "true"},
        )
    )
    read = respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=STOPPED))
    start = respx.post(f"{BASE}/computers/vm-1/start").mock(httpx.Response(200, json={"ok": True}))
    return read, start


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"ends_at": PAST}, "its end time has passed"),
        ({"ends_in_hours": 1}, "its end time has most likely passed"),
    ],
)
@respx.mock
def test_a_replayed_launch_after_its_end_time_is_not_started_again(
    kwargs: dict[str, Any], why: str
) -> None:
    read, start = replayed_launch(kwargs.get("ends_at", ENDS))
    client = mc.Client("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match=rf"vm-1.*{why}.*did not start it") as caught:
        client.computers.launch(template="base", idempotency_key="order-1", **kwargs)
    assert read.called
    assert not start.called
    assert "launch of vm-1 failed" in str(caught.value)


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"ends_at": PAST}, "its end time has passed"),
        ({"ends_in_hours": 1}, "its end time has most likely passed"),
    ],
)
@respx.mock
async def test_an_async_replayed_launch_after_its_end_time_is_not_started_again(
    kwargs: dict[str, Any], why: str
) -> None:
    read, start = replayed_launch(kwargs.get("ends_at", ENDS))
    client = mc.AsyncClient("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match=rf"vm-1.*{why}.*did not start it"):
        await client.computers.launch(template="base", idempotency_key="order-1", **kwargs)
    assert read.called
    assert not start.called


# --- a replayed relative update ----------------------------------------------
#
# Resent under the same key, a set_end_time(ends_in_hours=...) is answered with
# the FIRST call's stored answer: hours from then, a little earlier than hours
# from now. It is held to the upper bound alone.


def server_date() -> str:
    """The ``Date`` header a server answers with: its clock, to the second."""
    return format_datetime(datetime.now(timezone.utc), usegmt=True)


def replayed_patch(ends_at: str, replayed: bool) -> None:
    headers = {"Date": server_date()}
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "ends_at": ends_at}, headers=headers)
    )


def hours_from(seconds_ago: float, hours: float) -> str:
    then = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return (then + timedelta(hours=hours)).isoformat()


@respx.mock
def test_a_replayed_relative_update_from_minutes_ago_is_accepted() -> None:
    replayed_patch(hours_from(300, 2), replayed=True)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    c.set_end_time(ends_in_hours=2, idempotency_key="move-1")
    assert c.ends_at is not None


@respx.mock
def test_the_same_answer_not_marked_a_replay_is_refused() -> None:
    replayed_patch(hours_from(300, 2), replayed=False)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=2)


@respx.mock
def test_a_replayed_relative_update_past_the_upper_bound_is_refused() -> None:
    replayed_patch(hours_from(-600, 2), replayed=True)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=2, idempotency_key="move-1")


@respx.mock
async def test_an_async_replayed_relative_update_from_minutes_ago_is_accepted() -> None:
    replayed_patch(hours_from(300, 2), replayed=True)
    c = await mc.AsyncClient("com_test", base_url=BASE).computers.get("vm-1")
    await c.set_end_time(ends_in_hours=2, idempotency_key="move-1")
    assert c.ends_at is not None


@respx.mock
async def test_an_async_unmarked_or_too_late_answer_is_refused() -> None:
    replayed_patch(hours_from(300, 2), replayed=False)
    c = await mc.AsyncClient("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        await c.set_end_time(ends_in_hours=2)


# --- a client clock set wrong ------------------------------------------------
#
# Hours are turned into a time by the platform's clock, which its answer's
# Date header carries. The client's clock is not what the answer is judged by,
# so one a few seconds ahead or minutes behind changes nothing — and a value
# that is wrong by the server's own clock is still refused.

SKEWS = [
    pytest.param(timedelta(seconds=5), id="client-5s-ahead"),
    pytest.param(timedelta(minutes=-2), id="client-2min-behind"),
]


def skew_client_clock(monkeypatch: pytest.MonkeyPatch, by: timedelta) -> None:
    monkeypatch.setattr(_api, "utcnow", lambda: datetime.now(timezone.utc) + by)


def honest_patch(off: timedelta = timedelta(0), *, date: bool = True) -> None:
    """A server that sets hours from ITS now (plus ``off``, to be wrong)."""

    def answer(request: httpx.Request) -> httpx.Response:
        hours = json.loads(request.content)["ends_in_hours"]
        ends = datetime.now(timezone.utc) + timedelta(hours=hours) + off
        return httpx.Response(
            200,
            json={**COMPUTER, "ends_at": ends.isoformat()},
            headers={"Date": server_date()} if date else {},
        )

    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.patch(f"{BASE}/computers/vm-1").mock(side_effect=answer)


@pytest.mark.parametrize("skew", SKEWS)
@respx.mock
def test_a_skewed_client_clock_accepts_the_servers_hours(
    monkeypatch: pytest.MonkeyPatch, skew: timedelta
) -> None:
    skew_client_clock(monkeypatch, skew)
    honest_patch()
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    c.set_end_time(ends_in_hours=2)
    assert c.ends_at is not None


@pytest.mark.parametrize("skew", SKEWS)
@respx.mock
async def test_a_skewed_client_clock_accepts_the_servers_hours_async(
    monkeypatch: pytest.MonkeyPatch, skew: timedelta
) -> None:
    skew_client_clock(monkeypatch, skew)
    honest_patch()
    c = await mc.AsyncClient("com_test", base_url=BASE).computers.get("vm-1")
    await c.set_end_time(ends_in_hours=2)
    assert c.ends_at is not None


@pytest.mark.parametrize("skew", SKEWS)
@pytest.mark.parametrize("off", [timedelta(minutes=5), timedelta(minutes=-5)])
@respx.mock
def test_a_wrong_value_is_refused_by_the_servers_clock(
    monkeypatch: pytest.MonkeyPatch, skew: timedelta, off: timedelta
) -> None:
    skew_client_clock(monkeypatch, skew)
    honest_patch(off)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=2)


@pytest.mark.parametrize("skew", SKEWS)
@respx.mock
def test_without_a_date_the_client_window_is_widened(
    monkeypatch: pytest.MonkeyPatch, skew: timedelta
) -> None:
    skew_client_clock(monkeypatch, skew)
    honest_patch(date=False)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    c.set_end_time(ends_in_hours=2)
    honest_patch(timedelta(minutes=30), date=False)
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=2)


@pytest.mark.parametrize("skew", SKEWS)
@respx.mock
def test_an_absolute_end_time_ignores_every_clock(
    monkeypatch: pytest.MonkeyPatch, skew: timedelta
) -> None:
    skew_client_clock(monkeypatch, skew)
    replayed_patch(ENDS, replayed=False)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    c.set_end_time(ENDS)
    replayed_patch("2026-11-01T12:00:05Z", replayed=False)
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ENDS)


# --- launch never starts an end-timed computer it finds stopped -------------


@respx.mock
def test_launch_does_not_start_an_end_timed_computer_it_finds_stopped() -> None:
    """Even one whose end time is still there: launch does not start it."""
    respx.post(f"{BASE}/computers").mock(
        httpx.Response(200, json=ENDING, headers={"Idempotent-Replayed": "true"})
    )
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={**STOPPED, "ends_at": ENDS}))
    start = respx.post(f"{BASE}/computers/vm-1/start")
    client = mc.Client("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match=r"vm-1 is stopped.*Call start\(\)"):
        client.computers.launch(template="base", ends_at=ENDS, idempotency_key="order-1")
    assert not start.called


@respx.mock
async def test_async_launch_does_not_start_an_end_timed_computer_created_stopped() -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(201, json={**STOPPED, "ends_at": ENDS}))
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={**STOPPED, "ends_at": ENDS}))
    start = respx.post(f"{BASE}/computers/vm-1/start")
    client = mc.AsyncClient("com_test", base_url=BASE)
    with pytest.raises(mc.MandalaError, match=r"vm-1 is stopped.*Call start\(\)"):
        await client.computers.launch(template="base", ends_at=ENDS, start=False)
    assert not start.called


# --- a replay judged by the first request's own timing -----------------------


@pytest.fixture(autouse=True)
def forget_timings() -> None:
    _api._TIMINGS.clear()


def remembering_server(route: str, method: str) -> list[str]:
    """Answers hours from ITS now the first time a key is seen, and replays
    that answer (marked) every time after, as the platform's store does."""
    first: dict[str, dict[str, Any]] = {}
    answers: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        key = request.headers["Idempotency-Key"]
        headers = {"Date": server_date()}
        if key in first:
            headers["Idempotent-Replayed"] = "true"
            return httpx.Response(200, json=first[key], headers=headers)
        hours = json.loads(request.content)["ends_in_hours"]
        ends = datetime.now(timezone.utc) + timedelta(hours=hours)
        first[key] = {**COMPUTER, "ends_at": ends.isoformat()}
        answers.append(first[key]["ends_at"])
        return httpx.Response(200, json=first[key], headers=headers)

    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.route(method=method, url=f"{BASE}/{route}").mock(side_effect=answer)
    return answers


def later(monkeypatch: pytest.MonkeyPatch, by: timedelta) -> None:
    """The client's wall clock moved on — as if the resend came later."""
    monkeypatch.setattr(_api, "utcnow", lambda: datetime.now(timezone.utc) + by)


@respx.mock
def test_a_replayed_set_end_time_is_judged_by_the_first_requests_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remembering_server("computers/vm-1", "PATCH")
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    c.set_end_time(ends_in_hours=2, idempotency_key="move-1")
    # Five minutes on, by the server's Date too, the replay still matches the
    # first request, so it is accepted on the first request's window.
    later(monkeypatch, timedelta(minutes=5))
    c.set_end_time(ends_in_hours=2, idempotency_key="move-1")
    assert c.ends_at is not None


@respx.mock
def test_a_replay_that_does_not_match_the_first_requests_timing_is_refused() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    patch = respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(
            200, json={**COMPUTER, "ends_at": hours_from(0, 2)}, headers={"Date": server_date()}
        )
    )
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    c.set_end_time(ends_in_hours=2, idempotency_key="move-2")
    # A replay answering five minutes EARLIER than the first call could have
    # been set: by the first request's timing that is wrong, so it is refused
    # even though hours-from-now's upper bound alone would let it through.
    patch.mock(
        httpx.Response(
            200,
            json={**COMPUTER, "ends_at": hours_from(300, 2)},
            headers={"Date": server_date(), "Idempotent-Replayed": "true"},
        )
    )
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=2, idempotency_key="move-2")


@respx.mock
async def test_an_async_replayed_set_end_time_is_judged_by_the_first_requests_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remembering_server("computers/vm-1", "PATCH")
    c = await mc.AsyncClient("com_test", base_url=BASE).computers.get("vm-1")
    await c.set_end_time(ends_in_hours=2, idempotency_key="move-1")
    later(monkeypatch, timedelta(minutes=5))
    await c.set_end_time(ends_in_hours=2, idempotency_key="move-1")
    assert c.ends_at is not None


@respx.mock
def test_a_replayed_create_is_judged_by_the_first_requests_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remembering_server("computers", "POST")
    client = mc.Client("com_test", base_url=BASE)
    client.computers.create(template="base", ends_in_hours=2, idempotency_key="order-9")
    later(monkeypatch, timedelta(minutes=5))
    again = client.computers.create(template="base", ends_in_hours=2, idempotency_key="order-9")
    assert again.ends_at is not None


@respx.mock
async def test_an_async_replayed_create_is_judged_by_the_first_requests_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remembering_server("computers", "POST")
    client = mc.AsyncClient("com_test", base_url=BASE)
    await client.computers.create(template="base", ends_in_hours=2, idempotency_key="order-9")
    later(monkeypatch, timedelta(minutes=5))
    again = await client.computers.create(
        template="base", ends_in_hours=2, idempotency_key="order-9"
    )
    assert again.ends_at is not None


def test_the_timing_memory_is_bounded_and_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(timezone.utc)
    ex = _api.Exchange(began=now, ended=now, seconds=0.0)
    for i in range(_api._TIMINGS_MAX + 10):
        _api._timing(("r", str(i)), ex)
    assert len(_api._TIMINGS) == _api._TIMINGS_MAX
    assert _api._timing(("r", "0")) is None
    real = time.monotonic
    monkeypatch.setattr(_api.time, "monotonic", lambda: real() + _api._TIMINGS_TTL + 1)
    assert _api._timing(("r", str(_api._TIMINGS_MAX + 9))) is None


# --- the first exchange's window, however late the replay --------------------


def late_server(clock: dict[str, timedelta], off: timedelta) -> None:
    """Answers hours from its now plus ``off`` (wrong when ``off`` is large)
    the first time a key is seen, and replays that answer after. Its Date, and
    the client's clock, both read ``clock["now"]`` ahead of real time."""
    first: dict[str, dict[str, Any]] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        now = datetime.now(timezone.utc) + clock["now"]
        headers = {"Date": format_datetime(now, usegmt=True)}
        key = request.headers["Idempotency-Key"]
        if key in first:
            headers["Idempotent-Replayed"] = "true"
        else:
            hours = json.loads(request.content)["ends_in_hours"]
            first[key] = {**COMPUTER, "ends_at": (now + timedelta(hours=hours) + off).isoformat()}
        return httpx.Response(200, json=first[key], headers=headers)

    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.patch(f"{BASE}/computers/vm-1").mock(side_effect=answer)


@respx.mock
def test_a_rejected_late_deadline_replayed_half_an_hour_later_stays_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": timedelta(0)}
    monkeypatch.setattr(_api, "utcnow", lambda: datetime.now(timezone.utc) + clock["now"])
    late_server(clock, timedelta(minutes=10))
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=2, idempotency_key="late-1")
    # Thirty minutes on, by every clock, the same late answer is replayed. Hours
    # from THEN would cover it; the first exchange's window does not.
    clock["now"] = timedelta(minutes=30)
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=2, idempotency_key="late-1")


@respx.mock
async def test_an_async_rejected_late_deadline_replayed_later_stays_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": timedelta(0)}
    monkeypatch.setattr(_api, "utcnow", lambda: datetime.now(timezone.utc) + clock["now"])
    late_server(clock, timedelta(minutes=10))
    c = await mc.AsyncClient("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        await c.set_end_time(ends_in_hours=2, idempotency_key="late-1")
    clock["now"] = timedelta(minutes=30)
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        await c.set_end_time(ends_in_hours=2, idempotency_key="late-1")


def test_a_replay_of_a_send_whose_answer_was_lost_is_held_to_that_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first send went and its answer never came back: its replay is held
    to [first send + hours − 1 min, first send + hours + 1 min + 2 min] — not
    to hours from whenever the resend happened."""
    clock = {"now": timedelta(0)}
    monkeypatch.setattr(_api, "utcnow", lambda: datetime.now(timezone.utc) + clock["now"])
    key = ("PATCH computers/vm-1", "lost-1")
    body = {"ends_in_hours": 2}
    first = _api.exchange_start(key)[0]  # sent; the answer never arrives
    clock["now"] = timedelta(minutes=30)
    replay = httpx.Headers(
        {"Idempotent-Replayed": "true", "Date": format_datetime(_api.utcnow(), usegmt=True)}
    )
    # A resend does not move the first send's record.
    ex = _api.exchange_end(_api.exchange_start(key), replay, key)
    assert ex.original is not None and not ex.original.answered
    assert ex.original.began == first

    def judged(ends: datetime) -> mc.MandalaError | None:
        return _api.end_time_refusal("vm-1", body, ends.isoformat(), ex)

    assert judged(first + timedelta(hours=2, seconds=30)) is None
    assert judged(first + timedelta(hours=2, minutes=2)) is None
    assert judged(first + timedelta(hours=2, minutes=4)) is not None
    assert judged(first + timedelta(hours=2, minutes=-2)) is not None
    # Hours from the resend — what a growing bound would let through.
    assert judged(_api.utcnow() + timedelta(hours=2)) is not None


def test_a_key_whose_original_was_sent_elsewhere_stays_upper_bound_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The original went from another process: this one's first send is
    answered as a replay. Neither it nor any later retry is the original, so
    each is held to the upper bound alone — never to a retry's own timing."""
    key = ("PATCH computers/vm-1", "elsewhere-1")
    body = {"ends_in_hours": 2}
    elsewhere = _api.utcnow() - timedelta(minutes=20)  # when the original was set
    answered = (elsewhere + timedelta(hours=2)).isoformat()

    def retry() -> _api.Exchange:
        headers = httpx.Headers(
            {"Idempotent-Replayed": "true", "Date": format_datetime(_api.utcnow(), usegmt=True)}
        )
        return _api.exchange_end(_api.exchange_start(key), headers, key)

    first = retry()
    assert first.original is None
    assert _api.end_time_refusal("vm-1", body, answered, first) is None
    # A second identical retry, a minute on: still no original — were the
    # first retry's timing taken for one, twenty minutes early would be refused.
    real = _api.utcnow
    monkeypatch.setattr(_api, "utcnow", lambda: real() + timedelta(minutes=1))
    second = retry()
    assert second.original is None
    assert _api.end_time_refusal("vm-1", body, answered, second) is None
    assert _api._timing(key) is not None and _api._timing(key).unknown_origin  # type: ignore[union-attr]
