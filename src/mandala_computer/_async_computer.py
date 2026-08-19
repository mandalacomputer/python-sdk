"""The async Computer handle.

Mirrors :class:`mandala_computer.Computer` method for method. Field accessors come
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
from ._computer import GUEST_PROBE, ComputerFields, _cursor
from ._exceptions import MandalaError, TimeoutError
from ._models import ExecResult, Snapshot

__all__ = ["AsyncComputer"]


class AsyncComputer(ComputerFields):
    """A cloud desktop, driven with ``await``.

    Obtain one from :class:`mandala_computer.AsyncClient` rather than constructing
    it directly.
    """

    def __init__(self, transport: AsyncTransport, data: Mapping[str, Any]) -> None:
        self._t = transport
        self._data = dict(data)

    # --- lifecycle ------------------------------------------------------

    async def refresh(self) -> AsyncComputer:
        """Re-read this computer's state from the API.

        Also how a computer from :meth:`AsyncComputers.list` acquires a
        :attr:`vnc` connect surface, which the list deliberately omits.
        """
        self._data = _api.computer_payload(await self._t.json("GET", _api.computer(self.id)))
        return self

    async def start(self) -> AsyncComputer:
        """Start this computer, or resume it if its session was suspended.

        A suspended computer does not boot: its saved RAM is read back and the
        same processes and windows come up roughly a second later. An ordinary
        stopped computer boots as usual.
        """
        await self._t.request("POST", _api.computer_action(self.id, "start"))
        return await self.refresh()

    async def stop(self) -> AsyncComputer:
        """Stop this computer, discarding a suspended session if it has one.

        Use :meth:`suspend` to keep it.
        """
        await self._t.request("POST", _api.computer_action(self.id, "stop"))
        return await self.refresh()

    async def suspend(self) -> AsyncComputer:
        """Write this computer's RAM to disk and give the host its memory back.

        A pause rather than a stop: :meth:`start` afterwards resumes the same
        session — same processes, same open windows — in about a second instead
        of booting. :meth:`stop` discards it and leaves an ordinary stopped
        computer.

        The computer must be running. Raises
        :class:`~mandala_computer.ConflictError` for the states that clear on their
        own — a capture or a clone reading the disk, a migration in flight, or
        somebody driving the guest at that moment.
        """
        await self._t.request("POST", _api.computer_action(self.id, "suspend"))
        return await self.refresh()

    async def restart(self) -> AsyncComputer:
        """Reset this computer.

        Raises :class:`~mandala_computer.ConflictError` while a suspended session is
        saved, since a restart would have to guess whether you meant to resume
        that session or throw it away. Start it or stop it first.

        Desktop credentials do not survive this — see :attr:`vnc`.
        """
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
        return AsyncComputer(self._t, _api.computer_payload(data))

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
        self._data = _api.computer_payload(
            await self._t.json("PATCH", _api.computer(self.id), json=_api.rename_body(name))
        )
        return self

    async def delete(self) -> None:
        """Destroy this computer and its disk. Snapshots taken from it survive."""
        await self._t.request("DELETE", _api.computer(self.id))

    # --- readiness ------------------------------------------------------

    async def wait_until_built(self, timeout: float = 900.0, poll: float = 5.0) -> AsyncComputer:
        """Await until a cloned computer's disk has been copied.

        Returns immediately for anything not being built, so it is safe to call
        on any computer. Raises :class:`~mandala_computer.MandalaError` if the
        copy failed, and :class:`~mandala_computer.TimeoutError` if it is still
        going when ``timeout`` runs out — the computer keeps building either
        way; only the waiting stops.

        The default timeout is generous because the work is: a compressed
        conversion of a 40 GB Windows disk takes several minutes on a busy host.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self.build_failed:
                raise MandalaError(
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

    async def wait_until_running(self, timeout: float = 120.0, poll: float = 2.0) -> AsyncComputer:
        """Await until the machine is running.

        This is the *machine*, not the desktop: it returns as soon as the VM is
        up, while the guest OS is still booting. Use :meth:`wait_for_guest` when
        you need something inside the guest to be ready.

        Raises :class:`~mandala_computer.MandalaError` rather than waiting out
        the timeout for the two states that will not become "running" on their
        own — a failed build, and a suspended session nobody has resumed.
        """
        deadline = time.monotonic() + timeout
        while True:
            await self.refresh()
            if self.status == "running":
                return self
            # A computer with no disk will never start on its own, and waiting
            # out the full timeout to say so helps nobody.
            if self.build_failed:
                raise MandalaError(
                    f"{self.id} could not be built: {self.build_error or 'the disk copy failed'}"
                )
            # Nor will a suspended one. It is a state this wait predates, and
            # left to spin it reports a machine that is one call from running as
            # a timeout — the least informative answer available about the one
            # case the caller can fix in a line.
            if self.is_suspended:
                raise MandalaError(
                    f"{self.id} is suspended and will not start on its own: "
                    "call start() to resume it"
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
            except MandalaError:
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

        A screenshot is not *use* as far as the platform's idle sweep is
        concerned, and does not resume a suspended computer. A loop that only
        polls the screen can therefore watch its own machine be suspended out
        from under it after the host's idle window; anything that drives the
        desktop — :meth:`click`, :meth:`type`, :meth:`exec` — both counts as use
        and resumes it.
        """
        resp = await self._t.request(
            "GET",
            _api.computer_action(self.id, "screenshot"),
            params=_api.screenshot_params(width),
        )
        return resp.content

    # --- controlling ----------------------------------------------------

    async def _input(self, body: dict[str, Any]) -> Mapping[str, Any]:
        return await self._t.json("POST", _api.computer_action(self.id, "input"), json=body) or {}

    async def move(self, x: int, y: int) -> None:
        """Move the pointer to ``(x, y)`` in this computer's screen space.

        Coordinates are in the computer's own :attr:`resolution`, which is a
        create-time choice — not a fixed 1280x800.
        """
        await self._input(_api.pointer_body("move", x, y))

    async def click(self, x: int | None = None, y: int | None = None, *modifiers: str) -> None:
        """Click. With no coordinate, clicks wherever the pointer already is.

        ``modifiers`` are held down for the click, e.g.
        ``await click(100, 200, "shift")`` to extend a selection.
        """
        await self._input(_api.click_body("left_click", x, y, modifiers))

    async def right_click(
        self, x: int | None = None, y: int | None = None, *modifiers: str
    ) -> None:
        await self._input(_api.click_body("right_click", x, y, modifiers))

    async def middle_click(
        self, x: int | None = None, y: int | None = None, *modifiers: str
    ) -> None:
        await self._input(_api.click_body("middle_click", x, y, modifiers))

    async def double_click(
        self, x: int | None = None, y: int | None = None, *modifiers: str
    ) -> None:
        await self._input(_api.click_body("double_click", x, y, modifiers))

    async def triple_click(
        self, x: int | None = None, y: int | None = None, *modifiers: str
    ) -> None:
        """Three clicks, which is how most editors select a whole line."""
        await self._input(_api.click_body("triple_click", x, y, modifiers))

    async def drag(
        self, to_x: int, to_y: int, *, from_x: int | None = None, from_y: int | None = None
    ) -> None:
        """Press, move, release — one gesture.

        The pointer passes through intermediate positions, which is what makes
        this a drag rather than two clicks: text selection, canvas tools and
        drag-and-drop all watch for the motion between the ends.

        Without ``from_x``/``from_y`` the drag starts wherever the pointer is.
        That is refused if nothing has moved it yet, rather than guessing at an
        origin and selecting the wrong thing.
        """
        await self._input(_api.drag_body(from_x, from_y, to_x, to_y))

    async def mouse_down(self, x: int | None = None, y: int | None = None) -> None:
        """Press the left button and leave it down.

        Pair with :meth:`mouse_up`. Between the two the desktop is mid-gesture,
        so a call that raises in between leaves the button held — wrap them in
        ``try``/``finally`` if that matters.
        """
        await self._input(_api.button_body("left_mouse_down", x, y))

    async def mouse_up(self, x: int | None = None, y: int | None = None) -> None:
        """Release the left button."""
        await self._input(_api.button_body("left_mouse_up", x, y))

    async def scroll(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        direction: str = "down",
        amount: int = 3,
        modifiers: tuple[str, ...] = (),
    ) -> None:
        """Scroll the wheel, first moving to ``(x, y)`` when a point is given.

        With no coordinate it scrolls whatever is under the pointer, which is
        what a bare ``scroll()`` has always meant.

        ``direction`` is up, down, left or right. Horizontal scrolling needs a
        hypervisor running QEMU 7.1 or newer; an older one refuses it by name
        rather than scrolling the wrong way.
        """
        await self._input(_api.scroll_body(x, y, direction, amount, modifiers))

    async def type(self, text: str) -> None:
        """Type text as keystrokes.

        Characters with no key mapping are skipped rather than raising, so a
        stray emoji in a prompt cannot fail the whole call.
        """
        await self._input(_api.type_body(text))

    async def key(self, *keys: str) -> None:
        """Press a chord, e.g. ``await key("ctrl", "c")``.

        Both this SDK's names and X11 keysyms are accepted, so the spellings a
        computer-use model produces — ``Page_Down``, ``BackSpace``, ``period`` —
        work without translation. An unknown key raises and names itself rather
        than being silently dropped from the chord.
        """
        await self._input(_api.key_body(keys))

    async def hold_key(self, *keys: str, seconds: float) -> None:
        """Hold a chord down for ``seconds``, then release it.

        For the keys that mean something while held rather than when tapped — an
        arrow key that repeats, a modifier that changes what a UI shows.
        """
        await self._input(_api.hold_key_body(keys, seconds))

    async def wait(self, seconds: float) -> None:
        """Pause, inside the platform, without holding this computer's monitor.

        Sleeping locally does the same thing for a script. This exists because a
        computer-use model emits ``wait`` as an action, and because it does not
        block the screenshot polls of anything else watching the desktop.
        """
        await self._input(_api.wait_body(seconds))

    async def cursor_position(self) -> tuple[int, int] | None:
        """Where the pointer is, or ``None`` if nothing has placed it yet.

        This is where the *platform* last put the pointer. The virtual pointing
        device accepts coordinates and reports none back, so there is nothing to
        read from the guest: after a fresh boot, before anything has moved it,
        the honest answer is that nobody knows — hence ``None`` rather than a
        confident ``(0, 0)``.
        """
        return _cursor(await self._input(_api.cursor_body()))

    async def exec(self, command: str, timeout_s: int = 30, *, desktop: bool = False) -> ExecResult:
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

        Or call :meth:`open` and let the SDK write that line.
        """
        data = await self._t.json(
            "POST",
            _api.computer_action(self.id, "exec"),
            json=_api.exec_body(command, timeout_s, desktop),
        )
        return ExecResult.from_api(data or {})

    async def open(self, url: str, *, timeout_s: int = 30) -> ExecResult:
        """Open a URL in the guest's browser, on the screen::

            await c.open("https://example.com")

        Sugar over :meth:`exec` with ``desktop=True``: it names a browser that
        works on the image, quotes the URL, and detaches the launch so the call
        returns in well under a second instead of blocking until ``timeout_s``.

        The result describes the *launch*, not the page — a zero exit means the
        shell started the browser, not that the URL resolved. Take a
        :meth:`screenshot` to see what actually loaded.

        Raises ``ValueError`` for an empty URL or one starting with ``-``, which
        a browser would read as a flag rather than an address. On Windows the
        API rejects it, the same as any ``desktop=True`` exec.
        """
        return await self.exec(_api.open_url_command(url), timeout_s, desktop=True)

    # --- files ----------------------------------------------------------

    async def read_file(self, path: str) -> bytes:
        """Read one file out of the guest, as bytes.

        See :meth:`mandala_computer.Computer.read_file`.
        """
        resp = await self._t.request("GET", _api.files(self.id), params=_api.files_params(path))
        return resp.content

    async def write_file(self, path: str, data: bytes | str) -> None:
        """Write ``data`` to one file inside the guest, creating it if needed.

        See :meth:`mandala_computer.Computer.write_file`.
        """
        body = data.encode() if isinstance(data, str) else data
        await self._t.request(
            "PUT", _api.files(self.id), params=_api.files_params(path), content=body
        )

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
