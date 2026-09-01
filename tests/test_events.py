"""The event stream — decoding, reconnecting, and the wait that replaces polling.

The socket is faked throughout. What is being tested is the part of this SDK
that sits between a websocket and a `for` loop, and a real socket would only
add ports and timing to questions that are about neither.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
import respx
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.http11 import Response

import mandala_computer as mc
from mandala_computer import _events
from mandala_computer._events import (
    Hello,
    to_computer_event,
    to_hello,
    with_cursor,
)

BASE = "https://api.test/api/v1"

COMPUTER = {
    "id": "vm-1",
    "name": "dev",
    "status": "running",
    "os": "linux",
    "template": "base",
    "cpu": 2,
    "ram_mb": 2048,
    "disk_gb": 20,
    "created_at": "2026-07-31T00:00:00Z",
    "vnc": {
        "url": "wss://vnc.test/?token=control",
        "view_url": "wss://vnc.test/?token=view",
        "token": "control",
        "view_token": "view",
        "embed_url": "https://embed.test",
        "terminal_url": "wss://term.test/?token=control",
        "events_url": "wss://events.test/?token=control",
    },
}


def hello_frame(**over: Any) -> str:
    import json

    frame = {
        "type": "hello",
        "computer": "vm-1",
        "cursor": "c0",
        "ready": False,
        "events": ["window.opened", "window.closed", "computer.ready", "process.exited"],
        "windows": [],
    }
    frame.update(over)
    return json.dumps(frame)


def resumed_hello(**over: Any) -> str:
    """A hello with no ``windows`` key at all.

    Which is the platform saying "your cursor was honoured, you already hold
    the desktop" — and is a different answer from an empty list, which says
    nothing is open.
    """
    import json

    payload = json.loads(hello_frame(**over))
    del payload["windows"]
    return json.dumps(payload)


def event_frame(kind: str, **over: Any) -> str:
    import json

    frame: dict[str, Any] = {
        "type": kind,
        "at": "2026-08-31T12:00:00Z",
        "computer": "vm-1",
        "seq": 1,
        "cursor": "c1",
        "source": "guest",
        "data": {},
    }
    frame.update(over)
    return json.dumps(frame)


# --- the fake socket --------------------------------------------------------


class FakeSocket:
    """One connection's worth of scripted frames.

    A frame is a string to deliver, an exception to raise, or the sentinel
    ``HANG`` — which is a socket that is open, healthy, and has nothing to say.
    That last one is not a curiosity: it is every idle desktop, and it is what
    the stream's own deadline has to be able to end.
    """

    HANG = object()

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.closed = False
        self.reads = 0

    def _next(self, timeout: float | None) -> Any:
        self.reads += 1
        if self.closed:
            raise ConnectionClosedOK(None, None)
        if not self.script:
            raise ConnectionClosedOK(None, None)
        item = self.script.pop(0)
        if item is FakeSocket.HANG:
            # A read that never completes, bounded only by what the caller
            # asked for. `None` would block a test for ever, which is the one
            # thing a suite must not do, so an unbounded wait here is a bug in
            # the test rather than something to emulate faithfully.
            assert timeout is not None, "the stream waited for ever on an idle socket"
            raise TimeoutError
        if isinstance(item, BaseException):
            raise item
        return item

    def recv(self, timeout: float | None = None) -> Any:
        return self._next(timeout)

    def close(self) -> None:
        self.closed = True


class AsyncFakeSocket(FakeSocket):
    async def recv(self) -> Any:  # type: ignore[override]
        # The async half bounds a read with `asyncio.wait_for` rather than with
        # a parameter, so HANG has to be a wait the loop can cancel.
        self.reads += 1
        if self.closed or not self.script:
            raise ConnectionClosedOK(None, None)
        item = self.script.pop(0)
        if item is FakeSocket.HANG:
            await asyncio.sleep(3600)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:  # type: ignore[override]
        self.closed = True


class Dialer:
    """Hands out one socket per connection, and remembers what was asked for."""

    def __init__(self, *scripts: Any, cls: type[FakeSocket] = FakeSocket) -> None:
        self.scripts = list(scripts)
        self.cls = cls
        self.urls: list[str] = []
        self.sockets: list[FakeSocket] = []

    def _dial(self, url: str) -> FakeSocket:
        self.urls.append(url)
        if not self.scripts:
            raise AssertionError(f"the stream opened more sockets than the test scripted: {url}")
        script = self.scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        sock = self.cls(script)
        self.sockets.append(sock)
        return sock

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Dialer:
        def connect(url: str, **kw: Any) -> FakeSocket:
            return self._dial(url)

        async def aconnect(url: str, **kw: Any) -> FakeSocket:
            return self._dial(url)

        monkeypatch.setattr(_events, "_connect", connect)
        monkeypatch.setattr(_events, "_aconnect", aconnect)
        return self


def refused(status: int, body: bytes = b"") -> InvalidStatus:
    return InvalidStatus(Response(status, "", Headers(), body))


def take(stream: mc.EventStream, n: int) -> list[mc.ComputerEvent]:
    """The first ``n`` events, then stop.

    A stream reconnects for ever by default, which is what a program wants and
    what hangs a test — so anything checking behaviour ACROSS reconnects has to
    say when it has seen enough.
    """
    got: list[mc.ComputerEvent] = []
    with stream:
        for ev in stream:
            got.append(ev)
            if len(got) == n:
                break
    return got


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


@pytest.fixture
def computer(client: mc.Client) -> mc.Computer:
    with respx.mock:
        respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
        return client.computers.get("vm-1")


def route(**over: Any) -> None:
    """Point `GET /computers/vm-1` at a computer, for the URL re-read."""
    data = {**COMPUTER, **over}
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=data))


# --- decoding ---------------------------------------------------------------


def test_a_window_event_carries_the_window() -> None:
    ev = to_computer_event(
        {
            "type": "window.opened",
            "cursor": "c1",
            "source": "guest",
            "data": {"id": "0x1", "class": "Firefox", "x": 10, "y": 20, "visible": True},
        }
    )
    assert ev is not None
    assert ev.window is not None
    assert ev.window.wm_class == "Firefox"
    assert ev.window.x == 10
    assert ev.window.visible is True
    assert ev.window_id is None


def test_a_window_frame_it_cannot_read_decodes_rather_than_raising() -> None:
    """The listing refuses a window with no id and answers None for geometry it
    could not read (OPL-4200). The refusal deliberately stops at the route.

    `Window.from_api` also runs HERE, on frames arriving off a socket, and this
    stream's policy for one it cannot read is to skip it and read the next
    rather than end the connection over it. So the decoder stays total, and the
    coordinate is absent rather than the corner of the screen on this path too —
    the two routes decode the same window through the same function.
    """
    ev = to_computer_event({"type": "window.opened", "data": {"title": "Terminal", "x": "wide"}})
    assert ev is not None
    assert ev.window is not None
    assert ev.window.id == "", "an unnamed window on the stream is news, not a reason to raise"
    assert ev.window.title == "Terminal"
    assert (ev.window.x, ev.window.y) == (None, None)


def test_a_close_names_the_window_and_describes_nothing() -> None:
    """A window that is gone has no geometry, and none is invented for it."""
    ev = to_computer_event({"type": "window.closed", "data": {"id": "0x1"}})
    assert ev is not None
    assert ev.window_id == "0x1"
    assert ev.window is None


def test_an_exit_carries_its_code() -> None:
    ev = to_computer_event({"type": "process.exited", "data": {"pid": 4321, "exit_code": 0}})
    assert ev is not None
    assert ev.pid == 4321
    assert ev.exit_code == 0
    assert ev.lost is False


def test_a_lost_command_has_no_exit_code_rather_than_a_made_up_one() -> None:
    """`-1` is a real exit code on this path, so it cannot also mean "no answer"."""
    ev = to_computer_event({"type": "process.exited", "data": {"pid": 7, "lost": True}})
    assert ev is not None
    assert ev.lost is True
    assert ev.exit_code is None


def test_a_lost_beside_a_code_does_not_hand_back_both() -> None:
    ev = to_computer_event(
        {"type": "process.exited", "data": {"pid": 7, "lost": True, "exit_code": 0}}
    )
    assert ev is not None
    assert ev.exit_code is None


def test_a_frame_with_neither_is_not_answered_with_lost() -> None:
    """Saying `lost` here would assert the guest lost track of a command nobody
    said that about. An unreadable code is simply no code."""
    ev = to_computer_event({"type": "process.exited", "data": {"pid": 7}})
    assert ev is not None
    assert ev.lost is False
    assert ev.exit_code is None


def test_a_power_transition_may_arrive_without_a_previous() -> None:
    """The first transition a host reports after a daemon restart has none."""
    ev = to_computer_event({"type": "computer.stopped", "data": {"status": "stopped"}})
    assert ev is not None
    assert ev.status == "stopped"
    assert ev.previous is None


def test_idle_carries_its_seconds() -> None:
    ev = to_computer_event({"type": "computer.idle", "data": {"idle_seconds": 900}})
    assert ev is not None
    assert ev.idle_seconds == 900


def test_a_clipboard_change_names_the_selection_and_not_its_contents() -> None:
    ev = to_computer_event({"type": "clipboard.changed", "data": {"selection": "primary"}})
    assert ev is not None
    assert ev.selection == "primary"


def test_a_gap_reads_its_fields_off_data() -> None:
    ev = to_computer_event(
        {"type": "gap", "data": {"oldest_cursor": "c9", "detail": "too far behind"}}
    )
    assert ev is not None
    assert ev.oldest_cursor == "c9"
    assert ev.detail == "too far behind"


def test_a_closed_frame_reads_its_detail_off_the_envelope() -> None:
    """`closed` carries `detail` beside `type`, not inside a `data` it has none of."""
    ev = to_computer_event({"type": "closed", "detail": "you stopped reading"})
    assert ev is not None
    assert ev.detail == "you stopped reading"


def test_a_capabilities_frame_reads_its_list_off_the_envelope() -> None:
    ev = to_computer_event(
        {"type": "capabilities", "events": ["computer.idle"], "detail": "no watcher"}
    )
    assert ev is not None
    assert ev.events == ["computer.idle"]
    assert ev.detail == "no watcher"


def test_a_capabilities_frame_nobody_could_read_revises_nothing() -> None:
    """The empty list is an answer here — "this machine emits nothing" — so a
    malformed one must not decode to it, or a bad frame ends every wait."""
    ev = to_computer_event({"type": "capabilities", "events": "window.opened"})
    assert ev is not None
    assert ev.events is None


@pytest.mark.parametrize("kind", ["gap", "closed", "capabilities"])
def test_a_statement_about_the_stream_is_never_a_position_in_it(kind: str) -> None:
    """The platform shipped a gap carrying `seq: 0`, and a client applying the
    obvious rule — ignore anything not newer than the last sequence — discarded
    the one frame that reports unrecoverable loss."""
    ev = to_computer_event({"type": kind, "seq": 0})
    assert ev is not None
    assert ev.seq is None


def test_an_unknown_type_arrives_whole() -> None:
    """The vocabulary grows, and a client cannot ignore what it was never handed."""
    ev = to_computer_event({"type": "file.changed", "data": {"path": "/tmp/x"}, "cursor": "c2"})
    assert ev is not None
    assert ev.type == "file.changed"
    assert ev.data == {"path": "/tmp/x"}
    assert ev.cursor == "c2"
    assert ev.window is None


def test_hello_is_not_an_event() -> None:
    assert to_computer_event({"type": "hello", "cursor": "c0"}) is None


@pytest.mark.parametrize("frame", [None, 5, "hi", [1, 2], {}, {"type": ""}, {"type": 7}])
def test_a_frame_that_is_not_an_event_decodes_to_nothing(frame: Any) -> None:
    assert to_computer_event(frame) is None


def test_a_source_nobody_could_read_is_not_claimed_as_the_platforms_own() -> None:
    """The one field whose job is to say how much a payload is worth. A
    fallback to `daemon` asserts the platform observed something it never
    claimed to — see the divergence noted on `ComputerEvent.source`."""
    ev = to_computer_event({"type": "computer.idle", "source": 7})
    assert ev is not None
    assert ev.source == ""


def test_a_cursor_that_is_not_a_string_is_no_cursor() -> None:
    """`str([1, 2])` would make an unreadable position look like a readable one,
    and it goes back on the wire as `since=`."""
    ev = to_computer_event({"type": "computer.idle", "cursor": [1, 2]})
    assert ev is not None
    assert ev.cursor == ""


def test_at_is_never_this_clients_own_clock() -> None:
    ev = to_computer_event({"type": "computer.idle"})
    assert ev is not None
    assert ev.at == ""


# --- hello ------------------------------------------------------------------


def test_hello_reads_what_the_connection_joined() -> None:
    h = to_hello(
        {
            "type": "hello",
            "computer": "vm-1",
            "cursor": "c0",
            "ready": True,
            "events": ["window.opened"],
            "windows": [{"id": "0x1", "class": "Xfce4-panel"}],
        }
    )
    assert h is not None
    assert h.cursor == "c0"
    assert h.ready is True
    assert h.events == ["window.opened"]
    assert h.windows is not None
    assert h.windows[0].wm_class == "Xfce4-panel"


def test_absent_windows_and_empty_windows_are_different_answers() -> None:
    """Absent means "you already hold this picture"; empty means nothing is open."""
    absent = to_hello({"type": "hello", "cursor": "c0"})
    empty = to_hello({"type": "hello", "cursor": "c0", "windows": []})
    assert absent is not None and absent.windows is None
    assert empty is not None and empty.windows == []


@pytest.mark.parametrize("said", [None, "yes", 2, [], "TRUE"])
def test_ready_is_true_only(said: Any) -> None:
    """A readiness nobody claimed is one to wait for, which ends at the caller's
    own timeout. Concluding a desktop is up because a field was malformed hands
    an agent a screen that is still booting."""
    h = to_hello({"type": "hello", "ready": said})
    assert h is not None
    assert h.ready is (said == "TRUE")


def test_not_a_hello() -> None:
    assert to_hello({"type": "gap"}) is None
    assert to_hello(None) is None


# --- the resume position ----------------------------------------------------


def test_with_cursor_appends_and_leaves_the_credential_alone() -> None:
    assert (
        with_cursor("wss://h/events?token=abc%2Fd", "c1") == "wss://h/events?token=abc%2Fd&since=c1"
    )
    assert with_cursor("wss://h/events", "c1") == "wss://h/events?since=c1"
    assert with_cursor("wss://h/events", None) == "wss://h/events"
    assert with_cursor("wss://h/events", "") == "wss://h/events"


def test_with_cursor_encodes_a_position_that_needs_it() -> None:
    assert with_cursor("wss://h/e", "a/b&c=d") == "wss://h/e?since=a%2Fb%26c%3Dd"


# --- the stream -------------------------------------------------------------


@respx.mock
def test_events_arrive_in_order(computer: mc.Computer, monkeypatch: pytest.MonkeyPatch) -> None:
    route()
    Dialer([hello_frame(), event_frame("computer.idle"), event_frame("window.closed")]).install(
        monkeypatch
    )
    got = [ev.type for ev in computer.events(reconnect=False)]
    assert got == ["computer.idle", "window.closed"]


@respx.mock
def test_the_opening_frame_is_readable_before_the_first_event(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer([hello_frame(events=["computer.idle"]), event_frame("computer.idle")]).install(
        monkeypatch
    )
    seen: list[list[str]] = []
    stream = computer.events(reconnect=False, on_connect=lambda h: seen.append(h.events))
    for _ in stream:
        break
    assert seen == [["computer.idle"]]
    assert stream.event_types == ["computer.idle"]


@respx.mock
def test_the_advertised_vocabulary_is_a_copy(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is what decides whether a wait can end, so handing out the live list
    would let an accidental append talk a wait into hanging."""
    route()
    Dialer([hello_frame(), event_frame("computer.idle")]).install(monkeypatch)
    stream = computer.events(reconnect=False)
    for _ in stream:
        break
    types = stream.event_types
    assert types is not None
    types.append("nonsense")
    assert stream.event_types is not None
    assert "nonsense" not in stream.event_types


