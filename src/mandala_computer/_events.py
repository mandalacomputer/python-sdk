"""The event stream, as an iterator.

``GET computers/{id}/events`` is a websocket that says what a computer is doing
without being asked, and it exists so that an agent stops paying for a
screenshot to learn that nothing has changed. What a caller wants out of that is
not a socket::

    for ev in c.events():
        ...

    c.wait_for("computer.ready")

So this module is the part in between — the reconnect, the cursor, the opening
frame's state, and the three frames that are not events about the computer.

Everything here is written against the ``events_url`` entry in the platform's
``web/lib/apidoc.ts``, which is the reference this must not contradict.

**Two halves, one set of decisions.** The sync and async streams differ only in
how they wait; every judgement either of them makes — which frame is an event,
when the cursor moves, whether a refusal is worth retrying, whether a readiness
has to be manufactured — lives in :class:`_Core`, which does no IO at all. Two
copies of that reasoning is how the halves drift, and this is the one place on
this surface where being wrong is silent: a stream that quietly stops
reconnecting looks exactly like a computer with nothing to say.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ._exceptions import ConnectionError, MandalaError
from ._models import Window, _opt_num, _text, _texts, _Wire, _wire

__all__ = [
    "GUEST_EVENT_TYPES",
    "STREAM_FRAME_TYPES",
    "AsyncEventStream",
    "ComputerEvent",
    "EventStream",
    "Hello",
]


#: The frames that are statements about the STREAM rather than about the
#: computer: a hole in the history, this host ending the socket on purpose, and
#: the vocabulary being revised under an open one.
#:
#: They reach a caller as events because a client cannot ignore what it was
#: never handed, and they are named here because they are never in what the
#: opening frame advertises — that list is about what the machine can produce.
STREAM_FRAME_TYPES: tuple[str, ...] = ("gap", "closed", "capabilities")

#: The event types the platform sends only where the GUEST can produce them.
#:
#: A Windows guest, or a Linux one whose hardware carries no terminal channel,
#: has nowhere to run the watcher these come from. :attr:`Hello.events` is the
#: authority for a given computer; this is the shape of the half that goes
#: missing.
GUEST_EVENT_TYPES: tuple[str, ...] = (
    "window.opened",
    "window.closed",
    "window.focused",
    "window.blurred",
    "clipboard.changed",
    "computer.ready",
)


def _opt_texts(value: Any) -> list[str] | None:
    """:func:`~mandala_computer._models._texts`, keeping "did not say" apart from "none".

    ``_texts`` answers ``[]`` for anything it cannot read, which is right for a
    field whose emptiness is only ever a fact about the value. It is wrong for
    this one: a ``capabilities`` frame carrying an unreadable ``events`` would
    replace a computer's whole vocabulary with the empty list, and
    :meth:`EventStream.wait_for`'s refusal reads that as a machine that can
    emit nothing at all — so a malformed frame would end every wait on a
    perfectly healthy desktop.
    """
    return _texts(value) if isinstance(value, list) else None


@dataclass(frozen=True)
class ComputerEvent:
    """One frame off a computer's event stream.

    A single flat shape rather than a class per type. The reference says in as
    many words that the vocabulary grows and that a client must ignore a
    ``type`` it does not recognise, so anything closed here would turn a
    forward-compatible addition into a frame this SDK drops. Nothing is
    dropped: an unknown type arrives with its :attr:`data` intact and none of
    the promoted fields below set.

    Match on :attr:`type`, and read the field the type carries::

        for ev in c.events():
            if ev.type == "process.exited" and ev.pid == job.pid:
                break
    """

    #: What happened. One of the documented names, or one added since this
    #: release — ignore a type you do not recognise rather than raising on it.
    type: str
    #: RFC 3339, UTC, as the platform wrote it.
    #:
    #: ``""`` where the frame carried none, which ``closed`` and
    #: ``capabilities`` legitimately do. Deliberately not this client's own
    #: clock: a timestamp made up here is indistinguishable from the
    #: platform's, so a frame that said nothing about when would read back as
    #: though it had. The one exception is a :attr:`synthesized` event, which
    #: says so.
    at: str
    #: The computer this is about.
    computer: str
    #: Where to resume from, having consumed this event.
    #:
    #: Opaque, and it counts events CONSUMED rather than naming the last one
    #: seen — so it is the position AFTER this event, not at it. The stream
    #: stores it for you and passes it as ``since=`` on every reconnect; it is
    #: here for a caller keeping their own place across a process restart.
    #:
    #: ``""`` on ``closed`` and ``capabilities``, which carry no position.
    cursor: str
    #: Who is describing this: ``"daemon"`` where the platform observed it,
    #: ``"guest"`` where the machine reported it about itself.
    #:
    #: Worth reading. Every ``window.*``, ``clipboard.changed`` and
    #: ``computer.ready`` is the tenant's own machine describing itself, and
    #: anyone with root inside that guest can make those say anything — which
    #: is exactly as much as they are worth. The same caller that trusts
    #: ``process.exited`` to end a wait may be putting a window title on a page.
    #:
    #: ``""`` where the frame did not say, rather than a guess. This SDK does
    #: not fall back to ``"daemon"`` — the mandala-computer-typescript half
    #: does — because that fallback asserts the platform observed something it
    #: never claimed to, on the one field whose whole job is to say how much a
    #: payload is worth. See ``_Wire`` in ``_models``: every fallback boolean is
    #: wrong somewhere, and this is the string with the same problem.
    source: str
    #: The payload verbatim, including fields this SDK predates.
    data: Mapping[str, Any] = field(default_factory=dict)
    #: The whole frame, envelope included. Unknown and malformed fields survive
    #: here as they do everywhere else on this surface.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    # --- promoted payloads ------------------------------------------------
    #
    # Each is set for the types named on it and ``None`` everywhere else.
    #
    # Keyword-only, all of them, and that is a decision about the release after
    # this one rather than about this one. The vocabulary is documented as
    # growing, so this class grows with it — and a field inserted into a
    # positional order is the release `Template.ref` cost and `Window` was
    # taught to avoid on OPL-4191. Nothing here is ever positional, so nothing
    # here can be moved by what comes next.

    #: Position in this computer's stream.
    #:
    #: ``None`` for the three frames that are statements ABOUT the stream
    #: rather than positions in it, and for a :attr:`synthesized` event. A gap
    #: in particular carried ``seq: 0`` in an early build of the platform, and
    #: a client applying the obvious rule — ignore anything not newer than the
    #: last sequence I saw — discarded the one frame that reports unrecoverable
    #: loss. Absent is what it means, and this decodes it that way whatever the
    #: host spelled.
    seq: int | None = field(default=None, kw_only=True)
    #: ``window.opened`` and ``window.focused``: the window, in the shape
    #: :meth:`~mandala_computer.Computer.windows` returns.
    #:
    #: Its position and size are as they were AT THIS EVENT. Moving or resizing
    #: a window produces no event at all, so where a window is *now* is a
    #: question for the listing rather than for the last event about it.
    window: Window | None = field(default=None, kw_only=True)
    #: ``window.closed`` and ``window.blurred``: the window's id, and nothing
    #: else.
    #:
    #: There is deliberately no geometry on either — a window that is gone has
    #: no position to report, and a window that lost focus is otherwise
    #: unchanged. Match it against a window you were told about:
    #: :attr:`Hello.windows`, or a ``window.opened`` or ``window.focused`` this
    #: stream sent you.
    window_id: str | None = field(default=None, kw_only=True)
    #: ``process.exited``: the pid :meth:`~mandala_computer.Computer.start_exec`
    #: handed you.
    pid: int | None = field(default=None, kw_only=True)
    #: ``process.exited``: what the command exited with.
    #:
    #: ``None`` exactly where :attr:`lost` is true, and the pair is the whole
    #: point: ``-1`` is already a real exit code on this path, so it could not
    #: also mean "no answer". Nothing is invented for a command whose outcome
    #: the platform does not know.
    exit_code: int | None = field(default=None, kw_only=True)
    #: ``process.exited``: the guest stopped knowing about this command — which
    #: is what a restart of the machine underneath it looks like.
    #:
    #: The handle goes with it, so :meth:`~mandala_computer.BackgroundCommand.poll`
    #: answers 404 from here on. The event is sent so that a caller waiting on
    #: it stops waiting, not because anything was learned about how the command
    #: ended.
    lost: bool | None = field(default=None, kw_only=True)
    #: ``clipboard.changed``: ``"clipboard"`` or ``"primary"``. The contents are
    #: not on this stream — read them at
    #: :meth:`~mandala_computer.Computer.clipboard`.
    selection: str | None = field(default=None, kw_only=True)
    #: ``computer.started`` / ``.stopped`` / ``.suspended``: the state it is in
    #: now.
    status: str | None = field(default=None, kw_only=True)
    #: ``computer.started`` / ``.stopped`` / ``.suspended``: the state it was in
    #: before.
    #:
    #: ``None`` on the first transition a host reports for a computer after the
    #: daemon restarts, which has no earlier status to have moved from — so
    #: read it as optional rather than assuming a string is always there.
    previous: str | None = field(default=None, kw_only=True)
    #: ``computer.idle``: how long nobody had touched it.
    idle_seconds: int | None = field(default=None, kw_only=True)
    #: ``gap``: the oldest position this host can still replay from.
    #:
    #: ``None`` where it holds nothing at all. Resuming from it is legal and is
    #: not what a gap is for — the events between where you were and here are
    #: gone, and a listing is what reconciles that.
    oldest_cursor: str | None = field(default=None, kw_only=True)
    #: ``gap``, ``closed`` and ``capabilities``: the platform's own sentence
    #: about what just happened, meant to be read by a person.
    detail: str | None = field(default=None, kw_only=True)
    #: ``capabilities``: the event types this computer can emit, replacing what
    #: the opening frame advertised.
    #:
    #: It goes both ways. A guest that turns out to have no watcher — an image
    #: built without the X bindings — withdraws the guest half after ``hello``
    #: promised it, and a computer stopped and started under an open socket can
    #: ACQUIRE the channel its watcher runs over and get it back.
    events: list[str] | None = field(default=None, kw_only=True)
    #: ``True`` for a ``computer.ready`` this SDK made out of the opening
    #: frame's STATE rather than one the platform sent as an event.
    #:
    #: ``computer.ready`` fires once per desktop SESSION. Attach to a machine
    #: whose desktop is already up — somebody else got there first, or this is
    #: a reconnect — and the event has happened and will not happen again, so
    #: waiting for it over a raw socket waits forever on a computer that has
    #: been ready for an hour. The opening frame says which it is, and this is
    #: that answer arriving in the shape the caller is already reading.
    #:
    #: Flagged rather than passed off as the real thing, because it is not one:
    #: it has no :attr:`seq`, its :attr:`at` is when this client asked rather
    #: than when the desktop came up, and its :attr:`cursor` is the stream's
    #: own position. A caller counting desktop sessions wants it counted; a
    #: caller reconciling against the platform's record wants to know it was
    #: never there.
    synthesized: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class Hello:
    """The opening frame, once per connection.

    Read off :attr:`EventStream.hello`, or handed to an ``on_connect`` hook
    before any of that connection's events are yielded.
    """

    computer: str
    #: Where this stream is for this client. Everything it sends comes after
    #: it, and it is the cursor to store if you disconnect before seeing an
    #: event.
    cursor: str
    #: Whether the desktop has ALREADY been announced ready for the session it
    #: is in. See :attr:`ComputerEvent.synthesized`, which is what this SDK
    #: does with it.
    ready: bool
    #: What THIS computer can emit — not everything the platform knows how to.
    #:
    #: A guest with nowhere to run a watcher never produces the half named in
    #: :data:`GUEST_EVENT_TYPES`, and this list says so rather than leaving a
    #: caller waiting for something that cannot arrive.
    #:
    #: ``None`` where the opening frame did not state one, which is a different
    #: answer from the empty list and is kept apart from it for the same reason
    #: :attr:`ComputerEvent.events` is: an empty vocabulary is a computer that
    #: emits nothing, and :meth:`EventStream.wait_for` refuses against it. A
    #: frame nobody could read is not that computer.
    events: list[str] | None
    #: The desktop as this host last saw it, or ``None``.
    #:
    #: Present — possibly as an empty list — on a connection with no
    #: continuity, and ABSENT when a cursor was honoured, because a resuming
    #: client already holds those windows. The two are different answers, so
    #: this is ``None`` rather than ``[]`` for the second, and the difference is
    #: worth keeping: an empty list means nothing is open. Test for the field
    #: rather than for its length.
    #:
    #: Last SEEN, not guaranteed live. A window whose close happened while this
    #: host had lost its link to the guest is reported into a dead pipe and
    #: stays in the picture, so an entry here can name a window that is already
    #: gone. :meth:`~mandala_computer.Computer.windows` asks the machine and is
    #: the authority on the present; this is what makes a later
    #: ``window.closed`` correlatable.
    windows: list[Window] | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)


def _str(value: Any) -> str:
    """A string field, or ``""`` for anything that is not already one.

    Stricter than :func:`~mandala_computer._models._text`, which stringifies
    whatever it is handed. On this wire the fields it guards are an identity
    and two opaque positions, and ``str(...)`` on a number or a list would turn
    a frame nobody could read into one that looks readable — a cursor of
    ``"[1, 2]"`` sent back as ``since=`` is a resume that fails for a reason
    nothing in the message explains.
    """
    return value if isinstance(value, str) else ""


def to_computer_event(frame: Any) -> ComputerEvent | None:
    """One decoded text frame, as an event.

    ``None`` for ``hello``, which is state rather than an event and is read by
    :func:`to_hello`, and for anything that is not an object with a type. Every
    other frame comes through with its ``type`` intact whether or not this build
    has heard of it — the reference asks a client to ignore an unrecognised
    type, and a client cannot ignore what it was never handed.
    """
    if not isinstance(frame, Mapping):
        return None
    kind = frame.get("type")
    if not isinstance(kind, str) or not kind or kind == "hello":
        return None
    payload = frame.get("data")
    data: Mapping[str, Any] = payload if isinstance(payload, Mapping) else {}
    promoted: dict[str, Any] = {}
    if kind in ("window.opened", "window.focused"):
        promoted["window"] = Window.from_api(data)
    elif kind in ("window.closed", "window.blurred"):
        promoted["window_id"] = _text(data.get("id")) or None
    elif kind == "process.exited":
        # Classified with `_wire`, like every other boolean off this wire,
        # rather than by truthiness. The polarity matters more here than it
        # does on `hello.ready`: a `lost` this client cannot read is a command
        # whose outcome is unknown, and calling that False presents it as one
        # that finished.
        lost = _wire(data, "lost") is _Wire.TRUE
        promoted["pid"] = _opt_num(data.get("pid"))
        promoted["lost"] = lost
        # `lost` and `exit_code` are exclusive on the wire and stay exclusive
        # here: a truthy `lost` beside a number would hand a caller both an
        # exit code and a statement that there is none.
        #
        # The reverse does NOT hold, and this does not invent it. A frame
        # carrying neither is a malformed one, and answering it with
        # `lost=True` would be this client asserting the guest lost track of a
        # command nobody said that about.
        promoted["exit_code"] = None if lost else _opt_num(data.get("exit_code"))
    elif kind == "clipboard.changed":
        promoted["selection"] = _text(data.get("selection")) or None
    elif kind in ("computer.started", "computer.stopped", "computer.suspended"):
        promoted["status"] = _text(data.get("status")) or None
        promoted["previous"] = _text(data.get("previous")) or None
    elif kind == "computer.idle":
        promoted["idle_seconds"] = _opt_num(data.get("idle_seconds"))
    elif kind == "gap":
        promoted["oldest_cursor"] = _text(data.get("oldest_cursor")) or None
        promoted["detail"] = _text(data.get("detail")) or None
    elif kind == "closed":
        # Not an event frame: `detail` sits at the top level beside `type`, and
        # there is no cursor, sequence or source to read.
        promoted["detail"] = _text(frame.get("detail")) or None
    elif kind == "capabilities":
        promoted["detail"] = _text(frame.get("detail")) or None
        promoted["events"] = _opt_texts(frame.get("events"))
    return ComputerEvent(
        type=kind,
        at=_str(frame.get("at")),
        computer=_str(frame.get("computer")),
        cursor=_str(frame.get("cursor")),
        source=_str(frame.get("source")),
        data=dict(data),
        raw=dict(frame),
        # A statement ABOUT the stream is not a position IN it, and the
        # envelope has to say so however the host spelled it. The platform
        # shipped a gap carrying `"seq": 0` once, and a client applying the
        # obvious rule discarded the one frame that reports unrecoverable loss.
        # Copying that zero through would leave this field's own promise false
        # against exactly the build that made the promise necessary.
        seq=None if kind in STREAM_FRAME_TYPES else _opt_num(frame.get("seq")),
        **promoted,
    )


def to_hello(frame: Any) -> Hello | None:
    """The opening frame, or ``None`` for anything that is not one."""
    if not isinstance(frame, Mapping) or frame.get("type") != "hello":
        return None
    rows = frame.get("windows")
    windows = (
        [Window.from_api(w) for w in rows if isinstance(w, Mapping)]
        if isinstance(rows, list)
        else None
    )
    return Hello(
        computer=_str(frame.get("computer")),
        cursor=_str(frame.get("cursor")),
        # TRUE only, and this is the recoverable direction rather than the
        # symmetric one. A readiness nobody claimed is a readiness to wait for,
        # which ends at the caller's own timeout; concluding a desktop is up
        # because a field was malformed hands an agent a screen that is still
        # booting.
        ready=_wire(frame, "ready") is _Wire.TRUE,
        # `_opt_texts`, for the reason it exists: `_texts` answers `[]` for
        # anything that is not a list, and `[]` here is a computer that can
        # emit nothing — which `wait_for` refuses outright. A hello that omits
        # `events`, or spells it as a bare string, would end every wait on a
        # perfectly healthy desktop. `None` is "did not say", and a vocabulary
        # nobody stated is not one to refuse a wait against (OPL-4222).
        events=_opt_texts(frame.get("events")),
        windows=windows,
        raw=dict(frame),
    )


def with_cursor(url: str, cursor: str | None) -> str:
    """``events_url`` with a resume position on it, keeping the credential intact.

    Appended rather than assembled through :mod:`urllib.parse`, which
    normalises the path and re-encodes the query it parsed. The token in this
    URL was percent-encoded by the platform, and a round trip through a parser
    is a chance to change one byte of a credential.
    """
    if not cursor:
        return url
    from urllib.parse import quote

    return f"{url}{'&' if '?' in url else '?'}since={quote(cursor, safe='')}"


# --- refusals that will not clear ------------------------------------------

#: Marks an error that will not become a different answer by being asked again.
#:
#: The three the reference names — a suspended computer, a stopped one, and a
#: computer this platform no longer holds — are decisions rather than weather. A
#: reconnect loop over one is a client asking the same question every fifteen
#: seconds forever and never saying the answer out loud.
#:
#: An attribute on the error rather than a class of its own, because the error
#: is often whatever the platform already produced — a ``NotFoundError`` for a
#: computer that is gone, an ``AuthenticationError`` for a revoked key — and
#: wrapping those would hide the type a caller already knows how to catch.
_SETTLED = "_mandala_settled"

_E = MandalaError


def _settle(err: _E) -> _E:
    setattr(err, _SETTLED, True)
    return err


def _is_settled(err: BaseException) -> bool:
    return getattr(err, _SETTLED, False) is True


def unreachable_types(
    computer_id: str, wanted: Sequence[str], advertised: Sequence[str] | None
) -> MandalaError | None:
    """The refusal for a wait whose event cannot arrive, or ``None``.

    Only where NONE of the wanted types is possible. A caller waiting for
    ``process.exited`` or ``computer.ready`` on a guest with no watcher is
    still waiting for something reachable, and refusing that would be this SDK
    deciding the half they can have is not the half they meant.

    The three stream-control frames are always reachable and are never in the
    advertised list, which is about the COMPUTER. Counting them as impossible
    would refuse ``wait_for("gap")`` — a reasonable thing to wait for, and the
    one this list has no opinion about.

    ``None`` for ``advertised`` is a vocabulary nobody stated, and refuses
    nothing. This is a refusal built entirely out of what the computer said it
    can do, so where it said nothing there is no ground to stand on — and the
    alternative, treating silence as the empty list, ends every wait on a
    desktop whose opening frame was merely malformed (OPL-4222).
    """
    if advertised is None:
        return None
    reachable = set(advertised) | set(STREAM_FRAME_TYPES)
    # Deduplicated FIRST. Counting `impossible` with duplicates in it and
    # comparing against the distinct types asked for made the two sides count
    # different things, so `["missing", "missing", "process.exited"]` refused a
    # wait whose second type the computer advertises (Codex review).
    # `dict.fromkeys` rather than a set, so the sentence below names them in
    # the order the caller wrote them.
    asked = list(dict.fromkeys(wanted))
    impossible = [t for t in asked if t not in reachable]
    if len(impossible) < len(asked):
        return None
    can = ", ".join(advertised) or "nothing"
    return _settle(
        MandalaError(
            f"{computer_id} cannot emit {' or '.join(impossible)}, so waiting for it would "
            f"never end. It advertises: {can}."
        )
    )


# --- what neither half decides on its own ----------------------------------


class _Ended(Exception):
    """The socket closed. Internal; never reaches a caller."""


class _Expired(Exception):
    """The stream's own deadline passed. Internal; ends the iteration quietly."""


