"""The `mandala` CLI — everything except the live websocket.

The interactive pump needs a server on the other end and a TTY on this one, so
these tests cover what surrounds it: how arguments name a remote side, how a
name or id finds a computer, and that scp moves the right bytes through the
right routes. The HTTP layer is respx, same as the client tests.
"""

from __future__ import annotations

import threading

import httpx
import pytest
import respx

from mandala_computer import _cli

BASE = "https://api.test/api/v1"

COMPUTERS = [
    {"id": "vm-1", "name": "dev", "status": "running", "os": "linux"},
    {"id": "vm-2", "name": "scratch", "status": "stopped", "os": "linux"},
    {"id": "vm-3", "name": "scratch", "status": "running", "os": "linux"},
]


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


# --- remote-side parsing ---------------------------------------------------


@pytest.mark.parametrize(
    ("arg", "want"),
    [
        ("dev:/home/user/.env", ("dev", "/home/user/.env")),
        ("vm-1:/tmp/x", ("vm-1", "/tmp/x")),
        ("dev:", ("dev", "")),
        ("plain.txt", None),
        ("./odd:name", None),  # scp's rule: a slash before the colon is local
        ("/abs/path:with-colon", None),
        (":path", None),
    ],
)
def test_remote_side(arg: str, want: tuple[str, str] | None) -> None:
    assert _cli._remote_side(arg) == want


def test_a_local_windows_drive_is_not_a_computer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, "LOCAL_WINDOWS", True)
    assert _cli._remote_side(r"C:\work\notes.txt") is None
    assert _cli._remote_side("D:/work/notes.txt") is None


# --- resolution ------------------------------------------------------------


@respx.mock
def test_resolve_prefers_exact_id() -> None:
    route = respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    assert _cli._resolve(_cli._client(), "vm-2").id == "vm-2"
    assert route.calls.last.request.url.params["allow_partial"] == "1"


@respx.mock
def test_resolve_by_unique_name() -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    assert _cli._resolve(_cli._client(), "dev").id == "vm-1"


@respx.mock
def test_resolve_ambiguous_name_dies_with_ids() -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    with pytest.raises(SystemExit, match="vm-2, vm-3"):
        _cli._resolve(_cli._client(), "scratch")


@respx.mock
def test_resolve_unknown_name_lists_what_exists() -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    with pytest.raises(SystemExit, match="dev"):
        _cli._resolve(_cli._client(), "nope")


# --- scp -------------------------------------------------------------------


@respx.mock
def test_scp_upload_puts_bytes(tmp_path) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(return_value=httpx.Response(200))
    src = tmp_path / "secret.env"
    src.write_bytes(b"TOKEN=hunter2\n")

    assert _cli.main(["scp", str(src), "dev:/home/user/.env"]) == 0
    request = put.calls.last.request
    assert request.url.params["path"] == "/home/user/.env"
    assert request.content == b"TOKEN=hunter2\n"


@respx.mock
def test_scp_upload_to_directory_keeps_basename(tmp_path) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(return_value=httpx.Response(200))
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hi")

    assert _cli.main(["scp", str(src), "dev:/home/user/"]) == 0
    assert put.calls.last.request.url.params["path"] == "/home/user/notes.txt"


@respx.mock
def test_scp_download_writes_local_file(tmp_path) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        return_value=httpx.Response(200, content=b"report,1\n")
    )
    dst = tmp_path / "report.csv"

    assert _cli.main(["scp", "dev:/home/user/report.csv", str(dst)]) == 0
    assert dst.read_bytes() == b"report,1\n"


@respx.mock
def test_scp_download_into_directory_keeps_basename(tmp_path) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.get(f"{BASE}/computers/vm-1/files").mock(return_value=httpx.Response(200, content=b"x"))

    assert _cli.main(["scp", "dev:/home/user/report.csv", str(tmp_path)]) == 0
    assert (tmp_path / "report.csv").read_bytes() == b"x"


def test_scp_both_local_dies(tmp_path) -> None:
    with pytest.raises(SystemExit, match="exactly one side"):
        _cli.main(["scp", str(tmp_path / "a"), str(tmp_path / "b")])


def test_scp_both_remote_dies() -> None:
    with pytest.raises(SystemExit, match="exactly one side"):
        _cli.main(["scp", "dev:/a", "dev:/b"])


def test_scp_remote_needs_a_path() -> None:
    with pytest.raises(SystemExit, match="say where"):
        _cli.main(["scp", __file__, "dev:"])


@respx.mock
def test_scp_relative_guest_path_refused(tmp_path, capsys) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    src = tmp_path / "a.txt"
    src.write_bytes(b"x")
    assert _cli.main(["scp", str(src), "dev:relative/path"]) == 1
    assert "absolute" in capsys.readouterr().err


@respx.mock
def test_api_error_is_a_message_not_a_traceback(tmp_path, capsys) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        return_value=httpx.Response(404, json={"error": "no such file"})
    )

    assert _cli.main(["scp", "dev:/gone.txt", str(tmp_path / "out")]) == 1
    assert "no such file" in capsys.readouterr().err


