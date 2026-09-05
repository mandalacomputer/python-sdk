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
from ._models import Window, _opt_num, _opt_whole, _text, _texts, _Wire, _wire

__all__ = [
    "CHANNEL_EVENT_TYPES",
    "DESKTOP_EVENT_TYPES",
    "GUEST_EVENT_TYPES",
    "STREAM_FRAME_TYPES",
    "WATCH_EVENT_TYPE",
    "AsyncEventStream",
    "ComputerEvent",
    "EventStream",
    "Hello",
    "WatchedTree",
]


#: The frames that are statements about the STREAM rather than about the
#: computer: a hole in the history, this host ending the socket on purpose, and
#: the vocabulary being revised under an open one.
#:
#: They reach a caller as events because a client cannot ignore what it was
#: never handed, and they are named here because they are never in what the
#: opening frame advertises — that list is about what the machine can produce.
STREAM_FRAME_TYPES: tuple[str, ...] = ("gap", "closed", "capabilities")

#: The event type that never arrives unasked.
#:
#: ``file.changed`` is reported only under a directory the STREAM nominated —
#: see the ``watch`` option on :meth:`~mandala_computer.Computer.events`. A
#: computer advertises it whenever it could report one, which is not the same
#: as this socket being able to receive one.
WATCH_EVENT_TYPE: str = "file.changed"

#: The event types whose ``source`` is ``"guest"``: the machine describing
#: itself, rather than the platform describing the machine.
#:
#: This is a statement about PROVENANCE — how much a payload is worth, since
#: anyone with root inside the guest can make one of these say anything — and
#: deliberately not a statement about availability. The two sets below are the
#: availability question, and they do not move together.
GUEST_EVENT_TYPES: tuple[str, ...] = (
    "window.opened",
    "window.closed",
    "window.focused",
    "window.blurred",
    "clipboard.changed",
    "file.changed",
    "computer.ready",
)

#: The guest half that needs the desktop watcher, and therefore both the
#: terminal channel it speaks over AND the X bindings it is written against.
#:
#: A Windows guest and a Linux one whose hardware carries no channel have
#: nowhere to run it; so does a Linux image built without ``python3-xlib``,
#: which the platform cannot know from the record and reports in a
#: ``capabilities`` frame once it has asked.
DESKTOP_EVENT_TYPES: tuple[str, ...] = (
    "window.opened",
    "window.closed",
    "window.focused",
    "window.blurred",
    "clipboard.changed",
    "computer.ready",
)