@respx.mock
def test_the_cursor_is_where_the_caller_got_to_not_where_the_socket_did(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole of why a reconnect does not lose unread frames: the position
    never moved past what nobody consumed."""
    route()
    Dialer(
        [
            hello_frame(),
            event_frame("computer.idle", cursor="c1"),
            event_frame("computer.idle", cursor="c2"),
            event_frame("computer.idle", cursor="c3"),
        ]
    ).install(monkeypatch)
    stream = computer.events(reconnect=False)
    with stream:
        for _ in stream:
            break
    assert stream.cursor == "c1"


@respx.mock
def test_a_reconnect_asks_from_where_the_caller_got_to(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    dialer = Dialer(
        [hello_frame(), event_frame("computer.idle", cursor="c1")],
        [hello_frame(cursor="c9"), event_frame("window.closed", cursor="c2")],
    ).install(monkeypatch)
    got = []
    stream = computer.events(backoff=0.001)
    with stream:
        for ev in stream:
            got.append(ev.type)
            if len(got) == 2:
                break
    assert got == ["computer.idle", "window.closed"]
    assert dialer.urls[0] == "wss://events.test/?token=control"
    assert dialer.urls[1] == "wss://events.test/?token=control&since=c1"


@respx.mock
def test_every_reconnect_re_reads_the_url(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart rotates the credential, and a restart is one of the ordinary
    reasons the socket dropped in the first place."""
    rotated = {**COMPUTER, "vnc": {**COMPUTER["vnc"], "events_url": "wss://events.test/?token=2"}}  # type: ignore[dict-item]
    calls = {"n": 0}

    def answer(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=COMPUTER if calls["n"] == 1 else rotated)

    respx.get(f"{BASE}/computers/vm-1").mock(side_effect=answer)
    dialer = Dialer(
        [hello_frame(), event_frame("computer.idle", cursor="c1")],
        [hello_frame(), event_frame("window.closed", cursor="c2")],
    ).install(monkeypatch)
    stream = computer.events(backoff=0.001)
    with stream:
        for i, _ in enumerate(stream):
            if i == 1:
                break
    assert dialer.urls[1].startswith("wss://events.test/?token=2")


@respx.mock
def test_reconnect_off_ends_when_the_socket_does(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket ending IS the answer there, so it is not reported as a failure
    to reopen something nobody asked to have reopened."""
    route()
    Dialer([hello_frame(), event_frame("computer.idle")]).install(monkeypatch)
    assert len(list(computer.events(reconnect=False))) == 1


@respx.mock
def test_a_gap_is_an_event_not_an_exception(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not thrown and not swallowed: it is the signal to reconcile with a
    listing rather than to assume nothing happened."""
    route()
    Dialer([hello_frame(), event_frame("gap", data={"oldest_cursor": "c5"}, seq=0)]).install(
        monkeypatch
    )
    (ev,) = list(computer.events(reconnect=False))
    assert ev.type == "gap"
    assert ev.oldest_cursor == "c5"
    assert ev.seq is None


@respx.mock
def test_a_capabilities_frame_revises_the_vocabulary_under_an_open_socket(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer([hello_frame(), event_frame("capabilities", events=["computer.idle"])]).install(
        monkeypatch
    )
    stream = computer.events(reconnect=False)
    list(stream)
    assert stream.event_types == ["computer.idle"]


@respx.mock
def test_frames_that_are_not_events_are_skipped_rather_than_fatal(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proxy's error page reaching a websocket is not an event, and a parse
    failure is not worth ending a stream over."""
    route()
    Dialer(
        [
            hello_frame(),
            "<html>gateway timeout</html>",
            b"\x00\x01",
            "null",
            event_frame("computer.idle"),
        ]
    ).install(monkeypatch)
    assert [ev.type for ev in computer.events(reconnect=False)] == ["computer.idle"]


@respx.mock
def test_one_stream_has_one_consumer(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second loop would split the events between the two rather than give
    each of them all, which is not what anybody writing the second loop means."""
    route()
    Dialer([hello_frame(), event_frame("computer.idle")]).install(monkeypatch)
    stream = computer.events(reconnect=False)
    list(stream)
    with pytest.raises(mc.MandalaError, match="already being consumed"):
        list(stream)


@respx.mock
def test_closing_ends_the_iteration(computer: mc.Computer, monkeypatch: pytest.MonkeyPatch) -> None:
    route()
    dialer = Dialer([hello_frame(), event_frame("computer.idle"), FakeSocket.HANG]).install(
        monkeypatch
    )
    stream = computer.events()
    got = []
    for ev in stream:
        got.append(ev)
        stream.close()
    assert len(got) == 1
    assert dialer.sockets[0].closed


@respx.mock
def test_a_stream_deadline_ends_the_iteration_without_raising(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Python answer to an AbortSignal: it stops, it does not throw, which
    is what makes it composable with a loop looking for one thing."""
    route()
    Dialer([hello_frame(), FakeSocket.HANG]).install(monkeypatch)
    stream = computer.events(timeout=0.05)
    assert list(stream) == []
    assert stream.expired is True


# --- refusals ---------------------------------------------------------------


@respx.mock
def test_a_suspended_computer_is_named_from_the_refusal_itself(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`websockets` hands over the status and the body, so the reason is read
    rather than inferred from a follow-up read the way a browser client must."""
    route()
    Dialer(refused(409, b'{"resume_required": true}')).install(monkeypatch)
    with pytest.raises(mc.MandalaError, match="is suspended"):
        list(computer.events())


@respx.mock
def test_a_stopped_computer_is_named_from_the_refusal_itself(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer(refused(409, b'{"reason": "unavailable"}')).install(monkeypatch)
    with pytest.raises(mc.MandalaError, match="only a running computer"):
        list(computer.events())


@respx.mock
def test_a_settled_refusal_is_not_retried(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A decision rather than weather. Retrying is a client asking the same
    question every fifteen seconds for as long as the process lives."""
    route()
    dialer = Dialer(refused(409, b'{"resume_required": true}')).install(monkeypatch)
    with pytest.raises(mc.MandalaError):
        list(computer.events(backoff=0.001))
    assert len(dialer.urls) == 1


@respx.mock
def test_a_watch_only_credential_is_refused_permanently(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer(refused(401)).install(monkeypatch)
    with pytest.raises(mc.MandalaError, match="watch-only"):
        list(computer.events())


@respx.mock
def test_a_host_that_is_not_ready_is_weather_and_is_retried(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    dialer = Dialer(
        refused(503),
        [hello_frame(), event_frame("computer.idle")],
    ).install(monkeypatch)
    got = take(computer.events(backoff=0.001, max_retries=3), 1)
    assert [ev.type for ev in got] == ["computer.idle"]
    assert len(dialer.urls) == 2


@respx.mock
def test_a_retry_budget_that_runs_out_says_so(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer(refused(503), refused(503), refused(503)).install(monkeypatch)
    with pytest.raises(mc.MandalaError, match="would not open"):
        list(computer.events(backoff=0.001, max_retries=2))


@respx.mock
def test_a_connection_that_delivered_something_resets_the_budget(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consecutive, so a stream that has been up for a week and drops twice has
    not failed twice. Deliberately not "the handshake succeeded" — a host that
    accepts a socket and drops it at once succeeds at every handshake."""
    route()
    Dialer(
        [hello_frame(), event_frame("computer.idle", cursor="c1")],
        refused(503),
        [hello_frame(), event_frame("computer.idle", cursor="c2")],
        refused(503),
        [hello_frame(), event_frame("computer.idle", cursor="c3")],
    ).install(monkeypatch)
    got = take(computer.events(backoff=0.001, max_retries=2), 3)
    assert [ev.cursor for ev in got] == ["c1", "c2", "c3"]


@respx.mock
def test_a_socket_that_says_nothing_is_not_left_open_for_ever(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    dialer = Dialer([FakeSocket.HANG]).install(monkeypatch)
    with pytest.raises(mc.ConnectionError, match="said nothing"):
        list(computer.events(reconnect=False, connect_timeout=0.05))
    assert dialer.sockets[0].closed


@respx.mock
def test_a_socket_that_closes_before_saying_what_it_is(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer([ConnectionClosedError(None, None)]).install(monkeypatch)
    with pytest.raises(mc.ConnectionError, match="closed before it said what it was"):
        list(computer.events(reconnect=False))


# --- the readiness that has already happened --------------------------------


@respx.mock
def test_an_already_ready_desktop_yields_a_readiness_it_will_never_send_again(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`computer.ready` fires once per desktop session, so a raw socket waiting
    for it on a machine that has been up for an hour waits for ever."""
    route()
    Dialer([hello_frame(ready=True, windows=[])]).install(monkeypatch)
    (ev,) = list(computer.events(reconnect=False))
    assert ev.type == "computer.ready"
    assert ev.synthesized is True
    assert ev.seq is None


@respx.mock
def test_a_resumed_connection_manufactures_no_readiness(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`windows` absent is the platform's own test for continuity: either this
    client was already sent the readiness, or it is in the backlog about to
    arrive. Inventing one there is a session replacement that never happened."""
    route()
    Dialer([resumed_hello(ready=True)]).install(monkeypatch)
    assert list(computer.events(reconnect=False, since="c0")) == []


@respx.mock
def test_a_reconnect_that_keeps_continuity_manufactures_no_second_readiness(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With continuity there is nothing to make up: the caller was already sent
    the readiness, or the event carrying it is in the backlog about to arrive."""
    route()
    Dialer(
        [hello_frame(ready=True, windows=[]), event_frame("computer.idle", cursor="c1")],
        [resumed_hello(ready=True), event_frame("window.closed", cursor="c2")],
    ).install(monkeypatch)
    got = take(computer.events(backoff=0.001), 3)
    assert [ev.type for ev in got] == ["computer.ready", "computer.idle", "window.closed"]


@respx.mock
def test_a_reconnect_that_lost_continuity_announces_the_desktop_again(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hole a once-per-STREAM latch left, and why this half does not keep one.

    mandala-computer-typescript's `sawReady` survives reconnects, so a stream
    handed session A's readiness and then resumed WITHOUT continuity declined
    to synthesize for session B — and a gapped resume is exactly the case where
    session B's own `computer.ready` is what the gap says was lost. Restarting
    the display manager inside a guest makes a new session without the computer
    leaving `running`, so that is a real desktop to be waiting for, and the
    latch turned the wait into one that cannot end (Codex review).
    """
    route()
    Dialer(
        [hello_frame(ready=True, windows=[]), event_frame("computer.idle", cursor="c1")],
        [
            hello_frame(ready=True, windows=[]),
            event_frame("gap", data={"oldest_cursor": "c9"}),
        ],
    ).install(monkeypatch)
    got = take(computer.events(backoff=0.001), 3)
    assert [ev.type for ev in got] == ["computer.ready", "computer.idle", "computer.ready"]
    assert got[2].synthesized is True


@respx.mock
def test_a_real_readiness_stops_a_resuming_connection_inventing_one(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer(
        [hello_frame(), event_frame("computer.ready", cursor="c1")],
        [resumed_hello(ready=True), event_frame("computer.idle", cursor="c2")],
    ).install(monkeypatch)
    got = take(computer.events(backoff=0.001), 2)
    assert [(ev.type, ev.synthesized) for ev in got] == [
        ("computer.ready", False),
        ("computer.idle", False),
    ]


@respx.mock
def test_the_desktop_a_connection_joined_is_readable(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer(
        [hello_frame(windows=[{"id": "0x1", "class": "Xfce4-panel"}]), event_frame("computer.idle")]
    ).install(monkeypatch)
    stream = computer.events(reconnect=False)
    list(stream)
    assert stream.windows is not None
    assert [w.wm_class for w in stream.windows] == ["Xfce4-panel"]


# --- the URL --------------------------------------------------------------


@respx.mock
def test_a_windows_guest_has_nowhere_to_run_the_watcher(
    client: mc.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = {**COMPUTER, "os": "windows", "vnc": {**COMPUTER["vnc"], "events_url": ""}}  # type: ignore[dict-item]
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=windows))
    Dialer().install(monkeypatch)
    c = client.computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="runs Windows"):
        list(c.events(backoff=0.001))


@respx.mock
def test_a_missing_events_url_is_not_retried(
    client: mc.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = {**COMPUTER, "vnc": {**COMPUTER["vnc"], "events_url": ""}}  # type: ignore[dict-item]
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=old))
    Dialer().install(monkeypatch)
    c = client.computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="no events_url"):
        list(c.events(backoff=0.001))


@respx.mock
def test_a_host_that_could_not_be_reached_is_weather(
    client: mc.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No connect surface at all means the platform could not reach the host
    holding this computer, and the backoff is the right response to that."""
    bare = {k: v for k, v in COMPUTER.items() if k != "vnc"}
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=bare))
    Dialer().install(monkeypatch)
    c = client.computers.get("vm-1")
    with pytest.raises(mc.ConnectionError, match="connect surface"):
        list(c.events(backoff=0.001, max_retries=1))


@respx.mock
def test_a_computer_that_is_gone_ends_the_stream(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this a deleted computer is a reconnect loop with no `max_retries`
    to stop it, asking a question that has already been answered."""
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(404, json={"error": "gone"}))
    Dialer().install(monkeypatch)
    with pytest.raises(mc.NotFoundError):
        list(computer.events(backoff=0.001))


# --- wait_for ---------------------------------------------------------------


@respx.mock
def test_wait_for_returns_the_event_and_closes_the_socket(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    dialer = Dialer(
        [hello_frame(), event_frame("computer.idle"), event_frame("process.exited")]
    ).install(monkeypatch)
    ev = computer.wait_for("process.exited")
    assert ev.type == "process.exited"
    assert dialer.sockets[0].closed


@respx.mock
def test_wait_for_takes_whichever_of_several_arrives_first(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer([hello_frame(), event_frame("window.closed")]).install(monkeypatch)
    assert computer.wait_for(["process.exited", "window.closed"]).type == "window.closed"


@respx.mock
def test_wait_for_ready_returns_at_once_on_a_desktop_that_is_already_up(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason the opening frame's state becomes an event."""
    route()
    Dialer([hello_frame(ready=True, windows=[]), FakeSocket.HANG]).install(monkeypatch)
    ev = computer.wait_for("computer.ready", timeout=5)
    assert ev.synthesized is True


@respx.mock
def test_wait_for_refuses_an_event_this_computer_cannot_emit(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Waiting for it is waiting for something the platform has already said
    will not arrive, which from inside a loop is indistinguishable from a
    desktop that is merely slow."""
    route()
    Dialer([hello_frame(events=["computer.idle"]), FakeSocket.HANG]).install(monkeypatch)
    with pytest.raises(mc.MandalaError, match="cannot emit window.opened"):
        computer.wait_for("window.opened", timeout=5)


@respx.mock
def test_wait_for_does_not_refuse_when_one_of_several_is_reachable(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing would be this SDK deciding the half a caller can have is not the
    half they meant."""
    route()
    Dialer([hello_frame(events=["process.exited"]), event_frame("process.exited")]).install(
        monkeypatch
    )
    assert computer.wait_for(["window.opened", "process.exited"], timeout=5).type == (
        "process.exited"
    )


@respx.mock
def test_wait_for_allows_a_stream_frame_the_vocabulary_never_mentions(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hello.events` is about the COMPUTER, and a gap is about the stream."""
    route()
    Dialer([hello_frame(events=[]), event_frame("gap", data={"oldest_cursor": "c5"})]).install(
        monkeypatch
    )
    assert computer.wait_for("gap", timeout=5).type == "gap"


@respx.mock
def test_a_capabilities_frame_can_end_a_wait_that_can_no_longer_finish(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer(
        [
            hello_frame(events=["window.opened"]),
            event_frame("capabilities", events=["computer.idle"]),
            FakeSocket.HANG,
        ]
    ).install(monkeypatch)
    with pytest.raises(mc.MandalaError, match="cannot emit window.opened"):
        computer.wait_for("window.opened", timeout=5)


@respx.mock
def test_wait_for_composes_the_callers_own_hook(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is an ordinary option here, and a hook accepted by the signature and
    silently never called is a defect rather than a smaller version of one."""
    route()
    Dialer([hello_frame(), event_frame("process.exited")]).install(monkeypatch)
    seen: list[Hello] = []
    computer.wait_for("process.exited", on_connect=seen.append, timeout=5)
    assert len(seen) == 1


@respx.mock
def test_wait_for_gives_up_with_a_timeout(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer([hello_frame(), FakeSocket.HANG]).install(monkeypatch)
    with pytest.raises(mc.TimeoutError, match="within 0.05s"):
        computer.wait_for("process.exited", timeout=0.05)


@respx.mock
def test_wait_for_says_the_stream_ended_rather_than_inventing_a_timeout(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reached with `reconnect=False`, where the socket ending IS the answer."""
    route()
    Dialer([hello_frame(), event_frame("computer.idle")]).install(monkeypatch)
    with pytest.raises(mc.MandalaError, match="ended before process.exited arrived"):
        computer.wait_for("process.exited", reconnect=False, timeout=5)


def test_wait_for_needs_something_to_wait_for(computer: mc.Computer) -> None:
    with pytest.raises(mc.MandalaError, match="at least one event type"):
        computer.wait_for([])


# --- numbers that would make a wait never end -------------------------------


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"backoff": 0}, "backoff must be a positive"),
        ({"backoff": float("nan")}, "backoff must be a positive"),
        ({"backoff": float("inf")}, "backoff must be a positive"),
        ({"max_backoff": -1}, "max_backoff must be a positive"),
        ({"connect_timeout": float("nan")}, "connect_timeout must be a positive"),
        ({"max_retries": -1}, "max_retries must be a non-negative"),
        ({"max_retries": 1.5}, "max_retries must be a non-negative"),
        ({"max_queue": 0}, "max_queue must be a positive"),
        ({"timeout": 0}, "timeout must be a positive"),
        ({"timeout": float("inf")}, "timeout must be a positive"),
        ({"timeout": float("nan")}, "timeout must be a positive"),
    ],
)
def test_a_number_that_would_never_throttle_is_refused(
    computer: mc.Computer, kw: dict[str, Any], match: str
) -> None:
    """A NaN backoff is an unthrottled reconnect loop against the platform, and
    a NaN deadline is a wait that never arrives — refused before a socket is
    opened rather than discovered by the platform."""
    with pytest.raises(mc.MandalaError, match=match):
        computer.events(**kw)


# --- the async half ---------------------------------------------------------


@respx.mock
async def test_async_events_arrive_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    route()
    Dialer(
        [hello_frame(), event_frame("computer.idle"), event_frame("window.closed")],
        cls=AsyncFakeSocket,
    ).install(monkeypatch)
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        got = [ev.type async for ev in c.events(reconnect=False)]
    assert got == ["computer.idle", "window.closed"]


@respx.mock
async def test_async_reconnect_asks_from_where_the_caller_got_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route()
    dialer = Dialer(
        [hello_frame(), event_frame("computer.idle", cursor="c1")],
        [hello_frame(), event_frame("window.closed", cursor="c2")],
        cls=AsyncFakeSocket,
    ).install(monkeypatch)
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        stream = c.events(backoff=0.001)
        got = []
        async for ev in stream:
            got.append(ev.type)
            if len(got) == 2:
                break
        await stream.aclose()
    assert got == ["computer.idle", "window.closed"]
    assert dialer.urls[1].endswith("since=c1")


@respx.mock
async def test_async_wait_for_returns_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    route()
    Dialer(
        [hello_frame(), event_frame("computer.idle"), event_frame("process.exited")],
        cls=AsyncFakeSocket,
    ).install(monkeypatch)
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        ev = await c.wait_for("process.exited", timeout=5)
    assert ev.type == "process.exited"


@respx.mock
async def test_async_wait_for_ready_returns_at_once_on_a_ready_desktop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route()
    Dialer([hello_frame(ready=True, windows=[]), FakeSocket.HANG], cls=AsyncFakeSocket).install(
        monkeypatch
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        ev = await c.wait_for("computer.ready", timeout=5)
    assert ev.synthesized is True


@respx.mock
async def test_async_wait_for_refuses_an_event_that_cannot_arrive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route()
    Dialer([hello_frame(events=["computer.idle"]), FakeSocket.HANG], cls=AsyncFakeSocket).install(
        monkeypatch
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        with pytest.raises(mc.MandalaError, match="cannot emit window.opened"):
            await c.wait_for("window.opened", timeout=5)


@respx.mock
async def test_async_wait_for_gives_up_with_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    route()
    Dialer([hello_frame(), FakeSocket.HANG], cls=AsyncFakeSocket).install(monkeypatch)
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        with pytest.raises(mc.TimeoutError):
            await c.wait_for("process.exited", timeout=0.05)


@respx.mock
async def test_async_a_suspended_computer_is_named_from_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route()
    Dialer(refused(409, b'{"resume_required": true}'), cls=AsyncFakeSocket).install(monkeypatch)
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        with pytest.raises(mc.MandalaError, match="is suspended"):
            async for _ in c.events():
                pass


# --- the connect surface ----------------------------------------------------


def test_the_connect_surface_decodes_an_events_url() -> None:
    vnc = mc.VncConnect.from_api(COMPUTER["vnc"])  # type: ignore[arg-type]
    assert vnc is not None
    assert vnc.events_url == "wss://events.test/?token=control"


def test_an_events_url_is_redacted_in_a_repr() -> None:
    """It carries the controlling credential, so a traceback rendering this
    object must not hand over the desktop."""
    vnc = mc.VncConnect.from_api(COMPUTER["vnc"])  # type: ignore[arg-type]
    assert vnc is not None
    assert "token=control" not in repr(vnc)
    assert "events_url='wss://events.test/?<redacted>'" in repr(vnc)


def test_a_host_that_sends_no_events_url_reads_as_empty() -> None:
    vnc = mc.VncConnect.from_api({"token": "a", "view_token": "b"})
    assert vnc is not None
    assert vnc.events_url == ""


def test_the_connect_surface_keeps_its_positional_order() -> None:
    """This class is exported, so its field order IS its constructor, and `raw`
    has been the seventh positional argument since this SDK shipped."""
    vnc = mc.VncConnect("u", "v", "t", "vt", "e", "term", {"raw": True})
    assert vnc.raw == {"raw": True}
    assert vnc.events_url == ""


# --- stopping a stream that is waiting --------------------------------------


class BlockingSocket(FakeSocket):
    """A socket that genuinely parks, and is released by its own close.

    What a real one does on a quiet desktop, and the only way to test that
    stopping a stream reaches a reader rather than waiting for the next frame
    of a stream nobody is reading any more.
    """

    def __init__(self, script: list[Any]) -> None:
        super().__init__(script)
        self.released = __import__("threading").Event()

    def recv(self, timeout: float | None = None) -> Any:
        if self.script:
            return super().recv(timeout)
        self.released.wait(timeout)
        raise ConnectionClosedOK(None, None)

    def close(self) -> None:
        self.closed = True
        self.released.set()


@respx.mock
def test_a_stop_from_another_thread_reaches_a_reader_that_is_parked(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sync half is woken by the closing handshake `close()` performs, which
    is a thing only another thread can do while this one is inside `recv`."""
    import threading

    route()
    dialer = Dialer([hello_frame(), event_frame("computer.idle")], cls=BlockingSocket).install(
        monkeypatch
    )
    stream = computer.events(reconnect=False)
    got: list[mc.ComputerEvent] = []

    def read() -> None:
        got.extend(stream)

    reader = threading.Thread(target=read)
    reader.start()
    # The socket exists only once the connection has been made, so wait for it
    # rather than racing the thread to the first `recv`.
    for _ in range(500):
        if dialer.sockets and not stream.hello is None:
            break
        __import__("time").sleep(0.002)
    stream.close()
    reader.join(timeout=5)
    assert not reader.is_alive(), "close() did not reach the parked reader"
    assert [ev.type for ev in got] == ["computer.idle"]


@respx.mock
async def test_an_async_stop_reaches_a_reader_that_is_parked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`close()` on the async half cannot await a closing handshake from where
    it is called, so the read has to be watching for the stop itself."""
    route()
    Dialer(
        [hello_frame(), event_frame("computer.idle"), FakeSocket.HANG], cls=AsyncFakeSocket
    ).install(monkeypatch)
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        stream = c.events(reconnect=False)
        got = []

        async def read() -> None:
            async for ev in stream:
                got.append(ev.type)

        task = asyncio.ensure_future(read())
        while not got:
            await asyncio.sleep(0.005)
        stream.close()
        await asyncio.wait_for(task, timeout=5)
    assert got == ["computer.idle"]


@respx.mock
def test_a_frame_that_arrives_ahead_of_the_opening_one_is_not_dropped(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reference says `hello` is first and the platform sends it first, so
    this never happens — and a frame discarded here would be silent loss on the
    one stream whose whole purpose is not to have any."""
    route()
    Dialer([event_frame("computer.idle", cursor="c1"), hello_frame(), event_frame("gap")]).install(
        monkeypatch
    )
    assert [ev.type for ev in computer.events(reconnect=False)] == ["computer.idle", "gap"]


# --- what a Codex review of PR #43 found ------------------------------------


@respx.mock
def test_an_empty_since_is_no_cursor_rather_than_a_position_that_never_advances(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`""` is the shape an unset environment variable arrives in.

    `with_cursor` already skips it, so the first connection correctly joins at
    the head — but stored as `""` it is not `None` either, so the opening
    frame's cursor was never adopted and a reconnect joined at the head a
    SECOND time, skipping whatever happened in between. Silent loss on the one
    stream whose whole purpose is not to have any.
    """
    route()
    dialer = Dialer(
        [hello_frame(cursor="c0"), ConnectionClosedError(None, None)],
        [hello_frame(cursor="c7"), event_frame("computer.idle", cursor="c1")],
    ).install(monkeypatch)
    take(computer.events(since="", backoff=0.001), 1)
    assert dialer.urls[0] == "wss://events.test/?token=control"
    assert dialer.urls[1] == "wss://events.test/?token=control&since=c0"


@respx.mock
def test_a_duplicate_wanted_type_does_not_refuse_a_reachable_wait(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counting `impossible` with duplicates in it against the DISTINCT types
    asked for made the two sides count different things, so a repeated
    unreachable type outvoted a reachable one."""
    route()
    Dialer([hello_frame(events=["process.exited"]), event_frame("process.exited")]).install(
        monkeypatch
    )
    got = computer.wait_for(["window.opened", "window.opened", "process.exited"], timeout=5)
    assert got.type == "process.exited"


@respx.mock
def test_a_repeated_impossible_type_is_still_refused_and_named_once(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    route()
    Dialer([hello_frame(events=["computer.idle"]), FakeSocket.HANG]).install(monkeypatch)
    with pytest.raises(mc.MandalaError, match=r"cannot emit window\.opened, so waiting"):
        computer.wait_for(["window.opened", "window.opened"], timeout=5)


def test_the_first_backoff_step_is_capped_by_the_ceiling() -> None:
    """`max_backoff` is documented as the most any step may be, and the first
    one used only to be doubled TOWARDS it — so `backoff=30, max_backoff=15`
    slept for thirty once, which is the sleep the ceiling exists to bound."""
    from mandala_computer._events import _Core

    core = _Core(
        reconnect=True,
        backoff=30.0,
        max_backoff=15.0,
        max_retries=0,
        connect_timeout=15.0,
        cursor=None,
        on_connect=None,
    )
    assert core.wait_out() == 15.0
    assert core.wait_out() == 15.0


def test_a_delivering_connection_does_not_uncap_the_backoff() -> None:
    """``worked()`` used to reset ``step`` to the raw ``backoff``, undoing the
    ``__post_init__`` cap. After a connection that delivered events died, the
    next ``wait_out()`` slept thirty seconds of a fifteen-second ceiling."""
    from mandala_computer._events import _Core

    core = _Core(
        reconnect=True,
        backoff=30.0,
        max_backoff=15.0,
        max_retries=0,
        connect_timeout=15.0,
        cursor=None,
        on_connect=None,
    )
    core.worked()
    assert core.wait_out() == 15.0
    assert core.wait_out() == 15.0


@respx.mock
def test_the_connect_budget_covers_the_read_that_fetches_the_url(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It was the phase nobody was bounding. The read goes through the ordinary
    transport, whose own timeout is a minute, so a five-second wait could sit
    in it for sixty seconds before anything checked the five."""
    caps: list[float | None] = []
    real = mc.Computer._refresh

    def capped(self: mc.Computer, *, timeout_cap: float | None = None) -> mc.Computer:
        caps.append(timeout_cap)
        return real(self, timeout_cap=timeout_cap)

    monkeypatch.setattr(mc.Computer, "_refresh", capped)
    route()
    Dialer([hello_frame(), event_frame("computer.idle")]).install(monkeypatch)
    list(computer.events(reconnect=False, connect_timeout=4))
    assert caps == [4.0]


@respx.mock
def test_a_wait_caps_the_url_read_by_whichever_deadline_is_nearer(
    computer: mc.Computer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One budget across all three phases, so the wait's own deadline wins when
    it is shorter than `connect_timeout`."""
    caps: list[float | None] = []
    real = mc.Computer._refresh

    def capped(self: mc.Computer, *, timeout_cap: float | None = None) -> mc.Computer:
        caps.append(timeout_cap)
        return real(self, timeout_cap=timeout_cap)

    monkeypatch.setattr(mc.Computer, "_refresh", capped)
    route()
    Dialer([hello_frame(), event_frame("process.exited")]).install(monkeypatch)
    computer.wait_for("process.exited", timeout=2, connect_timeout=30)
    assert caps and caps[0] is not None and caps[0] <= 2.0


@respx.mock
async def test_the_async_connect_budget_covers_the_url_read_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps: list[float | None] = []
    real = mc.AsyncComputer._refresh

    async def capped(
        self: mc.AsyncComputer, *, timeout_cap: float | None = None
    ) -> mc.AsyncComputer:
        caps.append(timeout_cap)
        return await real(self, timeout_cap=timeout_cap)

    monkeypatch.setattr(mc.AsyncComputer, "_refresh", capped)
    route()
    Dialer([hello_frame(), event_frame("computer.idle")], cls=AsyncFakeSocket).install(monkeypatch)
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = await client.computers.get("vm-1")
        caps.clear()
        assert [ev.type async for ev in c.events(reconnect=False, connect_timeout=4)] == [
            "computer.idle"
        ]
    assert caps == [4.0]
