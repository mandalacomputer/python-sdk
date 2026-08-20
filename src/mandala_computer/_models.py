"""Response objects.

Deliberately permissive: unknown fields are preserved in ``raw`` rather than
rejected, so a server that starts returning more does not break older clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, SupportsIndex, TypeVar, overload

__all__ = [
    "ExecResult",
    "ExecStatus",
    "Listing",
    "Size",
    "Snapshot",
    "SnapshotHoldings",
    "Template",
    "VncConnect",
    "Window",
    "WindowResult",
]

T = TypeVar("T")
S = TypeVar("S")


class Listing(list[T]):
    """A collection read that the platform may have had to answer short.

    A ``list``, so everything written against the old return type keeps working
    and nothing has to be unwrapped. What it adds is the answer to a question a
    bare list cannot carry: whether this is all of them.

    ``GET /computers`` and ``GET /snapshots`` fan out across every hypervisor
    holding something of yours. One that cannot be reached makes the answer
    incomplete, and by default the platform refuses to send it —
    :class:`~mandala_computer.UnavailableError`. Passing ``allow_partial=True``
    takes the short answer instead, and this is where it says so::

        computers = client.computers.list(allow_partial=True)
        if not computers.is_complete:
            ...  # do not treat anything absent from this as deleted

    :attr:`incomplete` is ``None`` when the listing is whole. When it is not, it
    is how many rows the platform's placement cache could account for — which is
    legitimately ``0``, because a computer created during the outage was never
    cached against the host now holding it. So branch on
    :attr:`is_complete`, never on the number.
    """

    #: Rows missing, or ``None`` when nothing was missing. See the class note on
    #: why ``0`` is not the same as ``None``.
    incomplete: int | None = None

    @property
    def is_complete(self) -> bool:
        """False when the platform said this listing is short."""
        return self.incomplete is None

    def copy(self) -> Listing[T]:
        """Copy the rows without discarding whether the server answered short."""
        return Listing.of(list(self), self.incomplete)

    @overload
    def __getitem__(self, index: SupportsIndex, /) -> T: ...

    @overload
    def __getitem__(self, index: slice, /) -> Listing[T]: ...

    def __getitem__(self, index: SupportsIndex | slice, /) -> T | Listing[T]:
        if isinstance(index, slice):
            return Listing.of(super().__getitem__(index), self.incomplete)
        return super().__getitem__(index)

    @overload
    def __add__(self, other: list[T], /) -> Listing[T]: ...

    @overload
    def __add__(self, other: list[S], /) -> Listing[T | S]: ...

    def __add__(self, other: list[Any], /) -> Listing[Any]:
        """Concatenate without promoting a partial answer to a complete one."""
        incomplete = self.incomplete
        if isinstance(other, Listing) and other.incomplete is not None:
            incomplete = other.incomplete if incomplete is None else incomplete + other.incomplete
        return Listing.of(super().__add__(other), incomplete)

    @classmethod
    def of(cls, items: list[T], incomplete: int | None = None) -> Listing[T]:
        listing = cls(items)
        listing.incomplete = incomplete
        return listing


@dataclass(frozen=True, repr=False)
class VncConnect:
    """Everything needed to put a computer's live desktop on a page.

    Two credentials rather than one, and the difference is enforced by the
    platform rather than by the client asking politely:

    ``token``
        Full control — keyboard, pointer, clipboard. Root-equivalent on that one
        machine, so it belongs on a server or in a page you trust.
    ``view_token``
        Watch only. The daemon drops input on a socket opened with it, so a
        browser holding this one cannot type even from a patched client.

    Both are scoped to a single computer, and neither is the account API key —
    which is every computer on the account, forever, and must never reach a
    browser. Both end when the computer restarts.
    """

    #: Websocket URL carrying ``token``. Full control.
    url: str
    #: Websocket URL carrying ``view_token``. Watch only.
    view_url: str
    #: The credential inside :attr:`url`, for building your own noVNC URL.
    token: str
    #: The credential inside :attr:`view_url`.
    view_token: str
    #: The platform's hosted viewer, watch-only, for an ``<iframe>``. The
    #: credential is in the URL fragment, which browsers never send to a server —
    #: so it stays out of access logs and out of ``Referer`` on everything the
    #: page then loads.
    embed_url: str
    #: Websocket URL opening an interactive terminal — a PTY in the guest,
    #: carried on the same controlling credential as :attr:`url`, so treat it
    #: as that credential. ``""`` on a Windows guest, which has no terminal
    #: yet, and on a server from before the terminal existed.
    terminal_url: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any] | None) -> VncConnect | None:
        """Build one, or ``None`` when the API did not supply a full set.

        Absent rather than partial is the platform's own rule: a URL built over
        a missing credential is a string indistinguishable from a working one
        that answers 401 forever. Anything short of both credentials is treated
        as no connect surface at all.
        """
        if not isinstance(d, Mapping):
            return None
        token = str(d.get("token") or "")
        view_token = str(d.get("view_token") or "")
        if not token or not view_token:
            return None
        return cls(
            url=str(d.get("url", "")),
            view_url=str(d.get("view_url", "")),
            token=token,
            view_token=view_token,
            embed_url=str(d.get("embed_url", "")),
            terminal_url=str(d.get("terminal_url") or ""),
            raw=dict(d),
        )

    @staticmethod
    def _without_credential(url: str) -> str:
        """A URL with everything after the path dropped.

        Each of these URLs carries a token in its query or its fragment, so the
        origin and path are the whole of what can be shown.
        """
        if not url:
            return ""
        bare = url.split("?", 1)[0].split("#", 1)[0]
        return bare if bare == url else f"{bare}?<redacted>"

    def __repr__(self) -> str:
        """Deliberately hand-written, and lossy.

        The generated one printed both tokens and the three URLs carrying them.
        These credentials have no expiry — they last until the computer restarts
        or somebody rotates them — and ``token`` is root-equivalent on that
        machine, so a single log line or traceback rendering this object hands
        over the desktop for as long as it runs. Everything a repr is actually
        for survives: which computer this is, and whether each field is set.

        :attr:`raw` still holds the real values, and it is excluded from the
        repr for the same reason.
        """
        return (
            f"VncConnect(url={self._without_credential(self.url)!r}, "
            f"view_url={self._without_credential(self.view_url)!r}, "
            f"token=<redacted>, view_token=<redacted>, "
            f"embed_url={self._without_credential(self.embed_url)!r}, "
            f"terminal_url={self._without_credential(self.terminal_url)!r})"
        )


@dataclass(frozen=True)
class Template:
    name: str
    label: str
    os: str
    cpu: int
    ram_mb: int
    disk_gb: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Template:
        return cls(
            name=d.get("name", ""),
            label=d.get("label", ""),
            os=d.get("os", ""),
            cpu=int(d.get("cpu", 0)),
            ram_mb=int(d.get("ram_mb", 0)),
            disk_gb=int(d.get("disk_gb", 0)),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Size:
    """A named size: a template plus a CPU/RAM/disk shape, from ``GET /sizes``.

    These are the shapes the platform keeps pre-booted, so a create that passes
    ``id`` as ``size`` is typically answered from the warm pool in about a
    second where a custom shape boots cold.

    ``allowed`` is about the plan's per-computer ceilings only — what the
    account already holds is not counted, so a create at an allowed size can
    still be refused against the plan's pools. ``cheapest_plan`` is the plan to
    name when it is False, or ``None`` if no purchasable plan admits the row.
    """

    id: str
    label: str
    template: str
    cpu: int
    ram_mb: int
    disk_gb: int
    allowed: bool
    cheapest_plan: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Size:
        return cls(
            id=d.get("id", ""),
            label=d.get("label", ""),
            template=d.get("template", ""),
            cpu=int(d.get("cpu", 0)),
            ram_mb=int(d.get("ram_mb", 0)),
            disk_gb=int(d.get("disk_gb", 0)),
            allowed=bool(d.get("allowed", False)),
            cheapest_plan=d.get("cheapest_plan"),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Snapshot:
    id: str
    computer_id: str
    name: str
    kind: str
    state: str
    size_bytes: int
    created_at: str
    incremental: bool
    auto: bool
    #: For a computer that still exists, its current name — so a rename shows up
    #: here without re-reading anything. For an orphan, the name it had at
    #: capture, which is all that is left of it.
    computer_name: str = ""
    #: The computer this was captured from no longer exists, which decides which
    #: of the two things you can do with a snapshot still works:
    #: :meth:`~mandala_computer.Snapshots.clone` builds a new computer out of it
    #: and is fine, while :meth:`~mandala_computer.Snapshots.restore` puts the
    #: disk back on the source and has nowhere to put it. Snapshots outlive
    #: their computers on purpose, so an ordinary account's listing has these in
    #: it as a matter of course rather than as a fault.
    orphaned: bool = False
    #: This is a placeholder standing in for a snapshot nobody could read, seen
    #: only in a listing taken with ``allow_partial=True``. The platform does
    #: not merely omit what it could not reach — it appends one of these per
    #: missing row, carrying an id and nothing else, so that something short is
    #: visibly short rather than quietly smaller. Such a row has no
    #: :attr:`computer_id`: there was no daemon to say which computer it belongs
    #: to. Which is why filtering a partial listing by computer keeps these —
    #: dropping them removes precisely the markers saying the answer is
    #: incomplete, and then reports a confident count.
    unreachable: bool = False
    #: The shape the capture was taken at, which is the shape a
    #: :meth:`~mandala_computer.Snapshots.clone` of it comes up as. Worth
    #: reading before cloning: a snapshot carries its own sizing rather than
    #: the source computer's current one, so a computer resized after the
    #: capture clones back to what it was, not to what it is.
    os: str = ""
    template: str = ""
    cpu: int = 0
    ram_mb: int = 0
    disk_gb: int = 0
    resolution: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_memory(self) -> bool:
        """True for a live RAM+disk capture, which forks/restores without booting."""
        return self.kind == "memory"

    @property
    def is_durable(self) -> bool:
        """True once the snapshot has been replicated to backup storage."""
        return self.state == "durable"

    @property
    def is_scheduled(self) -> bool:
        """True if the scheduler took this, rather than a person.

        Also what makes it eligible for retention: snapshots you take yourself
        are never aged out automatically.
        """
        return self.auto

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Snapshot:
        return cls(
            id=d.get("id", ""),
            computer_id=d.get("computer_id", ""),
            name=d.get("name", ""),
            kind=d.get("kind", "disk"),
            state=d.get("state", ""),
            size_bytes=int(d.get("size_bytes", 0)),
            created_at=d.get("created_at", ""),
            incremental=bool(d.get("incremental", False)),
            auto=bool(d.get("auto", False)),
            computer_name=str(d.get("computer_name", "")),
            orphaned=bool(d.get("orphaned", False)),
            unreachable=bool(d.get("unreachable", False)),
            os=str(d.get("os", "")),
            template=str(d.get("template", "")),
            cpu=int(d.get("cpu", 0)),
            ram_mb=int(d.get("ram_mb", 0)),
            disk_gb=int(d.get("disk_gb", 0)),
            resolution=str(d.get("resolution", "")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class SnapshotHoldings:
    """What a computer would leave behind — and the interlock on destroying it.

    From ``GET /computers/{id}/snapshots``, which is not a listing: the
    snapshots themselves come from :meth:`~mandala_computer.Snapshots.list`,
    and these two routes answer different shapes on purpose.

    :attr:`fingerprint` is the reason to come here. It names the exact set the
    count and the size describe, it cannot be reconstructed from a listing, and
    it is what makes a purge binding — see
    :meth:`~mandala_computer.Computer.delete`. Read the numbers, decide, then
    pass the fingerprint you were shown; the daemon refuses the sweep if a
    capture has landed in between, which is exactly the race that would
    otherwise destroy something nobody agreed to.
    """

    count: int
    size_bytes: int
    fingerprint: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> SnapshotHoldings:
        return cls(
            count=int(d.get("count", 0)),
            size_bytes=int(d.get("size_bytes", 0)),
            fingerprint=str(d.get("fingerprint", "")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Window:
    """One window on the guest's desktop.

    What a screenshot cannot tell you: a picture says what the desktop looks
    like, this says what any of it *is* — which is how a browser that failed to
    launch is told apart from one that has not painted yet.

    Match on :attr:`wm_class` rather than :attr:`title`. The class is the
    application and is stable; the title is whatever page or document it happens
    to be showing.
    """

    id: str
    title: str
    #: The X11 ``WM_CLASS`` — the application, e.g. ``"Firefox"``. Spelled with
    #: the prefix because ``class`` is a Python keyword and cannot be a field.
    wm_class: str
    #: The window manager's own type, e.g. ``"normal"``, ``"dock"``.
    type: str
    x: int
    y: int
    width: int
    height: int
    focused: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Window:
        return cls(
            id=str(d.get("id", "")),
            title=str(d.get("title", "")),
            wm_class=str(d.get("class", "")),
            type=str(d.get("type", "")),
            x=int(d.get("x", 0)),
            y=int(d.get("y", 0)),
            width=int(d.get("width", 0)),
            height=int(d.get("height", 0)),
            focused=bool(d.get("focused", False)),
            raw=dict(d),
        )


@dataclass(frozen=True)
class WindowResult:
    """What a window action left behind.

    :attr:`window` is the window *as it now is*, not an acknowledgement of what
    was asked. Believe it rather than the request: the window manager places the
    frame and applications snap to their own increments, so a move to (300, 200)
    routinely lands at (305, 229).

    It is ``None`` in two different situations, and :attr:`gone` is what tells
    them apart — ``True`` after a ``close``, which is the action succeeding, and
    ``False`` when the action happened but the guest could not describe the
    result.
    """

    window: Window | None
    gone: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> WindowResult:
        w = d.get("window")
        return cls(
            window=Window.from_api(w) if isinstance(w, Mapping) else None,
            gone=bool(d.get("gone", False)),
            raw=dict(d),
        )


@dataclass(frozen=True)
class ExecStatus:
    """A backgrounded command's state, and what it has printed since last time.

    The output is a **cursor, not a buffer**. Each read returns what has arrived
    since the previous read and advances the daemon's own offset, so output you
    receive and drop is gone, and two readers polling one pid split the stream
    between them rather than each seeing all of it. :attr:`stdout_offset` and
    :attr:`stderr_offset` report how far it has read — they are not parameters
    to send back.

    :attr:`more` is the flag to poll on: it says there is further output waiting
    right now.
    """

    pid: int
    #: The command line, echoed back.
    command: str
    running: bool
    exited: bool
    #: ``None`` until it has exited — ``None`` rather than ``0``, which is the
    #: one value that would be read as success by anything not checking first.
    exit_code: int | None
    #: What it has printed since the previous read. This read consumed it.
    stdout: str
    stderr: str
    #: How far the daemon has now read, reported rather than requested.
    stdout_offset: int
    stderr_offset: int
    #: There is further output waiting right now — poll again straight away
    #: rather than sleeping first.
    more: bool
    #: It was stopped by :meth:`~mandala_computer.BackgroundCommand.kill` rather
    #: than ending on its own.
    killed: bool
    started_at: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def done(self) -> bool:
        """True once the command has stopped, however it stopped.

        Read with :attr:`more`, not instead of it: a command can exit with
        output still queued, and a loop that stops at ``done`` alone drops
        whatever the last read did not reach.
        """
        return self.exited or not self.running

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ExecStatus:
        code = d.get("exit_code")
        return cls(
            pid=int(d.get("pid", 0)),
            command=str(d.get("command", "")),
            running=bool(d.get("running", False)),
            exited=bool(d.get("exited", False)),
            exit_code=None if code is None else int(code),
            stdout=str(d.get("stdout") or ""),
            stderr=str(d.get("stderr") or ""),
            stdout_offset=int(d.get("stdout_offset", 0)),
            stderr_offset=int(d.get("stderr_offset", 0)),
            more=bool(d.get("more", False)),
            killed=bool(d.get("killed", False)),
            started_at=str(d.get("started_at", "")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class ExecResult:
    """The outcome of a shell command run inside the guest."""

    #: ``None`` when the platform could not report an exit code, such as a
    #: timed-out command. It must not be coerced to zero and mistaken for success.
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    #: True when the guest agent stopped capturing stdout before the command
    #: stopped producing it. See :attr:`truncated`.
    out_truncated: bool = False
    #: The same for stderr.
    err_truncated: bool = False
    #: ``compare=False``, unlike the other models here. An ``ExecResult`` is a
    #: value, not a handle: callers assert on one against a result they built
    #: themselves, and put them in sets. Comparing the unknown fields the
    #: server happened to send would make ``res == ExecResult(0, "hi", "",
    #: False)`` false for a command that did exactly that, and comparing a
    #: ``dict`` at all makes the frozen dataclass unhashable.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        """The command ran and exited zero.

        Deliberately says nothing about :attr:`truncated`: a command that
        succeeded and produced more output than the guest agent would carry is
        still a command that succeeded. Whether a short answer is acceptable
        depends on what you were going to do with it, so it is reported
        separately rather than folded in here.
        """
        return self.exit_code == 0 and not self.timed_out

    @property
    def truncated(self) -> bool:
        """True if either stream was cut short.

        The guest agent caps a command's captured output at 16 MiB. Past that it
        keeps running and keeps producing, and what comes back is the first
        16 MiB with no other sign that there was more — which is why this is
        worth checking before parsing the output of anything that could be
        large. Redirect to a file inside the guest and fetch it instead when it
        might be.
        """
        return self.out_truncated or self.err_truncated

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ExecResult:
        code = d.get("exit_code")
        return cls(
            exit_code=None if code is None else int(code),
            stdout=d.get("stdout", "") or "",
            stderr=d.get("stderr", "") or "",
            timed_out=bool(d.get("timed_out", False)),
            out_truncated=bool(d.get("out_truncated", False)),
            err_truncated=bool(d.get("err_truncated", False)),
            raw=dict(d),
        )
