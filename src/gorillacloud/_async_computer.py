"""The async Computer handle.

Mirrors :class:`gorillacloud.Computer` method for method. Field accessors come
from the shared ``ComputerFields``; paths, payloads, and validation come from
``_api``. What is left here is genuinely only the awaits.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

from . import _api
from ._client import AsyncTransport
from ._computer import GUEST_PROBE, ComputerFields
from ._exceptions import GorillaCloudError, TimeoutError
from ._models import ExecResult, Snapshot

__all__ = ["AsyncComputer"]


class AsyncComputer(ComputerFields):
    """A cloud desktop, driven with ``await``.

    Obtain one from :class:`gorillacloud.AsyncClient` rather than constructing
    it directly.
    """

    def __init__(self, transport: AsyncTransport, data: Mapping[str, Any]) -> None:
        self._t = transport
        self._data = dict(data)

    # --- lifecycle ------------------------------------------------------

    async def refresh(self) -> AsyncComputer:
        """Re-read this computer's state from the API."""
        self._data = dict(await self._t.json("GET", _api.computer(self.id)) or {})
        return self

    async def start(self) -> AsyncComputer:
        await self._t.request("POST", _api.computer_action(self.id, "start"))
        return await self.refresh()

    async def stop(self) -> AsyncComputer:
        await self._t.request("POST", _api.computer_action(self.id, "stop"))
        return await self.refresh()

    async def restart(self) -> AsyncComputer:
        await self._t.request("POST", _api.computer_action(self.id, "restart"))
        return await self.refresh()

    async def clone(self, name: str | None = None) -> AsyncComputer:
        """Copy this computer into a new one. The source must be stopped.

        Returns as soon as the new computer exists, which is before its disk
        does: copying a disk runs for minutes, so the clone comes back
        ``"building"`` and fills in behind you. Follow with
        :meth:`wait_until_built` before starting it.
        """
        data = await self._t.json(
            "POST", _api.computer_action(self.id, "clone"), json=_api.name_body(name)
        )
        return AsyncComputer(self._t, data or {})

    async def rename(self, name: str) -> AsyncComputer:
        """Give this computer a new name, and return it renamed.

        The name is a label. Nothing is derived from it — the id is what
        identifies a computer everywhere — so this moves no bytes and breaks no
        reference anything else is holding. Names need not be unique.

        The server trims surrounding whitespace and control characters and caps
        the result at 64 characters, so :attr:`name` afterwards may not be
        exactly what was passed in. Read it back rather than assuming.

        Snapshots already taken keep the name they were captured under. While
        this computer exists they are listed under its current name; once it is
        deleted they fall back to what it was called at the time, which is then
        all that is left of it.
        """
        self._data = dict(
            await self._t.json("PATCH", _api.computer(self.id), json=_api.rename_body(name)) or {}
        )
        return self

    async def delete(self) -> None:
        """Destroy this computer and its disk. Snapshots taken from it survive."""
        await self._t.request("DELETE", _api.computer(self.id))

    # --- readiness ------------------------------------------------------

    async def wait_until_built(
        self, timeout: float = 900.0, poll: float = 5.0
    ) -> AsyncComputer:
        """Await until a cloned computer's disk has been copied.

        Returns immediately for anything not being built, so it is safe to call
        on any computer. Raises :class:`~gorillacloud.GorillaCloudError` if the
        copy failed, and :class:`~gorillacloud.TimeoutError` if it is still
        going when ``timeout`` runs out — the computer keeps building either
        way; only the waiting stops.

        The default timeout is generous because the work is: a compressed
        conversion of a 40 GB Windows disk takes several minutes on a busy host.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.build_failed:
                raise GorillaCloudError(
                    f"{self.id} could not be built: {self.build_error or 'the disk copy failed'}"
                )
            if not self.is_building:
                return self
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"{self.id} was still building after {timeout:g}s "
                    "(it has not stopped; only this wait has)"
                )
            await asyncio.sleep(poll)
            await self.refresh()

    async def wait_until_running(
        self, timeout: float = 120.0, poll: float = 2.0
    ) -> AsyncComputer:
        """Await until the machine is running.

        This is the *machine*, not the desktop: it returns as soon as the VM is
        up, while the guest OS is still booting. Use :meth:`wait_for_guest` when
        you need something inside the guest to be ready.
        """
        deadline = time.monotonic() + timeout
        while True:
            await self.refresh()
            if self.status == "running":
                return self
            # A computer with no disk will never start on its own, and waiting
            # out the full timeout to say so helps nobody.
            if self.build_failed:
                raise GorillaCloudError(
                    f"{self.id} could not be built: {self.build_error or 'the disk copy failed'}"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.id} was still {self.status!r} after {timeout:g}s")
            await asyncio.sleep(poll)

    async def wait_for_guest(self, timeout: float = 180.0, poll: float = 3.0) -> AsyncComputer:
        """Await until the guest OS answers, by running a trivial command in it.

        Works on Linux and Windows: the probe is ``exit 0``, a builtin of both
        bash and cmd.exe, so it needs nothing on the guest's PATH and nothing
        about which OS this is.

        What it establishes is that the *guest agent* answers, which is earlier
        than the desktop being usable — on Windows especially, since the agent
        runs in session 0 and replies well before anyone has logged in. When you
        need the desktop rather than the machine, poll :meth:`screenshot`.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                res = await self.exec(GUEST_PROBE, timeout_s=5)
                if res.ok:
                    return self
            except GorillaCloudError:
                pass  # agent not up yet
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.id} guest did not respond within {timeout:g}s")
            await asyncio.sleep(poll)

    # --- observing ------------------------------------------------------

    async def screenshot(self, width: int | None = None) -> bytes:
        """Capture the screen.

        Full-resolution PNG by default. Passing ``width`` returns a downscaled
        JPEG instead — much cheaper, and enough for a thumbnail or a quick
        "has anything changed" check.
        """
        resp = await self._t.request(
            "GET",
            _api.computer_action(self.id, "screenshot"),
            params=_api.screenshot_params(width),
        )
        return resp.content

    # --- controlling ----------------------------------------------------

    async def _input(self, body: dict[str, Any]) -> None:
        await self._t.request("POST", _api.computer_action(self.id, "input"), json=body)

    async def move(self, x: int, y: int) -> None:
        """Move the pointer to ``(x, y)`` in the guest's 1280x800 screen space."""
        await self._input(_api.pointer_body("move", x, y))

    async def click(self, x: int, y: int) -> None:
        await self._input(_api.pointer_body("left_click", x, y))

    async def right_click(self, x: int, y: int) -> None:
        await self._input(_api.pointer_body("right_click", x, y))

    async def middle_click(self, x: int, y: int) -> None:
        await self._input(_api.pointer_body("middle_click", x, y))

    async def double_click(self, x: int, y: int) -> None:
        await self._input(_api.pointer_body("double_click", x, y))

    async def scroll(
        self, x: int = 0, y: int = 0, *, direction: str = "down", amount: int = 3
    ) -> None:
        """Scroll the wheel, first moving to ``(x, y)`` when either is non-zero."""
        await self._input(_api.scroll_body(x, y, direction, amount))

    async def type(self, text: str) -> None:
        """Type text as keystrokes.

        Characters with no key mapping are skipped rather than raising, so a
        stray emoji in a prompt cannot fail the whole call.
        """
        await self._input(_api.type_body(text))

    async def key(self, *keys: str) -> None:
        """Press a chord, e.g. ``await key("ctrl", "c")``."""
        await self._input(_api.key_body(keys))

    async def exec(
        self, command: str, timeout_s: int = 30, *, desktop: bool = False
    ) -> ExecResult:
        """Run a shell command inside the guest.

        Uses the guest's native shell — bash on Linux, cmd.exe on Windows. A
        non-zero exit is returned, not raised; check :attr:`ExecResult.ok`.

        By default the command runs in the system context: as ``root`` on Linux,
        with no display attached. Pass ``desktop=True`` to run it in the logged-in
        desktop session instead — as the desktop user, with ``DISPLAY``, ``HOME``
        and ``XAUTHORITY`` set — which is what anything with a window needs.

        A GUI program does not exit on its own, so launch it detached or the call
        blocks until ``timeout_s`` kills it::

            await c.exec("nohup firefox https://example.com >/dev/null 2>&1 &", desktop=True)
        """
        data = await self._t.json(
            "POST",
            _api.computer_action(self.id, "exec"),
            json=_api.exec_body(command, timeout_s, desktop),
        )
        return ExecResult.from_api(data or {})

    # --- snapshots ------------------------------------------------------

    async def snapshot(self, *, memory: bool = False) -> Snapshot:
        """Capture a snapshot of this computer.

        Works while it is running. ``memory=True`` also captures live RAM and
        device state, so a restore or fork resumes exactly where it was instead
        of booting — the computer must be running for that.
        """
        data = await self._t.json(
            "POST",
            _api.computer_action(self.id, "snapshots"),
            json=_api.snapshot_body(memory),
        )
        return Snapshot.from_api(data or {})

    async def snapshots(self) -> list[Snapshot]:
        """This computer's snapshots, in the order the API returns them."""
        data = await self._t.json("GET", _api.SNAPSHOTS) or []
        return [Snapshot.from_api(s) for s in data if s.get("computer_id") == self.id]

    async def schedule(self) -> Mapping[str, Any]:
        """The automatic daily snapshot schedule."""
        return await self._t.json("GET", _api.computer_action(self.id, "schedule")) or {}

    async def set_schedule(
        self,
        *,
        enabled: bool,
        hour: int = 4,
        minute: int = 0,
        tz: str = "UTC",
    ) -> Mapping[str, Any]:
        """Set the automatic daily snapshot window, in the given IANA timezone."""
        await self._t.request(
            "PUT",
            _api.computer_action(self.id, "schedule"),
            json=_api.schedule_body(enabled=enabled, hour=hour, minute=minute, tz=tz),
        )
        return await self.schedule()

    async def clear_schedule(self) -> Mapping[str, Any]:
        """Remove the schedule, as distinct from disabling it.

        ``set_schedule(enabled=False)`` keeps the chosen time so toggling back on
        restores it, and keeps the scheduler's bookkeeping with it. Clearing
        returns the computer to never having had a schedule.
        """
        return await self._t.json("DELETE", _api.computer_action(self.id, "schedule")) or {}
