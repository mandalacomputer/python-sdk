"""A computer's end time: set at create, read back, moved and cleared."""

from __future__ import annotations

import json
import math
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


# --- a replayed relative answer is held to the upper bound alone -------------


def replay_without_date(ends_at: str) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(
            200,
            json={**COMPUTER, "ends_at": ends_at},
            headers={"Idempotent-Replayed": "true"},
        )
    )


@respx.mock
def test_a_replay_without_a_date_from_long_ago_is_accepted() -> None:
    """Its first call's timing is unknown, so an earlier time is not refused."""
    replay_without_date(hours_from(3600, 2))
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    c.set_end_time(ends_in_hours=2, idempotency_key="move-1")
    assert c.ends_at is not None


@respx.mock
def test_a_replay_without_a_date_past_the_upper_bound_is_refused() -> None:
    replay_without_date(hours_from(-20 * 60, 2))
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=2, idempotency_key="move-1")


@respx.mock
async def test_an_async_replay_past_the_upper_bound_is_refused() -> None:
    replayed_patch(hours_from(-600, 2), replayed=True)
    c = await mc.AsyncClient("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        await c.set_end_time(ends_in_hours=2, idempotency_key="move-1")


@respx.mock
def test_a_replayed_relative_create_is_held_to_the_upper_bound_alone() -> None:
    answer = {**COMPUTER, "ends_at": hours_from(1800, 2)}
    route = respx.post(f"{BASE}/computers").mock(
        httpx.Response(
            200, json=answer, headers={"Idempotent-Replayed": "true", "Date": server_date()}
        )
    )
    client = mc.Client("com_test", base_url=BASE)
    assert client.computers.create(template="base", ends_in_hours=2).ends_at is not None
    route.mock(
        httpx.Response(
            200,
            json={**COMPUTER, "ends_at": hours_from(-600, 2)},
            headers={"Idempotent-Replayed": "true", "Date": server_date()},
        )
    )
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        client.computers.create(template="base", ends_in_hours=2)


@pytest.mark.parametrize("off", [timedelta(seconds=5), timedelta(seconds=-5)])
@respx.mock
def test_a_minute_from_now_answered_five_seconds_out_is_refused(off: timedelta) -> None:
    """With a Date, the window is the server's second-precision stamp, give or
    take two seconds: five seconds out is not the time asked for."""
    honest_patch(off)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=1 / 60)


@pytest.mark.parametrize("off", [timedelta(seconds=5), timedelta(seconds=-5)])
@respx.mock
async def test_an_async_minute_from_now_answered_five_seconds_out_is_refused(
    off: timedelta,
) -> None:
    honest_patch(off)
    c = await mc.AsyncClient("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        await c.set_end_time(ends_in_hours=1 / 60)


@respx.mock
def test_a_minute_from_now_answered_on_time_is_accepted() -> None:
    honest_patch()
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    c.set_end_time(ends_in_hours=1 / 60)
    assert c.ends_at is not None


@respx.mock
def test_a_replay_five_seconds_past_the_upper_bound_is_refused() -> None:
    replayed_patch(hours_from(-5, 1 / 60), replayed=True)
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="not the time asked for"):
        c.set_end_time(ends_in_hours=1 / 60, idempotency_key="move-1")


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"ends_at": "2026-11-31T12:00:00Z"}, "not a real date"),
        ({"ends_in_hours": 8785}, "at most 8784"),
        ({"ends_in_hours": 1e300}, "at most 8784"),
    ],
)
async def test_an_end_time_that_cannot_be_a_time_is_refused_before_sending(
    kwargs: dict[str, Any], match: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{BASE}/computers")
        with pytest.raises(ValueError, match=match):
            mc.Client("com_test", base_url=BASE).computers.create(template="base", **kwargs)
        with pytest.raises(ValueError, match=match):
            await mc.AsyncClient("com_test", base_url=BASE).computers.create(
                template="base", **kwargs
            )
        assert not route.called
