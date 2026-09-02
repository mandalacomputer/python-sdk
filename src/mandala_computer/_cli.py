"""The ``mandala`` command — a computer's shell and files from your own terminal,
and the account's webhooks.

Two subcommands address a computer by name or id:

``mandala ssh <computer>``
    An interactive shell in the guest, over the platform's terminal websocket —
    a PTY the platform keeps alive server-side. Disconnecting detaches the
    session rather than ending it; running the same command reattaches and
    replays recent output. ``--session`` names one of several.

``mandala scp <src> <dst>``
    Copy one file in or out, ``scp``-style: the side spelled
    ``<computer>:/path`` is the guest. Rides the files API, so it needs no
    shell in the guest at all. A download is paged, so a file larger than the
    64 MiB one request moves copies like any other; an upload is one request,
    and one over the limit is refused before it is read.

``mandala webhooks <list|create|get|update|delete|rotate|test|deliveries>``
    The account's webhook subscriptions — the CRUD only. The CLI does not
    receive webhooks; a receiver is a server, and :func:`mandala_computer.verify`
    is what it calls. ``create`` and ``rotate`` print the secret ONCE.

Authentication is the SDK's: ``MANDALA_API_KEY`` (and optionally
``MANDALA_BASE_URL``) in the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import select
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from types import FrameType
from typing import TYPE_CHECKING, Any, NoReturn

from ._api import looks_windows_guest_path
from ._client import FILE_SIZE_LIMIT
from ._computer import Computer
from ._exceptions import MandalaError
from ._models import Webhook, WebhookDelivery

if TYPE_CHECKING:
    from websockets.sync.client import ClientConnection

    from mandala_computer import Client

# The whole guest-side scrollback is smaller than this; anything bigger in one
# frame is not the terminal protocol.
_MAX_FRAME = 1 << 22

# Captured once so path parsing and platform guards can be tested without
# mutating the process-wide ``os`` module.
LOCAL_WINDOWS = os.name == "nt"


def _die(message: str) -> NoReturn:
    raise SystemExit(f"mandala: {message}")


#: How long a write to stdout may hold the local terminal before it gives it back.
#:
#: A consumer that is only slow — a ``tee`` to a busy disk, a pager between
#: keystrokes — is not an error, and dropping guest output to escape one would
#: corrupt the stream, so the write itself still waits as long as it takes.
#: What stops waiting is the *terminal*: past this the tty comes out of raw
#: mode, so Ctrl-C is a signal again and the session can be ended from the
#: keyboard that opened it. Same length as :data:`_SENDER_JOIN_TIMEOUT`, for
#: the same reason — long enough that an ordinary hiccup passes unnoticed,
#: short enough that a wedged consumer is not a wedged terminal.
_STDOUT_STALL_GRACE = 2.0

#: What a guarded write is broken into.
#:
#: A blocking write larger than a pipe's atomic unit does not come back short
#: on Linux — it waits for the whole count — which would put the wait back
#: inside a single ``os.write``, where no timeout can see it.
_WRITE_CHUNK = getattr(select, "PIPE_BUF", 512)


def _writable(fd: int, timeout: float) -> bool:
    """Whether *fd* will take a byte now, as far as ``select`` can tell.

    An fd ``select`` cannot answer for — a regular file, an odd platform — is
    called ready, which is what it is: a write to a file does not stall on a
    reader that stopped reading.
    """
    try:
        _, ready, _ = select.select((), (fd,), (), timeout)
    except (OSError, ValueError):
        return True
    return bool(ready)


def _write_all(fd: int, data: bytes, on_stall: Callable[[], None] | None = None) -> None:
    """Write every byte, however many calls that takes.

    ``os.write`` is allowed to write less than it was given, and here it will:
    frames run to :data:`_MAX_FRAME` on a scrollback replay, against a pipe
    whose buffer is a few kilobytes, and a SIGWINCH landing mid-write ends it
    early with a short count rather than being retried. The unwritten tail is
    guest output, and dropping it silently corrupts the terminal.

    *on_stall*, if given, is called once when the fd has refused a byte for
    :data:`_STDOUT_STALL_GRACE`. The write still finishes afterwards: the
    caller uses this to stop paying for the stall with something it cares about
    more, not to give up on the output. Without it this loop is unbounded, and
    a consumer that read one pipe buffer and stopped —
    ``mandala ssh dev | head -c 1`` and then nothing — parked the recv pump
    here forever with the local terminal still raw and ``ISIG`` off, so Ctrl-C
    was a byte to a guest nobody was reading and recovery meant a ``kill`` from
    another window (OPL-4246).
    """
    view = memoryview(data)
    while view:
        chunk = view
        if on_stall is not None:
            chunk = view[:_WRITE_CHUNK]
            if not _writable(fd, _STDOUT_STALL_GRACE):
                on_stall()
                on_stall = None
                chunk = view
        written = os.write(fd, chunk)
        if written == 0:
            raise OSError("write made no progress")
        view = view[written:]


def _terminal_fd() -> int | None:
    """The fd to measure the local terminal on, or ``None`` if there is none.

    stdin first: it is the fd raw mode is set from, and its terminal is the one
    SIGWINCH reports on. stdout is not the right answer on its own —
    ``mandala ssh dev | tee session.log`` is still a session in whatever window
    the user is sitting in, and sizing it from the pipe left the guest PTY at
    the platform's 80x24 default for the whole session, with ``vim``, ``htop``
    and ``less`` wrong all the way through (OPL-4246). The other two are tried
    after it so a redirected stdin (``mandala ssh dev < script``) still reports
    the window its output is being drawn in.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        with suppress(AttributeError, ValueError, OSError):
            if stream is not None and stream.isatty():
                return stream.fileno()
    return None


