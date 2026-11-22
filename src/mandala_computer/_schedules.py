"""Agent schedules: a cron expression that starts a background agent run on
one computer at each time it names, in a time zone.

Each fire is an ordinary background run, read back with
``client.agent_runs.get(fire.run_id)``. Every scheduled run spends the
account's Mandala AI credits: nothing is there to send a model key when one
fires, so a schedule takes no ``model_key`` and no ``system`` prompt. A
schedule acts as the API key that created it, or last changed it.
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from . import _api
from ._client import Transport
from ._exceptions import MandalaError

__all__ = ["AgentSchedule", "AgentScheduleFire", "AgentSchedulePage"]

#: Leave a field as it is in :meth:`AgentSchedules.update`.
_UNSET: Any = object()


def _text(d: Mapping[str, Any], key: str) -> str:
    v = d.get(key)
    return "" if v is None else str(v)


def _optional(d: Mapping[str, Any], key: str, where: str) -> str | None:
    v = d.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise MandalaError(f"{where}: {key} is not a string or null")
    return v


def _number(d: Mapping[str, Any], key: str) -> float:
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


@dataclass(frozen=True)
class AgentScheduleFire:
    """One fire of a schedule."""

    #: The time this fire was for.
    scheduled_for: str
    #: When it was sent, or recorded as missed.
    fired_at: str
    #: ``started``, ``skipped``, ``missed``, ``failed``, ``pending`` or
    #: ``interrupted``; a value this client does not know is kept as sent.
    outcome: str
    #: The run it started, for ``started``.
    run_id: str | None = None
    #: Why no run started, in a sentence.
    error: str | None = None

    @classmethod
    def from_api(cls, d: Any, where: str) -> AgentScheduleFire:
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        outcome = d.get("outcome")
        if not isinstance(outcome, str) or not outcome:
            raise MandalaError(f"{where}: no outcome")
        return cls(
            scheduled_for=_text(d, "scheduled_for"),
            fired_at=_text(d, "fired_at"),
            outcome=outcome,
            run_id=_optional(d, "run_id", where),
            error=_optional(d, "error", where),
        )


@dataclass(frozen=True)
class AgentSchedule:
    """A schedule as the platform answers it."""

    #: ``sch_`` and twenty-four hex characters.
    id: str
    computer_id: str
    #: The thread each fire continues, or ``None`` for a new thread each time.
    thread_id: str | None
    name: str | None
    #: Five fields: minute, hour, day of month, month, day of week.
    cron: str
    #: The IANA time zone ``cron`` is read in.
    timezone: str
    prompt: str
    model: str
    max_steps: int
    max_cost_usd: float
    paused: bool
    #: Why it is paused: ``paused``, ``credential``, ``authority``,
    #: ``thread_deleted``, ``failing`` or ``never_fires``; a value this
    #: client does not know is kept as sent.
    paused_reason: str | None
    #: When it next fires; ``None`` while paused.
    next_fire_at: str | None
    last_fire_at: str | None
    #: The run its last started fire began.
    last_run_id: str | None
    created_at: str
    updated_at: str
    #: Its latest fires, newest first. Empty in a list.
    recent_fires: builtins.list[AgentScheduleFire] = field(default_factory=list)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Any, where: str = "an agent schedule") -> AgentSchedule:
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        sid = d.get("id")
        if not isinstance(sid, str) or not sid or sid != sid.strip():
            raise MandalaError(f"{where}: no schedule id")
        paused = d.get("paused")
        if not isinstance(paused, bool):
            raise MandalaError(f"{where}: paused is not true or false")
        fires = d.get("recent_fires", [])
        if not isinstance(fires, list):
            raise MandalaError(f"{where}: recent_fires is not a list")
        steps = d.get("max_steps")
        return cls(
            id=sid,
            computer_id=_text(d, "computer_id"),
            thread_id=_optional(d, "thread_id", where),
            name=_optional(d, "name", where),
            cron=_text(d, "cron"),
            timezone=_text(d, "timezone"),
            prompt=_text(d, "prompt"),
            model=_text(d, "model"),
            max_steps=steps if isinstance(steps, int) and not isinstance(steps, bool) else 0,
            max_cost_usd=_number(d, "max_cost_usd"),
            paused=paused,
            paused_reason=_optional(d, "paused_reason", where),
            next_fire_at=_optional(d, "next_fire_at", where),
            last_fire_at=_optional(d, "last_fire_at", where),
            last_run_id=_optional(d, "last_run_id", where),
            created_at=_text(d, "created_at"),
            updated_at=_text(d, "updated_at"),
            recent_fires=[
                AgentScheduleFire.from_api(f, f"fire {i} of {where}") for i, f in enumerate(fires)
            ],
            raw=dict(d),
        )


@dataclass(frozen=True)
class AgentSchedulePage:
    """One page of ``client.agent_schedules.list``, newest first."""

    schedules: builtins.list[AgentSchedule]
    has_more: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def next_before(self) -> str | None:
        """What to pass as ``before=`` for the next page, or ``None`` on the last."""
        return self.schedules[-1].id if self.has_more and self.schedules else None

    @classmethod
    def from_api(cls, d: Any, where: str = "GET agent/schedules") -> AgentSchedulePage:
        if not isinstance(d, Mapping) or not isinstance(d.get("schedules"), list):
            raise MandalaError(f"{where}: not a list of schedules")
        more = d.get("has_more")
        if not isinstance(more, bool):
            raise MandalaError(f"{where}: has_more is not true or false")
        rows = [
            AgentSchedule.from_api(row, f"schedule {i} from {where}")
            for i, row in enumerate(d["schedules"])
        ]
        if more and not rows:
            raise MandalaError(f"{where}: says more follow, with no row to page from")
        return cls(schedules=rows, has_more=more, raw=dict(d))


def _body(
    *,
    cron: Any,
    prompt: Any,
    timezone: Any,
    model: Any,
    provider: Any,
    max_steps: Any,
    max_cost_usd: Any,
    thread_id: Any,
    name: Any,
    paused: Any,
) -> dict[str, Any]:
    """The wire body, checked for type here; the platform holds the values to
    its own rules and answers a 400 for any it refuses."""
    body: dict[str, Any] = {}
    for key, value in (
        ("cron", cron),
        ("prompt", prompt),
        ("timezone", timezone),
        ("model", model),
    ):
        if value is _UNSET:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        body[key] = value
    if provider is not _UNSET:
        if provider != "anthropic":
            raise ValueError("provider must be anthropic: a schedule runs on Mandala AI credits")
        body["provider"] = provider
    if max_steps is not _UNSET:
        if not isinstance(max_steps, int) or isinstance(max_steps, bool) or max_steps < 1:
            raise ValueError("max_steps must be a whole number of at least 1")
        body["max_steps"] = max_steps
    if max_cost_usd is not _UNSET:
        if not isinstance(max_cost_usd, (int, float)) or isinstance(max_cost_usd, bool):
            raise ValueError("max_cost_usd must be a number")
        body["max_cost_usd"] = max_cost_usd
    for key, value in (("thread_id", thread_id), ("name", name)):
        if value is _UNSET:
            continue
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be a string or None")
        body[key] = value
    if paused is not _UNSET:
        if not isinstance(paused, bool):
            raise ValueError("paused must be True or False")
        body["paused"] = paused
    return body


class AgentSchedules:
    """Agent schedules (``client.agent_schedules``): background runs started on
    a timetable.

    Members and owners may make and read them, with an API key that neither
    expires nor is limited to specific computers: a schedule acts as its key on
    every fire. A key confined to a workspace sees that workspace's schedules
    only. A fire is skipped while the schedule's previous run is still going or
    the computer is not running, and one the platform could not send within
    five minutes of its time is ``missed``, never sent late.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def create(
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
        """Make a schedule on a computer. ``timezone`` defaults to UTC on the platform."""
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
        return AgentSchedule.from_api(self._t.json_object("POST", path, json=body), f"POST {path}")

    def list(
        self,
        *,
        computer_id: str | None = None,
        limit: int | None = None,
        before: str | None = None,
    ) -> AgentSchedulePage:
        """One page of schedules, newest first, each with an empty
        :attr:`~mandala_computer.AgentSchedule.recent_fires`."""
        params = _api.agent_page_params(computer_id, limit, before)
        return AgentSchedulePage.from_api(
            self._t.json_object("GET", _api.AGENT_SCHEDULES, params=params)
        )

    def get(self, schedule_id: str) -> AgentSchedule:
        """One schedule, when it next fires, and its 20 latest fires."""
        path = _api.agent_schedule(schedule_id)
        return AgentSchedule.from_api(self._t.json_object("GET", path), f"GET {path}")

    def update(
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
        """Change any of a schedule's fields; what is not passed stays, and
        ``thread_id=None`` or ``name=None`` clears one. The schedule acts as
        this client's key from then on, and its next fire is the first time
        after now. One paused for a reason of its own stays paused until
        :meth:`resume`."""
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
            self._t.json_object("PATCH", path, json=body), f"PATCH {path}"
        )

    def pause(self, schedule_id: str) -> AgentSchedule:
        """Pause it: it does not fire until resumed."""
        return self.update(schedule_id, paused=True)

    def resume(self, schedule_id: str) -> AgentSchedule:
        """Resume it, whatever paused it. It acts as this client's key from then on."""
        return self.update(schedule_id, paused=False)

    def delete(self, schedule_id: str) -> None:
        """Delete it. Runs it already started are untouched."""
        self._t.json_object("DELETE", _api.agent_schedule(schedule_id))
