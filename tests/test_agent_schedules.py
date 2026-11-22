"""Agent schedules: ``client.agent_schedules`` — background runs started on a
timetable, made on a computer and read, changed, paused, resumed and deleted
by id, sync and async."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
SCHEDULE_ID = "sch_fedcba9876543210fedcba98"
RUN_ID = "run_89abcdef0123456789abcdef"
MAKE = f"{BASE}/computers/vm-1/agent/schedules"
ONE = f"{BASE}/agent/schedules/{SCHEDULE_ID}"
LIST = f"{BASE}/agent/schedules"

SCHEDULE: dict[str, Any] = {
    "id": SCHEDULE_ID,
    "computer_id": "vm-1",
    "thread_id": None,
    "name": "Weekday build check",
    "cron": "0 9 * * MON-FRI",
    "timezone": "America/New_York",
    "prompt": "Check the overnight build",
    "model": "claude-sonnet-5-5",
    "max_steps": 60,
    "max_cost_usd": 2,
    "paused": False,
    "paused_reason": None,
    "next_fire_at": "2026-11-24T14:00:00.000Z",
    "last_fire_at": "2026-11-23T14:00:00.000Z",
    "last_run_id": RUN_ID,
    "created_at": "2026-11-20T16:12:00.000Z",
    "updated_at": "2026-11-20T16:12:00.000Z",
    "recent_fires": [
        {
            "scheduled_for": "2026-11-23T14:00:00.000Z",
            "fired_at": "2026-11-23T14:00:04.000Z",
            "outcome": "started",
            "run_id": RUN_ID,
            "error": None,
        }
    ],
}


def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


@respx.mock
def test_create_sends_the_wire_names_and_no_model_key() -> None:
    route = respx.post(MAKE).mock(return_value=httpx.Response(201, json=SCHEDULE))
    made = client().agent_schedules.create(
        "vm-1",
        "0 9 * * MON-FRI",
        "Check the overnight build",
        timezone="America/New_York",
        max_steps=60,
        max_cost_usd=2,
        name="Weekday build check",
    )
    assert sent(route) == {
        "cron": "0 9 * * MON-FRI",
        "prompt": "Check the overnight build",
        "timezone": "America/New_York",
        "max_steps": 60,
        "max_cost_usd": 2,
        "name": "Weekday build check",
    }
    assert "x-model-key" not in route.calls.last.request.headers
    assert made.id == SCHEDULE_ID
    assert made.paused is False and made.paused_reason is None
    assert made.recent_fires[0].outcome == "started"
    assert made.recent_fires[0].run_id == RUN_ID


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_steps": 0},
        {"max_steps": True},
        {"provider": "openai"},
        {"thread_id": 5},
        {"timezone": ""},
    ],
)
@respx.mock
def test_create_refuses_bad_values_before_any_request(kwargs: dict[str, Any]) -> None:
    route = respx.post(MAKE).mock(return_value=httpx.Response(201, json=SCHEDULE))
    with pytest.raises(ValueError):
        client().agent_schedules.create("vm-1", "0 9 * * *", "x", **kwargs)
    assert not route.called


@respx.mock
def test_list_pages_by_the_last_id() -> None:
    route = respx.get(LIST).mock(
        return_value=httpx.Response(200, json={"schedules": [SCHEDULE], "has_more": True})
    )
    page = client().agent_schedules.list(computer_id="vm-1", limit=1)
    assert dict(route.calls.last.request.url.params) == {"computer_id": "vm-1", "limit": "1"}
    assert page.has_more and page.next_before == SCHEDULE_ID


@respx.mock
def test_update_sends_only_what_was_passed_and_none_clears() -> None:
    route = respx.patch(ONE).mock(return_value=httpx.Response(200, json=SCHEDULE))
    c = client()
    c.agent_schedules.update(SCHEDULE_ID, prompt="New job", thread_id=None)
    assert sent(route) == {"prompt": "New job", "thread_id": None}
    c.agent_schedules.pause(SCHEDULE_ID)
    assert sent(route) == {"paused": True}
    c.agent_schedules.resume(SCHEDULE_ID)
    assert sent(route) == {"paused": False}


@respx.mock
def test_get_keeps_an_unknown_reason_and_outcome() -> None:
    respx.get(ONE).mock(
        return_value=httpx.Response(
            200,
            json={
                **SCHEDULE,
                "paused": True,
                "paused_reason": "something_new",
                "recent_fires": [{**SCHEDULE["recent_fires"][0], "outcome": "deferred"}],
            },
        )
    )
    read = client().agent_schedules.get(SCHEDULE_ID)
    assert read.paused_reason == "something_new"
    assert read.recent_fires[0].outcome == "deferred"


@respx.mock
def test_an_answer_without_paused_is_refused() -> None:
    respx.get(ONE).mock(return_value=httpx.Response(200, json={**SCHEDULE, "paused": None}))
    with pytest.raises(mc.MandalaError):
        client().agent_schedules.get(SCHEDULE_ID)


@respx.mock
def test_a_schedule_this_key_cannot_see_is_not_found() -> None:
    respx.get(ONE).mock(return_value=httpx.Response(404, json={"error": "schedule not found"}))
    with pytest.raises(mc.NotFoundError):
        client().agent_schedules.get(SCHEDULE_ID)


@respx.mock
def test_delete() -> None:
    route = respx.delete(ONE).mock(return_value=httpx.Response(200, json={"ok": True}))
    client().agent_schedules.delete(SCHEDULE_ID)
    assert route.called


@respx.mock
@pytest.mark.asyncio
async def test_the_async_half_makes_the_same_calls() -> None:
    make = respx.post(MAKE).mock(return_value=httpx.Response(201, json=SCHEDULE))
    change = respx.patch(ONE).mock(return_value=httpx.Response(200, json=SCHEDULE))
    gone = respx.delete(ONE).mock(return_value=httpx.Response(200, json={"ok": True}))
    async with mc.AsyncClient("gck_test", base_url=BASE) as c:
        made = await c.agent_schedules.create("vm-1", "0 9 * * *", "x", timezone="UTC")
        await c.agent_schedules.pause(made.id)
        await c.agent_schedules.delete(made.id)
    assert sent(make) == {"cron": "0 9 * * *", "prompt": "x", "timezone": "UTC"}
    assert sent(change) == {"paused": True}
    assert gone.called