def _terminal_size(fd: int) -> tuple[int, int]:
    """The local terminal's geometry, measured on *fd*.

    Not :func:`shutil.get_terminal_size`, which consults ``COLUMNS``/``LINES``
    and then ``sys.__stdout__``: with stdout piped that last one is not a
    terminal, so it answers with its own (80, 24) fallback and the guest is
    told the window is 80 columns however wide it is. Moving the call is
    therefore not the fix; the fd has to change too.

    The environment override is kept, because that is how a user tells a
    program a size no ioctl can report, and each half falls back on its own
    just as ``shutil`` does.
    """
    columns = rows = 0
    with suppress(KeyError, ValueError):
        columns = int(os.environ["COLUMNS"])
    with suppress(KeyError, ValueError):
        rows = int(os.environ["LINES"])
    if columns <= 0 or rows <= 0:
        try:
            size = os.get_terminal_size(fd)
        except (OSError, ValueError):
            size = os.terminal_size((80, 24))
        if columns <= 0:
            columns = size.columns
        if rows <= 0:
            rows = size.lines
    return columns, rows


def _client() -> Client:
    # Imported at call time: the package's __init__ imports nothing from here,
    # so the cycle stays one-way.
    from mandala_computer import Client

    return Client()


def _resolve(client: Client, target: str) -> Computer:
    """The computer ``target`` names — an exact id, or a unique name."""
    # Resolution is not a fleet-wide consistency decision. One unreachable
    # hypervisor must not block ssh/scp to a computer on a healthy one, and the
    # partial response still carries cached id-only rows for unavailable hosts.
    computers = client.computers.list(allow_partial=True)
    for c in computers:
        if c.id == target:
            return c
    named = [c for c in computers if c.name == target]
    if len(named) == 1:
        return named[0]
    if named:
        ids = ", ".join(c.id for c in named)
        _die(f"{target!r} names {len(named)} computers — use an id: {ids}")
    if not computers.is_complete:
        if computers:
            have = "\n".join(f"  {c.id}  {c.name}  {c.status}" for c in computers)
            _die(
                f"no computer named {target!r} in an incomplete fleet listing. "
                f"Known computers:\n{have}\nretry when every host is reachable"
            )
        _die(
            f"no computer named {target!r} in an incomplete fleet listing; "
            "retry when every host is reachable"
        )
    if not computers:
        _die(f"no computer named {target!r}; the account has no computers")
    have = "\n".join(f"  {c.id}  {c.name}  {c.status}" for c in computers)
    _die(f"no computer named {target!r}. You have:\n{have}")


# --- ssh -------------------------------------------------------------------


def _cmd_ssh(args: argparse.Namespace) -> int:
    if LOCAL_WINDOWS:
        _die("interactive ssh requires a Unix-like local terminal")
    with _client() as client:
        c = _resolve(client, args.target).refresh()
    vnc = c.vnc
    if vnc is None or not vnc.terminal_url:
        if c.os == "windows":
            _die(f"{c.name} is a Windows computer; terminals are Linux-only for now")
        if c.status not in ("running", "suspended"):
            _die(f"{c.name} is {c.status or 'not running'} — start it, then retry")
        _die(f"{c.name} has no terminal endpoint (server too old?)")
    url = vnc.terminal_url
    params: list[tuple[str, str]] = []
    if args.session != "main":
        params.append(("session", args.session))
    # The broker sizes the PTY from the upgrade URL — `cols`/`rows`, defaulting
    # to 80x24 — and only honours a `resize` frame after that. A session opened
    # without them therefore draws its login prompt, its MOTD and any replayed
    # scrollback 80 columns wide before the first resize lands, however wide
    # the window is (OPL-4246). Measured on the terminal fd, not on stdout,
    # because a piped stdout is still a session someone is watching.
    tty_fd = _terminal_fd()
    if tty_fd is not None:
        cols, rows = _terminal_size(tty_fd)
        params += [("cols", str(cols)), ("rows", str(rows))]
    if params:
        from urllib.parse import quote, urlencode

        url += ("&" if "?" in url else "?") + urlencode(params, quote_via=quote)
    return _interact(url)


