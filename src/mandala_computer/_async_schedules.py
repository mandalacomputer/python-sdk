"""The async half of :mod:`mandala_computer._schedules`."""

from __future__ import annotations

from typing import Any

from . import _api
from ._client import AsyncTransport
from ._schedules import _UNSET, AgentSchedule, AgentSchedulePage, AgentSchedules, _body


class AsyncAgentSchedules:
    __doc__ = AgentSchedules.__doc__

    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def create(
        self,
        computer_id: str,
        cron: str,
        prompt: str,
        *,
        timezone: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        max_steps: int | None = None,
        max_cost_usd: float | None = None,
        thread_id: str | None = None,
        name: str | None = None,
        paused: bool | None = None,
    ) -> AgentSchedule:
        body = _body(
            cron=cron,
            prompt=prompt,
            timezone=_UNSET if timezone is None else timezone,
            model=_UNSET if model is None else model,
            provider=_UNSET if provider is None else provider,
            max_steps=_UNSET if max_steps is None else max_steps,
            max_cost_usd=_UNSET if max_cost_usd is None else max_cost_usd,
            thread_id=_UNSET if thread_id is None else thread_id,
            name=_UNSET if name is None else name,
            paused=_UNSET if paused is None else paused,
        )
        path = _api.computer_agent_schedules(computer_id)
        return AgentSchedule.from_api(
            await self._t.json_object("POST", path, json=body), f"POST {path}"
        )

    async def list(
        self,
        *,
        computer_id: str | None = None,
        limit: int | None = None,
        before: str | None = None,
    ) -> AgentSchedulePage:
        params = _api.agent_page_params(computer_id, limit, before)
        return AgentSchedulePage.from_api(
            await self._t.json_object("GET", _api.AGENT_SCHEDULES, params=params)
        )

    async def get(self, schedule_id: str) -> AgentSchedule:
        path = _api.agent_schedule(schedule_id)
        return AgentSchedule.from_api(await self._t.json_object("GET", path), f"GET {path}")

    async def update(
        self,
        schedule_id: str,
        *,
        cron: Any = _UNSET,
        prompt: Any = _UNSET,
        timezone: Any = _UNSET,
        model: Any = _UNSET,
        provider: Any = _UNSET,
        max_steps: Any = _UNSET,
        max_cost_usd: Any = _UNSET,
        thread_id: Any = _UNSET,
        name: Any = _UNSET,
        paused: Any = _UNSET,
    ) -> AgentSchedule:
        body = _body(
            cron=cron,
            prompt=prompt,
            timezone=timezone,
            model=model,
            provider=provider,
            max_steps=max_steps,
            max_cost_usd=max_cost_usd,
            thread_id=thread_id,
            name=name,
            paused=paused,
        )
        path = _api.agent_schedule(schedule_id)
        return AgentSchedule.from_api(
            await self._t.json_object("PATCH", path, json=body), f"PATCH {path}"
        )

    async def pause(self, schedule_id: str) -> AgentSchedule:
        return await self.update(schedule_id, paused=True)

    async def resume(self, schedule_id: str) -> AgentSchedule:
        return await self.update(schedule_id, paused=False)

    async def delete(self, schedule_id: str) -> None:
        await self._t.json_object("DELETE", _api.agent_schedule(schedule_id))

    create.__doc__ = AgentSchedules.create.__doc__
    list.__doc__ = AgentSchedules.list.__doc__
    get.__doc__ = AgentSchedules.get.__doc__
    update.__doc__ = AgentSchedules.update.__doc__
    pause.__doc__ = AgentSchedules.pause.__doc__
    resume.__doc__ = AgentSchedules.resume.__doc__
    delete.__doc__ = AgentSchedules.delete.__doc__
