"""The Computer handle — a cloud desktop and everything you can do to it."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from . import _api
from ._client import Transport
from ._exceptions import GorillaCloudError, TimeoutError
from ._models import ExecResult, Snapshot

__all__ = ["Computer"]

# What a computer renders at when its create did not ask for anything else.
#
# These were the guest's screen, full stop, until resolution became a create-time
# choice. They are the default now — still what every existing computer is, and
# still the right thing to assume about a server too old to report one — which is
# why they are kept rather than deleted: code that read them wants a number, and
# this is the number that was true and remains the fallback. For a computer in
# hand, read :attr:`Computer.resolution` instead; it is what coordinates are in.
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800
DEFAULT_RESOLUTION = f"{SCREEN_WIDTH}x{SCREEN_HEIGHT}x24"


def _cursor(res: Mapping[str, Any]) -> tuple[int, int] | None:
    """The pointer position out of an input response, if it is known.

    ``known`` is false on a computer whose pointer nothing has placed yet. It is
    checked rather than assumed because the coordinates are still present and
    still zero in that case, which is indistinguishable from the corner of the
    screen — the exact wrong answer to give a caller about to move relative to it.
    """
    if not res.get("known"):
        return None
    return int(res.get("x", 0)), int(res.get("y", 0))


# What wait_for_guest() runs to decide the guest is answering. A builtin of both
# bash and cmd.exe, so it works on either OS without asking which one this is —
# and keeps working on an image with nothing installed. ``true`` used to be the
# probe and silently made the wait Linux-only: cmd.exe has no such command, so
# on Windows it could only spin until it timed out.
GUEST_PROBE = "exit 0"


class ComputerFields:
    """Read-only accessors over a computer payload.

    Shared by the sync and async handles — the field names are the API contract
    and there is no reason for two copies of them.
    """

    _data: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self._data.get("id", ""))

    @property
    def name(self) -> str:
        return str(self._data.get("name", ""))

    @property
    def status(self) -> str:
        """State as of the last refresh.

        ``"running"`` or ``"stopped"`` for an ordinary computer. A computer
        made by cloning — from a snapshot or from another computer — starts as
        ``"building"`` while its disk is copied, and becomes ``"build-failed"``
        if that copy never finished. See :attr:`is_building`.
        """
        return str(self._data.get("status", ""))

    @property
    def is_building(self) -> bool:
        """True while this computer's disk is still being copied.

        A clone returns before its disk exists, because copying one can run for
        minutes. Until it lands there is nothing to boot, and starting,
        stopping, snapshotting or cloning it raises
        :class:`~gorillacloud.ConflictError`. Wait with
        :meth:`Computer.wait_until_built`.
        """
        return self.status == "building"

    @property
    def build_failed(self) -> bool:
        """True if this computer's disk copy never finished.

        The computer exists and is listed, and is holding whatever the copy got
        through, but it has no usable disk. Nothing will fix it on its own:
        delete it and clone again. :attr:`build_error` says what went wrong.
        """
        return self.status == "build-failed"

    @property
    def build_error(self) -> str:
        """Why the disk copy failed, or ``""`` if it did not.

        Empty is also what an older server returns, which reported that a build
        had failed without saying why.
        """
        build = self._data.get("build")
        if isinstance(build, Mapping):
            return str(build.get("failed") or "")
        return ""

    @property
    def os(self) -> str:
        return str(self._data.get("os", ""))

    @property
    def template(self) -> str:
        return str(self._data.get("template", ""))

    @property
    def cpu(self) -> int:
        return int(self._data.get("cpu", 0))

    @property
    def ram_mb(self) -> int:
        return int(self._data.get("ram_mb", 0))

    @property
    def disk_gb(self) -> int:
        return int(self._data.get("disk_gb", 0))

    @property
    def resolution(self) -> str:
        """The screen this computer renders at, as ``"WIDTHxHEIGHTxDEPTH"``.

        This is the coordinate space every pointer method and every screenshot
        is in. Read it rather than assuming 1280x800: since resolution became a
        create-time choice, assuming makes every click land proportionally short
        on any computer that asked for something else.

        Falls back to the default for a server old enough not to report one,
        which is what such a server's computers actually render at.
        """
        return str(self._data.get("resolution") or DEFAULT_RESOLUTION)

    @property
    def screen(self) -> tuple[int, int]:
        """:attr:`resolution` as ``(width, height)``, for arithmetic.

        Handy for the computer-use tool definition, which wants the two numbers
        separately — ``display_width_px``/``display_height_px`` have to equal
        what screenshots actually are or the model's coordinates are wrong.
        """
        parts = self.resolution.split("x")
        try:
            w, h = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            return SCREEN_WIDTH, SCREEN_HEIGHT
        return (w, h) if w > 0 and h > 0 else (SCREEN_WIDTH, SCREEN_HEIGHT)

    @property
    def created_at(self) -> str:
        return str(self._data.get("created_at", ""))

    @property
    def raw(self) -> Mapping[str, Any]:
        """The API response verbatim, including any fields this SDK predates."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id} {self.name!r} {self.status}>"