def _connect(url: str) -> ClientConnection:
    from websockets.exceptions import InvalidStatus, WebSocketException
    from websockets.sync.client import connect

    try:
        return connect(url, max_size=_MAX_FRAME, compression=None)
    except InvalidStatus as e:
        status = e.response.status_code
        if status == 409:
            # The terminal channel is added to a computer at cold boot only —
            # a machine running since before its host learned the feature
            # answers 409 until it picks the device up.
            _die(
                "this computer predates the terminal channel — "
                "stop it and start it again (a restart is not enough), then retry"
            )
        _die(f"terminal refused: HTTP {status}")
    except OSError as e:
        _die(f"could not reach the terminal: {e}")
    except WebSocketException:
        # Not ``str(e)``: ``InvalidURI`` includes the full URI, and
        # ``terminal_url`` carries a credential with no expiry. The
        # ``InvalidStatus`` branch above already prints a token-free sentence.
        _die("could not open the terminal")


def _exit_code(message: str) -> int | None:
    """The status carried by a text frame, or ``None`` if it carries none.

    Text frames are control, and the only one that ends a session is
    ``{"type": "exit", "code": N}``. Everything else — a resize echo, an error
    notice, a frame from a peer that is not the daemon — is not news here.

    Everything that is not that shape is junk, *including* valid JSON that is
    not an object: ``null``, a number and a list all parse cleanly and then
    have no ``.get``. That reached the pump as an ``AttributeError``, which
    ``main()`` does not catch, so a single stray frame ended an interactive
    session in a traceback. An unreadable ``code`` is the same story one line
    down, and is answered the same way — the frame's arrival is the news that
    the shell ended, and no code to read is not a reason to invent a failure.
    """
    try:
        control = json.loads(message)
    except ValueError:
        return None
    if not isinstance(control, dict) or control.get("type") != "exit":
        return None
    try:
        return int(control.get("code") or 0)
    # `OverflowError` with the two obvious ones: `json.loads("1e309")` is `inf`,
    # and `int(inf)` raises neither of them. It escapes the pump's own handlers
    # and `main()`, so one stray frame ends an interactive session in a
    # traceback — which is the outcome the docstring above says this function
    # exists to prevent (/code-review, OPL-4232).
    except (OverflowError, TypeError, ValueError):
        return 0


# Cap on unread local keystrokes / piped stdin waiting to go out. A stalled
# websocket plus a firehose on stdin used to grow without bound; blocking the
# stdin pump here is the backpressure. Resize uses put_nowait so a signal
# handler never blocks.
_OUTBOUND_QUEUE_MAX = 256


def _outbound_queue() -> queue.Queue[bytes | str | object]:
    return queue.Queue(maxsize=_OUTBOUND_QUEUE_MAX)


#: How long shutdown waits for the sender before it stops being polite.
#:
#: Long enough that an ordinary drain finishes inside it, short enough that a
#: wedged session is not a wedged terminal. See :func:`_abort`.
_SENDER_JOIN_TIMEOUT = 2.0


def _stop_sender(outbound: queue.Queue[Any], sentinel: object) -> None:
    """Get the stop sentinel into the queue without waiting for room.

    A blocking ``put`` here is a second way to hang. The queue is bounded, and
    the case that fills it is exactly the case shutdown is running for: a peer
    that stopped reading, a sender parked in ``send()``, and a paste still
    draining behind it. Nothing will ever call ``get`` again, so the ``put``
    that ends the sender waits forever on the sender.

    Room is made by dropping what is queued, which costs nothing: ``closed`` is
    already set, and the sender skips every message it takes after that. The
    loop ends because stdin has already stopped and each pass either enqueues
    the sentinel or removes one item from a bounded queue.
    """
    while True:
        try:
            outbound.put_nowait(sentinel)
            return
        except queue.Full:
            with suppress(queue.Empty):
                outbound.get_nowait()