#: The guest half that needs the terminal channel and NOTHING else.
#:
#: The third case, and the one that catches a client modelling "the guest half"
#: as a single thing. The file watcher is written against libc's own inotify
#: calls through :mod:`ctypes`, which is the standard library — so every Linux
#: image this platform has ever published can emit :data:`WATCH_EVENT_TYPE`,
#: INCLUDING the ones that predate the X bindings and therefore emit no window
#: events at all. A computer whose :attr:`Hello.events` names ``file.changed``
#: and no ``window.*`` is an ordinary computer, not a malformed frame.
CHANNEL_EVENT_TYPES: tuple[str, ...] = ("file.changed",)


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
    #: Worth reading. Every ``window.*``, ``clipboard.changed``,
    #: ``file.changed`` and ``computer.ready`` is the tenant's own machine
    #: describing itself, and
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
    #: ``None`` where :attr:`lost` is true, and the pair is the whole point:
    #: ``-1`` is already a real exit code on this path, so it could not also
    #: mean "no answer". Nothing is invented for a command whose outcome the
    #: platform does not know.
    #:
    #: Also ``None`` — with :attr:`lost` FALSE — where the platform sent a code
    #: this client could not read as a whole number. That pair is rarer and
    #: says something narrower: the guest reported an outcome and the envelope
    #: carrying it was malformed. It is ``None`` rather than a number because
    #: the obvious reading of a fractional code truncates, and ``int(0.9)`` is
    #: ``0`` — a failed command presented as a successful one, which is the
    #: misread ``_exit_code`` refuses on ``exec()`` (OPL-4232). Read
    #: :attr:`data` for what actually arrived.
    exit_code: int | None = field(default=None, kw_only=True)
    #: ``process.exited``: the guest stopped knowing about this command — which
    #: is what a restart of the machine underneath it looks like.
    #:
    #: The handle goes with it, so :meth:`~mandala_computer.BackgroundCommand.poll`
    #: answers 404 from here on. The event is sent so that a caller waiting on
    #: it stops waiting, not because anything was learned about how the command
    #: ended.
    #:
    #: ``file.changed`` has a ``lost`` of its own on the wire and it is a REASON
    #: rather than a flag; it decodes to :attr:`lost_reason`.
    lost: bool | None = field(default=None, kw_only=True)
    #: ``clipboard.changed``: ``"clipboard"`` or ``"primary"``. The contents are
    #: not on this stream — read them at
    #: :meth:`~mandala_computer.Computer.clipboard`.
    selection: str | None = field(default=None, kw_only=True)
    #: ``file.changed``: the nominated tree this is reported under, as the host
    #: NORMALISED it.
    #:
    #: Set on all three shapes of the event, and it is the only field that is —
    #: a marker is about the tree rather than about anything in it. Match it
    #: against :attr:`Hello.watching`, never against the string you passed to
    #: ``watch``: a trailing slash and a ``.`` segment are cleaned away by the
    #: host, and the cleaned form is what arrives here.
    watch: str | None = field(default=None, kw_only=True)
    #: ``file.changed``: the absolute path that changed, always inside
    #: :attr:`watch`.
    #:
    #: ``None`` on the two marker shapes — :attr:`armed` and
    #: :attr:`lost_reason` — which say something about the tree and name nothing
    #: in it. It is ``None`` rather than ``""`` for the reason this class
    #: promotes fields rather than defaulting them: a marker decoded to an empty
    #: path would read as a change to a file with no name, and the distinction
    #: the platform bothered to send would be gone.
    path: str | None = field(default=None, kw_only=True)
    #: ``file.changed``: ``"created"``, ``"modified"`` or ``"deleted"``.
    #:
    #: A rename inside the tree is a ``deleted`` and a ``created``, not a move:
    #: inotify reports the two ends separately and one of them is usually
    #: outside the tree, so each event is true about the path it names. Writes
    #: are coalesced, so a file created and then written reads as ``created``.
    #:
    #: ``None`` on the markers, with :attr:`path`.
    kind: str | None = field(default=None, kw_only=True)
    #: ``file.changed``: whether the thing that changed is a directory.
    #:
    #: ``None`` on the markers, where there is no path for it to be about.
    #: Spelled out rather than ``dir``, which reads as a listing.
    is_dir: bool | None = field(default=None, kw_only=True)
    #: ``file.changed``: the tree is being watched FROM HERE ON.
    #:
    #: ``True`` on the arming marker and ``None`` on every other shape, which is
    #: the same split :attr:`synthesized` draws and not a tri-state to reason
    #: about: the platform sends this only to say a watch went live.
    #:
    #: Until it arrives, silence about a tree means "not watching yet" rather
    #: than "nothing has changed" — arming is asynchronous, and on a computer
    #: nobody has opened a terminal on, the host has to install the watcher into
    #: the guest first. inotify reports changes and not state, so whatever
    #: happened in that window is never reported and never will be. It arrives
    #: AGAIN after anything that re-arms the watch — a stop and a start, a guest
    #: reboot, a broker replaced — and a second one means what the first did:
    #: reporting starts here, so re-read the tree if the interruption mattered.
    #:
    #: :attr:`Hello.watching` is the other half of this, and the half a client
    #: gets wrong. The guest answers a nomination once, so a stream joining a
    #: tree somebody else nominated is never sent this event at all; the opening
    #: frame is where that state is. State in ``hello``, transitions here — the
    #: same division :attr:`Hello.ready` and ``computer.ready`` make.
    armed: bool | None = field(default=None, kw_only=True)
    #: ``file.changed``: this tree is not reporting everything under it, and
    #: which of the three reasons it is.
    #:
    #: * ``"flood"`` — the tree changed faster than the cap allows it to be
    #:   reported. Transient: re-read the tree and keep listening. A build under
    #:   a watched path costs one of these rather than thousands of events.
    #: * ``"budget"`` — the tree is bigger than the directory budget one watch
    #:   gets, so part of it is not being watched at all. Permanent for this
    #:   watch; nominate a narrower path.
    #: * ``"unwatchable"`` — the directory is not there yet, is not a directory,
    #:   cannot be read, or is a SYMLINK, which is refused rather than followed
    #:   because inotify pins whatever the link resolved to. This one recovers
    #:   on its own where it can — nominating the directory a job is about to
    #:   create is a supported thing to do — and the recovery is announced by
    #:   :attr:`armed` and by nothing else.
    #:
    #: Only ``unwatchable`` means the tree is not being watched. Treat any
    #: non-empty value as "my picture of this tree is wrong".
    #:
    #: Spelled apart from :attr:`lost`, which is ``process.exited``'s boolean:
    #: the wire calls both of them ``lost`` and they are not the same kind of
    #: answer, so one field could not carry both without a caller having to know
    #: the event type before reading it.
    lost_reason: str | None = field(default=None, kw_only=True)
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
    #: built without the X bindings — withdraws the DESKTOP half after ``hello``
    #: promised it, keeping ``file.changed``, which needs no bindings; and a
    #: computer stopped and started under an open socket can ACQUIRE the channel
    #: its watcher runs over and get the whole guest half back.
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
class WatchedTree:
    """One tree this stream nominated, as the opening frame reports it back.

    Read off :attr:`Hello.watching` or :attr:`EventStream.watching`. Both halves
    of it are answers a client cannot get any other way, and both are ones a
    client gets wrong by assuming.
    """

    #: The nomination as the host NORMALISED it: a trailing slash and a ``.``
    #: segment are accepted and cleaned away.
    #:
    #: This — not the string you passed to ``watch`` — is what every
    #: ``file.changed`` carries in :attr:`ComputerEvent.watch`, so a client
    #: matching on what it sent matches nothing. A path the host cannot honour
    #: is a refusal on the upgrade rather than an entry here.
    path: str
    #: Whether this tree is ALREADY being watched.
    #:
    #: ``False`` is the ordinary case on a fresh nomination and means "not live
    #: yet": wait for this tree's ``file.changed`` with
    #: :attr:`~ComputerEvent.armed` set before reading silence as "nothing has
    #: changed".
    #:
    #: ``True`` means live NOW, and no event is coming to say so — somebody else
    #: nominated this tree first and the guest answers a nomination once. A
    #: client that models only the transition waits forever on a machine that
    #: has been reporting the whole time, which is the same trap
    #: :attr:`Hello.ready` takes out of ``computer.ready``.
    armed: bool
    #: The entry verbatim. Unknown fields survive here as they do everywhere
    #: else on this surface.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


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
    #: That half does not go missing all at once, which is the case a client
    #: gets wrong. :data:`DESKTOP_EVENT_TYPES` needs the terminal channel AND
    #: the X bindings; :data:`CHANNEL_EVENT_TYPES` needs the channel alone, so
    #: a computer here can name ``file.changed`` and no ``window.*`` at all.
    #: Read this list rather than inferring the rest of it from one entry.
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    #: The trees this stream will report file changes under, and whether each is
    #: live yet. ``None`` where the frame carried no ``watching`` at all.
    #:
    #: ``None`` is the honest shape for a stream that nominated nothing, and it
    #: is a stronger statement than the empty list would be: no ``file.changed``
    #: can reach that socket AT ALL. Nomination is an option on the connection —
    #: ``events(watch=...)`` — rather than an event type to wait for, which is
    #: why this is state on the opening frame rather than something to discover.
    #:
    #: Keyword-only and last, like every field added to a released record on
    #: this surface: the positional order is a promise already made.
    watching: list[WatchedTree] | None = field(default=None, kw_only=True)


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
        # `_opt_whole`, not `_opt_num`: the latter truncates, and `int(0.9)` is
        # `0` — a failed command reported as a successful one, which is the one
        # outcome `_exit_code` was written to prevent on `exec()` and the
        # background poll. This path was left on the truncating decoder, so the
        # same wire value that `exec()` refuses outright decoded here to a clean
        # pass with `lost` false (adversarial review, OPL-4232). A code this
        # client cannot read is now `None`, which is what it is.
        promoted["exit_code"] = None if lost else _opt_whole(data.get("exit_code"))
    elif kind == "clipboard.changed":
        promoted["selection"] = _text(data.get("selection")) or None
    elif kind == WATCH_EVENT_TYPE:
        # THREE shapes behind one type, and the fields are what tell them
        # apart: `{watch, path, kind, dir}` is a change, `{watch, armed}` says
        # the tree went live, `{watch, lost}` says the picture of it is
        # incomplete. Only `watch` is on all three.
        #
        # Nothing is defaulted into place here. A dataclass field defaulting to
        # `""` would decode a marker as a change to a file with no name and
        # swallow a distinction the platform went out of its way to send, so
        # every field a shape does not carry stays `None` — which is what the
        # promoted fields mean everywhere else on this class.
        promoted["watch"] = _text(data.get("watch")) or None
        promoted["path"] = _text(data.get("path")) or None
        promoted["kind"] = _text(data.get("kind")) or None
        promoted["lost_reason"] = _text(data.get("lost")) or None
        # TRUE only, and never manufactured False. `armed` is `omitempty` on the
        # wire, so its absence is every ordinary change event rather than a
        # statement that the tree is not armed — and a client reading a `False`
        # here as "the watch went away" would act on a transition nobody
        # reported. The one thing this field ever says is that a watch went live.
        promoted["armed"] = True if _wire(data, "armed") is _Wire.TRUE else None
        # Read only where there is a path for it to be about. `dir` is
        # `omitempty` too, so on a change frame absent means "not a directory";
        # on a marker it means nothing at all, and `False` there would describe
        # a file that is not in the event.
        promoted["is_dir"] = None if promoted["path"] is None else _wire(data, "dir") is _Wire.TRUE
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


