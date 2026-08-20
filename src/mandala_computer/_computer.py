"""The Computer handle — a cloud desktop and everything you can do to it."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from . import _api
from ._client import DEADLINE_SLACK, FILE_TIMEOUT, Transport
from ._exceptions import (
    AuthenticationError,
    MandalaError,
    NotFoundError,
    PermissionDeniedError,
    PlanLimitError,
    TimeoutError,
)
from ._models import (
    ExecResult,
    ExecStatus,
    Listing,
    Snapshot,
    SnapshotHoldings,
    VncConnect,
    Window,
    WindowResult,
)

__all__ = ["BackgroundCommand", "Computer"]

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

#: Errors that no amount of waiting resolves, so :meth:`Computer.wait_for_guest`
#: re-raises them rather than polling through them. Everything else in the
#: hierarchy is either transient by definition (:class:`ConflictError`, which
#: the guest agent answers with in the first seconds of a start, and
#: :class:`UnavailableError`) or a 502 from an agent that has not spoken yet —
#: all of which are exactly what this method exists to wait out.
_FATAL_WHILE_WAITING = (
    AuthenticationError,
    PermissionDeniedError,
    NotFoundError,
    PlanLimitError,
)


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

        ``"running"`` or ``"stopped"`` for an ordinary computer, and
        ``"suspended"`` for one whose session has been written to disk — see
        :attr:`is_suspended`. A computer made by cloning — from a snapshot or
        from another computer — starts as ``"building"`` while its disk is
        copied, and becomes ``"build-failed"`` if that copy never finished. See
        :attr:`is_building`.
        """
        return str(self._data.get("status", ""))

    @property
    def is_suspended(self) -> bool:
        """True while this computer's RAM is on disk rather than in the host.

        A suspend is a pause, not a stop: the session is written down, the host
        gets its memory back, and the next :meth:`Computer.start` resumes the
        same processes and the same open windows in about a second rather than
        booting. :attr:`suspended_at` says when it was saved.

        A computer can arrive here without anyone asking. Its host suspends
        anything nobody has used for the host's idle window — 30 minutes by
        default — and input, exec and file transfers resume it automatically.
        Screenshots deliberately do not count as use and do not resume it, so a
        loop that only polls the screen can be suspended out from under itself.
        """
        return self.status == "suspended"

    @property
    def suspended_at(self) -> str:
        """When this computer's session was saved, or ``""`` if it is not saved.

        How old the desktop behind the suspend is, which is the one part of the
        platform's suspend record that is a caller's business — the rest of it
        describes the host's QEMU rather than this machine.
        """
        suspended = self._data.get("suspended")
        if isinstance(suspended, Mapping):
            return str(suspended.get("at") or "")
        return ""

    @property
    def start_error(self) -> str:
        """Why this computer was made but would not boot, or ``""``.

        Only ever set on the response to a create that asked for a running
        machine and got as far as building one. The computer exists and is
        billable, which is why the platform answers with it rather than with an
        error alone; it is simply stopped, and :meth:`Computer.start` may well
        work on a second attempt.

        Cleared by :meth:`Computer.refresh`, because it describes one start
        attempt rather than the machine.
        """
        return str(self._data.get("start_error") or "")

    @property
    def is_building(self) -> bool:
        """True while this computer's disk is still being copied.

        A clone returns before its disk exists, because copying one can run for
        minutes. Until it lands there is nothing to boot, and starting,
        stopping, snapshotting or cloning it raises
        :class:`~mandala_computer.ConflictError`. Wait with
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
    def idle_suspend_min(self) -> int | None:
        """Minutes this computer may sit untouched before its host suspends it.

        ``None`` on a computer with no override of its own, which follows
        whatever its host is sweeping at — 30 minutes at the time of writing.
        The host's number is deliberately not reported in its place: it is a
        property of the host and changes when an operator changes it, so
        answering with it would be this SDK asserting something about a machine
        it does not own. Set it with :meth:`Computer.set_idle_suspend`.
        """
        value = self._data.get("idle_suspend_min")
        return None if value is None else int(value)

    @property
    def workspace_id(self) -> str:
        """The workspace this computer is in, or ``""`` when it is in none.

        Not something a create can choose: the workspace comes from the API key,
        and a key scoped to one creates in it. This is how to tell which, for a
        key that is not.
        """
        return str(self._data.get("workspace_id", ""))

    @property
    def snapshot_schedule(self) -> Mapping[str, Any] | None:
        """This computer's automatic snapshot window, if it has one.

        The same shape :meth:`Computer.schedule` returns and ``None`` where that
        would answer an empty mapping — carried on the computer itself, so a
        caller that already holds one does not spend a second metered call to
        find out whether it snapshots itself. Read it here; change it with
        :meth:`Computer.set_schedule`.
        """
        value = self._data.get("snapshot_schedule")
        return dict(value) if isinstance(value, Mapping) else None

    @property
    def unreachable(self) -> bool:
        """True on a row served from the placement cache, with nothing else on it.

        Only ever seen in a listing taken with ``allow_partial=True``: the host
        holding this computer could not be reached, so what came back is its id
        and this flag. Every other field on such a row is absent, which means
        :attr:`status` reads ``""`` rather than anything true — check this
        before believing anything else here.
        """
        return bool(self._data.get("unreachable", False))

    @property
    def vnc(self) -> VncConnect | None:
        """Credentials and URLs for this computer's live desktop, or ``None``.

        What makes it possible to show somebody their own screen — in your page,
        not the platform's dashboard — without a second call. See
        :class:`~mandala_computer.VncConnect` for why there are two credentials.

        ``None`` on a computer that came from :meth:`Computers.list`, and that is
        the platform's decision rather than an omission: a desktop credential in
        every list response is a credential in every log line that ever captured
        one, whereas a caller holding a single machine is the caller about to
        connect to it. Every response that *is* one computer — a create, a clone,
        a :meth:`Computer.refresh`, a rename — carries it, so
        ``c.refresh().vnc`` is how a listed computer gets one.

        Also ``None`` when the platform could not reach the host holding this
        computer, since a URL built over a missing credential answers 401 forever
        rather than failing where it was built.
        """
        return VncConnect.from_api(self._data.get("vnc"))

    @property
    def raw(self) -> Mapping[str, Any]:
        """The API response verbatim, including any fields this SDK predates."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id} {self.name!r} {self.status}>"