@dataclass
class _Core:
    """Every decision a stream makes, and no IO whatsoever.

    The sync and async halves differ in how they wait and in nothing else, so
    this is where the reasoning lives: which frame is an event, when the cursor
    moves, what the opening frame settles, how long to back off, and whether a
    failure is worth another attempt. A rule written here is a rule both halves
    follow by construction rather than by review.
    """

    reconnect: bool
    backoff: float
    max_backoff: float
    max_retries: int
    connect_timeout: float
    cursor: str | None
    on_connect: Callable[[Hello], None] | None

    hello: Hello | None = None
    types: list[str] | None = None
    #: Consecutive failures to get a working connection.
    failures: int = 0
    #: The current backoff step, doubling towards :attr:`max_backoff`.
    step: float = 0.0

    def __post_init__(self) -> None:
        # CAPPED at the ceiling, not merely doubled towards it. `backoff` is
        # the first step and `max_backoff` is documented as the most any step
        # may be, so a caller who passes `backoff=30, max_backoff=15` was
        # asking for at most fifteen and used to get thirty once — the one
        # sleep the ceiling exists to bound, unbounded (Codex review).
        self.step = min(self.backoff, self.max_backoff)

    # -- the opening frame --------------------------------------------------

    def accept_hello(self, hello: Hello) -> ComputerEvent | None:
        """What the opening frame settles, and the one event it can imply.

        Returns a synthesized ``computer.ready`` where the readiness could not
        have arrived as an event, and ``None`` otherwise. Yielding it ahead of
        the connection's own frames is the caller's job, and the order matters:
        a socket that says hello and closes in the same breath — which is what
        a host putting a slow subscriber down looks like — must still deliver
        the readiness that frame implied.
        """
        self.hello = hello
        self.types = None if hello.events is None else list(hello.events)
        # The cursor a client stores when it disconnects before seeing an
        # event. Adopted only when nothing has been consumed, because it names
        # a position BEFORE the backlog this connection is about to deliver:
        # taken while events were pending it would resume in front of frames
        # the caller has already been given, and they would arrive twice.
        #
        # `not self.cursor` rather than `is None`, and the empty string is why
        # this object's own constructor already writes `cursor=since or None`:
        # `""` is not a position, but it is not `None` either, so once one was
        # stored this branch never fired again. `_str` answers `""` for a hello
        # that omits `cursor` or spells it as a non-string — so the wire could
        # plant exactly the value the constructor guards against, and every
        # reconnect for the life of the stream rejoined at the head and
        # redelivered the same prefix (adversarial review, OPL-4222).
        # `consumed` only ever stores a non-empty cursor, so "nothing has been
        # consumed" and "this is falsy" are the same question.
        if not self.cursor and hello.cursor:
            self.cursor = hello.cursor
        # The readiness a subscriber would otherwise never hear about, and ONLY
        # where it could not arrive as an event.
        #
        # `windows` is present exactly when this connection has no continuity —
        # no cursor, or one that gapped — which is the platform's own test, and
        # the reason it is read here rather than `since` being remembered. With
        # continuity there is nothing to make up: either this client was
        # already sent the readiness on an earlier connection, or the event
        # that would have carried it is in the backlog about to arrive.
        # Manufacturing one there puts a second `computer.ready` in front of
        # the real one, and the reference tells a client to read that as a
        # desktop it has not seen — so the invention would be a session
        # replacement that never happened.
        #
        # PER CONNECTION, not once per stream, and this is where the Python
        # half parts company with mandala-computer-typescript's `#sawReady`
        # (Codex review). That latch survives reconnects, so a stream which
        # was handed session A's readiness and then reconnected WITHOUT
        # continuity — the gapped resume, where the backlog is by definition
        # gone — declined to synthesize for session B. Restarting the display
        # manager inside a guest destroys the desktop and brings up a new one
        # without the computer ever leaving `running`, so session B is a real
        # thing to be waiting for, and its own `computer.ready` is exactly what
        # the gap says was lost. The latch turned that into a wait that cannot
        # end, which is the failure this synthesis exists to prevent.
        #
        # The cost of the other direction is a duplicate: a second synthesized
        # readiness for a session the caller had already been told about, which
        # the reference says to read as a desktop it has not seen and reconcile
        # with a listing. That is one wasted `windows()` call, next to a
        # `wait_for("computer.ready")` that never returns — and the duplicate
        # only ever arrives immediately before the `gap` frame saying the same
        # thing. It is also flagged `synthesized`, so a caller reconciling
        # against the platform's own record can see it was never on the wire.
        if hello.ready and hello.windows is not None:
            return self.ready_from_hello(hello)
        return None

    def opening(self, hello: Hello, before: list[Any]) -> list[ComputerEvent]:
        """What a new connection owes the caller before its own frames.

        The readiness the opening frame implied, and then anything that
        arrived ahead of that frame. The reference says ``hello`` comes first
        and the platform sends it first, so the second list is empty in
        practice — but a frame dropped here would be silent loss on the one
        stream whose whole purpose is not to have any, and the cost of being
        wrong the other way is a loop over nothing.
        """
        ready = self.accept_hello(hello)
        out = [] if ready is None else [ready]
        for frame in before:
            ev = self.interpret(frame)
            if ev is not None:
                out.append(ev)
        return out

    def ready_from_hello(self, hello: Hello) -> ComputerEvent:
        """A ``computer.ready`` built from the opening frame's state.

        It carries THIS STREAM'S position rather than hello's, which are the
        same thing on a fresh join and are not on a resume: hello's cursor
        names where the connection starts, which on a gapped resume is behind
        the ``since`` the caller already holds. Yielded, that would rewind the
        stream's own position and put it in front of the ``gap`` — so a wait
        returning on this event would hand back a cursor pointing into history
        it had just been told was unrecoverable.
        """
        return ComputerEvent(
            type="computer.ready",
            at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            computer=hello.computer,
            cursor=self.cursor or hello.cursor,
            source="guest",
            data={},
            raw={},
            synthesized=True,
        )

    def call_on_connect(self, hello: Hello) -> None:
        """The caller's hook, run where an exception from it means what it says.

        Deliberately not called from inside the connect path's own error
        handling. A hook that raises there would be indistinguishable from a
        connection that failed, so the stream would back off and try again —
        running the hook once per attempt, forever, over a problem that is in
        the caller's code. Here it ends the stream, which is what an
        unhandled exception is supposed to do.
        """
        if self.on_connect is not None:
            self.on_connect(hello)

    # -- frames -------------------------------------------------------------

    def interpret(self, frame: Any) -> ComputerEvent | None:
        """One frame as an event, with the bookkeeping that happens on the way past."""
        ev = to_computer_event(frame)
        if ev is None:
            return None
        if ev.type == "capabilities" and ev.events is not None:
            self.types = list(ev.events)
        return ev

    def consumed(self, ev: ComputerEvent) -> None:
        """Move the position, having handed this event to the caller.

        Called BEFORE the yield, because the yield is the handover: a consumer
        that takes an event and breaks out of its loop never resumes the
        generator, so a line after the yield does not run for the one event
        most likely to be the last. Left there, the cursor was stale by exactly
        one on every early exit and a caller storing it was handed that event a
        second time on resume.

        What it must not do is move for an event nobody has been given, and it
        does not: a frame still unread when a socket dies was never consumed,
        so the position never reached it and the reconnect asks for it again.
        """
        if ev.cursor:
            self.cursor = ev.cursor

    # -- retrying -----------------------------------------------------------

    def worked(self) -> None:
        """A connection delivered something, so the failure count starts over.

        Deliberately not "the handshake succeeded". A host that accepts a
        socket and drops it a millisecond later succeeds at every handshake, so
        resetting there is a reconnect loop that can never reach
        :attr:`max_retries` and never backs off. A connection that delivered an
        event was a working connection; one that did not was an attempt.
        """
        self.failures = 0
        # Capped, for the reason __post_init__ is: resetting to backoff after a
        # delivering connection used to undo that cap, so the next wait_out()
        # slept the uncapped first step.
        self.step = min(self.backoff, self.max_backoff)

    def after_failure(self, err: MandalaError | None) -> tuple[bool, MandalaError | None]:
        """What to do about a connection that has just ended or failed to start.

        Answers ``(reconnect?, error to raise)``. ``(True, None)`` is another
        attempt; ``(False, None)`` is the iteration ending quietly, which is
        what a socket closing means to a caller who turned reconnect off; and
        ``(False, err)`` is the one case that raises.

        Counts this attempt as it goes, so it is called exactly once per
        connection that did not survive.
        """
        if not self.reconnect:
            # The socket ending IS the answer here, so an ordinary close ends
            # the iteration rather than being reported as a failure to reopen
            # something nobody asked to have reopened.
            return False, err
        if err is not None and _is_settled(err):
            # A refusal about the computer's STATE — suspended, stopped, gone —
            # does not clear by being asked again. Raised even with reconnect
            # on, because the alternative is this loop knocking on a machine
            # that is off every fifteen seconds for as long as the process
            # lives, and never saying the answer out loud.
            return False, err
        self.failures += 1
        if self.max_retries > 0 and self.failures > self.max_retries:
            return False, err or _not_reopened()
        return True, None

    def wait_out(self) -> float:
        """How long to sleep before the next attempt, doubling as it goes."""
        pause = self.step
        self.step = min(self.step * 2, self.max_backoff)
        return pause


