"""A computer's end time: set at create, read back, moved and cleared."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _api

BASE = "https://api.test/api/v1"
COMPUTER: dict[str, Any] = {"id": "vm-1", "name": "demo", "status": "running", "os": "linux"}
ENDS = "2026-11-01T12:00:00Z"


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


@respx.mock
def test_create_sends_a_time_or_hours_and_nothing_when_neither() -> None:
    route = respx.post(f"{BASE}/computers").mock(httpx.Response(201, json=COMPUTER))
    client = mc.Client("com_test", base_url=BASE)
    client.computers.create(template="base", ends_at=ENDS)
    assert sent(route)["ends_at"] == ENDS
    assert "ends_in_hours" not in sent(route)

    client.computers.create(template="base", ends_in_hours=1.5)
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
    route = respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "ends_at": ENDS})
    )
    c = mc.Client("com_test", base_url=BASE).computers.get("vm-1")
    assert c.ends_at is None
    c.set_end_time(ENDS)
    assert sent(route) == {"ends_at": ENDS}
    assert c.ends_at == ENDS

    c.set_end_time(ends_in_hours=2)
    assert sent(route) == {"ends_in_hours": 2}

    route.mock(httpx.Response(200, json=COMPUTER))
    c.set_end_time(None)
    assert sent(route) == {"ends_at": None}
    assert c.ends_at is None


@respx.mock
async def test_the_async_half_sends_the_same() -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    create = respx.post(f"{BASE}/computers").mock(httpx.Response(201, json=COMPUTER))
    patch = respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "ends_at": ENDS})
    )
    client = mc.AsyncClient("com_test", base_url=BASE)
    await client.computers.create(template="base", ends_in_hours=3)
    assert sent(create)["ends_in_hours"] == 3
    c = await client.computers.get("vm-1")
    await c.set_end_time(ENDS)
    assert sent(patch) == {"ends_at": ENDS}
    assert c.ends_at == ENDS
    await c.set_end_time()
    assert sent(patch) == {"ends_at": None}


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
