"""The Computer handle — a cloud desktop and everything you can do to it."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from typing import IO, Any

from . import _api
from ._agent import (
    AgentDone,
    AgentEvent,
    AgentFailed,
    AgentResult,
    AgentStepEvent,
    to_agent_event,
)
from ._client import (
    DEADLINE_SLACK,
    FILE_PART_SIZE,
    FILE_SIZE_LIMIT,
    FILE_TIMEOUT,
    MODEL_KEY_HEADER,
    NO_DEADLINE,
    Transport,
    error_for_status,
)
from ._exceptions import (
    AuthenticationError,
    MandalaError,
    NotFoundError,
    OriginTLSError,
    PermissionDeniedError,
    PlanLimitError,
    RangeNotSatisfiableError,
    RateLimitError,
    TimeoutError,
)
from ._models import (
    ExecResult,
    ExecStatus,
    FilePart,
    Listing,
    Move,
    Snapshot,
    SnapshotHoldings,
    VncConnect,
    Window,
    WindowResult,
    _num,
    _Wire,
    _wire,
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


def _require_model_key(model_key: str) -> None:
    """The agent routes need the caller's own Anthropic key, and only theirs.

    Refused here rather than sent empty because the failure is otherwise a 401
    on a route where a 401 reads as "your Mandala key is wrong" — which is the
    one thing it does not mean. The platform stores no model key and will not
    fall back to one.
    """
    if not model_key or not model_key.strip():
        raise MandalaError(
            "the agent needs your own Anthropic API key as model_key — "
            "the platform does not store one, and never bills you for it."
        )


def _agent_outcome(result: AgentResult | None, failure: AgentFailed | None) -> AgentResult:
    """What a finished stream comes to, once its events are in.

    The failure is checked first. A run can report one after a result — a
    connection lost while the tail of the stream was in flight — and the last
    word about a run that went wrong should not be a result that arrived before
    it did.

    The failure itself rides on the exception as
    :attr:`~mandala_computer.MandalaError.agent`. Raising is what a caller who
    is not reading the stream asked for, but the steps it had already taken are
    still on the desktop and their cost is already on the caller's model key, so
    the raise says how far it got rather than only that it stopped.
    """
    if failure is not None:
        n = len(failure.steps)
        taken = f" after {n} step{'' if n == 1 else 's'}" if n else ""
        message = f"the agent run failed{taken}: {failure.error}"
        error = (
            error_for_status(failure.status, message) if failure.status else MandalaError(message)
        )
        error.agent = failure
        raise error
    if result is None:
        raise MandalaError("the agent stream ended without a result")
    return result


def _agent_once_outcome(data: Mapping[str, Any]) -> AgentResult:
    """Map a non-streaming body through the streaming failure contract."""
    if "error" not in data:
        return AgentResult.from_api(data)
    failure = to_agent_event("error", data, 0)
    if not isinstance(failure, AgentFailed):  # defensive: the converter owns this shape
        raise MandalaError("the agent run failed")
    return _agent_outcome(None, failure)


def _file_body(data: bytes | str) -> bytes:
    body = data.encode() if isinstance(data, str) else data
    if len(body) > FILE_SIZE_LIMIT:
        raise ValueError(f"file data may not exceed {FILE_SIZE_LIMIT // (1024 * 1024)} MiB")
    return body


@contextmanager
def _download_sink(dest: str | os.PathLike[str] | IO[bytes]) -> Iterator[IO[bytes]]:
    """Where a download's bytes go, whether it was named a path or handed a file.

    A file this opened is one nothing else can close, so it is closed here. One
    the caller opened is left alone — closing it would end a transfer they may
    have meant to go on writing to, and a download is not the owner of somebody
    else's handle.
    """
    if isinstance(dest, (str, os.PathLike)):
        with open(dest, "wb") as f:
            yield f
        return
    yield dest


def _write_all(sink: IO[bytes], data: bytes) -> None:
    """Write one downloaded part completely before fetching the next one.

    Binary streams may accept fewer bytes than they were handed. Advancing the
    remote range after such a write would silently leave a hole in the local
    file, while counting the whole part as written. A write that accepts
    nothing cannot be retried safely here — a nonblocking sink needs its caller
    to arrange readiness — so it is reported rather than spun on forever.
    """
    view = memoryview(data)
    while view:
        written = sink.write(view)
        if written is None or written <= 0:
            raise OSError("download destination write made no progress")
        if written > len(view):
            raise OSError(
                f"download destination reported writing {written} bytes from "
                f"a {len(view)}-byte buffer"
            )
        view = view[written:]


def _empty_guest_file(exc: RangeNotSatisfiableError) -> bool:
    """Whether a refused range is really a file with nothing in it.

    An empty file has no byte at any position, so it refuses *every* window —
    including the first one a download asks for. That is the platform being
    consistent rather than a failure, and the zero length it puts on the refusal
    is what says so.

    Only ever asked of the FIRST window, which is why the offset is not a
    parameter. A range refused later in a download is a file that shrank while
    it was being read, and reading that as an ending would hand back a truncated
    file with nothing raised — the silent truncation every ``Content-Range`` on
    this path exists to prevent.
    """
    return exc.size == 0


def _continues(path: str, asked_from: int, part: FilePart, total_was: int | None) -> None:
    """Refuse a window that does not continue the download it arrived for.

    The loop's own invariant, and it has to be the loop's: neither of these is
    visible inside one response. A window is only wrong relative to the one that
    should have come, and a length is only wrong relative to the one before it.

    **It has to start where it was asked to.** A range anchored at the start
    keeps its start — only its far end is ever trimmed — so a window beginning
    anywhere else is not the one that was asked for. Appending it would put
    foreign bytes at a position nothing downstream would ever check, and where
    the same window comes back every time (a cache in front of the platform, a
    hop that drops the header) the loop would never end either. Asked of the
    first window too, where the offset is zero: an opening window that starts
    elsewhere writes the middle of the file over its beginning, and is no more
    obviously wrong for having gone first.

    **The file must not have got shorter.** Growing is followed and shrinking is
    not, and the asymmetry is the point rather than an inconsistency. Bytes
    appended to a file leave the ones already read exactly where they were, so a
    download that follows the new end is still one file. A file that got shorter
    was rewritten or truncated, which means the earlier windows came from
    something that no longer exists — and finishing would hand back two files
    spliced at whatever offset the change happened to land on, with a byte count
    that looks entirely reasonable.
    """
    if part.offset != asked_from:
        raise MandalaError(
            f"{path}: asked for the window at offset {asked_from} and got the one at "
            f"{part.offset}. Appending that would put the wrong bytes at the wrong "
            "place, and asking again would ask the same question."
        )
    if total_was is not None and part.total is not None and part.total < total_was:
        raise MandalaError(
            f"{path}: was {total_was} bytes and is {part.total} part-way through being "
            "read. What has already been written came from a file that no longer "
            "exists, so going on would splice two of them together."
        )


def _cursor(res: Mapping[str, Any]) -> tuple[int, int] | None:
    """The pointer position out of an input response, if it is known.

    ``known`` is false on a computer whose pointer nothing has placed yet. It is
    checked rather than assumed because the coordinates are still present and
    still zero in that case, which is indistinguishable from the corner of the
    screen — the exact wrong answer to give a caller about to move relative to it.
    """
    if not res.get("known"):
        return None
    x, y = res.get("x"), res.get("y")
    # known=true with a missing or unusable coordinate is the same as unknown:
    # coercing null to zero would report the corner of the screen.
    if x is None or y is None:
        return None
    try:
        return int(x), int(y)
    except (TypeError, ValueError):
        return None


def _windows_from_response(data: Mapping[str, Any]) -> list[Window]:
    """Validate the embedded windows collection before building its rows."""
    rows = data.get("windows")
    if rows is None:
        return []
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise MandalaError(
            "GET windows answered with a windows field that is not an array of objects"
        )
    return [Window.from_api(row) for row in rows]


def _snapshots_deleted(data: Mapping[str, Any]) -> int | None:
    """The delete count, with malformed success payloads kept inside the SDK error family."""
    deleted = data.get("snapshots_deleted")
    if deleted is None:
        return None
    # bool is an int subclass, so int(True) == 1 would report one snapshot gone.
    if isinstance(deleted, bool):
        raise MandalaError("DELETE computer answered with an invalid snapshots_deleted count")
    try:
        return int(deleted)
    except (TypeError, ValueError) as exc:
        raise MandalaError(
            "DELETE computer answered with an invalid snapshots_deleted count"
        ) from exc


# What wait_for_guest() runs to decide the guest is answering. A builtin of both
# bash and cmd.exe, so it works on either OS without asking which one this is —
# and keeps working on an image with nothing installed. ``true`` used to be the
# probe and silently made the wait Linux-only: cmd.exe has no such command, so
# on Windows it could only spin until it timed out.
GUEST_PROBE = "exit 0"


def _require_background_pid(data: Mapping[str, Any]) -> None:
    """Reject a successful start response that cannot identify its command."""
    raw = data.get("pid")
    try:
        pid = 0 if raw is None else int(raw)
    except (TypeError, ValueError):
        pid = 0
    if isinstance(raw, bool) or pid <= 0:
        raise MandalaError("exec start answered without a positive pid")


#: Errors that :meth:`Computer.wait_for_guest` must not hide. Most cannot be
#: resolved by waiting; a rate limit can, but only on the server's retry cadence,
#: which this helper must preserve for its caller rather than replacing with its
#: own shorter poll.
#:
#: Everything else in the
#: hierarchy is either transient by definition (:class:`ConflictError`, which
#: the guest agent answers with in the first seconds of a start, and
#: :class:`UnavailableError`) or a 502 from an agent that has not spoken yet —
#: all of which are exactly what this method exists to wait out.
_FATAL_WHILE_WAITING = (
    AuthenticationError,
    PermissionDeniedError,
    NotFoundError,
    PlanLimitError,
    RateLimitError,
    # A certificate the edge and the platform cannot agree on fails identically
    # on every retry, so waiting one out spends the whole timeout to report "the
    # guest did not respond" — the wrong cause, the wrong class, and three
    # minutes, about a deployment somebody has to go and fix. Its own message
    # says to report it rather than wait it out; this is what makes that true.
    OriginTLSError,
)


def _is_unreachable_stub(row: Mapping[str, Any]) -> bool:
    """Whether a snapshot row is a placeholder for one nobody could read.

    ROW SHAPE, not the flag alone, and that is why this is not a decoder call
    (adversarial review, OPL-3835). ``unreachable`` means opposite things on the
    two rows it can appear on: on a SPARSE row — an id and the flag, no
    ``computer_id``, because there was no daemon to say which computer it
    belonged to — it is the marker saying this listing is short, and dropping it
    reports a confident count over an incomplete answer. On a FULL row belonging
    to another computer, admitting it on an unreadable flag hands back somebody
    else's snapshots from a method read before an irreversible delete.

    So an unreadable flag is believed only where the row could not be anything
    else, and the two failures a single fallback boolean had to choose between
    both go away.
    """
    if row.get("computer_id"):
        return False
    said = _wire(row, "unreachable")
    return said is _Wire.TRUE or said in (_Wire.NULL, _Wire.MALFORMED)


class ComputerFields:
    """Read-only accessors over a computer payload.

    Shared by the sync and async handles — the field names are the API contract
    and there is no reason for two copies of them.
    """

    _data: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self._data.get("id") or "")

    @property
    def name(self) -> str:
        return str(self._data.get("name") or "")

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
        return str(self._data.get("status") or "")

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

    def _guest_wait_failure(self) -> MandalaError | None:
        """A cached lifecycle state from which a guest probe cannot recover."""
        if self.build_failed:
            return MandalaError(
                f"{self.id} could not be built: {self.build_error or 'the disk copy failed'}"
            )
        if self.start_error:
            return MandalaError(f"{self.id} did not start: {self.start_error}")
        if self.status == "stopped":
            return MandalaError(
                f"{self.id} is stopped and its guest cannot answer: call start() first"
            )
        # A suspended computer is deliberately allowed through. Exec is use,
        # and use resumes a suspended session, so the probe wakes it itself.
        return None

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
        return str(self._data.get("os") or "")

    @property
    def template(self) -> str:
        return str(self._data.get("template") or "")

    @property
    def cpu(self) -> int:
        return _num(self._data.get("cpu"))

    @property
    def ram_mb(self) -> int:
        return _num(self._data.get("ram_mb"))

    @property
    def disk_gb(self) -> int:
        return _num(self._data.get("disk_gb"))

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

        Raises :class:`ValueError` if a server reports a malformed resolution;
        only an absent resolution means the legacy default.
        """
        resolution = self.resolution
        parts = resolution.lower().split("x")
        try:
            if len(parts) not in (2, 3) or any(not part for part in parts):
                raise ValueError
            values = [int(part) for part in parts]
            if any(value <= 0 for value in values):
                raise ValueError
        except ValueError:
            raise ValueError(f"invalid computer resolution {resolution!r}") from None
        return values[0], values[1]

    @property
    def created_at(self) -> str:
        return str(self._data.get("created_at") or "")

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
        if value is None:
            return None
        # 0 is a real setting ("never suspend"); unusable wire must not become 0.
        if isinstance(value, bool):
            raise MandalaError("computer answered with an invalid idle_suspend_min")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise MandalaError("computer answered with an invalid idle_suspend_min") from exc

    @property
    def workspace_id(self) -> str:
        """The workspace this computer is in, or ``""`` when it is in none.

        Not something a create can choose: the workspace comes from the API key,
        and a key scoped to one creates in it. This is how to tell which, for a
        key that is not.
        """
        return str(self._data.get("workspace_id") or "")

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
        if not isinstance(value, Mapping) or not value:
            return None
        return dict(value)

    @property
    def unreachable(self) -> bool:
        """True on a row served from the placement cache, with nothing else on it.

        Only ever seen in a listing taken with ``allow_partial=True``: the host
        holding this computer could not be reached, so what came back is its id
        and this flag. Every other field on such a row is absent, which means
        :attr:`status` reads ``""`` rather than anything true — check this
        before believing anything else here.
        """
        # Row shape decides an unreadable flag, the same way it does for a
        # snapshot stub (adversarial review, OPL-3835). A stub has an id and this
        # and nothing else, so an empty `status` is what tells them apart: on a
        # FULL payload an unreadable flag must not make a healthy computer report
        # itself a placeholder and have callers stop believing valid data, and on
        # a sparse one it must not report a reachable computer we never heard
        # from.
        said = _wire(self._data, "unreachable")
        if said is _Wire.MALFORMED:
            return not self._data.get("status")
        return said is _Wire.TRUE

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

    def _my_move(self, listing: Mapping[str, Any]) -> Move | None:
        """This computer's move out of the account's listing, if it has one.

        On ``ComputerFields`` because both halves need exactly this and the only
        thing that differs between them is how the listing was fetched. The
        filter is the point: ``GET /moves`` is account-wide, one move runs at a
        time, and a FINISHED row for another computer stays for a day — so "the
        first row" is the wrong answer often enough to matter.
        """
        rows = listing.get("moves")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, Mapping) and row.get("computer_id") == self.id:
                return Move.from_api(row)
        return None

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
        return self._refresh()

    def _refresh(self, *, timeout_cap: float | None = None) -> Computer:
        """Refresh with an optional cap used by deadline-bound wait helpers."""
        self._data = _api.computer_payload(
            self._t.json_object("GET", _api.computer(self.id), timeout_cap=timeout_cap)
        )
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
        data = self._t.json_object(
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
            self._t.json_object("PATCH", _api.computer(self.id), json=_api.rename_body(name))
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
            self._t.json_object(
                "PATCH",
                _api.computer(self.id),
                json=_api.resize_body(cpu=cpu, ram_mb=ram_mb, disk_gb=disk_gb),
            )
        )
        return self

    def relocate(self, *, ram_mb: int, cpu: int | None = None, disk_gb: int | None = None) -> Move:
        """Move this computer to another host in its region, so a resize its
        current host cannot run becomes possible.

        THE SECOND HALF OF A REFUSED RESIZE, and only ever that. :meth:`resize`
        raises :class:`~mandala_computer.MoveRequiredError` when the size asked
        for is more RAM than the host this computer is on can run;
        ``move_possible`` on that exception says whether anywhere in the region
        can run it, and this is how you agree to go there. Calling it without
        having been refused first is an operation nobody needed: a size that fits
        where the computer already is is answered with a
        :class:`~mandala_computer.ConflictError` rather than a pointless
        multi-gigabyte copy.

        A separate method rather than a keyword on :meth:`resize`, and the
        platform draws the same line: this copies the computer's disk to
        different hardware, and a resize that relocated a machine without being
        asked is exactly what neither side will do.

        THE COMPUTER MUST BE STOPPED. Suspended is not stopped here, unlike a
        resize — a saved desktop only loads on the host that wrote it, so it
        cannot travel. Resume and stop it, or discard the session, first.

        ANSWERS BEFORE IT FINISHES. The returned :class:`~mandala_computer.Move`
        is the operation as it stood the moment it was accepted, with ``live``
        True and the disk copy running behind it; :meth:`wait_for_move` is the
        other half. One move runs per account at a time.

        Everything is decided again at the moment this runs — the plan, the state
        of the computer, and which host it goes to — so it can still refuse even
        though the resize offered it.

        Not called ``move``, which on this class is the mouse pointer and has
        been since before there was anything else to move. The platform calls the
        operation a move and the record it returns is a
        :class:`~mandala_computer.Move`; the verb here is ``relocate`` because a
        ``move(x, y)`` that sometimes migrated a virtual machine between hosts
        would be the worst overload in this file. The TypeScript SDK made the
        same choice for the same reason.
        """
        return Move.from_api(
            self._t.json_object(
                "POST",
                _api.computer_action(self.id, "move"),
                json=_api.move_body(ram_mb=ram_mb, cpu=cpu, disk_gb=disk_gb),
            )
        )

    def wait_for_move(self, timeout: float = 900.0, poll: float = 3.0) -> Move:
        """Block until this computer's move stops running, and answer what happened.

        Polls the account's moves and picks out this computer's. It does NOT
        raise for a move that ended badly, and that is the decision worth
        knowing: the three failures are three different situations with three
        different remedies — see :attr:`~mandala_computer.Move.state` — and
        collapsing them into one exception is exactly how ``moved``, where the
        computer HAS changed hardware, gets read as "nothing happened". Read
        ``state``.

        Raises :class:`~mandala_computer.TimeoutError` if the move is still going
        when ``timeout`` runs out. The move is not stopped by that; only the
        waiting is, and there is no calling back a disk crossing between two
        hosts in any case.

        Raises :class:`~mandala_computer.MandalaError` if the move stops being
        listed, which happens when the computer is deleted — the platform reaps
        the row along with it.

        The default timeout is generous because the work is: a small overlay
        crosses in seconds and a full Windows disk takes minutes, plus minutes
        more when the target has to be sent the image this computer was built
        from first.
        """
        deadline = time.monotonic() + timeout
        last: Move | None = None
        while True:
            mine = self._my_move(self._t.json_object("GET", _api.MOVES))
            if mine is None:
                raise MandalaError(
                    f"{self.id} has no move any more; the platform reaps one "
                    "when its computer is deleted"
                )
            last = mine
            if not mine.live:
                return mine
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"{self.id} was still moving after {timeout:g}s (state {last.state}; "
                    "the move has not stopped, only this wait has)"
                )
            time.sleep(min(poll, remaining))

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
            self._t.json_object(
                "PATCH", _api.computer(self.id), json=_api.idle_suspend_body(minutes)
            )
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
        return _snapshots_deleted(data)

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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"{self.id} was still building after {timeout:g}s "
                    "(it has not stopped; only this wait has)"
                )
            time.sleep(min(poll, remaining))
            remaining = deadline - time.monotonic()
            self._refresh(timeout_cap=remaining)

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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.id} was still {self.status!r} after {timeout:g}s")
            self._refresh(timeout_cap=remaining)
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
            if self.is_building:
                raise MandalaError(
                    f"{self.id} is still building: call wait_until_built(), then start()"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.id} was still {self.status!r} after {timeout:g}s")
            time.sleep(min(poll, remaining))

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
        plan that does not cover this, a rate limit, or a TLS handshake the edge
        and the platform cannot agree on. Those are raised at once. Waiting on
        one of them costs the full timeout and then reports "the guest did not
        respond", which is both wrong and the least useful thing this method
        could say about a 401 — or, as measured, about an expired certificate
        that had already told the caller to report it rather than wait it out.

        A stopped computer is also refused immediately, including one carrying
        :attr:`start_error` from a failed boot. A suspended computer is not:
        running the probe counts as use and resumes its saved session.
        """
        deadline = time.monotonic() + timeout
        while True:
            failure = self._guest_wait_failure()
            if failure is not None:
                raise failure
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.id} guest did not respond within {timeout:g}s")
            try:
                probe_timeout = max(1, min(5, math.ceil(remaining)))
                if self._exec(
                    GUEST_PROBE,
                    probe_timeout,
                    timeout_cap=remaining,
                ).ok:
                    return self
            except _FATAL_WHILE_WAITING:
                raise
            except MandalaError:
                # A failed probe may mean that the cached lifecycle state is
                # stale. Re-read it before deciding the failure is transient.
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{self.id} guest did not respond within {timeout:g}s")
                try:
                    self._refresh(timeout_cap=remaining)
                except _FATAL_WHILE_WAITING:
                    raise
                except MandalaError:
                    # A transient failure on the state read is no more final
                    # than one on the probe; retry while the budget remains.
                    pass
                else:
                    failure = self._guest_wait_failure()
                    if failure is not None:
                        raise failure
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{self.id} guest did not respond within {timeout:g}s")
            time.sleep(min(poll, remaining))

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
        return self._t.binary(
            "GET",
            _api.computer_action(self.id, "screenshot"),
            params=_api.screenshot_params(width, fresh),
            accept="image/png, image/jpeg",
            content_types=("image/", "application/octet-stream"),
        )

    # --- controlling ----------------------------------------------------

    def _input(self, body: dict[str, Any], *, timeout: float | None = None) -> Mapping[str, Any]:
        return self._t.json_object(
            "POST", _api.computer_action(self.id, "input"), json=body, timeout=timeout
        )

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
        self._input(_api.hold_key_body(keys, seconds), timeout=seconds + DEADLINE_SLACK)

    def wait(self, seconds: float) -> None:
        """Pause, inside the platform, without holding this computer's monitor.

        Sleeping locally does the same thing for a script. This exists because a
        computer-use model emits ``wait`` as an action, and because it does not
        block the screenshot polls of anything else watching the desktop.
        """
        self._input(_api.wait_body(seconds), timeout=seconds + DEADLINE_SLACK)

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
        timeout: int = 30,
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
        blocks until ``timeout`` kills it::

            c.exec("nohup firefox https://example.com >/dev/null 2>&1 &", desktop=True)

        Or call :meth:`open` and let the SDK write that line.

        ``cwd`` is an absolute path inside the guest and ``env`` is extra
        environment for this command alone. A command slower than a few seconds
        wants :meth:`start_exec` instead: hitting ``timeout`` means this call
        stopped waiting, not that the work was destroyed, and the output and the
        exit code are lost with the request.

        The transport waits out whatever ``timeout`` asks for, and the platform
        extends its own deadline to match — but neither is what ends a long
        command. A proxy in front of the platform abandons a request that has
        produced no response for about two minutes and answers 524, which
        arrives here as :class:`~mandala_computer.GatewayTimeoutError`; measured
        against ``app.mandala.computer``, an ``exec`` slower than that dies at
        ~125s whether ``timeout`` said 300 or 3600. The command survives the
        request that abandoned it, so the next call on this computer may well
        report the guest agent as busy with it. Past a couple of minutes,
        :meth:`start_exec` is the only thing that works.
        """
        return self._exec(
            command,
            timeout,
            desktop=desktop,
            cwd=cwd,
            env=env,
        )

    def _exec(
        self,
        command: str,
        timeout: int,
        *,
        desktop: bool = False,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_cap: float | None = None,
    ) -> ExecResult:
        """The exec request, with an optional wall-clock cap for readiness probes."""
        data = self._t.json_object(
            "POST",
            _api.computer_action(self.id, "exec"),
            json=_api.exec_body(command, timeout, desktop, cwd=cwd, env=env),
            timeout=timeout + DEADLINE_SLACK,
            timeout_cap=timeout_cap,
        )
        return ExecResult.from_api(data)

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
                if status.drained:
                    break
                if not status.more:
                    time.sleep(2)

        The handle is the guest pid. It survives this process — a later session
        can rebuild one with :meth:`background_command` — but not a restart of
        the computer, and only commands this API started can be read back.
        """
        data = self._t.json_object(
            "POST",
            _api.computer_action(self.id, "exec"),
            json=_api.exec_body(command, 0, desktop, background=True, cwd=cwd, env=env),
        )
        _require_background_pid(data)
        return BackgroundCommand(self._t, self.id, data)

    def background_command(self, pid: int) -> BackgroundCommand:
        """A handle onto a command :meth:`start_exec` started earlier.

        For picking up a pid carried across a process boundary — a job id in a
        queue, a build started by the run before this one. Makes no request, so
        it does not verify the pid: the first :meth:`BackgroundCommand.poll`
        raises :class:`~mandala_computer.NotFoundError` if the daemon has no
        such handle.
        """
        return BackgroundCommand(self._t, self.id, {"pid": pid})

    def open(self, url: str, *, timeout: int = 30) -> ExecResult:
        """Open a URL in the guest's browser, on the screen::

            c.open("https://example.com")

        Sugar over :meth:`exec` with ``desktop=True``: it names a browser that
        works on the image, quotes the URL, and detaches the launch so the call
        returns in well under a second instead of blocking until ``timeout``.

        The result describes the *launch*, not the page — a zero exit means the
        shell started the browser, not that the URL resolved. Take a
        :meth:`screenshot` to see what actually loaded.

        Raises ``ValueError`` for an empty URL or one starting with ``-``, which
        a browser would read as a flag rather than an address. On Windows the
        API rejects it, the same as any ``desktop=True`` exec.
        """
        return self.exec(_api.open_url_command(url), timeout, desktop=True)

    # --- files ----------------------------------------------------------

    def read_file(self, path: str) -> bytes:
        """Read one file out of the guest, as bytes.

        ``path`` is absolute, inside the guest — there is no shell and no
        working directory behind this, so a relative path is refused before the
        request is made. Works while the computer is running or suspended
        (a transfer resumes a suspended computer, like any other use).

        The whole file crosses in one request, so a file past the 64 MiB that
        one request moves raises :class:`~mandala_computer.FileTooLargeError`.
        That is a limit on the request rather than on the file:
        :meth:`download_file` fetches one of any size by asking for it a window
        at a time, and :meth:`read_file_part` is the single window underneath.
        """
        return self._t.binary(
            "GET",
            _api.files(self.id),
            params=_api.files_params(path),
            timeout=FILE_TIMEOUT,
            accept="application/octet-stream",
            content_types=("application/octet-stream",),
        )

    def read_file_part(
        self,
        path: str,
        *,
        offset: int = 0,
        length: int | None = None,
    ) -> FilePart:
        """Read one window of a guest file, and where that window sits in it.

        The way anything larger than 64 MiB comes off a computer at all. The
        ceiling is on what one request moves, so applying it to a *window*
        leaves the file behind it any size you like::

            head = c.read_file_part("/home/user/out.tar", length=512)
            tail = c.read_file_part("/var/log/build.log", offset=-4096)

        ``offset`` counts from the start of the file, or from its end when it is
        negative — the reading Python gives an index, and the one the ``Range``
        header's own tail form has. ``length`` is how many bytes to ask for, and
        ``None`` means to the end of the file. A tail takes no ``length``: it is
        already anchored at both the end it starts from and the end it stops at.

        **Asking for more than one request moves is not an error, and you may
        get fewer bytes than you asked for.** The platform trims the window
        instead of refusing it, precisely because a caller cannot know the
        ceiling before asking — so the returned
        :class:`~mandala_computer.FilePart` is the authority on what came back
        and where to ask from next, never the numbers passed in. Which end gets
        trimmed follows the end you anchored: a window counted from the start
        keeps its start, and a tail keeps its end, so an over-long tail is still
        the tail of the file rather than the middle of it.

        Raises :class:`~mandala_computer.RangeNotSatisfiableError` for a window
        naming no byte the file has — past the end, or any window at all of an
        empty file — carrying the file's real length so the next ask does not
        have to guess.

        :meth:`download_file` is this in a loop, and is what a whole large file
        wants.
        """
        data, at, total, partial = self._t.binary_part(
            "GET",
            _api.files(self.id),
            params=_api.files_params(path),
            headers=_api.files_range(offset, length),
            timeout=FILE_TIMEOUT,
            accept="application/octet-stream",
            content_types=("application/octet-stream",),
        )
        return FilePart(data=data, offset=at, total=total, partial=partial)

    def download_file(
        self,
        path: str,
        dest: str | os.PathLike[str] | IO[bytes],
        *,
        part_size: int = FILE_PART_SIZE,
    ) -> int:
        """Fetch a whole guest file of any size, a window at a time::

            c.download_file("/home/user/out.tar", "out.tar")

        Returns how many bytes were written. ``dest`` is a path to write, or an
        already-open binary file — a path is opened and closed here, a handle is
        written to and left as it was found. A path is not opened until the
        first window has arrived, so a download that is refused outright leaves
        nothing behind on this side.

        This is :meth:`read_file_part` in the loop its ``Content-Range`` is
        designed for, which is what makes it, and not :meth:`read_file`, the way
        to move a large file. Nothing is held in memory but one part, so
        ``part_size`` is the memory cost and also what a mid-transfer failure
        costs: a part that dies is re-fetched from its start. Asking for more
        than the platform moves in one request is allowed and simply gets
        trimmed, so the ceiling is not something this has to know.

        A file that **grows** while it is being read is followed: each
        answer carries the length as it is now, and the loop ends where the last
        one does. Appending leaves the windows already read where they were, so
        that is still one file.

        A file that **shrinks** is not, and raises. Getting shorter means it was
        rewritten or truncated, so the bytes already written came from something
        that is gone — and going on would hand back two files spliced at whatever
        offset the change landed on, under a byte count that looks perfectly
        reasonable. Either the next window falls off the new end, which is
        :class:`~mandala_computer.RangeNotSatisfiableError`, or it lands inside
        it and the length it reports has dropped, which is a
        :class:`~mandala_computer.MandalaError` naming both lengths.

        An empty file is not an error and writes nothing.
        """
        if part_size < 1:
            raise ValueError(f"part_size must be at least 1 byte, not {part_size}")
        # The whole of the first window — the request AND the check on what came
        # back — happens before anything local is opened, so a download that was
        # never going to happen leaves nothing in the place of the file it was
        # meant to become. Opening for write is destructive on its own: a
        # refusal that had already truncated somebody's file would be keeping the
        # letter of "nothing was written" and none of the point.
        first: FilePart | None
        try:
            first = self.read_file_part(path, offset=0, length=part_size)
        except RangeNotSatisfiableError as exc:
            if not _empty_guest_file(exc):
                raise
            first = None
        if first is not None:
            _continues(path, 0, first, None)
        written = 0
        with _download_sink(dest) as sink:
            part = first
            while part is not None:
                _write_all(sink, part.data)
                written += len(part.data)
                if part.at_end:
                    break
                # From where the answer ended, never from where the ask would
                # have: a window past what one request moves comes back trimmed,
                # and advancing by part_size would leave holes in the file with
                # nothing raised.
                asked, was = part.end, part.total
                part = self.read_file_part(path, offset=asked, length=part_size)
                _continues(path, asked, part, was)
        return written

    def write_file(self, path: str, data: bytes | str) -> None:
        """Write ``data`` to one file inside the guest, creating it if needed.

        A ``str`` is written as UTF-8. The path rules are :meth:`read_file`'s.
        The bytes land exactly as given — this is how a credential reaches a
        guest ``.env`` without echoing it through a shell command line.
        Bodies over 64 MiB are refused before any request is made.
        """
        body = _file_body(data)
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
        data = self._t.json_object(
            "GET",
            _api.computer_action(self.id, "windows"),
            params=_api.windows_params(include_all),
        )
        return _windows_from_response(data)

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
        data = self._t.json_object(
            "POST",
            _api.window(self.id, window_id),
            json=_api.window_body(action, x=x, y=y, width=width, height=height),
        )
        return WindowResult.from_api(data)

    # --- snapshots ------------------------------------------------------

    def snapshot(self, *, memory: bool = False, name: str | None = None) -> Snapshot:
        """Capture a snapshot of this computer.

        Works while it is running. ``memory=True`` also captures live RAM and
        device state, so a restore or fork resumes exactly where it was instead
        of booting — the computer must be running for that. An omitted ``name``
        asks the platform to generate one.
        """
        data = self._t.json_object(
            "POST",
            _api.computer_action(self.id, "snapshots"),
            json=_api.snapshot_body(memory, name),
        )
        return Snapshot.from_api(data)

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
            if s.get("computer_id") == self.id or _is_unreachable_stub(s)
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
        data = self._t.json_object("GET", _api.computer_action(self.id, "snapshots"))
        return SnapshotHoldings.from_api(data)

    def schedule(self) -> Mapping[str, Any]:
        """The automatic daily snapshot schedule."""
        stored = dict(self._t.json_object("GET", _api.computer_action(self.id, "schedule")))
        self._data["snapshot_schedule"] = stored or None
        return stored

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
        stored = dict(
            self._t.json_object(
                "PUT",
                _api.computer_action(self.id, "schedule"),
                json=_api.schedule_body(enabled=enabled, hour=hour, minute=minute, tz=tz),
            )
        )
        self._data["snapshot_schedule"] = stored
        return stored

    def clear_schedule(self) -> Mapping[str, Any]:
        """Remove the schedule, as distinct from disabling it.

        ``set_schedule(enabled=False)`` keeps the chosen time so toggling back on
        restores it, and keeps the scheduler's bookkeeping with it. Clearing
        returns the computer to never having had a schedule.
        """
        cleared = dict(self._t.json_object("DELETE", _api.computer_action(self.id, "schedule")))
        self._data["snapshot_schedule"] = None
        return cleared

    # --- the agent loop -------------------------------------------------

    def agent_stream(
        self,
        prompt: str,
        *,
        model_key: str,
        system: str | None = None,
        max_steps: int | None = None,
        model: str | None = None,
    ) -> Iterator[AgentEvent]:
        """Have the platform drive this computer, reporting as it goes.

        Screenshot, decide, click, type, repeat — inside the platform, on your
        own Anthropic key, which it never stores and never bills you for. What
        it buys you is that ten clicks stop being ten images in your context.

        The computer must already be RUNNING. This route will not start one:
        starting is billable, and it is not a decision to make on somebody's
        behalf because they sent a prompt. A stopped or suspended computer is a
        :class:`~mandala_computer.ConflictError`, and so is a computer another
        run is already driving.

        Yielded rather than returned because a run is minutes of clicking, and
        something that says nothing until it is over cannot be told from a
        hang::

            for event in c.agent_stream("Turn on dark mode", model_key=key):
                match event:
                    case mc.AgentStepEvent(step):
                        print(f"{step.n}. {step.detail}")
                    case mc.AgentDone(result):
                        print(result.text)

        Every step spends your rate budget as well — the same budget your own
        calls draw on, at the same price, because a click through here costs
        what a click plus a screenshot costs anywhere. A run that exhausts it
        stops where it is and ends ``rate_limited`` rather than failing.

        Events this SDK does not model are skipped rather than raised on. To
        stop a run early, close the iterator explicitly; a bare ``break`` does
        not guarantee generator cleanup on every Python implementation. The
        standard :func:`contextlib.closing` helper makes that concise.
        """
        _require_model_key(model_key)
        steps = 0
        with closing(
            self._t.sse(
                "POST",
                _api.computer_action(self.id, "agent"),
                json=_api.agent_body(
                    prompt, stream=True, system=system, max_steps=max_steps, model=model
                ),
                headers={MODEL_KEY_HEADER: model_key},
            )
        ) as frames:
            for frame in frames:
                event = to_agent_event(frame.event, frame.data, steps)
                if event is None:
                    continue
                if isinstance(event, AgentStepEvent):
                    steps += 1
                yield event

    def agent(
        self,
        prompt: str,
        *,
        model_key: str,
        system: str | None = None,
        max_steps: int | None = None,
        model: str | None = None,
    ) -> AgentResult:
        """:meth:`agent_stream`, waited out — one call, one result.

        ::

            result = c.agent("Open the settings and turn on dark mode.", model_key=key)
            if not result.finished:
                print(f"did not finish: {result.stop}")

        It does **not** raise when a run ends unfinished. ``max_steps``,
        ``rate_limited`` and ``refusal`` leave real work on the desktop, and
        raising would discard the only account of what was done to the machine —
        check :attr:`~mandala_computer.AgentResult.finished`. What it does raise
        is a failure the platform reported mid-run, as the class that status
        deserves, and a :class:`~mandala_computer.MandalaError` for a stream
        that ended without saying how the run came out.

        This still streams underneath, and that is deliberate: it is the same
        request either way, and the streaming one is the request a proxy between
        you and the platform will not close for being quiet.
        """
        result: AgentResult | None = None
        failure: AgentFailed | None = None
        try:
            for event in self.agent_stream(
                prompt, model_key=model_key, system=system, max_steps=max_steps, model=model
            ):
                if isinstance(event, AgentDone):
                    result = event.result
                elif isinstance(event, AgentFailed):
                    failure = event
        except TimeoutError:
            # The stream deliberately remains open after done so a trailing
            # failure can override it. Silence after done is not itself a
            # failure, however, and must not discard the result already sent.
            if result is None and failure is None:
                raise
        return _agent_outcome(result, failure)

    def agent_once(
        self,
        prompt: str,
        *,
        model_key: str,
        system: str | None = None,
        max_steps: int | None = None,
        model: str | None = None,
    ) -> AgentResult:
        """The agent loop as a single non-streaming request.

        Worse than :meth:`agent` for anything long, and here for the callers
        who cannot use a stream at all: nothing is reported until the whole run
        is over, and a reverse proxy between you and the platform is entitled to
        close a request held open for minutes with nothing crossing it. Prefer
        :meth:`agent`.

        A proxy that answers instead of the platform raises here rather than
        coming back as a run of no steps that ended for no reason — the same
        check :meth:`agent` makes on the content type, made on the body.

        The proxy in front of ``app.mandala.computer`` gives up at about two
        minutes, measured — so on that deployment this is not a risk but a
        certainty for any run longer than that, and it arrives as
        :class:`~mandala_computer.GatewayTimeoutError`. The remedy is
        :meth:`agent`, not ``start_exec``: a stream sends its headers at once
        and heartbeats every ten seconds, so nothing about it looks idle to the
        hop that would otherwise stop waiting.
        """
        _require_model_key(model_key)
        data = self._t.json_object(
            "POST",
            _api.computer_action(self.id, "agent"),
            json=_api.agent_body(
                prompt, stream=False, system=system, max_steps=max_steps, model=model
            ),
            headers={MODEL_KEY_HEADER: model_key},
            timeout=NO_DEADLINE,
        )
        return _agent_once_outcome(data)


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
        return str(self._data.get("command") or "")

    @property
    def started_at(self) -> str:
        return str(self._data.get("started_at") or "")

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
        data = self._t.json_object("GET", _api.exec_handle(self._computer_id, self.pid))
        return ExecStatus.from_api(data)

    def kill(self) -> ExecStatus:
        """Stop it, and everything it started.

        Answers with its final state, including whatever it printed that had not
        been read — so this is a way to end a command and collect its tail in
        one call, not only a way to abandon one.
        """
        data = self._t.json_object("DELETE", _api.exec_handle(self._computer_id, self.pid))
        return ExecStatus.from_api(data)