class Computer(ComputerFields):
    """A cloud desktop.

    Obtain one from :class:`gorillacloud.Client` — ``client.computers.create()``,
    ``.get()``, or ``.list()`` — rather than constructing it directly.
    """

    def __init__(self, transport: Transport, data: Mapping[str, Any]) -> None:
        self._t = transport
        self._data = dict(data)

    # --- lifecycle ------------------------------------------------------

    def refresh(self) -> Computer:
        """Re-read this computer's state from the API."""
        self._data = dict(self._t.json("GET", _api.computer(self.id)) or {})
        return self

    def start(self) -> Computer:
        self._t.request("POST", _api.computer_action(self.id, "start"))
        return self.refresh()

    def stop(self) -> Computer:
        self._t.request("POST", _api.computer_action(self.id, "stop"))
        return self.refresh()

    def restart(self) -> Computer:
        self._t.request("POST", _api.computer_action(self.id, "restart"))
        return self.refresh()

    def clone(self, name: str | None = None) -> Computer:
        """Copy this computer into a new one. The source must be stopped.

        Returns as soon as the new computer exists, which is before its disk
        does: copying a disk runs for minutes, so the clone comes back
        ``"building"`` and fills in behind you. Follow with
        :meth:`wait_until_built` before starting it.
        """
        data = self._t.json(
            "POST", _api.computer_action(self.id, "clone"), json=_api.name_body(name)
        )
        return Computer(self._t, data or {})

    def rename(self, name: str) -> Computer:
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
            self._t.json("PATCH", _api.computer(self.id), json=_api.rename_body(name)) or {}
        )
        return self

    def delete(self) -> None:
        """Destroy this computer and its disk. Snapshots taken from it survive."""
        self._t.request("DELETE", _api.computer(self.id))

    # --- readiness ------------------------------------------------------

    def wait_until_built(self, timeout: float = 900.0, poll: float = 5.0) -> Computer:
        """Block until a cloned computer's disk has been copied.

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
            time.sleep(poll)
            self.refresh()

    def wait_until_running(self, timeout: float = 120.0, poll: float = 2.0) -> Computer:
        """Block until the machine is running.

        This is the *machine*, not the desktop: it returns as soon as the VM is
        up, while the guest OS is still booting. Use :meth:`wait_for_guest` when
        you need something inside the guest to be ready.
        """
        deadline = time.monotonic() + timeout
        while True:
            self.refresh()
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
            time.sleep(poll)

    def wait_for_guest(self, timeout: float = 180.0, poll: float = 3.0) -> Computer:
        """Block until the guest OS answers, by running a trivial command in it.

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
                if self.exec(GUEST_PROBE, timeout_s=5).ok:
                    return self
            except GorillaCloudError:
                pass  # agent not up yet
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.id} guest did not respond within {timeout:g}s")
            time.sleep(poll)

    # --- observing ------------------------------------------------------

    def screenshot(self, width: int | None = None) -> bytes:
        """Capture the screen.

        Full-resolution PNG by default. Passing ``width`` returns a downscaled
        JPEG instead — much cheaper, and enough for a thumbnail or a quick
        "has anything changed" check.
        """
        resp = self._t.request(
            "GET",
            _api.computer_action(self.id, "screenshot"),
            params=_api.screenshot_params(width),
        )
        return resp.content

    # --- controlling ----------------------------------------------------

    def _input(self, body: dict[str, Any]) -> Mapping[str, Any]:
        return self._t.json("POST", _api.computer_action(self.id, "input"), json=body) or {}

    def move(self, x: int, y: int) -> None:
        """Move the pointer to ``(x, y)`` in this computer's screen space.

        Coordinates are in the computer's own :attr:`resolution`, which is a
        create-time choice — not a fixed 1280x800.
        """
        self._input(_api.pointer_body("move", x, y))

    def click(self, x: int | None = None, y: int | None = None, *modifiers: str) -> None:
        """Click. With no coordinate, clicks wherever the pointer already is.

        ``modifiers`` are held down for the click, e.g.
        ``click(100, 200, "shift")`` to extend a selection.
        """
        self._input(_api.click_body("left_click", x, y, modifiers))

    def right_click(self, x: int | None = None, y: int | None = None, *modifiers: str) -> None:
        self._input(_api.click_body("right_click", x, y, modifiers))

    def middle_click(self, x: int | None = None, y: int | None = None, *modifiers: str) -> None:
        self._input(_api.click_body("middle_click", x, y, modifiers))

    def double_click(self, x: int | None = None, y: int | None = None, *modifiers: str) -> None:
        self._input(_api.click_body("double_click", x, y, modifiers))

    def triple_click(self, x: int | None = None, y: int | None = None, *modifiers: str) -> None:
        """Three clicks, which is how most editors select a whole line."""
        self._input(_api.click_body("triple_click", x, y, modifiers))

    def drag(
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
        self._input(_api.drag_body(from_x, from_y, to_x, to_y))

    def mouse_down(self, x: int | None = None, y: int | None = None) -> None:
        """Press the left button and leave it down.

        Pair with :meth:`mouse_up`. Between the two the desktop is mid-gesture,
        so a call that raises in between leaves the button held — wrap them in
        ``try``/``finally`` if that matters.
        """
        self._input(_api.button_body("left_mouse_down", x, y))

    def mouse_up(self, x: int | None = None, y: int | None = None) -> None:
        """Release the left button."""
        self._input(_api.button_body("left_mouse_up", x, y))

    def scroll(
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
        self._input(_api.scroll_body(x, y, direction, amount, modifiers))

    def type(self, text: str) -> None:
        """Type text as keystrokes.

        Characters with no key mapping are skipped rather than raising, so a
        stray emoji in a prompt cannot fail the whole call.
        """
        self._input(_api.type_body(text))

    def key(self, *keys: str) -> None:
        """Press a chord, e.g. ``key("ctrl", "c")`` or ``key("Return")``.

        Both this SDK's names and X11 keysyms are accepted, so the spellings a
        computer-use model produces — ``Page_Down``, ``BackSpace``, ``period`` —
        work without translation. An unknown key raises and names itself rather
        than being silently dropped from the chord.
        """
        self._input(_api.key_body(keys))

    def hold_key(self, *keys: str, seconds: float) -> None:
        """Hold a chord down for ``seconds``, then release it.

        For the keys that mean something while held rather than when tapped — an
        arrow key that repeats, a modifier that changes what a UI shows.
        """
        self._input(_api.hold_key_body(keys, seconds))

    def wait(self, seconds: float) -> None:
        """Pause, inside the platform, without holding this computer's monitor.

        Sleeping locally does the same thing for a script. This exists because a
        computer-use model emits ``wait`` as an action, and because it does not
        block the screenshot polls of anything else watching the desktop.
        """
        self._input(_api.wait_body(seconds))

    def cursor_position(self) -> tuple[int, int] | None:
        """Where the pointer is, or ``None`` if nothing has placed it yet.

        This is where the *platform* last put the pointer. The virtual pointing
        device accepts coordinates and reports none back, so there is nothing to
        read from the guest: after a fresh boot, before anything has moved it,
        the honest answer is that nobody knows — hence ``None`` rather than a
        confident ``(0, 0)``.
        """
        return _cursor(self._input(_api.cursor_body()))

    def exec(self, command: str, timeout_s: int = 30, *, desktop: bool = False) -> ExecResult:
        """Run a shell command inside the guest.

        Uses the guest's native shell — bash on Linux, cmd.exe on Windows. A
        non-zero exit is returned, not raised; check :attr:`ExecResult.ok`.

        By default the command runs in the system context: as ``root`` on Linux,
        with no display attached. Pass ``desktop=True`` to run it in the logged-in
        desktop session instead — as the desktop user, with ``DISPLAY``, ``HOME``
        and ``XAUTHORITY`` set — which is what anything with a window needs.

        A GUI program does not exit on its own, so launch it detached or the call
        blocks until ``timeout_s`` kills it::

            c.exec("nohup firefox https://example.com >/dev/null 2>&1 &", desktop=True)

        Or call :meth:`open` and let the SDK write that line.
        """
        data = self._t.json(
            "POST",
            _api.computer_action(self.id, "exec"),
            json=_api.exec_body(command, timeout_s, desktop),
        )
        return ExecResult.from_api(data or {})

    def open(self, url: str, *, timeout_s: int = 30) -> ExecResult:
        """Open a URL in the guest's browser, on the screen::

            c.open("https://example.com")

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
        return self.exec(_api.open_url_command(url), timeout_s, desktop=True)

    # --- snapshots ------------------------------------------------------

    def snapshot(self, *, memory: bool = False) -> Snapshot:
        """Capture a snapshot of this computer.

        Works while it is running. ``memory=True`` also captures live RAM and
        device state, so a restore or fork resumes exactly where it was instead
        of booting — the computer must be running for that.
        """
        data = self._t.json(
            "POST",
            _api.computer_action(self.id, "snapshots"),
            json=_api.snapshot_body(memory),
        )
        return Snapshot.from_api(data or {})

    def snapshots(self) -> list[Snapshot]:
        """This computer's snapshots, in the order the API returns them."""
        data = self._t.json("GET", _api.SNAPSHOTS) or []
        return [Snapshot.from_api(s) for s in data if s.get("computer_id") == self.id]

    def schedule(self) -> Mapping[str, Any]:
        """The automatic daily snapshot schedule."""
        return self._t.json("GET", _api.computer_action(self.id, "schedule")) or {}

    def set_schedule(
        self,
        *,
        enabled: bool,
        hour: int = 4,
        minute: int = 0,
        tz: str = "UTC",
    ) -> Mapping[str, Any]:
        """Set the automatic daily snapshot window, in the given IANA timezone."""
        self._t.request(
            "PUT",
            _api.computer_action(self.id, "schedule"),
            json=_api.schedule_body(enabled=enabled, hour=hour, minute=minute, tz=tz),
        )
        return self.schedule()

    def clear_schedule(self) -> Mapping[str, Any]:
        """Remove the schedule, as distinct from disabling it.

        ``set_schedule(enabled=False)`` keeps the chosen time so toggling back on
        restores it, and keeps the scheduler's bookkeeping with it. Clearing
        returns the computer to never having had a schedule.
        """
        return self._t.json("DELETE", _api.computer_action(self.id, "schedule")) or {}