def _abort(ws: object) -> None:
    """Break a send that will never finish, so shutdown can.

    ``websockets``' sync ``send`` has no send timeout: it is ``sendall`` on a
    blocking socket, so a peer that stops reading blocks it until the peer comes
    back — which a peer that has just sent ``exit`` and closed its read side
    never does. Joining the sender then never returns, and ``close()`` is
    deliberately sequenced AFTER that join so a close frame cannot overlap a
    send, which means nothing was left that could break the deadlock. The
    terminal stayed raw, and Ctrl-C in raw mode is a byte to a reader that has
    already exited (adversarial review, OPL-4222).

    Shutting the socket down at the transport is what unblocks it. Reached only
    once the polite join has expired, so the ordinary path still closes the
    connection the ordinary way. Failures are ignored on purpose: this runs
    while something is already wrong, and every caller below it is cleanup.
    """
    sock = getattr(ws, "socket", None)
    if sock is None:
        return
    import socket as _socket

    with suppress(OSError, ValueError, AttributeError):
        sock.shutdown(_socket.SHUT_RDWR)


def _interact(url: str) -> int:
    """Pump the local terminal into the websocket and back, until the shell ends.

    Binary frames are the terminal's bytes in both directions; text frames are
    control — resize out, exit in. The local TTY goes raw so every keystroke
    (including Ctrl-C) belongs to the remote shell.
    """
    from websockets.exceptions import ConnectionClosed, WebSocketException

    ws = _connect(url)
    stdin = sys.stdin.fileno()
    stdout = sys.stdout.fileno()
    stdout_is_tty = sys.stdout.isatty()
    tty_fd = _terminal_fd()
    closed = threading.Event()
    resize = object()
    stop_sender = object()
    outbound = _outbound_queue()
    pending_resize = threading.Event()
    stdin_wakeup_read: int | None = None
    stdin_wakeup_write: int | None = None

    def send_size(*_sig: object) -> None:
        # A Python signal handler must not touch the socket, and websockets
        # forbids overlapping sends. The sender owns both the terminal query
        # and every outbound frame; this handler does nothing but enqueue.
        try:
            outbound.put_nowait(resize)
        except queue.Full:
            # SIGWINCH is edge-triggered and nothing re-arms it, so a resize
            # dropped here is wrong for the rest of the session rather than
            # merely late: a window resized while a paste is draining would
            # leave the remote pty at the old geometry for good. The sender
            # reads the geometry itself, so one flag stands in for however many
            # resizes the queue could not take.
            pending_resize.set()

    def pump_stdin() -> None:
        assert stdin_wakeup_read is not None
        with suppress(OSError, ValueError):
            while not closed.is_set():
                ready, _, _ = select.select((stdin, stdin_wakeup_read), (), ())
                if stdin_wakeup_read in ready:
                    return
                data = os.read(stdin, 4096)
                if not data:
                    return  # piped stdin ran dry; the shell decides what's next
                # Bounded queue: a stall+firehose must not pin join() on put().
                while True:
                    try:
                        outbound.put(data, timeout=0.1)
                        break
                    except queue.Full:
                        if closed.is_set():
                            return

    def pump_outbound() -> None:
        def send(message: bytes | str) -> None:
            with suppress(ConnectionClosed, OSError, WebSocketException):
                ws.send(message)

        def resize_frame() -> str:
            # Cleared before the size is read, so a resize arriving during the
            # read is flagged again rather than swallowed by the clear.
            pending_resize.clear()
            cols, rows = _terminal_size(stdout if tty_fd is None else tty_fd)
            return json.dumps({"type": "resize", "cols": cols, "rows": rows})

        # Keep draining until the stop sentinel even after the socket dies, or a
        # full queue cannot accept that sentinel and shutdown deadlocks. Do not
        # send once closed: a peer that already exited may have stopped reading,
        # and flushing the backlog would delay or hang join().
        while True:
            message = outbound.get()
            if message is stop_sender:
                return
            if closed.is_set():
                continue
            if message is resize:
                message = resize_frame()
            assert isinstance(message, (bytes, str))
            send(message)
            # The flag is only ever set while the queue is full, so there is
            # always a later iteration to pick it up here.
            if pending_resize.is_set() and not closed.is_set():
                send(resize_frame())

    saved_tty = None
    saved_winch: Callable[[int, FrameType | None], Any] | int | signal.Handlers | None = (
        signal.SIG_DFL
    )
    winch_installed = False
    sigwinch: int | signal.Signals | None = getattr(signal, "SIGWINCH", None)
    saved_exit_handlers: dict[
        int, Callable[[int, FrameType | None], Any] | int | signal.Handlers
    ] = {}
    sender_thread: threading.Thread | None = None
    stdin_thread: threading.Thread | None = None
    exit_code: int | None = None
    tty_raw = False

    def restore_tty() -> None:
        nonlocal tty_raw
        if saved_tty is None or not tty_raw:
            return
        import termios

        termios.tcsetattr(stdin, termios.TCSADRAIN, saved_tty)
        tty_raw = False

    def stdout_stalled() -> None:
        """Nothing is reading stdout. Give the terminal back and keep waiting.

        The output is not dropped — it is still the guest's, and the consumer
        may yet come back — but the user's terminal stops being collateral:
        out of raw mode ``ISIG`` is on again, so Ctrl-C is a signal that ends
        the session rather than a byte to a pipe nobody is reading.

        The notice goes out only on a terminal stderr. Anywhere else it is
        either unread or, under ``2>&1``, the very pipe that is stalled.
        """
        if not tty_raw:
            return
        restore_tty()
        with suppress(OSError, ValueError):
            if sys.stderr is not None and sys.stderr.isatty():
                print(
                    "mandala: nothing is reading stdout — the terminal is no "
                    "longer raw; Ctrl-C ends the session",
                    file=sys.stderr,
                    flush=True,
                )

    def forward_exit_signal(signum: int, frame: FrameType | None) -> None:
        """Put the TTY back before preserving the process's prior signal behavior."""
        nonlocal tty_raw
        previous = saved_exit_handlers[signum]
        if previous == signal.SIG_IGN:
            return
        restore_tty()
        signal.signal(signum, previous)
        if callable(previous):
            previous(signum, frame)
            # A custom handler is allowed to return. If it does, the session is
            # still alive, so resume raw mode and keep protecting the terminal.
            import tty

            signal.signal(signum, forward_exit_signal)
            tty.setraw(stdin)
            tty_raw = True
            return
        os.kill(os.getpid(), signum)
        # A default terminating signal never returns. This is only a fallback
        # for an unusual runtime (or a test double) that did not deliver it.
        raise SystemExit(128 + signum)

    try:
        # Setup belongs under the same finally as restoration. In particular,
        # installing the resize handler can fail after raw mode has been set.
        if sys.stdin.isatty():
            import termios
            import tty

            saved_tty = termios.tcgetattr(stdin)
            # Install before raw mode, so there is no interval in which a
            # terminating signal can leave the terminal changed.
            for name in ("SIGTERM", "SIGHUP", "SIGQUIT"):
                signum = getattr(signal, name, None)
                if signum is None or int(signum) in saved_exit_handlers:
                    continue
                previous = signal.getsignal(signum)
                if previous is None:
                    previous = signal.SIG_DFL
                saved_exit_handlers[int(signum)] = previous
                try:
                    signal.signal(signum, forward_exit_signal)
                except Exception:
                    del saved_exit_handlers[int(signum)]
                    raise
            tty.setraw(stdin)
            tty_raw = True
        # Not `sys.stdout.isatty()`: raw mode above is set from stdin, and
        # sizing from stdout meant a piped session never resized at all
        # (OPL-4246).
        if tty_fd is not None:
            send_size()
            if sigwinch is not None:
                saved_winch = signal.getsignal(sigwinch)
                signal.signal(sigwinch, send_size)
                winch_installed = True

        stdin_wakeup_read, stdin_wakeup_write = os.pipe()
        sender_thread = threading.Thread(target=pump_outbound, daemon=True)
        sender_thread.start()
        stdin_thread = threading.Thread(target=pump_stdin, daemon=True)
        stdin_thread.start()
        while True:
            message = ws.recv()
            if isinstance(message, bytes):
                # Guard the write only when a stall would cost the terminal:
                # raw mode on, and a stdout that can stop being read. A tty
                # stdout only blocks under flow control the user asked for.
                guarded = tty_raw and not stdout_is_tty
                _write_all(stdout, message, stdout_stalled if guarded else None)
                continue
            code = _exit_code(message)
            if code is not None:
                exit_code = code
                break
    except ConnectionClosed:
        pass
    except WebSocketException as e:
        _die(f"terminal connection failed: {e}")
    except KeyboardInterrupt:
        # Reachable with a non-tty stdin, and once a stalled stdout has handed
        # the terminal back; while raw, ^C is a byte to the guest.
        exit_code = 130
    finally:
        closed.set()
        if stdin_wakeup_write is not None:
            with suppress(OSError):
                os.write(stdin_wakeup_write, b"\0")
        if stdin_thread is not None:
            stdin_thread.join()
        # No producer can enqueue after the sentinel. Uninstall SIGWINCH first
        # so a resize cannot land after it. Joining makes the sender the sole
        # websocket writer all the way through its final frame; close() writes
        # a close frame and must not overlap send().
        if winch_installed and sigwinch is not None:
            # getsignal() answers None for a handler that was installed from C
            # rather than from Python — readline and ncurses both do that —
            # and signal() refuses None. Raising here would strand the terminal
            # in raw mode with every line below this one unrun.
            with suppress(TypeError, ValueError, OSError):
                signal.signal(sigwinch, saved_winch)
            winch_installed = False
        # BEFORE anything below that can block. The reader was joined two lines
        # up, so it still cannot take a keystroke meant for the parent shell —
        # and everything after this point talks to the websocket, not the
        # terminal. Left below the sender join, a send the peer had stopped
        # reading held the terminal in raw mode for as long as the process
        # lived (adversarial review, OPL-4222).
        restore_tty()
        _stop_sender(outbound, stop_sender)
        if sender_thread is not None:
            sender_thread.join(_SENDER_JOIN_TIMEOUT)
            if sender_thread.is_alive():
                # Parked in send(). Nothing else will ever end it: close() is
                # below this join by design, and the thread is a daemon that
                # would hold the process open through interpreter shutdown.
                _abort(ws)
                sender_thread.join(_SENDER_JOIN_TIMEOUT)
        for fd in (stdin_wakeup_read, stdin_wakeup_write):
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
        for signum, previous in saved_exit_handlers.items():
            signal.signal(signum, previous)
        with suppress(ConnectionClosed, OSError, WebSocketException):
            ws.close()
    if exit_code is None:
        # The link dropped without the shell ending: the session is still
        # alive server-side, and saying so is what makes that a feature.
        print("mandala: detached — run the same command to reattach", file=sys.stderr)
        return 0
    return exit_code