def _not_reopened() -> ConnectionError:
    """The failure a retry budget runs out on with nothing else to report.

    Built per raise rather than kept as a module-level instance: an exception
    is stateful — its traceback and its ``__context__`` are attached when it is
    raised — so a shared one accumulates the history of every stream that ever
    gave up, and the second caller to see it reads the first caller's chain.
    """
    return ConnectionError("the event stream closed and could not be reopened")


def _check_numbers(
    *, backoff: float, max_backoff: float, max_retries: int, connect_timeout: float
) -> None:
    """Refused before a socket is opened, for the reason every wait here is.

    A non-finite backoff is an unthrottled reconnect loop against the platform,
    and a non-finite connect timeout is a handshake nobody ever gives up on.
    Both are the failure shape this SDK refuses everywhere else it takes a
    number: a wait that never ends is worse than a wrong answer, because
    nothing about it says anything is wrong.
    """
    for name, value in (
        ("backoff", backoff),
        ("max_backoff", max_backoff),
        ("connect_timeout", connect_timeout),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise MandalaError(f"{name} must be a number (got {value!r})")
        # `not (value > 0)` catches NaN as well as zero and the negatives — a
        # NaN fails every comparison, which is exactly the value that would
        # otherwise become a sleep of no length and an unthrottled reconnect
        # loop against the platform.
        if not (value > 0) or value == float("inf"):
            raise MandalaError(f"{name} must be a positive finite number (got {value!r})")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise MandalaError(f"max_retries must be a non-negative integer (got {max_retries!r})")


def _decode(message: Any) -> Any:
    """A frame's JSON, or ``None`` for anything that is not a text frame of it.

    Binary frames are dropped rather than decoded: nothing on this stream sends
    one, and a client guessing at its meaning would be inventing a shape. So is
    a text frame that is not JSON — a proxy's error page reaching a websocket is
    not an event, and a parse failure is not worth ending a stream over.
    """
    if not isinstance(message, str):
        return None
    try:
        return json.loads(message)
    except ValueError:
        return None


def _refusal(computer_id: str, status: int, body: bytes | bytearray | None) -> MandalaError:
    """Why the upgrade was refused, from what the refusal actually said.

    This is where the Python half parts company with
    mandala-computer-typescript, and it parts company because it can. A browser
    ``WebSocket`` exposes neither the status line nor the body of a failed
    upgrade — a 409, a 401 and a TCP reset all reach that client as an error
    with an empty message — so the TypeScript SDK infers the reason from a
    follow-up ``GET computers/{id}``, and says so wherever it reports one.
    ``websockets`` hands over both, which the CLI has relied on since ``mandala
    ssh`` shipped, so the two refusals the reference names are read rather than
    guessed: no second request, no window in which the machine changes state
    between the failure and the explanation, and no inference to disclaim.
    """
    said = None
    if body:
        try:
            said = json.loads(body)
        except ValueError:
            said = None
        if not isinstance(said, Mapping):
            said = None
    detail = _text(said.get("error")) if said else ""
    if status == 409:
        if said is not None and _wire(said, "resume_required") is _Wire.TRUE:
            return _settle(
                MandalaError(
                    f"{computer_id} is suspended, and the event stream is the one part of this "
                    "API that does not resume a computer for you: call start() and open it again."
                )
            )
        if said is not None and said.get("reason") == "unavailable":
            return _settle(
                MandalaError(
                    f"{computer_id} is not running, and only a running computer has an event "
                    "stream: call start() and open it again."
                )
            )
        # A 409 this build has not seen the shape of. Settled anyway: every
        # documented one on this route is about the machine's state, and a
        # conflict is by definition not something the same request resolves.
        return _settle(
            MandalaError(
                f"{computer_id}'s event stream was refused with a 409"
                + (f": {detail}" if detail else "")
            )
        )
    if status in (401, 403):
        return _settle(
            MandalaError(
                f"{computer_id}'s event stream refused this credential (HTTP {status}). A "
                "watch-only connect surface carries no events_url at all, because a window "
                "title is content." + (f" {detail}" if detail else "")
            )
        )
    if status == 404:
        return _settle(
            MandalaError(
                f"{computer_id} has no event stream here (HTTP 404). Its host may predate the "
                "stream, or the computer may be gone." + (f" {detail}" if detail else "")
            )
        )
    # Everything else is weather until proven otherwise — a 502 from an edge, a
    # 503 from a host that is still coming up — and the backoff is the right
    # response to weather.
    return ConnectionError(
        f"{computer_id}'s event stream would not open (HTTP {status})"
        + (f": {detail}" if detail else "")
    )


# --- the sockets ------------------------------------------------------------
#
# Module-level rather than inlined so a test can put its own connection behind
# them, which is what every websocket test in this suite does. The imports are
# deferred for the reason `_cli` defers its own: `websockets` is a core
# dependency because `mandala ssh` needs it, but importing the package costs a
# few milliseconds that a caller who only ever makes API calls should not pay.


def _connect(url: str, *, open_timeout: float, max_queue: int) -> Any:
    from websockets.sync.client import connect

    return connect(url, open_timeout=open_timeout, max_queue=max_queue)


async def _aconnect(url: str, *, open_timeout: float, max_queue: int) -> Any:
    from websockets.asyncio.client import connect

    return await connect(url, open_timeout=open_timeout, max_queue=max_queue)


#: How many frames may sit in the library's receive buffer before it stops
#: reading from the socket.
#:
#: This is real backpressure rather than a policy this SDK has to implement:
#: ``websockets`` stops draining the TCP window when the buffer is full, the
#: platform's own send blocks, and its subscriber-too-slow logic — which puts a
#: stalled reader down with a ``closed`` frame — is what decides what happens
#: next. Nothing is dropped on either side, which is the property that matters:
#: silent loss is the exact failure this whole stream exists to end.
#:
#: mandala-computer-typescript has to close its own socket at a threshold and
#: reconnect, because a browser ``WebSocket`` delivers into a listener and
#: cannot be paused. Here the reader pulls, so there is nothing to bound.
DEFAULT_MAX_QUEUE = 4096


def _connect_failed(computer_id: str, exc: BaseException) -> MandalaError:
    """A websocket that would not open, as this SDK's own error.

    Split by whether asking again could answer differently. A refused upgrade
    carries a status and is read by :func:`_refusal`; a URL that is not a
    websocket URL at all is a settled fact about the connect surface; anything
    else — a reset, a DNS failure, a handshake that timed out — is weather, and
    the backoff is the right response to weather.
    """
    from websockets.exceptions import InvalidStatus, InvalidURI, WebSocketException

    if isinstance(exc, InvalidStatus):
        return _refusal(computer_id, exc.response.status_code, exc.response.body)
    if isinstance(exc, InvalidURI):
        return _settle(MandalaError(f"{computer_id}'s events_url is not a websocket URL: {exc}"))
    # ``asyncio.TimeoutError`` alongside the builtin because they are separate
    # classes on 3.10, which ``requires-python`` still admits: without it an
    # ``open_timeout`` on that version leaves this function through the
    # ``raise`` below and reaches the caller as something that is not a
    # MandalaError at all. ``_sleep`` in this file already spells it this way.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError, WebSocketException)):
        return ConnectionError(f"{computer_id}'s event stream would not open: {exc}")
    raise exc


def _lost(computer_id: str, exc: BaseException) -> MandalaError:
    """A socket that failed mid-stream, as this SDK's own error."""
    return ConnectionError(f"{computer_id}'s event stream failed: {exc}")


def _shut_quietly(sock: Any) -> None:
    """Close a socket, and have nothing to say about a close that failed.

    Every failure, not only the library's own: the state this is trying to
    reach is a socket that is not open, and a socket that is already gone —
    or one whose close raised something this SDK has never heard of — is
    already in it. There is no second thing to try and nobody to tell, and an
    exception raised here would displace whatever sent the stream home.
    """
    try:
        sock.close()
    except Exception:  # noqa: BLE001, S110
        pass


async def _ashut_quietly(sock: Any) -> None:
    """:func:`_shut_quietly`, awaited."""
    try:
        await sock.close()
    except Exception:  # noqa: BLE001, S110
        pass


def _already_consumed() -> MandalaError:
    return MandalaError(
        "this event stream is already being consumed — open a second one with events() "
        "rather than iterating this one twice"
    )


def _said_nothing(computer_id: str, seconds: float) -> ConnectionError:
    return ConnectionError(
        f"{computer_id}'s event stream opened but said nothing within {seconds:g}s"
    )


def _closed_early(computer_id: str) -> ConnectionError:
    return ConnectionError(f"{computer_id}'s event stream closed before it said what it was")


class _StreamBase:
    """What the two halves share below the IO: options, state, and the reading.

    Not an abstract base with hooks — the halves genuinely differ in how they
    wait, and pretending otherwise would put an ``await`` in a method the sync
    side calls. What lives here is everything that is the same either way, so
    that the two loops below are only ever the shape of the waiting.
    """

    def __init__(
        self,
        url: Callable[[float], Any],
        computer_id: str,
        *,
        since: str | None = None,
        reconnect: bool = True,
        backoff: float = 0.5,
        max_backoff: float = 15.0,
        max_retries: int = 0,
        connect_timeout: float = 15.0,
        max_queue: int = DEFAULT_MAX_QUEUE,
        on_connect: Callable[[Hello], None] | None = None,
        timeout: float | None = None,
    ) -> None:
        _check_numbers(
            backoff=backoff,
            max_backoff=max_backoff,
            max_retries=max_retries,
            connect_timeout=connect_timeout,
        )
        if isinstance(max_queue, bool) or not isinstance(max_queue, int) or max_queue < 1:
            raise MandalaError(f"max_queue must be a positive integer (got {max_queue!r})")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not (timeout > 0)
            or timeout == float("inf")
        ):
            # Finite as well as positive, and for the reason `_api.real` gives:
            # NaN fails every ordered comparison, so `timeout > 0` alone lets it
            # through and it becomes a deadline that never arrives; infinity
            # passes honestly and means the same thing. A wait that never ends
            # is the one failure shape worse than a wrong answer, because
            # nothing about it says anything is wrong.
            raise MandalaError(
                f"timeout must be a positive finite number or None (got {timeout!r})"
            )
        self._url = url
        self._id = computer_id
        self._max_queue = max_queue
        self._timeout = None if timeout is None else float(timeout)
        self._core = _Core(
            reconnect=reconnect,
            backoff=float(backoff),
            max_backoff=float(max_backoff),
            max_retries=max_retries,
            connect_timeout=float(connect_timeout),
            # `or None`, because `""` is not a position. `with_cursor` already
            # treats it as no cursor and joins at the head — but stored as
            # `""` it is not `None` either, so `accept_hello` declined to adopt
            # the opening frame's cursor and a reconnect joined at the head a
            # SECOND time, skipping whatever happened in between. An empty
            # string is the shape an unset environment variable arrives in, so
            # this is the likely way to hold it wrong (Codex review).
            cursor=since or None,
            on_connect=on_connect,
        )
        self._iterated = False
        self._deadline: float | None = None
        self._sock: Any = None

    # -- what the connection said -------------------------------------------

    @property
    def hello(self) -> Hello | None:
        """The opening frame of the connection currently open, or ``None``.

        Replaced on every reconnect, so :attr:`Hello.windows` is the desktop as
        of the newest connection — and ``None`` there when that connection
        resumed from a cursor, which is the platform saying "you already hold
        this picture" rather than "nothing is open".
        """
        return self._core.hello

    @property
    def event_types(self) -> list[str] | None:
        """What this computer can emit, as last stated.

        The opening frame's list, replaced by any ``capabilities`` frame since.
        The one thing worth checking it for is a wait that cannot end: an image
        built without the X bindings the watcher needs emits no ``window.*`` and
        no ``computer.ready``, and waiting on one of those is waiting for
        something the platform has already said will not arrive.
        """
        # A COPY. The list behind this is what decides whether a wait can end —
        # `wait_for` refuses a type that is not in it — so handing out the live
        # one would let an accidental `append` talk a wait into hanging on a
        # machine that cannot produce the event.
        return None if self._core.types is None else list(self._core.types)

    @property
    def windows(self) -> list[Window] | None:
        """The desktop the newest connection joined, where it was sent one.

        ``None`` on a connection that resumed from a cursor the host could
        honour, because a resuming client already holds those windows. Empty
        means nothing is open — test for ``is None`` rather than for length.
        """
        hello = self._core.hello
        if hello is None or hello.windows is None:
            return None
        # A copy of the list, for the reason `event_types` is copied. The
        # windows in it are frozen records and are not copied themselves.
        return list(hello.windows)

    @property
    def expired(self) -> bool:
        """Whether this stream stopped because its own ``timeout`` elapsed.

        The difference between a wait that ran out of time and a stream that
        ended on its own, which are the same silence from inside a loop and
        want opposite sentences said about them.
        """
        return self._deadline is not None and self._left() == 0.0

    @property
    def cursor(self) -> str | None:
        """The position after the last event this stream YIELDED.

        What a reconnect resumes from, and what to store if you keep your own
        place across a process restart. Advanced at the yield rather than on
        arrival, which is the whole of why a reconnect does not lose frames
        that were still unread when a socket died: they were never consumed, so
        the position never moved past them.
        """
        return self._core.cursor

    # -- reading -------------------------------------------------------------

    def _left(self) -> float | None:
        """Seconds until this stream's own deadline, or ``None`` if it has none."""
        if self._deadline is None:
            return None
        return max(self._deadline - time.monotonic(), 0.0)

    def _budget(self) -> float:
        """How long getting a usable connection may take, deadline included.

        ONE budget across all three phases — the read that fetches a fresh
        ``events_url``, the handshake, and the opening frame — and not one
        each. ``connect_timeout`` is the one number a caller sets to bound how
        long a dead connection ties them up, so three sequential timers of that
        length would mean five seconds asked for and forty-five waited.

        The URL read is in it because it was the phase nobody was bounding:
        it goes through the ordinary transport, whose own timeout is a minute,
        so `wait_for(timeout=5)` could sit in that read for sixty seconds
        before anything checked the deadline it was given (Codex review).
        """
        left = self._left()
        budget = self._core.connect_timeout
        return budget if left is None else min(budget, left)

    def _begin(self) -> None:
        """Claim the single consumer, and start the clock."""
        if self._iterated:
            raise _already_consumed()
        self._iterated = True
        # Set at iteration rather than at construction, because the deadline
        # bounds the WAITING and a stream that is built and iterated a minute
        # later has not been waiting for a minute.
        if self._timeout is not None:
            self._deadline = time.monotonic() + self._timeout

    def _drop(self) -> Any:
        """Take the socket off this object, for the caller to shut."""
        sock, self._sock = self._sock, None
        return sock

    def _read_hello(self, frame: Any, buffered: list[Any]) -> Hello | None:
        """Sort one pre-hello frame, answering the opening frame when it lands.

        Anything that is not the opening frame is kept rather than dropped. The
        reference says ``hello`` comes first and the platform sends it first,
        so this buffer is empty in practice — but a frame discarded here would
        be silent loss on the one stream whose whole purpose is not to have
        any, and the cost of being wrong the other way is a list that stays
        empty.
        """
        hello = to_hello(frame)
        if hello is None:
            if frame is not None:
                buffered.append(frame)
            return None
        return hello