# --- ssh preflight ---------------------------------------------------------


def _computer(payload: dict) -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(200, json=[{**payload, "id": "vm-9", "name": "dev"}])
    )
    respx.get(f"{BASE}/computers/vm-9").mock(
        return_value=httpx.Response(200, json={**payload, "id": "vm-9", "name": "dev"})
    )


@respx.mock
def test_ssh_windows_guest_dies_plainly() -> None:
    _computer({"status": "running", "os": "windows"})
    with pytest.raises(SystemExit, match="Windows"):
        _cli.main(["ssh", "dev"])


@respx.mock
def test_ssh_stopped_computer_says_start_it() -> None:
    _computer({"status": "stopped", "os": "linux"})
    with pytest.raises(SystemExit, match="start it"):
        _cli.main(["ssh", "dev"])


def test_ssh_on_a_local_windows_terminal_dies_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, "LOCAL_WINDOWS", True)
    monkeypatch.setattr(_cli, "_client", lambda: pytest.fail("must not make an API request"))
    with pytest.raises(SystemExit, match="Unix-like local terminal"):
        _cli.main(["ssh", "dev"])


# --- guest paths are not local paths ---------------------------------------


@pytest.mark.parametrize(
    ("remote", "want"),
    [
        ("/home/user/notes.txt", "notes.txt"),
        (r"C:\Users\me\notes.txt", "notes.txt"),
        (r"C:/Users/me/notes.txt", "notes.txt"),
        (r"\\share\team\notes.txt", "notes.txt"),
        (r"C:\notes.txt", "notes.txt"),
        ("C:notes.txt", "notes.txt"),  # drive-relative: the drive is not a name
        ("/trailing/slash/", "slash"),
        ("bare.txt", "bare.txt"),
        # A Linux path is read by Linux rules: `:` and `\` are ordinary
        # characters in a filename there, and the Windows reading renamed the
        # file on the way to disk.
        ("/tmp/a:b.txt", "a:b.txt"),
        (r"/tmp/back\slash.txt", r"back\slash.txt"),
    ],
)
def test_guest_basename_understands_both_families(remote: str, want: str) -> None:
    r"""os.path.basename is the *local* machine's rule.

    On a POSIX host it does not know `\` separates anything, so a Windows
    guest's path came back whole and scp wrote one local file literally named
    `C:\Users\me\notes.txt`.
    """
    assert _cli._guest_basename(remote) == want


@respx.mock
def test_scp_from_a_windows_guest_into_a_directory(tmp_path) -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(
            200, json=[{"id": "vm-9", "name": "win", "status": "running", "os": "windows"}]
        )
    )
    respx.get(f"{BASE}/computers/vm-9/files").mock(
        return_value=httpx.Response(200, content=b"payload")
    )
    assert _cli.main(["scp", r"win:C:\Users\me\notes.txt", str(tmp_path)]) == 0
    assert (tmp_path / "notes.txt").read_bytes() == b"payload"
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]


@respx.mock
def test_scp_into_a_windows_directory_appends_the_name(tmp_path) -> None:
    r"""`C:\Users\me\` names a directory the way `/tmp/` does.

    The trailing-separator rule was the local machine's: only `/` counted, so
    a guest directory spelled the Windows way had the file's name never
    appended, and the PUT went to a path ending in a separator.
    """
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(
            200, json=[{"id": "vm-9", "name": "win", "status": "running", "os": "windows"}]
        )
    )
    put = respx.put(f"{BASE}/computers/vm-9/files").mock(return_value=httpx.Response(200, json={}))
    src = tmp_path / "notes.txt"
    src.write_bytes(b"payload")
    assert _cli.main(["scp", str(src), r"win:C:\Users\me" + "\\"]) == 0
    assert put.calls.last.request.url.params["path"] == r"C:\Users\me\notes.txt"


# --- the terminal pump loses no output -------------------------------------


def test_write_all_drains_a_short_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """os.write may write less than it was given, and here it will.

    Frames run to 4 MiB on a scrollback replay against a pipe buffered in
    kilobytes, and a SIGWINCH landing mid-write ends it early with a short
    count. The unwritten tail is guest output; dropping it corrupts the
    terminal with nothing said.
    """
    written = bytearray()

    def short_write(fd: int, data: object) -> int:
        chunk = bytes(data)[:7]  # a stubborn pipe: seven bytes at a time
        written.extend(chunk)
        return len(chunk)

    monkeypatch.setattr(_cli.os, "write", short_write)
    payload = bytes(range(256)) * 400
    _cli._write_all(1, payload)
    assert bytes(written) == payload


def test_write_all_handles_an_empty_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(_cli.os, "write", lambda fd, data: calls.append(data) or len(data))
    _cli._write_all(1, b"")
    assert not calls


def test_write_all_refuses_a_zero_length_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="no progress"):
        _cli._write_all(1, b"guest output")