# --- scp -------------------------------------------------------------------


def _remote_side(arg: str) -> tuple[str, str] | None:
    """``<computer>:<path>`` split apart, or ``None`` for a local path.

    scp's own rule: a colon marks the remote side unless a ``/`` comes before
    it, so ``./odd:name`` stays a local file.
    """
    head, sep, tail = arg.partition(":")
    if not sep or not head or "/" in head:
        return None
    if LOCAL_WINDOWS and len(head) == 1 and head.isascii() and head.isalpha():
        return None
    return head, tail


def _guest_basename(path: str) -> str:
    r"""The last component of a guest path, on the family the path is spelled in.

    Not :func:`os.path.basename`, which is the *local* machine's rule: on a
    POSIX host it does not know ``\`` is a separator, so a Windows guest's
    ``C:\Users\me\notes.txt`` comes back whole and lands in a local file
    named exactly that — one file with backslashes in its name, in the
    directory the user asked us to write into.

    But the Windows rules are just as wrong applied the other way: ``\`` and
    ``:`` are ordinary characters in a Linux filename, so reading every path as
    possibly-Windows would write ``/tmp/a:b.txt`` to a local file called
    ``b.txt`` and ``/tmp/back\slash.txt`` to one called ``slash.txt`` — the
    same silent wrong-filename outcome, on the far more common guest. So the
    path's own spelling picks the rule.
    """
    if not looks_windows_guest_path(path):
        return path.rstrip("/").rsplit("/", 1)[-1]
    tail = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    # `C:notes.txt` is drive-relative on Windows: the drive is not part of the
    # name, and the daemon's own path rules accept the spelling.
    if len(tail) > 2 and tail[1] == ":" and tail[0].isascii() and tail[0].isalpha():
        tail = tail[2:]
    return tail


