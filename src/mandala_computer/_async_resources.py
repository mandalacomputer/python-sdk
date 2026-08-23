"""Resource collections hanging off the async client."""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from . import _api
from ._async_computer import AsyncComputer
from ._client import AsyncTransport
from ._models import Listing, Move, Size, Snapshot, Template, UsageReport
from ._resources import EPHEMERAL_DOC, warn_cleanup_failed

__all__ = [
    "AsyncComputers",
    "AsyncMoves",
    "AsyncSizes",
    "AsyncSnapshots",
    "AsyncTemplates",
    "AsyncUsage",
]


class AsyncComputers:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self, *, allow_partial: bool = False) -> Listing[AsyncComputer]:
        """Every computer on the account, or every one in the key's workspace.

        No ``vnc`` on these rows — fetch one computer to get its desktop
        credentials, or call :meth:`AsyncComputer.refresh` on a listed one.

        ``allow_partial`` accepts a listing the platform knows is short. Without
        it a hypervisor that cannot be reached makes this raise
        :class:`~mandala_computer.UnavailableError` rather than answering short,
        because a short list is not a smaller truth: it reads exactly like the
        missing computers were deleted, and the obvious next thing a script does
        with a computer that has disappeared is tidy up after it. With it, the
        returned :class:`~mandala_computer.Listing` says so —
        ``is_complete`` — and the rows that could not be read carry
        :attr:`AsyncComputer.unreachable` and nothing else.
        """
        data, incomplete = await self._t.listing(
            _api.COMPUTERS, params=_api.partial_params(allow_partial)
        )
        return Listing.of([AsyncComputer(self._t, c) for c in data or []], incomplete)

    async def get(self, computer_id: str) -> AsyncComputer:
        data = await self._t.json_object("GET", _api.computer(computer_id))
        return AsyncComputer(self._t, _api.computer_payload(data))

    async def create(
        self,
        *,
        name: str | None = None,
        size: str | None = None,
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

        ``size`` is a named size from :meth:`Client.sizes` — a template and a
        CPU/RAM/disk shape together, and the shapes the platform keeps
        pre-booted, so naming one is the likeliest way to get a computer in
        about a second rather than a cold boot. It cannot be combined with
        ``template``, ``cpu``, ``ram_mb`` or ``disk_gb``; sending both raises
        :class:`ValueError` before any request is made.

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
            size=size,
        )
        data = await self._t.json_object("POST", _api.COMPUTERS, json=body)
        return AsyncComputer(self._t, _api.computer_payload(data))

    @asynccontextmanager
    async def ephemeral(self, **kwargs: Any) -> AsyncIterator[AsyncComputer]:
        computer = await self.create(**kwargs)
        try:
            yield computer
        except BaseException:
            # See the sync half: a failing delete must not displace the
            # caller's exception.
            try:
                await computer.delete()
            except Exception as cleanup_failed:  # noqa: BLE001
                # See the sync half: every failure, transport errors included.
                warn_cleanup_failed(computer.id, cleanup_failed)
            raise
        else:
            await computer.delete()

    ephemeral.__doc__ = EPHEMERAL_DOC


class AsyncSnapshots:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(
        self, *, include_unfinished: bool = False, allow_partial: bool = False
    ) -> Listing[Snapshot]:
        """Every snapshot on the account that you can act on.

        Snapshots outlive the computers they came from, so this routinely
        contains rows whose :attr:`Snapshot.computer_id` resolves to nothing —
        those carry :attr:`Snapshot.orphaned`, and :meth:`clone` is the
        operation that still works on them.

        ``include_unfinished`` widens it to deletions that began and did not
        finish. Nothing can be restored or cloned from one, but they still hold
        objects and are still billed, so it is the flag for a question about
        storage rather than about what can be used.

        ``allow_partial`` is :meth:`AsyncComputers.list`'s, with the same warning.
        """
        data, incomplete = await self._t.listing(
            _api.SNAPSHOTS,
            params=_api.snapshot_listing_params(
                include_unfinished=include_unfinished, allow_partial=allow_partial
            ),
        )
        return Listing.of([Snapshot.from_api(s) for s in data or []], incomplete)

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
        data = await self._t.json_object(
            "POST", _api.snapshot_action(snapshot_id, "clone"), json=_api.name_body(name)
        )
        return AsyncComputer(self._t, _api.computer_payload(data))

    async def delete(self, snapshot_id: str) -> None:
        await self._t.request("DELETE", _api.snapshot(snapshot_id))


class AsyncTemplates:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> builtins.list[Template]:
        data = await self._t.json_array("GET", _api.TEMPLATES)
        return [Template.from_api(t) for t in data]


class AsyncMoves:
    """The moves on this account, live and recently finished.

    Its own collection because ``GET /moves`` is its own route, account-scoped
    rather than hanging off a computer — which is the platform's decision and the
    right one: a move is a fact about a computer that is currently on one host
    and about to be on another, and during the window that matters that is
    exactly what nobody can say.
    """

    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> builtins.list[Move]:
        """Every move worth reading: the ones still running, and the ones that
        finished within the last day and have not been dismissed.

        Two things to get from a listing rather than a per-computer read. A move
        you started is found by its ``computer_id`` —
        :meth:`~mandala_computer.Computer.wait_for_move` does exactly that. And a
        move you did NOT start is what the "another computer on this account is
        being moved right now" refusal is about: one runs per account at a time,
        and this is where you find out which and how far along.

        A finished move stays here for a day so that an outcome is still readable
        by somebody who went away while it ran. Read ``live``, not the row's
        absence.

        An API key issued against a workspace sees the moves of computers in that
        workspace only.
        """
        data = await self._t.json_object("GET", _api.MOVES)
        rows = data.get("moves")
        return [Move.from_api(m) for m in rows] if isinstance(rows, list) else []


class AsyncSizes:
    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def list(self) -> builtins.list[Size]:
        data = await self._t.json_array("GET", _api.SIZES)
        return [Size.from_api(s) for s in data]


class AsyncUsage:
    """What this account has used.

    Its own collection because ``GET /usage`` is its own route, account-scoped
    like ``GET /moves`` rather than hanging off a computer — which it could not
    be: the figures include computers that have since been deleted, and those are
    exactly the ones an unexplained line on an invoice belongs to.
    """

    def __init__(self, transport: AsyncTransport) -> None:
        self._t = transport

    async def read(
        self,
        *,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
    ) -> UsageReport:
        """Running hours weighted by cores and memory, the storage held, and
        the per-computer breakdown behind the totals.

        The read to build a spend check around: a loop that launches computers is
        the caller that can run up a bill without noticing, and this is the same
        figure the dashboard shows the person who will ask about it.

        With no arguments the window is the account's current billing period,
        which is what makes the numbers comparable with an invoice. Name
        ``since``/``until`` for a window that has CLOSED — the billing period is
        always the current one, and by the time an invoice arrives the period it
        covers is not. Both take an aware ``datetime`` or an RFC 3339 string with
        a zone; a naive datetime is refused rather than guessed at, because the
        zone that would have to be assumed is not necessarily yours. They are
        sent as ``from``/``to``, which ``from`` being a keyword is the whole
        reason for the other spelling.

        Check :attr:`~mandala_computer.UsageReport.degraded` and
        :attr:`~mandala_computer.UsageReport.unmetered` on the way out. Each
        figure is a sum across the fleet, so a hypervisor that did not answer
        leaves a total that is quietly short rather than an obviously missing
        row, and those two flags are the only thing that says so.
        """
        data = await self._t.json_object("GET", _api.USAGE, params=_api.usage_params(since, until))
        return UsageReport.from_api(data)