def to_watched_tree(row: Any) -> WatchedTree | None:
    """One ``hello.watching`` entry, or ``None`` for a row with no path in it.

    Dropped rather than carried with an empty path, and this is the one decoder
    on this module that refuses a row. Everywhere else a field nobody could read
    costs a caller one attribute; here it would cost them the whole feature —
    ``""`` matches no ``file.changed`` ever sent, so a stream would look like it
    was watching a tree it can never be told about. A row that is missing tells
    a caller to look at :attr:`Hello.raw`; a row that is present and false does
    not.
    """
    if not isinstance(row, Mapping):
        return None
    path = _text(row.get("path"))
    if not path:
        return None
    # `_wire`, like every other boolean off this wire, and TRUE only. The
    # recoverable direction is the one that waits: a tree reported unarmed that
    # is in fact live ends at the caller's own timeout, while an unreadable
    # `armed` called True is a client acting on silence from a watch that was
    # never running — which is the exact failure `armed` exists to prevent.
    return WatchedTree(path=path, armed=_wire(row, "armed") is _Wire.TRUE, raw=dict(row))


def _watching(rows: Any) -> list[WatchedTree] | None:
    """``hello.watching``, keeping "nominated nothing" apart from "nothing armed"."""
    if not isinstance(rows, list):
        return None
    return [tree for tree in (to_watched_tree(row) for row in rows) if tree is not None]


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
        # ABSENT and empty are different answers here, as they are for
        # `windows`: absent is a stream that nominated nothing, on which no
        # `file.changed` can arrive at all. A list is kept even when every row
        # in it was unreadable, because the field's presence is the fact.
        watching=_watching(frame.get("watching")),
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