def _cmd_scp(args: argparse.Namespace) -> int:
    src, dst = _remote_side(args.src), _remote_side(args.dst)
    if (src is None) == (dst is None):
        _die("exactly one side must be a computer, spelled <computer>:/path")

    if src is not None:
        target, remote_path = src
        if not remote_path:
            _die(f"say which file: {target}:/absolute/path")
        local = args.dst
        if os.path.isdir(local):
            basename = _guest_basename(remote_path)
            if not basename or basename in (".", "..") or os.path.basename(basename) != basename:
                _die(f"{target}:{remote_path} does not name a downloadable file")
            local = os.path.join(local, basename)
        # Paged rather than read whole: the ceiling is on what one request
        # moves, so a whole-file read makes anything past 64 MiB uncopyable, and
        # a file that big is exactly what somebody reaches for scp to move.
        # download_file writes as each window lands and does not open the local
        # file until the first one has, so a refused copy still leaves whatever
        # was there alone.
        with _client() as client:
            written = _resolve(client, target).download_file(remote_path, local)
        print(f"{target}:{remote_path} -> {local} ({written} bytes)", file=sys.stderr)
        return 0

    assert dst is not None
    target, remote_path = dst
    if not remote_path:
        _die(f"say where in the guest: {target}:/absolute/path")
    # The guest's separator, not this machine's: `win:C:\Users\me\` names a
    # directory just as `box:/tmp/` does, and appending to a path that already
    # ends in a separator joins with whichever one the caller wrote.
    if remote_path.endswith(("/", "\\")):
        remote_path += os.path.basename(args.src)
    with open(args.src, "rb") as f:
        if os.fstat(f.fileno()).st_size > FILE_SIZE_LIMIT:
            _die(f"{args.src} exceeds the 64 MiB file-transfer limit")
        # The bounded read also covers files that grow after fstat and special
        # files whose reported size is zero.
        data = f.read(FILE_SIZE_LIMIT + 1)
    if len(data) > FILE_SIZE_LIMIT:
        _die(f"{args.src} exceeds the 64 MiB file-transfer limit")
    with _client() as client:
        _resolve(client, target).write_file(remote_path, data)
    print(f"{args.src} -> {target}:{remote_path} ({len(data)} bytes)", file=sys.stderr)
    return 0


