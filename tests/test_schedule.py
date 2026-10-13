"""The snapshot schedule as a strict ``Schedule`` (OPL-5974).

``schedule()``, ``set_schedule()`` and ``clear_schedule()`` returned the answer
as a plain mapping, so a malformed one was handed on as if it were a window. An
empty answer read as nothing at all, and a caller defaulting the gaps got a
disabled midnight schedule nobody chose. They now answer a ``Schedule`` read the
way the reference describes it, and refuse anything else, on both clients.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux"}
CHICAGO = {"enabled": True, "hour": 23, "minute": 30, "tz": "America/Chicago"}
SCHEDULE = f"{BASE}/computers/vm-1/schedule"

BROKEN: list[tuple[dict[str, Any], str]] = [
    ({}, "enabled"),
    ({**CHICAGO, "enabled": None}, "enabled"),
    ({**CHICAGO, "enabled": "sometimes"}, "enabled"),
    ({k: v for k, v in CHICAGO.items() if k != "hour"}, "hour"),
    ({**CHICAGO, "hour": 24}, "hour"),
    ({**CHICAGO, "hour": 4.5}, "hour"),
    ({**CHICAGO, "hour": True}, "hour"),
    ({**CHICAGO, "minute": 60}, "minute"),
    ({**CHICAGO, "minute": -1}, "minute"),
    ({**CHICAGO, "tz": ""}, "timezone"),
    ({**CHICAGO, "tz": None}, "timezone"),
]


def test_a_whole_answer_is_a_schedule() -> None:
    got = mc.Schedule.from_api(CHICAGO)
    assert got == mc.Schedule(enabled=True, hour=23, minute=30, tz="America/Chicago")
    assert got.raw == CHICAGO


@pytest.mark.parametrize(("answer", "names"), BROKEN)
def test_an_answer_that_is_not_a_whole_schedule_is_refused(
    answer: dict[str, Any], names: str
) -> None:
    with pytest.raises(mc.MandalaError, match=names):
        mc.Schedule.from_api(answer)


@pytest.mark.parametrize(("answer", "names"), BROKEN)
@respx.mock
def test_each_method_refuses_a_malformed_answer(answer: dict[str, Any], names: str) -> None:
    respx.get(SCHEDULE).mock(httpx.Response(200, json=answer))
    put = respx.put(SCHEDULE).mock(httpx.Response(200, json=answer))
    respx.delete(SCHEDULE).mock(httpx.Response(200, json=answer))
    with mc.Client("gck_test", base_url=BASE) as client:
        c = mc.Computer(client._t, {**COMPUTER, "snapshot_schedule": CHICAGO})
        with pytest.raises(mc.MandalaError, match=f"GET computers/vm-1/schedule: .*{names}"):
            c.schedule()
        with pytest.raises(mc.MandalaError, match=f"PUT computers/vm-1/schedule: .*{names}"):
            c.set_schedule(enabled=False, hour=1, minute=2, tz="UTC")
        assert put.called
        # The handle keeps what it had: only a whole answer says what was stored.
        assert c.snapshot_schedule == CHICAGO
        with pytest.raises(mc.MandalaError, match=f"DELETE computers/vm-1/schedule: .*{names}"):
            c.clear_schedule()
        # The delete succeeded whatever its body said.
        assert c.snapshot_schedule is None


@pytest.mark.parametrize(("answer", "names"), BROKEN)
@respx.mock
async def test_the_async_methods_refuse_the_same_answers(
    answer: dict[str, Any], names: str
) -> None:
    respx.get(SCHEDULE).mock(httpx.Response(200, json=answer))
    respx.put(SCHEDULE).mock(httpx.Response(200, json=answer))
    respx.delete(SCHEDULE).mock(httpx.Response(200, json=answer))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, {**COMPUTER, "snapshot_schedule": CHICAGO})
        with pytest.raises(mc.MandalaError, match=names):
            await c.schedule()
        with pytest.raises(mc.MandalaError, match=names):
            await c.set_schedule(enabled=False, hour=1, minute=2, tz="UTC")
        assert c.snapshot_schedule == CHICAGO
        with pytest.raises(mc.MandalaError, match=names):
            await c.clear_schedule()
        assert c.snapshot_schedule is None


@respx.mock
def test_the_methods_answer_a_schedule() -> None:
    cleared = {"enabled": False, "hour": 0, "minute": 0, "tz": "UTC"}
    respx.get(SCHEDULE).mock(httpx.Response(200, json=CHICAGO))
    respx.put(SCHEDULE).mock(httpx.Response(200, json={**CHICAGO, "enabled": False}))
    respx.delete(SCHEDULE).mock(httpx.Response(200, json=cleared))
    with mc.Client("gck_test", base_url=BASE) as client:
        c = mc.Computer(client._t, COMPUTER)
        assert c.schedule() == mc.Schedule(True, 23, 30, "America/Chicago")
        stored = c.set_schedule(enabled=False, hour=23, minute=30, tz="America/Chicago")
        assert stored == mc.Schedule(False, 23, 30, "America/Chicago")
        assert c.snapshot_schedule == {**CHICAGO, "enabled": False}
        assert c.clear_schedule() == mc.Schedule(False, 0, 0, "UTC")
        assert c.snapshot_schedule is None


@respx.mock
async def test_the_async_methods_answer_a_schedule() -> None:
    respx.get(SCHEDULE).mock(httpx.Response(200, json=CHICAGO))
    respx.put(SCHEDULE).mock(httpx.Response(200, json=CHICAGO))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        assert await c.schedule() == mc.Schedule(True, 23, 30, "America/Chicago")
        stored = await c.set_schedule(enabled=True, hour=23, minute=30, tz="America/Chicago")
        assert isinstance(stored, mc.Schedule) and stored.tz == "America/Chicago"
        assert c.snapshot_schedule == CHICAGO