class EventStream(_StreamBase):
    """A computer's event stream: an iterator that reconnects and keeps its place.

    Obtained from :meth:`~mandala_computer.Computer.events`, not built
    directly::

        for ev in c.events():
            if ev.type == "process.exited" and ev.pid == job.pid:
                break

    **Single-consumer.** One socket, one position — a second loop over the same
    object would split the events between the two rather than give each of them
    all, which is not what anybody writing the second loop means, so it is
    refused instead.

    Breaking out of the loop leaves the socket to be closed by the generator's
    own cleanup, which CPython does promptly and other implementations do
    eventually. :func:`contextlib.closing`, or :meth:`close`, is how to be sure::

        with closing(c.events()) as stream:
            for ev in stream:
                ...
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # An Event rather than a bool, so that a `close()` from another thread
        # ends the backoff sleep as well as the socket. Without it a stream
        # that a caller stopped mid-backoff went on sleeping for up to
        # `max_backoff` before noticing.
        self._stop = threading.Event()

    def __enter__(self) -> EventStream:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop the stream and release the socket. Safe to call more than once.

        Safe from another thread, and that is the point of it: the closing
        handshake unblocks a reader parked on a socket with nothing to say.
        """
        self._stop.set()
        sock = self._drop()
        if sock is not None:
            _shut_quietly(sock)

    def _stopped(self) -> bool:
        return self._stop.is_set() or self.expired

    def _sleep(self, seconds: float) -> bool:
        """Back off, endably. ``True`` if the stream should stop instead."""
        left = self._left()
        if left is not None:
            seconds = min(seconds, left)
        self._stop.wait(max(seconds, 0.0))
        return self._stopped()

    def __iter__(self) -> Iterator[ComputerEvent]:
        self._begin()
        try:
            yield from self._run()
        finally:
            self.close()

    def _run(self) -> Iterator[ComputerEvent]:
        core = self._core
        while True:
            if self._stopped():
                return
            try:
                hello, pending = self._open()
            except _Expired:
                return
            except MandalaError as err:
                self._shut()
                if self._stopped():
                    return
                again, fatal = core.after_failure(err)
                if fatal is not None:
                    raise fatal
                if not again or self._sleep(core.wait_out()):
                    return
                continue
            # Outside the try above on purpose: see `_Core.call_on_connect`.
            core.call_on_connect(hello)
            if self._stopped():
                return
            delivered = 0
            failed: MandalaError | None = None
            for ev in pending:
                core.consumed(ev)
                # NOT counted when this SDK made it up. `delivered` is what
                # decides whether `worked()` clears the failure count, and a
                # synthesized `computer.ready` is a reading of the opening
                # frame rather than something the connection carried — so a
                # host that says hello and drops the socket in the same breath
                # produced one every cycle, reset the backoff every cycle, and
                # never reached `max_retries`. That is precisely the loop
                # `_Core.worked`'s own docstring says it exists to prevent
                # (adversarial review, OPL-4222). Still yielded: the readiness
                # it reports is real, and a caller waiting on it must hear it.
                delivered += 0 if ev.synthesized else 1
                yield ev
                if self._stopped():
                    return
            while True:
                try:
                    frame = self._recv()
                except (_Ended, _Expired):
                    break
                except MandalaError as err:
                    # Kept rather than raised, because what comes after this
                    # loop has to tell it apart from a socket that simply
                    # ended: one is worth reporting when a retry budget runs
                    # out, and the other is the ordinary way a connection
                    # finishes.
                    failed = err
                    break
                event = core.interpret(frame)
                if event is None:
                    continue
                core.consumed(event)
                delivered += 1
                yield event
                if self._stopped():
                    return
            self._shut()
            if self._stopped():
                return
            if delivered:
                core.worked()
            again, fatal = core.after_failure(failed)
            if fatal is not None:
                raise fatal
            if not again or self._sleep(core.wait_out()):
                return

    def _shut(self) -> None:
        sock = self._drop()
        if sock is not None:
            _shut_quietly(sock)

    def _open(self) -> tuple[Hello, list[ComputerEvent]]:
        """One socket, opened and asked what it is.

        Returns only once the opening frame has landed, so everything this
        object can be asked — the vocabulary, the desktop, whether the machine
        is already ready — is true before the first event of the new connection
        reaches anybody.
        """
        budget = self._budget()
        if budget <= 0:
            raise _Expired
        started = time.monotonic()
        url = with_cursor(self._url(budget), self._core.cursor)
        left = budget - (time.monotonic() - started)
        if left <= 0:
            raise ConnectionError(
                f"reading a fresh events_url for {self._id} used the whole {budget:g}s "
                "connect budget"
            )
        try:
            sock = _connect(url, open_timeout=left, max_queue=self._max_queue)
        except Exception as exc:
            raise _connect_failed(self._id, exc) from exc
        self._sock = sock
        buffered: list[Any] = []
        hello: Hello | None = None
        while hello is None:
            if budget - (time.monotonic() - started) <= 0:
                self._shut()
                raise _said_nothing(self._id, budget)
            try:
                frame = self._recv(deadline=started + budget)
            except _Ended:
                self._shut()
                raise _closed_early(self._id) from None
            except _Expired:
                self._shut()
                raise
            except MandalaError:
                self._shut()
                raise
            if frame is _TIMED_OUT:
                self._shut()
                raise _said_nothing(self._id, budget)
            hello = self._read_hello(frame, buffered)
        return hello, self._core.opening(hello, buffered)

    def _recv(self, *, deadline: float | None = None) -> Any:
        """The next decoded frame, or ``None`` for one that is not readable.

        ``deadline`` bounds this read alone, which is what the opening frame
        needs; without it the read is bounded only by the stream's own deadline
        and by :meth:`close`.
        """
        sock = self._sock
        if sock is None:
            raise _Ended
        left = self._left()
        if deadline is not None:
            budget = max(deadline - time.monotonic(), 0.0)
            left = budget if left is None else min(left, budget)
        if left is not None and left <= 0:
            raise _Expired
        from websockets.exceptions import ConnectionClosed, WebSocketException

        try:
            message = sock.recv(timeout=left)
        except TimeoutError:
            # WHICH deadline ran out decides what this means, and they are not
            # the same news: the stream's own is the caller giving up, and a
            # connect budget is a socket that opened and then said nothing.
            own = self._left()
            if deadline is not None and (own is None or own > 0):
                return _TIMED_OUT
            raise _Expired from None
        except ConnectionClosed:
            raise _Ended from None
        except (OSError, WebSocketException) as exc:
            raise _lost(self._id, exc) from exc
        return _decode(message)