# --- entry -----------------------------------------------------------------


# --- webhooks --------------------------------------------------------------


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Columns padded to their widest cell, the last one unpadded.

    Two spaces between columns and no border, which is what every tool a
    person pipes into ``awk`` prints, and the last column left ragged so a long
    URL does not pad every other row out to its width.
    """
    table = [list(header), *[list(r) for r in rows]]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    lines = []
    for row in table:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row[:-1])]
        lines.append("  ".join([*cells, row[-1]]).rstrip())
    return "\n".join(lines)


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _webhook_rows(hooks: Sequence[Webhook]) -> str:
    rows = []
    for w in hooks:
        state = "on" if w.enabled else f"off ({w.disabled_reason or 'unknown'})"
        status = "-" if w.last_status is None else str(w.last_status)
        events = "*" if not w.events else ",".join(w.events)
        rows.append((w.id, state, status, events, w.url))
    return _table(("ID", "ENABLED", "LAST", "EVENTS", "URL"), rows)


def _delivery_rows(deliveries: Sequence[WebhookDelivery]) -> str:
    rows = []
    for d in deliveries:
        outcome = d.last_error or ("-" if d.last_status is None else f"status {d.last_status}")
        rows.append((d.id, d.state, str(d.attempts), d.event_type, d.computer or "-", outcome))
    return _table(("ID", "STATE", "TRIES", "EVENT", "COMPUTER", "OUTCOME"), rows)


def _secret_once(kind: str) -> None:
    print(
        f"mandala: the secret above is shown once — store it now. {kind} it is not "
        "readable again; `mandala webhooks rotate` mints another.",
        file=sys.stderr,
    )


def _cmd_webhooks_list(args: argparse.Namespace) -> int:
    with _client() as client:
        hooks = client.webhooks.list()
    if args.json:
        _json([w.raw for w in hooks])
    elif hooks:
        print(_webhook_rows(hooks))
    else:
        print("no webhooks", file=sys.stderr)
    return 0


def _cmd_webhooks_create(args: argparse.Namespace) -> int:
    with _client() as client:
        created = client.webhooks.create(
            args.url,
            description=args.description,
            events=args.event,
            computers=args.computer,
            enabled=False if args.disabled else None,
        )
    _json(created.raw)
    _secret_once("After")
    return 0


def _cmd_webhooks_get(args: argparse.Namespace) -> int:
    with _client() as client:
        _json(client.webhooks.get(args.id).raw)
    return 0


def _cmd_webhooks_update(args: argparse.Namespace) -> int:
    # `[]` is how a filter is CLEARED — "every type", "every computer in
    # scope" — and a repeatable flag cannot spell an empty list, so each has a
    # word for it. Both together is a contradiction and argparse refuses it.
    events = [] if args.all_events else args.event
    computers = [] if args.all_computers else args.computer
    enabled = True if args.enable else (False if args.disable else None)
    with _client() as client:
        updated = client.webhooks.update(
            args.id,
            url=args.url,
            description=args.description,
            events=events,
            computers=computers,
            enabled=enabled,
        )
    _json(updated.raw)
    return 0


def _cmd_webhooks_delete(args: argparse.Namespace) -> int:
    with _client() as client:
        client.webhooks.delete(args.id)
    print(f"deleted {args.id}")
    return 0


def _cmd_webhooks_rotate(args: argparse.Namespace) -> int:
    with _client() as client:
        rotated = client.webhooks.rotate(args.id)
    _json(rotated.raw)
    _secret_once("The old secret is honoured for 24 hours;")
    return 0


def _cmd_webhooks_test(args: argparse.Namespace) -> int:
    with _client() as client:
        delivery = client.webhooks.test(args.id)
    _json(delivery.raw)
    print(
        f"mandala: queued, not finished — `mandala webhooks deliveries {args.id}` says what "
        "the endpoint answered.",
        file=sys.stderr,
    )
    return 0


def _cmd_webhooks_deliveries(args: argparse.Namespace) -> int:
    with _client() as client:
        deliveries = client.webhooks.deliveries(args.id)
    if args.json:
        _json([d.raw for d in deliveries])
    elif deliveries:
        print(_delivery_rows(deliveries))
    else:
        print("no deliveries", file=sys.stderr)
    return 0


def _webhooks_parser(sub: Any) -> None:
    hooks = sub.add_parser("webhooks", help="the account's webhook subscriptions")
    verbs = hooks.add_subparsers(dest="verb", required=True)

    listing = verbs.add_parser("list", help="every subscription, with its health")
    listing.add_argument("--json", action="store_true", help="the rows as JSON")
    listing.set_defaults(fn=_cmd_webhooks_list)

    create = verbs.add_parser(
        "create", help="subscribe an https:// endpoint; prints the secret ONCE"
    )
    create.add_argument("url", metavar="URL", help="where to POST; https:// and a public address")
    create.add_argument("--description", help="free text for the listing")
    create.add_argument(
        "--event",
        action="append",
        metavar="TYPE",
        help="an event type to deliver; repeat for several. Omit for every type",
    )
    create.add_argument(
        "--computer",
        action="append",
        metavar="ID",
        help="a computer id to deliver for; repeat for several. Omit for every computer",
    )
    create.add_argument("--disabled", action="store_true", help="create it switched off")
    create.set_defaults(fn=_cmd_webhooks_create)

    get = verbs.add_parser("get", help="one subscription, with its health")
    get.add_argument("id", metavar="ID")
    get.set_defaults(fn=_cmd_webhooks_get)

    update = verbs.add_parser("update", help="change the endpoint, filters, or enabled")
    update.add_argument("id", metavar="ID")
    update.add_argument("--url", help="a new endpoint, checked as on create")
    update.add_argument("--description")
    events = update.add_mutually_exclusive_group()
    events.add_argument("--event", action="append", metavar="TYPE", help="replace the type filter")
    events.add_argument("--all-events", action="store_true", help="clear the type filter")
    computers = update.add_mutually_exclusive_group()
    computers.add_argument(
        "--computer", action="append", metavar="ID", help="replace the computer filter"
    )
    computers.add_argument("--all-computers", action="store_true", help="clear the computer filter")
    switch = update.add_mutually_exclusive_group()
    switch.add_argument("--enable", action="store_true", help="resume deliveries")
    switch.add_argument("--disable", action="store_true", help="stop deliveries")
    update.set_defaults(fn=_cmd_webhooks_update)

    delete = verbs.add_parser("delete", help="remove it, and every delivery record it holds")
    delete.add_argument("id", metavar="ID")
    delete.set_defaults(fn=_cmd_webhooks_delete)

    rotate = verbs.add_parser("rotate", help="mint a new secret; prints it ONCE")
    rotate.add_argument("id", metavar="ID")
    rotate.set_defaults(fn=_cmd_webhooks_rotate)

    test = verbs.add_parser("test", help="queue one signed delivery of a synthetic event")
    test.add_argument("id", metavar="ID")
    test.set_defaults(fn=_cmd_webhooks_test)

    deliveries = verbs.add_parser("deliveries", help="the newest hundred deliveries, newest first")
    deliveries.add_argument("id", metavar="ID")
    deliveries.add_argument("--json", action="store_true", help="the rows as JSON")
    deliveries.set_defaults(fn=_cmd_webhooks_deliveries)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mandala",
        description="Your own terminal, against a Mandala computer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ssh = sub.add_parser("ssh", help="an interactive shell in the guest")
    ssh.add_argument("target", metavar="computer", help="computer name or id")
    ssh.add_argument(
        "-s",
        "--session",
        default="main",
        help="named session to attach; sessions persist across disconnects (default: main)",
    )
    ssh.set_defaults(fn=_cmd_ssh)

    scp = sub.add_parser("scp", help="copy one file in or out of the guest")
    scp.add_argument("src", metavar="SRC", help="local path, or <computer>:/path")
    scp.add_argument("dst", metavar="DST", help="local path, or <computer>:/path")
    scp.set_defaults(fn=_cmd_scp)

    _webhooks_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (MandalaError, ValueError) as e:
        print(f"mandala: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"mandala: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
