"""The ``mandala`` command — a computer's shell and files from your own terminal.

Two subcommands, both addressing a computer by name or id:

``mandala ssh <computer>``
    An interactive shell in the guest, over the platform's terminal websocket —
    a PTY the platform keeps alive server-side. Disconnecting detaches the
    session rather than ending it; running the same command reattaches and
    replays recent output. ``--session`` names one of several.

``mandala scp <src> <dst>``
    Copy one file in or out, ``scp``-style: the side spelled
    ``<computer>:/path`` is the guest. Rides the files API, so it needs no
    shell in the guest at all.

Authentication is the SDK's: ``MANDALA_API_KEY`` (and optionally
``MANDALA_BASE_URL``) in the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import select
import shutil
import signal
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from types import FrameType
from typing import TYPE_CHECKING, Any, NoReturn

from ._api import looks_windows_guest_path
from ._client import FILE_SIZE_LIMIT
from ._computer import Computer
from ._exceptions import MandalaError

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


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte, however many calls that takes.

    ``os.write`` is allowed to write less than it was given, and here it will:
    frames run to :data:`_MAX_FRAME` on a scrollback replay, against a pipe
    whose buffer is a few kilobytes, and a SIGWINCH landing mid-write ends it
    early with a short count rather than being retried. The unwritten tail is
    guest output, and dropping it silently corrupts the terminal.
    """
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:
            raise OSError("write made no progress")
        view = view[written:]


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
    c = _resolve(_client(), args.target).refresh()
    vnc = c.vnc
    if vnc is None or not vnc.terminal_url:
        if c.os == "windows":
            _die(f"{c.name} is a Windows computer; terminals are Linux-only for now")
        if c.status not in ("running", "suspended"):
            _die(f"{c.name} is {c.status or 'not running'} — start it, then retry")
        _die(f"{c.name} has no terminal endpoint (server too old?)")
    url = vnc.terminal_url
    if args.session != "main":
        from urllib.parse import quote

        url += ("&" if "?" in url else "?") + "session=" + quote(args.session)
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
    except WebSocketException as e:
        _die(f"could not open the terminal: {e}")


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
    except (TypeError, ValueError):
        return 0


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
    closed = threading.Event()
    resize = object()
    stop_sender = object()
    outbound: queue.SimpleQueue[bytes | str | object] = queue.SimpleQueue()
    stdin_wakeup_read: int | None = None
    stdin_wakeup_write: int | None = None

    def send_size(*_sig: object) -> None:
        # A Python signal handler must not touch the socket, and websockets
        # forbids overlapping sends. The sender owns both the terminal query
        # and every outbound frame; this handler does nothing but enqueue.
        outbound.put(resize)

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
                outbound.put(data)

    def pump_outbound() -> None:
        # The socket closing under us is fine; the main loop reports it.
        with suppress(ConnectionClosed, OSError, WebSocketException):
            while not closed.is_set():
                message = outbound.get()
                if message is stop_sender:
                    return
                if message is resize:
                    cols, rows = shutil.get_terminal_size()
                    message = json.dumps({"type": "resize", "cols": cols, "rows": rows})
                assert isinstance(message, (bytes, str))
                ws.send(message)

    saved_tty = None
    saved_winch: Callable[[int, FrameType | None], Any] | int | signal.Handlers | None = (
        signal.SIG_DFL
    )
    winch_installed = False
    sigwinch: int | signal.Signals | None = getattr(signal, "SIGWINCH", None)
    saved_exit_handlers: dict[
        int, Callable[[int, FrameType | None], Any] | int | signal.Handlers
    ] = {}
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
        if sys.stdout.isatty():
            send_size()
            if sigwinch is not None:
                saved_winch = signal.getsignal(sigwinch)
                signal.signal(sigwinch, send_size)
                winch_installed = True

        stdin_wakeup_read, stdin_wakeup_write = os.pipe()
        threading.Thread(target=pump_outbound, daemon=True).start()
        stdin_thread = threading.Thread(target=pump_stdin, daemon=True)
        stdin_thread.start()
        while True:
            message = ws.recv()
            if isinstance(message, bytes):
                _write_all(stdout, message)
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
        # Only reachable with a non-tty stdin; raw mode sends ^C to the guest.
        exit_code = 130
    finally:
        closed.set()
        outbound.put(stop_sender)
        if stdin_wakeup_write is not None:
            with suppress(OSError):
                os.write(stdin_wakeup_write, b"\0")
        if stdin_thread is not None:
            stdin_thread.join()
        for fd in (stdin_wakeup_read, stdin_wakeup_write):
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
        # The reader is gone before cooked mode returns, so it cannot consume a
        # keystroke intended for the parent shell after this function exits.
        restore_tty()
        for signum, previous in saved_exit_handlers.items():
            signal.signal(signum, previous)
        if winch_installed and sigwinch is not None:
            signal.signal(sigwinch, saved_winch)
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
        data = _resolve(_client(), target).read_file(remote_path)
        local = args.dst
        if os.path.isdir(local):
            local = os.path.join(local, _guest_basename(remote_path))
        with open(local, "wb") as f:
            f.write(data)
        print(f"{target}:{remote_path} -> {local} ({len(data)} bytes)", file=sys.stderr)
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
    _resolve(_client(), target).write_file(remote_path, data)
    print(f"{args.src} -> {target}:{remote_path} ({len(data)} bytes)", file=sys.stderr)
    return 0


# --- entry -----------------------------------------------------------------


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