#: What a read that ran out its connect budget answers with, as distinct from
#: one that ran out the stream's own deadline. Not an exception, because it is
#: not an error at every call site that can see it.
_TIMED_OUT = object()


class AsyncEventStream(_StreamBase):
    """:class:`EventStream`, awaited.

    Obtained from :meth:`~mandala_computer.AsyncComputer.events`::

        async for ev in c.events():
            if ev.type == "process.exited" and ev.pid == job.pid:
                break

    Everything :class:`EventStream` documents applies unchanged; the difference
    is that the waiting yields to the event loop instead of blocking a thread,
    and that cancelling the task iterating this ends it the way
    :meth:`~EventStream.close` does.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Constructed here rather than lazily: since 3.10 an `asyncio.Event`
        # binds no loop until it is awaited, so building one outside a running
        # loop — which is what `events()` does on a handle a caller made
        # earlier — is safe.
        self._stop = asyncio.Event()

    async def __aenter__(self) -> AsyncEventStream:  # noqa: PYI034
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Stop the stream and release the socket. Safe to call more than once."""
        self._stop.set()
        sock = self._drop()
        if sock is not None:
            await _ashut_quietly(sock)

    def close(self) -> None:
        """Stop the stream without waiting for the closing handshake.

        What a hook running inside the stream's own machinery uses — including
        :meth:`~mandala_computer.AsyncComputer.wait_for`'s — because it is
        reached from a place that cannot await. The socket is closed by the
        iteration's own cleanup a moment later; this is what makes it stop.
        """
        self._stop.set()

    def _stopped(self) -> bool:
        return self._stop.is_set() or self.expired

    async def _sleep(self, seconds: float) -> bool:
        """Back off, endably. ``True`` if the stream should stop instead."""
        left = self._left()
        if left is not None:
            seconds = min(seconds, left)
        try:
            await asyncio.wait_for(self._stop.wait(), max(seconds, 0.0))
        except (asyncio.TimeoutError, TimeoutError):
            pass
        return self._stopped()

    async def __aiter__(self) -> AsyncIterator[ComputerEvent]:
        self._begin()
        try:
            async for ev in self._run():
                yield ev
        finally:
            await self.aclose()

    async def _run(self) -> AsyncIterator[ComputerEvent]:
        core = self._core
        while True:
            if self._stopped():
                return
            try:
                hello, pending = await self._open()
            except _Expired:
                return
            except MandalaError as err:
                await self._shut()
                if self._stopped():
                    return
                again, fatal = core.after_failure(err)
                if fatal is not None:
                    raise fatal
                if not again or await self._sleep(core.wait_out()):
                    return
                continue
            core.call_on_connect(hello)
            if self._stopped():
                return
            delivered = 0
            failed: MandalaError | None = None
            for ev in pending:
                core.consumed(ev)
                # NOT counted when this SDK made it up. `delivered` is what
                # decides whether `worked()` clears the failure count, and a
                # synthesized `computer.ready` is a reading of the opening
                # frame rather than something the connection carried — so a
                # host that says hello and drops the socket in the same breath
                # produced one every cycle, reset the backoff every cycle, and
                # never reached `max_retries`. That is precisely the loop
                # `_Core.worked`'s own docstring says it exists to prevent
                # (adversarial review, OPL-4222). Still yielded: the readiness
                # it reports is real, and a caller waiting on it must hear it.
                delivered += 0 if ev.synthesized else 1
                yield ev
                if self._stopped():
                    return
            while True:
                try:
                    frame = await self._recv()
                except (_Ended, _Expired):
                    break
                except MandalaError as err:
                    failed = err
                    break
                event = core.interpret(frame)
                if event is None:
                    continue
                core.consumed(event)
                delivered += 1
                yield event
                if self._stopped():
                    return
            await self._shut()
            if self._stopped():
                return
            if delivered:
                core.worked()
            again, fatal = core.after_failure(failed)
            if fatal is not None:
                raise fatal
            if not again or await self._sleep(core.wait_out()):
                return

    async def _shut(self) -> None:
        sock = self._drop()
        if sock is not None:
            await _ashut_quietly(sock)

    async def _open(self) -> tuple[Hello, list[ComputerEvent]]:
        budget = self._budget()
        if budget <= 0:
            raise _Expired
        started = time.monotonic()
        url = with_cursor(await self._url(budget), self._core.cursor)
        left = budget - (time.monotonic() - started)
        if left <= 0:
            raise ConnectionError(
                f"reading a fresh events_url for {self._id} used the whole {budget:g}s "
                "connect budget"
            )
        try:
            sock = await _aconnect(url, open_timeout=left, max_queue=self._max_queue)
        except Exception as exc:
            raise _connect_failed(self._id, exc) from exc
        self._sock = sock
        buffered: list[Any] = []
        hello: Hello | None = None
        while hello is None:
            if budget - (time.monotonic() - started) <= 0:
                await self._shut()
                raise _said_nothing(self._id, budget)
            try:
                frame = await self._recv(deadline=started + budget)
            except _Ended:
                await self._shut()
                raise _closed_early(self._id) from None
            except _Expired:
                await self._shut()
                raise
            except MandalaError:
                await self._shut()
                raise
            if frame is _TIMED_OUT:
                await self._shut()
                raise _said_nothing(self._id, budget)
            hello = self._read_hello(frame, buffered)
        return hello, self._core.opening(hello, buffered)

    async def _recv(self, *, deadline: float | None = None) -> Any:
        sock = self._sock
        if sock is None:
            raise _Ended
        left = self._left()
        if deadline is not None:
            budget = max(deadline - time.monotonic(), 0.0)
            left = budget if left is None else min(left, budget)
        if left is not None and left <= 0:
            raise _Expired
        from websockets.exceptions import ConnectionClosed, WebSocketException

        # Raced against the stop rather than simply awaited. The sync half is
        # woken by the closing handshake `close()` performs, which is a thing
        # only a thread can do; here `close()` is reached from places that
        # cannot await one — a hook inside the stream's own machinery is the
        # first of them — so the stop has to be something this read watches.
        # Without it a caller who stopped an idle stream waited for the next
        # frame of a stream they had stopped, which on a quiet desktop is
        # indefinitely.
        #
        # Cancelling `recv` is safe on this library and loses no message: the
        # next call returns it. That is what makes the race legitimate rather
        # than a way to drop a frame, and it is why `websockets>=14` is the
        # floor in pyproject.
        reading = asyncio.ensure_future(sock.recv())
        stopping = asyncio.ensure_future(self._stop.wait())
        try:
            done, _ = await asyncio.wait(
                (reading, stopping), timeout=left, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            # Task cancellation reaches here, and both children have to go
            # with it or they outlive the stream as pending tasks asyncio
            # complains about at shutdown.
            reading.cancel()
            raise
        finally:
            stopping.cancel()
        if reading not in done:
            reading.cancel()
            if self._stop.is_set():
                raise _Ended
            # See the sync half: which deadline ran out is the whole meaning.
            own = self._left()
            if deadline is not None and (own is None or own > 0):
                return _TIMED_OUT
            raise _Expired
        try:
            message = reading.result()
        except ConnectionClosed:
            raise _Ended from None
        except (OSError, WebSocketException) as exc:
            raise _lost(self._id, exc) from exc
        return _decode(message)