def with_watches(url: str, paths: Sequence[str]) -> str:
    """``events_url`` with the trees this stream nominates on it.

    Repeated rather than comma-separated, which is the platform's own choice and
    the only one that works: a directory name may contain a comma, and a list
    format that cannot represent every value it is a list of is a defect waiting
    for the first tenant with a ``a,b`` in their home directory.

    ``/`` is percent-encoded along with everything else. It survives the round
    trip either way, and encoding it means this function has no opinion about
    which bytes of a path are structural — the host is the authority on the
    shape of a path, and this is a credential-bearing URL to leave otherwise
    alone (see :func:`with_cursor`).
    """
    from urllib.parse import quote

    for path in paths:
        url = f"{url}{'&' if '?' in url else '?'}watch={quote(path, safe='')}"
    return url


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
    computer_id: str,
    wanted: Sequence[str],
    advertised: Sequence[str] | None,
    *,
    nominated: bool = True,
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

    ``nominated`` is the second, independent way a wait cannot end, and it is
    the one the advertised list cannot express. A computer offers
    ``file.changed`` whenever it COULD report one — the platform decides that
    off the terminal channel, before anyone has said which directory they care
    about — so the vocabulary says yes to a stream that nominated nothing and on
    which no such event can ever arrive. That is a fact about this subscription
    rather than about the machine, so it is refused even where ``advertised`` is
    ``None``: silence about the vocabulary is not silence about the nomination.
    """
    # Deduplicated FIRST. Counting `impossible` with duplicates in it and
    # comparing against the distinct types asked for made the two sides count
    # different things, so `["missing", "missing", "process.exited"]` refused a
    # wait whose second type the computer advertises (Codex review).
    # `dict.fromkeys` rather than a set, so the sentences below name them in
    # the order the caller wrote them.
    asked = list(dict.fromkeys(wanted))
    reachable = None if advertised is None else set(advertised) | set(STREAM_FRAME_TYPES)
    unadvertised = [] if reachable is None else [t for t in asked if t not in reachable]
    # The vocabulary answers FIRST where it has an answer. A computer that
    # cannot emit `file.changed` at all and a stream that did not nominate a
    # tree are two different problems, and the one to report is the one a
    # nomination would not fix.
    unwatched = (
        [] if nominated else [t for t in asked if t == WATCH_EVENT_TYPE and t not in unadvertised]
    )
    if len(unwatched) + len(unadvertised) < len(asked):
        return None
    if not unadvertised:
        # The computer can emit it; this socket cannot receive it. Said in those
        # words rather than folded into the sentence below, because "cannot
        # emit" would be a false statement about the machine and would send a
        # caller looking at their image for a problem that is in their call.
        return _settle(
            MandalaError(
                f"{computer_id} reports {' or '.join(unwatched)} only under a directory this "
                "stream nominated, and it nominated none — so waiting for it would never end. "
                'Pass watch="/absolute/path" to say which tree you are waiting on.'
            )
        )
    said = "" if advertised is None else f" It advertises: {', '.join(advertised) or 'nothing'}."
    also = (
        ""
        if not unwatched
        else (
            f" {' and '.join(unwatched)} needs a watch= nomination as well, and this stream "
            "made none."
        )
    )
    return _settle(
        MandalaError(
            f"{computer_id} cannot emit {' or '.join(unadvertised)}, so waiting for it would "
            f"never end.{said}{also}"
        )
    )


def answers_wait(ev: ComputerEvent, wanted: Sequence[str]) -> bool:
    """Whether this event is the one a :meth:`~mandala_computer.Computer.wait_for` asked for.

    Matching on :attr:`~ComputerEvent.type` alone is right for every type but
    one. ``file.changed`` carries THREE shapes under a single name and only one
    of them is a change: the other two say the tree went live and that the
    picture of it is incomplete. A wait matched on the type returned whichever
    arrived first, so ``wait_for("file.changed", watch=...)`` on a fresh
    nomination came back with the arming marker — no file had changed — and
    closed the socket. Worse, it did that only SOMETIMES: the guest answers a
    nomination once, so the same call against a tree somebody else had already
    armed never sees that marker and waits for a real change.

    That is the ``computer.ready`` trap in a new place — state in ``hello``, the
    transition on the wire, and a wait meaning two different things depending on
    who got there first — and here there is nothing to synthesize, because the
    caller did not ask about arming at all (Grok review).

    So a wait for ``file.changed`` ends on a change and nothing else. The
    markers are still DELIVERED — :meth:`~mandala_computer.Computer.events`
    yields every frame, and :attr:`EventStream.watching` folds them into the
    tree's state — they simply do not answer this question.
    """
    if ev.type not in wanted:
        return False
    # A change is the shape that names a path. `armed` and `lost` name only the
    # tree, and so does a frame this build could not read — which a wait should
    # sit through rather than end on, for the same reason.
    return ev.path is not None if ev.type == WATCH_EVENT_TYPE else True


def unarmed_trees(watching: Sequence[WatchedTree] | None) -> list[str]:
    """The nominated trees that were never live, for a wait that ran out of time.

    A watch that never arms is silent in exactly the way a tree where nothing
    happened is, and the difference is the whole of what ``armed`` is for. Left
    unsaid, a nomination the guest could not honour — a directory that is not
    there, or is a symlink — reaches a caller as an ordinary timeout with
    nothing in it to explain the wait.

    Not an error, and deliberately: ``unwatchable`` recovers on its own, and
    nominating the directory a job is about to create is a supported thing to
    do. So this is a sentence added to the timeout rather than a reason to end
    the wait early.
    """
    return [] if watching is None else [t.path for t in watching if not t.armed]


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
    #: The nominated trees and whether each is live, as last stated: the
    #: opening frame's list, with an ``armed`` marker applied to it as one
    #: arrives and an ``unwatchable`` loss taking a tree back out. State in
    #: ``hello``, transitions on the stream — kept in one place so a caller
    #: reading :attr:`EventStream.watching` gets the current answer rather than
    #: the one from connect time.
    watching: list[WatchedTree] | None = None
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
        # Replaced per connection, like `types` and the desktop. A reconnect
        # re-nominates the same trees and is answered afresh, and the answer can
        # differ: a tree that was live on the last socket is not necessarily
        # armed on this one — a guest reboot in between disarms it — so carrying
        # the old list forward would report a watch that is not running.
        self.watching = None if hello.watching is None else list(hello.watching)
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
        elif ev.type == WATCH_EVENT_TYPE and ev.watch:
            if ev.armed:
                self.record_armed(ev.watch, True)
            elif ev.lost_reason == "unwatchable":
                # The ONE loss that means the tree is not being watched, and
                # therefore the one that moves this record. `flood` and `budget`
                # say the tree IS watched and is being reported incompletely, so
                # a client is still right to read silence under them as nothing
                # having changed — that is the platform's own division, and its
                # own armed set moves on exactly this reason (see the switch in
                # `emitFile`, server/fileevents.go).
                #
                # Without it a tree that armed and then went unwatchable went on
                # reporting `armed=True` for the life of the stream, while a
                # client connecting at that moment would be told `false` by
                # `hello` — so this SDK answered the question `armed` exists to
                # answer with the opposite of what the platform would have said
                # (Codex review, OPL-4220).
                self.record_armed(ev.watch, False)
        return ev

    def record_armed(self, tree: str, armed: bool) -> None:
        """Move one nominated tree between live and not.

        Only for a tree the opening frame named. A marker for anything else is
        either a host this build does not understand or an event that leaked
        past the delivery filter, and inventing a row for it would put a path in
        :attr:`EventStream.watching` that this stream never asked about — which
        is the one thing that list is relied on to mean.
        """
        if self.watching is None:
            return
        self.watching = [
            WatchedTree(path=w.path, armed=armed, raw=w.raw)
            if w.path == tree and w.armed != armed
            else w
            for w in self.watching
        ]

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


def _watch_paths(watch: str | Sequence[str] | None) -> tuple[str, ...]:
    """The nomination, checked for the two mistakes worth catching here.

    A bare string is one path, not four characters. ``str`` is a
    ``Sequence[str]`` whose members are its own letters, so the obvious
    implementation turns ``watch="/tmp"`` into a nomination of ``/``, ``t``,
    ``m``, ``p`` — three refusals and a watch on the root, from a call that is
    the common case.

    Beyond that this checks only what a caller can read off their own argument:
    that each path is a non-empty string and is absolute. The length cap, the
    control characters, ``/`` itself and how many trees one stream may name are
    the platform's rules and the platform states them in its own refusal, which
    reaches a caller as a settled error carrying that sentence. Duplicating them
    here would be a second place for them to be wrong, and would refuse a limit
    a later host has raised.

    NOT normalised, deliberately. The host cleans a trailing slash and a ``.``
    segment, and the cleaned form is what :attr:`Hello.watching` reports and
    every event carries — so a client is meant to read the answer back rather
    than predict it, and a normalisation done here would be a second opinion
    about a path that only one side is the authority on.
    """
    if watch is None:
        return ()
    if isinstance(watch, str):
        paths = [watch]
    else:
        try:
            paths = list(watch)
        except TypeError:
            # A number or any other non-iterable, answered in this SDK's own
            # error rather than as the `TypeError` that `list()` raises. The
            # handler a caller is told to write is `except MandalaError`, and an
            # argument check that goes past it is the failure OPL-4222 fixed on
            # `wait_for`'s own deadline.
            raise MandalaError(
                f"watch must be a path or a sequence of paths (got {watch!r})"
            ) from None
    for path in paths:
        if not isinstance(path, str) or not path:
            raise MandalaError(f"a watch path must be a non-empty string (got {path!r})")
        if not path.startswith("/"):
            raise MandalaError(
                f"a watch path must be absolute, naming a directory in the GUEST (got {path!r})"
            )
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:
            # A lone surrogate, which is what `os.fsdecode` hands back for a
            # filename whose bytes are not UTF-8 — so this is a real path a real
            # caller can hold, not a synthetic string. Caught HERE because it is
            # the last place it can be: `quote` raises on it while the URL is
            # being built, which is outside the connect path's error handling and
            # several steps after the call that supplied it, so it reached a
            # caller as a bare `UnicodeEncodeError` at the first step of the
            # iteration rather than as the `MandalaError` they were told to catch
            # (Codex review, OPL-4220).
            #
            # Not a platform limit duplicated: this is a value this client
            # cannot put on a query string at all, so there is no refusal to
            # defer to. The platform refuses invalid UTF-8 as well, which is
            # what makes rejecting it here a shortcut rather than a divergence.
            raise MandalaError(
                f"a watch path must be encodable as UTF-8, and {path!r} is not — which is what "
                "os.fsdecode() gives a filename whose bytes are not. Nominate a parent directory "
                "whose name is UTF-8; the platform refuses this one too."
            ) from None
    return tuple(paths)


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


def _refusal(
    computer_id: str, status: int, body: bytes | bytearray | None, *, watching: bool = False
) -> MandalaError:
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
            if watching:
                # TWO refusals share this reason once a stream nominates a tree,
                # and nothing structural separates them: a computer that is not
                # running, and one already watching all 32 trees it can watch at
                # once across every stream open on it. The platform's own
                # sentence is the only thing that tells them apart, so it is
                # repeated rather than replaced by a guess between the two.
                #
                # Settled either way. A stopped computer is a decision, and a
                # tree budget is freed by closing another stream rather than by
                # asking this one again.
                return _settle(
                    MandalaError(
                        f"{computer_id}'s event stream was refused: {detail or 'unavailable'}. "
                        "Either it is not running — call start() and open it again — or this "
                        "nomination would take it past the trees it can watch at once, which "
                        "closing another stream frees."
                    )
                )
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
    if status == 400:
        # A refusal about the REQUEST, and every documented one on this route is
        # about a nominated path: empty, relative, too long, carrying control
        # characters, `/` itself, or more than four of them. Settled, because
        # the same URL is refused the same way for ever — and this is the one
        # status on this route that used to fall through to the weather branch
        # below, where a mistyped path became a reconnect loop with no
        # `max_retries` to stop it.
        return _settle(
            MandalaError(
                f"{computer_id}'s event stream refused this request (HTTP 400)"
                + (f": {detail}" if detail else "")
                + (
                    ". A nominated path must be absolute, must not be / itself, and there is a "
                    "limit on how many one stream may name."
                    if watching
                    else ""
                )
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


def _public_exc(exc: BaseException) -> str:
    """Exception text that cannot carry a desktop token.

    ``InvalidURI.__str__`` includes the full URI, and the URI on this surface
    carries a credential with no expiry — the leak :meth:`VncConnect.__repr__`
    was rewritten to prevent. Other ``WebSocketException`` subclasses are
    named by class rather than interpolated, because several of them echo the
    URI they were given. OSError and TimeoutError do not.
    """
    from websockets.exceptions import InvalidURI, WebSocketException

    if isinstance(exc, InvalidURI):
        return "not a websocket URL"
    if isinstance(exc, WebSocketException):
        return type(exc).__name__
    return str(exc)


def _connect_failed(
    computer_id: str, exc: BaseException, *, watching: bool = False
) -> MandalaError:
    """A websocket that would not open, as this SDK's own error.

    Split by whether asking again could answer differently. A refused upgrade
    carries a status and is read by :func:`_refusal`; a URL that is not a
    websocket URL at all is a settled fact about the connect surface; anything
    else — a reset, a DNS failure, a handshake that timed out — is weather, and
    the backoff is the right response to weather.
    """
    from websockets.exceptions import InvalidStatus, InvalidURI, WebSocketException

    if isinstance(exc, InvalidStatus):
        return _refusal(computer_id, exc.response.status_code, exc.response.body, watching=watching)
    if isinstance(exc, InvalidURI):
        return _settle(MandalaError(f"{computer_id}'s events_url is not a websocket URL"))
    # ``asyncio.TimeoutError`` alongside the builtin because they are separate
    # classes on 3.10, which ``requires-python`` still admits: without it an
    # ``open_timeout`` on that version leaves this function through the
    # ``raise`` below and reaches the caller as something that is not a
    # MandalaError at all. ``_sleep`` in this file already spells it this way.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError, WebSocketException)):
        return ConnectionError(f"{computer_id}'s event stream would not open: {_public_exc(exc)}")
    raise exc


def _lost(computer_id: str, exc: BaseException) -> MandalaError:
    """A socket that failed mid-stream, as this SDK's own error."""
    return ConnectionError(f"{computer_id}'s event stream failed: {_public_exc(exc)}")


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
        watch: str | Sequence[str] | None = None,
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
        self._watch = _watch_paths(watch)
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

        It is a list rather than a switch, and reading one entry off it tells
        you nothing about the next. The file watcher needs only the terminal
        channel, so this can name ``file.changed`` with no ``window.*`` beside
        it — see :data:`DESKTOP_EVENT_TYPES` and :data:`CHANNEL_EVENT_TYPES`.
        And it is not the whole of what makes a wait unable to end:
        ``file.changed`` is advertised whether or not THIS stream nominated a
        tree, so :meth:`~mandala_computer.Computer.wait_for` refuses that one for
        a missing nomination rather than for a missing vocabulary.
        """
        # A COPY. The list behind this is what decides whether a wait can end —
        # `wait_for` refuses a type that is not in it — so handing out the live
        # one would let an accidental `append` talk a wait into hanging on a
        # machine that cannot produce the event.
        return None if self._core.types is None else list(self._core.types)

    @property
    def watching(self) -> list[WatchedTree] | None:
        """The trees this stream nominated, and whether each is live YET.

        The opening frame's answer, with an ``armed`` marker applied to it as
        one arrives and an ``unwatchable`` loss taking a tree back out — so this
        is the current state rather than the state at connect time. ``None``
        where nothing was nominated, which is a stronger answer than the empty
        list: no ``file.changed`` can reach this socket at all.

        The other two losses do NOT move it. ``flood`` and ``budget`` say the
        tree is being watched and reported incompletely, so silence under them
        still means nothing has changed; only ``unwatchable`` says it is not
        being watched at all.

        The paths in it are the host's NORMALISED spelling of what was
        nominated, and they are what every ``file.changed`` carries in
        :attr:`ComputerEvent.watch`. Match on these, not on the strings you
        passed to ``watch``.
        """
        # A copy of the list, for the reason `event_types` is copied. The trees
        # in it are frozen records and are not copied themselves.
        return None if self._core.watching is None else list(self._core.watching)

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
        url = with_watches(with_cursor(self._url(budget), self._core.cursor), self._watch)
        left = budget - (time.monotonic() - started)
        if left <= 0:
            raise ConnectionError(
                f"reading a fresh events_url for {self._id} used the whole {budget:g}s "
                "connect budget"
            )
        try:
            sock = _connect(url, open_timeout=left, max_queue=self._max_queue)
        except Exception as exc:
            raise _connect_failed(self._id, exc, watching=bool(self._watch)) from exc
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


async def _drained(connecting: asyncio.Task[Any]) -> Any | None:
    """The socket a cancelled connect task had already opened, or ``None``.

    Draining a task we just cancelled has to absorb THAT task's
    ``CancelledError``. It must not absorb one delivered to the ENCLOSING task
    at the same await: swallowing that leaves the caller finishing normally
    after a ``cancel()``, so ``task.cancelled()`` is False and an ``async for``
    ends quietly instead of propagating — precisely what a caller cancels in
    order not to get. ``connecting.cancelled()`` is what tells the two apart:
    it is true only once the cancel we asked for is the one that landed. The
    sibling ``_recv`` keeps the same discipline with
    ``except BaseException: reading.cancel(); raise``.

    One window is left, and is not closeable from here: if the outer cancel
    lands while the connect task is itself finishing cancelled, both readings
    are true at once and this absorbs it. Narrowing that needs
    ``Task.uncancel``, which is 3.11+, and this package supports 3.10
    (adversarial review, OPL-4479).
    """
    try:
        return await connecting
    except asyncio.CancelledError:
        if not connecting.cancelled():
            raise
        return None
    except Exception:  # noqa: BLE001 — any failed connect leaves nothing to shut
        return None


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
        url = with_watches(with_cursor(await self._url(budget), self._core.cursor), self._watch)
        left = budget - (time.monotonic() - started)
        if left <= 0:
            raise ConnectionError(
                f"reading a fresh events_url for {self._id} used the whole {budget:g}s "
                "connect budget"
            )
        # Raced against the stop the way ``_recv`` is. ``close()`` cannot await
        # a handshake, and without the race a hook (or another task) that
        # stopped the stream during connect waited out the remaining
        # ``open_timeout`` of a socket nobody would read.
        connecting = asyncio.ensure_future(
            _aconnect(url, open_timeout=left, max_queue=self._max_queue)
        )
        stopping = asyncio.ensure_future(self._stop.wait())
        try:
            done, _ = await asyncio.wait(
                (connecting, stopping), return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            connecting.cancel()
            raise
        finally:
            stopping.cancel()
        if connecting not in done:
            connecting.cancel()
            sock = None
            sock = await _drained(connecting)
            if sock is not None:
                await _ashut_quietly(sock)
            raise _Expired
        try:
            sock = connecting.result()
        except Exception as exc:
            raise _connect_failed(self._id, exc, watching=bool(self._watch)) from exc
        self._sock = sock
        if self._stopped():
            await self._shut()
            raise _Expired
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
