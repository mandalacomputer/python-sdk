"""Resource collections hanging off the async client."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from . import _api
from ._async_computer import AsyncComputer
from ._client import AsyncTransport
from ._models import Snapshot, Template
from ._resources import EPHEMERAL_DOC

__all__ = ["AsyncComputers", "AsyncSnapshots", "AsyncTemplates"]


class AsyncComputers:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> builtins.list[AsyncComputer]:
        data = await self._t.json("GET", _api.COMPUTERS) or []
        return [AsyncComputer(self._t, c) for c in data]

    async def get(self, computer_id: str) -> AsyncComputer:
        data = await self._t.json("GET", _api.computer(computer_id))
        return AsyncComputer(self._t, _api.computer_payload(data))

    async def create(
        self,
        *,
        name: str | None = None,
        template: str | None = None,
        cpu: int | None = None,
        ram_mb: int | None = None,
        disk_gb: int | None = None,
        start: bool = True,
        resolution: str | None = None,
    ) -> AsyncComputer:
        """Provision a computer.

        Anything omitted falls back to the template's defaults. Sizing is capped
        by the account's plan; exceeding a cap raises
        :class:`~mandala_computer.PlanLimitError` naming the limit.

        ``resolution`` is ``"WIDTHxHEIGHT"`` or ``"WIDTHxHEIGHTxDEPTH"`` and
        defaults to ``"1280x800x24"``. It is a create-time choice and only a
        create-time choice: the screen is part of the machine QEMU builds, so
        changing it needs a new one, and there is no method that resizes a
        computer's display. Pick it deliberately if a model is going to drive
        this desktop — computer-use accuracy is resolution-sensitive, and every
        coordinate the model produces is in this space.

        Returns as soon as the API does — the machine is starting, not ready.
        Follow with :meth:`AsyncComputer.wait_for_guest`.

        A create that builds a computer which then will not boot is *not* an
        error: it returns the computer, stopped, with
        :attr:`AsyncComputer.start_error` saying what went wrong. The machine
        exists and is billable either way, so it comes back rather than being
        thrown away with the exception — check ``start_error`` if it matters,
        and :meth:`AsyncComputer.start` may work on a second attempt.
        """
        body = _api.create_body(
            name=name,
            template=template,
            cpu=cpu,
            ram_mb=ram_mb,
            disk_gb=disk_gb,
            start=start,
            resolution=resolution,
        )
        data = await self._t.json("POST", _api.COMPUTERS, json=body)
        return AsyncComputer(self._t, _api.computer_payload(data))

    @asynccontextmanager
    async def ephemeral(self, **kwargs: Any) -> AsyncIterator[AsyncComputer]:
        computer = await self.create(**kwargs)
        try:
            yield computer
        finally:
            await computer.delete()

    ephemeral.__doc__ = EPHEMERAL_DOC


class AsyncSnapshots:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> builtins.list[Snapshot]:
        data = await self._t.json("GET", _api.SNAPSHOTS) or []
        return [Snapshot.from_api(s) for s in data]

    async def restore(self, snapshot_id: str) -> None:
        """Roll a computer back to a snapshot, replacing its current disk."""
        await self._t.request("POST", _api.snapshot_action(snapshot_id, "restore"))

    async def clone(self, snapshot_id: str, name: str | None = None) -> AsyncComputer:
        """Create a new computer from a snapshot.

        Cloning a memory snapshot forks it: the new machine resumes from the
        captured RAM rather than booting, so it starts as a live twin of the
        original — same hostname and network identity until it is re-identified.

        Returns as soon as the computer exists, which is before its disk does.
        A snapshot has to be copied out — and a snapshot taken incrementally is
        collapsed out of its whole chain — which runs for minutes, so the
        computer comes back ``"building"``. Until that lands there is nothing to
        boot and starting it raises :class:`~mandala_computer.ConflictError`; wait
        with :meth:`AsyncComputer.wait_until_built`.
        """
        data = await self._t.json(
            "POST", _api.snapshot_action(snapshot_id, "clone"), json=_api.name_body(name)
        )
        return AsyncComputer(self._t, _api.computer_payload(data))

    async def delete(self, snapshot_id: str) -> None:
        await self._t.request("DELETE", _api.snapshot(snapshot_id))


class AsyncTemplates:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> builtins.list[Template]:
        data = await self._t.json("GET", _api.TEMPLATES) or []
        return [Template.from_api(t) for t in data]