class Computer(ComputerFields):
    """A cloud desktop.

    Obtain one from :class:`mandala_computer.Client` — ``client.computers.create()``,
    ``.get()``, or ``.list()`` — rather than constructing it directly.
    """

    def __init__(self, transport: Transport, data: Mapping[str, Any]) -> None:
        self._t = transport
        self._data = dict(data)

    # --- lifecycle ------------------------------------------------------

    def refresh(self) -> Computer:
        """Re-read this computer's state from the API.

        Also how a computer from :meth:`Computers.list` acquires a :attr:`vnc`
        connect surface, which the list deliberately omits.
        """
        self._data = _api.computer_payload(self._t.json("GET", _api.computer(self.id)))
        return self

    def start(self) -> Computer:
        """Start this computer, or resume it if its session was suspended.

        A suspended computer does not boot: its saved RAM is read back and the
        same processes and windows come up roughly a second later. An ordinary
        stopped computer boots as usual.
        """
        self._t.request("POST", _api.computer_action(self.id, "start"))
        return self.refresh()

    def stop(self, *, force: bool = False) -> Computer:
        """Stop this computer, discarding a suspended session if it has one.

        Use :meth:`suspend` to keep it.

        The guest is asked to shut down and given time to do it. ``force=True``
        skips the asking and pulls the power — what to reach for when a guest
        will not come down on its own, at the cost of whatever it had not
        written to disk.
        """
        self._t.request(
            "POST", _api.computer_action(self.id, "stop"), params=_api.stop_params(force)
        )
        return self.refresh()

    def suspend(self) -> Computer:
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
        self._t.request("POST", _api.computer_action(self.id, "suspend"))
        return self.refresh()

    def restart(self) -> Computer:
        """Reset this computer.

        Raises :class:`~mandala_computer.ConflictError` while a suspended session is
        saved, since a restart would have to guess whether you meant to resume
        that session or throw it away. Start it or stop it first.

        Desktop credentials do not survive this — see :attr:`vnc`.
        """
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
        return Computer(self._t, _api.computer_payload(data))

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
        self._data = _api.computer_payload(
            self._t.json("PATCH", _api.computer(self.id), json=_api.rename_body(name))
        )
        return self

    def resize(
        self, *, cpu: int | None = None, ram_mb: int | None = None, disk_gb: int | None = None
    ) -> Computer:
        """Give this computer a new shape, and return it resized.

        The computer must be **stopped** — the shape is what QEMU builds the
        machine from, so changing it on a running one raises
        :class:`~mandala_computer.ConflictError`. Disks grow only.

        Its own method rather than keywords on :meth:`rename`, because the
        platform refuses a resize in combination with a rename or an idle window
        and is right to: those two do not need the computer stopped and this
        does, so one request could not honour both without applying half of it.

        Sizing is capped by the account's plan; exceeding a cap raises
        :class:`~mandala_computer.PlanLimitError` naming the limit. The screen is
        not part of this — see :attr:`resolution`, which is fixed at create.
        """
        self._data = _api.computer_payload(
            self._t.json(
                "PATCH",
                _api.computer(self.id),
                json=_api.resize_body(cpu=cpu, ram_mb=ram_mb, disk_gb=disk_gb),
            )
        )
        return self

    def set_idle_suspend(self, minutes: int | None) -> Computer:
        """Set how long this computer may go untouched before it is suspended.

        ``None`` clears the override and returns it to its host's own sweep. See
        :attr:`idle_suspend_min` for why that is not the same as reading a
        number back.

        A suspend is a pause, not a stop — :meth:`start` resumes the same
        session in about a second — and input, exec and file transfers resume it
        automatically. Screenshots deliberately do not, so a loop that only
        polls the screen is the one thing this setting can surprise.
        """
        self._data = _api.computer_payload(
            self._t.json("PATCH", _api.computer(self.id), json=_api.idle_suspend_body(minutes))
        )
        return self

    def delete(self, *, purge_snapshots: bool = False, expect: str | None = None) -> int | None:
        """Destroy this computer and its disk.

        Snapshots taken from it **survive by default** and become orphans, which
        can still be cloned into a new computer but can no longer be restored —
        a restore puts the disk back on a source that no longer exists.

        ``purge_snapshots=True`` destroys them with it, and needs ``expect``: the
        fingerprint from :meth:`snapshot_holdings`, which binds the sweep to the
        set you were actually shown. Read the holdings, check the count and the
        size are what you meant to destroy, then pass the fingerprint you read::

            held = c.snapshot_holdings()
            if held.count == 2:
                c.delete(purge_snapshots=True, expect=held.fingerprint)

        Do not fetch the fingerprint on the line above the delete. That binds
        the purge to whatever the set is now rather than to what anyone agreed
        to, which is precisely the race the interlock exists for: a capture that
        finishes between the decision and the call, then gets destroyed by a
        confirmation that predates it. A fingerprint that has gone stale raises
        :class:`~mandala_computer.ConflictError` and destroys nothing.

        Returns how many snapshots went with it, or ``None`` when the platform
        did not say. ``None`` rather than ``0``: this is the one irreversible
        call on this object, and reporting "nothing was destroyed" because the
        server was quiet is the one wrong answer worth going out of the way to
        avoid.
        """
        data = self._t.json(
            "DELETE",
            _api.computer(self.id),
            params=_api.delete_params(purge_snapshots=purge_snapshots, expect=expect),
        )
        if not isinstance(data, Mapping):
            return None
        deleted = data.get("snapshots_deleted")
        return None if deleted is None else int(deleted)

    # --- readiness ------------------------------------------------------

    def wait_until_built(self, timeout: float = 900.0, poll: float = 5.0) -> Computer:
        """Block until a cloned computer's disk has been copied.

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
            time.sleep(poll)
            self.refresh()

    def wait_until_running(self, timeout: float = 120.0, poll: float = 2.0) -> Computer:
        """Block until the machine is running.

        This is the *machine*, not the desktop: it returns as soon as the VM is
        up, while the guest OS is still booting. Use :meth:`wait_for_guest` when
        you need something inside the guest to be ready.

        Raises :class:`~mandala_computer.MandalaError` rather than waiting out
        the timeout for the two states that will not become "running" on their
        own — a failed build, and a suspended session nobody has resumed.
        """
        deadline = time.monotonic() + timeout
        while True:
            self.refresh()
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

        What it does not wait through is a refusal that will never clear: a revoked
        key, a computer that is not there, an account that is not allowed, a
        plan that does not cover this. Those are raised at once. Waiting on one
        of them costs the full timeout and then reports "the guest did not
        respond", which is both wrong and the least useful thing this method
        could say about a 401.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.exec(GUEST_PROBE, timeout_s=5).ok:
                    return self
            except _FATAL_WHILE_WAITING:
                raise
            except MandalaError:
                pass  # agent not up yet
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.id} guest did not respond within {timeout:g}s")
            time.sleep(poll)

    # --- observing ------------------------------------------------------

    def screenshot(self, width: int | None = None, *, fresh: bool = False) -> bytes:
        """Capture the screen.

        Full-resolution PNG by default. Passing ``width`` returns a downscaled
        JPEG instead — much cheaper, and enough for a thumbnail or a quick
        "has anything changed" check.

        PASS ``fresh=True`` WHENEVER THE IMAGE IS FEEDING A DECISION. Without it
        the platform may answer from a frame up to 1.5 seconds old, which is
        fine for a thumbnail and wrong for a drive loop: a model shown the
        screen from before its own click concludes the click missed and clicks
        again, and the second one lands on whatever the first one opened.

        A screenshot is not *use* as far as the platform's idle sweep is
        concerned, and does not resume a suspended computer. A loop that only
        polls the screen can therefore watch its own machine be suspended out
        from under it after the host's idle window; anything that drives the
        desktop — :meth:`click`, :meth:`type`, :meth:`exec` — both counts as use
        and resumes it.
        """
        resp = self._t.request(
            "GET",
            _api.computer_action(self.id, "screenshot"),
            params=_api.screenshot_params(width, fresh),
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

    def exec(
        self,
        command: str,
        timeout_s: int = 30,
        *,
        desktop: bool = False,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
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

            c.exec("nohup firefox https://example.com >/dev/null 2>&1 &", desktop=True)

        Or call :meth:`open` and let the SDK write that line.

        ``cwd`` is an absolute path inside the guest and ``env`` is extra
        environment for this command alone. A command slower than a few seconds
        wants :meth:`start_exec` instead: hitting ``timeout_s`` means this call
        stopped waiting, not that the work was destroyed, and the output and the
        exit code are lost with the request.

        The transport waits out whatever ``timeout_s`` asks for. There is no
        ceiling on it here or on the platform, which extends its own deadline to
        match, so the HTTP budget is derived from it rather than left at the
        client default that would otherwise cut a long command short.
        """
        data = self._t.json(
            "POST",
            _api.computer_action(self.id, "exec"),
            json=_api.exec_body(command, timeout_s, desktop, cwd=cwd, env=env),
            timeout=timeout_s + DEADLINE_SLACK,
        )
        return ExecResult.from_api(data or {})

    def start_exec(
        self,
        command: str,
        *,
        desktop: bool = False,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> BackgroundCommand:
        """Start a command that outlives the request, and return a handle to it.

        For builds, installers, test suites and servers — anything slower than
        the request it would otherwise be waiting inside. Strictly better than
        backgrounding with ``&`` in :meth:`exec`, which throws away both the exit
        code and the output::

            job = c.start_exec("apt-get install -y build-essential")
            while True:
                status = job.poll()
                print(status.stdout, end="")
                if status.done and not status.more:
                    break
                if not status.more:
                    time.sleep(2)

        The handle is the guest pid. It survives this process — a later session
        can rebuild one with :meth:`background_command` — but not a restart of
        the computer, and only commands this API started can be read back.
        """
        data = self._t.json(
            "POST",
            _api.computer_action(self.id, "exec"),
            json=_api.exec_body(command, 0, desktop, background=True, cwd=cwd, env=env),
        )
        return BackgroundCommand(self._t, self.id, data or {})

    def background_command(self, pid: int) -> BackgroundCommand:
        """A handle onto a command :meth:`start_exec` started earlier.

        For picking up a pid carried across a process boundary — a job id in a
        queue, a build started by the run before this one. Makes no request, so
        it does not verify the pid: the first :meth:`BackgroundCommand.poll`
        raises :class:`~mandala_computer.NotFoundError` if the daemon has no
        such handle.
        """
        return BackgroundCommand(self._t, self.id, {"pid": pid})

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

    # --- files ----------------------------------------------------------

    def read_file(self, path: str) -> bytes:
        """Read one file out of the guest, as bytes.

        ``path`` is absolute, inside the guest — there is no shell and no
        working directory behind this, so a relative path is refused before the
        request is made. Works while the computer is running or suspended
        (a transfer resumes a suspended computer, like any other use).
        """
        resp = self._t.request(
            "GET", _api.files(self.id), params=_api.files_params(path), timeout=FILE_TIMEOUT
        )
        return resp.content

    def write_file(self, path: str, data: bytes | str) -> None:
        """Write ``data`` to one file inside the guest, creating it if needed.

        A ``str`` is written as UTF-8. The path rules are :meth:`read_file`'s.
        The bytes land exactly as given — this is how a credential reaches a
        guest ``.env`` without echoing it through a shell command line.
        """
        body = data.encode() if isinstance(data, str) else data
        self._t.request(
            "PUT",
            _api.files(self.id),
            params=_api.files_params(path),
            content=body,
            timeout=FILE_TIMEOUT,
        )

    # --- windows --------------------------------------------------------

    def windows(self, *, include_all: bool = False) -> list[Window]:
        """What is on the desktop, as a list rather than a picture.

        A screenshot says what the desktop looks like; this says what any of it
        is — which is how a browser that failed to launch is told apart from one
        that has not painted yet, without asking a model to find it in a PNG.

        ``include_all`` keeps the desktop's own furniture: panels, docks and the
        wallpaper window. Off by default because a stock guest showing one
        terminal has five windows, four of which are not applications.

        Linux only.
        """
        data = self._t.json(
            "GET",
            _api.computer_action(self.id, "windows"),
            params=_api.windows_params(include_all),
        )
        rows = data.get("windows") if isinstance(data, Mapping) else None
        return [Window.from_api(w) for w in rows or []]

    def window_action(
        self,
        window_id: str,
        action: str,
        *,
        x: int | None = None,
        y: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> WindowResult:
        """Act on one window — focus, raise, minimize, maximize, unmaximize,
        close, move or resize.

        ``window_id`` comes from :meth:`windows`. ``x``/``y`` are for ``move``
        and ``width``/``height`` for ``resize``; each pair goes together or not
        at all.

        Prefer ``focus`` over ``raise``. Raising without focusing gives a window
        that is visibly in front and silently not receiving keystrokes, which
        looks in a screenshot exactly like one that is.

        The result is the window *as it now is* rather than an acknowledgement —
        see :class:`~mandala_computer.WindowResult`, and believe it rather than
        the request.
        """
        data = self._t.json(
            "POST",
            _api.window(self.id, window_id),
            json=_api.window_body(action, x=x, y=y, width=width, height=height),
        )
        return WindowResult.from_api(data if isinstance(data, Mapping) else {})

    # --- snapshots ------------------------------------------------------

    def snapshot(self, *, memory: bool = False, name: str | None = None) -> Snapshot:
        """Capture a snapshot of this computer.

        Works while it is running. ``memory=True`` also captures live RAM and
        device state, so a restore or fork resumes exactly where it was instead
        of booting — the computer must be running for that. An omitted ``name``
        asks the platform to generate one.
        """
        data = self._t.json(
            "POST",
            _api.computer_action(self.id, "snapshots"),
            json=_api.snapshot_body(memory, name),
        )
        return Snapshot.from_api(data or {})

    def snapshots(
        self, *, include_unfinished: bool = False, allow_partial: bool = False
    ) -> Listing[Snapshot]:
        """This computer's snapshots, in the order the API returns them.

        One account-wide read and a filter, which is what listing one computer's
        snapshots has always been — ``GET /computers/{id}/snapshots`` is not a
        narrower version of this. It answers a count, a byte total and a
        fingerprint, and never the snapshots themselves; see
        :meth:`snapshot_holdings`.

        ``allow_partial`` matters more here than it looks. Without it a short
        inventory is a 503, so this filter can never quietly narrow one; with
        it, a short list arrives as an ordinary 200 and the only thing saying so
        is the returned :class:`~mandala_computer.Listing`. Rows the platform
        could not read are kept rather than filtered out, even though they
        cannot be attributed to this computer — they are the markers that say
        something is missing, and dropping them would turn "some hosts did not
        answer" into a confident wrong number about one machine.
        """
        data, incomplete = self._t.listing(
            _api.SNAPSHOTS,
            params=_api.snapshot_listing_params(
                include_unfinished=include_unfinished, allow_partial=allow_partial
            ),
        )
        rows = [
            Snapshot.from_api(s)
            for s in data or []
            if s.get("computer_id") == self.id or s.get("unreachable")
        ]
        return Listing.of(rows, incomplete)

    def snapshot_holdings(self) -> SnapshotHoldings:
        """How many snapshots this computer has, what they weigh, and their
        fingerprint.

        Not a listing — that is :meth:`snapshots`, and the two routes answer
        different shapes deliberately. Read this before an irreversible delete:
        the fingerprint is the only interlock on a purge, and it is not
        something a caller can compute from a listing. See :meth:`delete`.
        """
        data = self._t.json("GET", _api.computer_action(self.id, "snapshots"))
        return SnapshotHoldings.from_api(data if isinstance(data, Mapping) else {})

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
        """Set the automatic daily snapshot window, in the given IANA timezone.

        Returns the schedule as stored, out of the PUT's own answer. A follow-up
        GET would cost a second metered round trip to report a *re-read* rather
        than what this call stored — so a change that landed in between would
        come back looking like yours. :meth:`clear_schedule` reads its own
        answer for the same reason, as do :meth:`rename` and :meth:`resize`.
        """
        return (
            self._t.json(
                "PUT",
                _api.computer_action(self.id, "schedule"),
                json=_api.schedule_body(enabled=enabled, hour=hour, minute=minute, tz=tz),
            )
            or {}
        )

    def clear_schedule(self) -> Mapping[str, Any]:
        """Remove the schedule, as distinct from disabling it.

        ``set_schedule(enabled=False)`` keeps the chosen time so toggling back on
        restores it, and keeps the scheduler's bookkeeping with it. Clearing
        returns the computer to never having had a schedule.
        """
        return self._t.json("DELETE", _api.computer_action(self.id, "schedule")) or {}


class BackgroundCommandFields:
    """Read-only accessors over a background command's handle.

    Shared by the sync and async handles, for the reason
    :class:`ComputerFields` is: the field names are the API contract and two
    copies of them is two chances to disagree.
    """

    _data: dict[str, Any]

    @property
    def pid(self) -> int:
        """The guest pid, which is this command's identity on the API."""
        return int(self._data.get("pid", 0))

    @property
    def command(self) -> str:
        """The command line, echoed back by the platform."""
        return str(self._data.get("command", ""))

    @property
    def started_at(self) -> str:
        return str(self._data.get("started_at", ""))

    @property
    def raw(self) -> Mapping[str, Any]:
        """The handle response verbatim, from whichever call produced it."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} pid={self.pid} {self.command!r}>"


class BackgroundCommand(BackgroundCommandFields):
    """A command running inside a guest, outliving the request that started it.

    Obtain one from :meth:`Computer.start_exec`, or rebuild one around a pid you
    already have with :meth:`Computer.background_command`.

    Reads are consuming — see :class:`~mandala_computer.ExecStatus` — so one
    handle per command, polled in one place. Two pollers on one pid split the
    output between them and neither sees all of it.
    """

    def __init__(self, transport: Transport, computer_id: str, data: Mapping[str, Any]) -> None:
        self._t = transport
        self._computer_id = computer_id
        self._data = dict(data)

    def poll(self) -> ExecStatus:
        """Read what it has printed since the last poll, and whether it is done.

        Each call advances the daemon's cursor, so what comes back is only the
        new bytes and dropping them drops them for good. Poll again immediately
        while :attr:`~mandala_computer.ExecStatus.more` is set; there is output
        waiting, and sleeping on it only makes the next read bigger.
        """
        data = self._t.json("GET", _api.exec_handle(self._computer_id, self.pid))
        return ExecStatus.from_api(data if isinstance(data, Mapping) else {})

    def kill(self) -> ExecStatus:
        """Stop it, and everything it started.

        Answers with its final state, including whatever it printed that had not
        been read — so this is a way to end a command and collect its tail in
        one call, not only a way to abandon one.
        """
        data = self._t.json("DELETE", _api.exec_handle(self._computer_id, self.pid))
        return ExecStatus.from_api(data if isinstance(data, Mapping) else {})