# --- control frames --------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "want"),
    [
        ('{"type": "exit", "code": 3}', 3),
        ('{"type": "exit", "code": 0}', 0),
        ('{"type": "exit"}', 0),
        ('{"type": "resize", "cols": 80}', None),
        ('{"type": "error", "message": "nope"}', None),
        ("not json at all", None),
        ("", None),
        # Valid JSON that is not an object. Each of these parses, and then has
        # no `.get` — an AttributeError main() does not catch, so one stray
        # frame ended an interactive session in a traceback.
        ("null", None),
        ("5", None),
        ('"hi"', None),
        ("[1, 2]", None),
        ("true", None),
        # An exit frame whose code is unreadable is still an exit.
        ('{"type": "exit", "code": [1]}', 0),
        ('{"type": "exit", "code": "abc"}', 0),
        ('{"type": "exit", "code": null}', 0),
        ('{"type": "exit", "code": "7"}', 7),
    ],
)
def test_exit_code_reads_only_an_exit_frame(frame: str, want: int | None) -> None:
    """Junk on this socket is tolerated, whatever shape it arrives in.

    The pump already skipped unparseable bytes; valid JSON that was not an
    object crashed instead. Tolerating one and dying on the other is not a
    distinction worth making about a peer we do not control.
    """
    assert _cli._exit_code(frame) == want


def test_an_exit_frame_ends_the_interaction_without_waiting_for_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFile:
        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return False

    class FakeConnection:
        def __init__(self) -> None:
            self.receives = 0
            self.closed = False

        def recv(self) -> str:
            self.receives += 1
            if self.receives > 1:
                raise AssertionError("waited for the websocket to close after exit")
            return '{"type": "exit", "code": 7}'

        def send(self, message: object) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    ws = FakeConnection()
    monkeypatch.setattr(_cli, "_connect", lambda url: ws)
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile())
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile())

    assert _cli._interact("wss://terminal.test") == 7
    assert ws.receives == 1
    assert ws.closed


def test_raw_mode_is_restored_when_resize_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios
    import tty

    class FakeFile:
        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return True

    class FakeConnection:
        def close(self) -> None:
            pass

    restored: list[object] = []
    saved = object()
    monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile())
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile())
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: saved)
    monkeypatch.setattr(tty, "setraw", lambda fd: None)
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, state: restored.append(state))
    monkeypatch.setattr(_cli.signal, "getsignal", lambda signum: object())
    monkeypatch.setattr(
        _cli.signal, "signal", lambda signum, handler: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    with pytest.raises(RuntimeError, match="boom"):
        _cli._interact("wss://terminal.test")
    assert restored == [saved]


def test_interaction_restores_the_previous_resize_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFile:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return self.tty

    class FakeConnection:
        def recv(self) -> str:
            return '{"type": "exit", "code": 0}'

        def send(self, message: object) -> None:
            pass

        def close(self) -> None:
            pass

    previous = _cli.signal.SIG_IGN
    installed: list[object] = []
    monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile(False))
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile(True))
    monkeypatch.setattr(_cli.signal, "getsignal", lambda signum: previous)
    monkeypatch.setattr(_cli.signal, "signal", lambda signum, handler: installed.append(handler))

    assert _cli._interact("wss://terminal.test") == 0
    assert installed[-1] is previous


def test_resize_and_stdin_frames_use_the_sender_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFile:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return self.tty

    handlers: dict[str, object] = {}

    class FakeConnection:
        def __init__(self) -> None:
            self.sent = 0
            self.sent_twice = threading.Event()
            self.sent_thrice = threading.Event()
            self.send_threads: list[threading.Thread] = []

        def recv(self) -> str:
            assert self.sent_twice.wait(1)
            handler = handlers["resize"]
            assert callable(handler)
            handler()
            assert self.sent_thrice.wait(1)
            return '{"type": "exit", "code": 0}'

        def send(self, message: object) -> None:
            self.send_threads.append(threading.current_thread())
            self.sent += 1
            if self.sent == 2:
                self.sent_twice.set()
            if self.sent == 3:
                self.sent_thrice.set()

        def close(self) -> None:
            pass

    ws = FakeConnection()
    monkeypatch.setattr(_cli, "_connect", lambda url: ws)
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile(False))
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile(True))
    reads = iter((b"guest input", b""))
    monkeypatch.setattr(_cli.os, "read", lambda fd, size: next(reads))
    monkeypatch.setattr(
        _cli.signal, "signal", lambda signum, handler: handlers.setdefault("resize", handler)
    )
    monkeypatch.setattr(_cli.shutil, "get_terminal_size", lambda: (80, 24))

    assert _cli._interact("wss://terminal.test") == 0
    assert ws.sent == 3
    assert all(thread is not threading.main_thread() for thread in ws.send_threads)


def test_non_status_websocket_failures_are_cli_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from websockets.exceptions import InvalidURI
    from websockets.sync import client as ws_client

    def invalid_uri(*args: object, **kwargs: object) -> None:
        raise InvalidURI("not-a-websocket", "scheme isn't ws or wss")

    monkeypatch.setattr(ws_client, "connect", invalid_uri)
    with pytest.raises(SystemExit, match="could not open the terminal"):
        _cli._connect("not-a-websocket")
