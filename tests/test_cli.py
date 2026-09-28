"""The `mandala` CLI — everything except the live websocket.

The interactive pump needs a server on the other end and a TTY on this one, so
these tests cover what surrounds it: how arguments name a remote side, how a
name or id finds a computer, and that scp moves the right bytes through the
right routes. The HTTP layer is respx, same as the client tests.
"""

from __future__ import annotations

import threading
from pathlib import Path

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


@respx.mock
def test_resolve_does_not_call_an_empty_partial_listing_an_empty_account() -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(200, json=[], headers={"X-GC-Incomplete": "0"})
    )
    with pytest.raises(SystemExit, match="incomplete fleet listing") as caught:
        _cli._resolve(_cli._client(), "missing")
    assert "account has no computers" not in str(caught.value)


# --- scp -------------------------------------------------------------------


@respx.mock
def test_scp_upload_puts_bytes(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(return_value=httpx.Response(200))
    client = _cli._client()
    closed = False
    original_close = client.close

    def close() -> None:
        nonlocal closed
        closed = True
        original_close()

    monkeypatch.setattr(client, "close", close)
    monkeypatch.setattr(_cli, "_client", lambda: client)
    src = tmp_path / "secret.env"
    src.write_bytes(b"TOKEN=hunter2\n")

    assert _cli.main(["scp", str(src), "dev:/home/user/.env"]) == 0
    request = put.calls.last.request
    assert request.url.params["path"] == "/home/user/.env"
    assert request.content == b"TOKEN=hunter2\n"
    assert closed


@respx.mock
def test_scp_upload_to_directory_keeps_basename(tmp_path) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(return_value=httpx.Response(200))
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hi")

    assert _cli.main(["scp", str(src), "dev:/home/user/"]) == 0
    assert put.calls.last.request.url.params["path"] == "/home/user/notes.txt"


@respx.mock
def test_scp_no_overwrite_sends_create_only(tmp_path) -> None:
    """``--no-overwrite`` sends ``overwrite=false``; without it nothing extra (OPL-4994)."""
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(return_value=httpx.Response(200))
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hi")

    assert _cli.main(["scp", str(src), "dev:/home/user/notes.txt"]) == 0
    assert "overwrite" not in put.calls.last.request.url.params
    assert _cli.main(["scp", "--no-overwrite", str(src), "dev:/home/user/notes.txt"]) == 0
    params = put.calls.last.request.url.params
    assert params["overwrite"] == "false" and params["path"] == "/home/user/notes.txt"


@respx.mock
def test_scp_no_overwrite_says_the_path_is_taken(tmp_path) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.put(f"{BASE}/computers/vm-1/files").mock(
        return_value=httpx.Response(409, json={"error": "already exists", "reason": "exists"})
    )
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hi")

    with pytest.raises(SystemExit) as caught:
        _cli.main(["scp", "--no-overwrite", str(src), "dev:/home/user/notes.txt"])
    message = str(caught.value)
    assert "dev:/home/user/notes.txt already exists" in message
    assert "this upload wrote nothing" in message
    # A retry after a lost answer meets its own file; only THIS attempt is known.
    assert "If an earlier attempt's outcome was unknown, the file may be yours" in message
    assert "read it and compare" in message and "--no-overwrite" in message


@pytest.mark.parametrize(
    "answer",
    [
        lambda: httpx.Response(409, json={"error": "a file already exists"}),
        lambda: httpx.Response(409, content=b""),
        lambda: httpx.Response(409, content=b"<html>conflict</html>"),
    ],
    ids=["reasonless-json", "empty", "proxy-page"],
)
@respx.mock
def test_scp_no_overwrite_never_says_taken_without_the_word(tmp_path, answer) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.put(f"{BASE}/computers/vm-1/files").mock(side_effect=lambda request: answer())
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hi")

    with pytest.raises(SystemExit) as caught:
        _cli.main(["scp", "--no-overwrite", str(src), "dev:/home/user/notes.txt"])
    message = str(caught.value)
    assert "refused as a conflict, reason unknown" in message
    assert "already exists" not in message
    # Even the JSON one: a reasonless 409 does not prove the write never landed.
    assert "wrote nothing" not in message
    assert "whether this upload wrote anything is unconfirmed" in message


def test_scp_no_overwrite_is_refused_on_a_download(tmp_path) -> None:
    with pytest.raises(SystemExit, match="applies to an upload, not a download"):
        _cli.main(["scp", "--no-overwrite", "dev:/home/user/report.csv", str(tmp_path / "r")])
    assert list(tmp_path.iterdir()) == []


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


@respx.mock
def test_scp_download_pages_a_file_larger_than_one_request(tmp_path) -> None:
    """The reason scp reads in windows at all.

    A whole-file read is refused past 64 MiB, and a file that size is exactly
    what somebody reaches for scp to move. Paging is what makes the copy
    possible.

    The guest here trims every window to four bytes whatever was asked for,
    which is what the platform does to anything past its ceiling — so this also
    pins the copy following the answers rather than its own arithmetic, without
    needing a 64 MiB fixture to reach the trim.
    """
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    body = b"0123456789"

    def handler(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Range"].removeprefix("bytes=").split("-")[0])
        window = body[first : first + 4]
        return httpx.Response(
            206,
            content=window,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes {first}-{first + len(window) - 1}/{len(body)}",
            },
        )

    route = respx.get(f"{BASE}/computers/vm-1/files").mock(side_effect=handler)
    dst = tmp_path / "big.bin"
    assert _cli.main(["scp", "dev:/home/user/big.bin", str(dst)]) == 0

    assert dst.read_bytes() == body
    assert route.call_count == 3
    assert [call.request.headers["Range"].split("-")[0] for call in route.calls] == [
        "bytes=0",
        "bytes=4",
        "bytes=8",
    ]


@respx.mock
def test_scp_download_reports_what_it_wrote(tmp_path, capsys) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        return_value=httpx.Response(200, content=b"report,1\n")
    )
    assert _cli.main(["scp", "dev:/home/user/report.csv", str(tmp_path / "r.csv")]) == 0
    assert "(9 bytes)" in capsys.readouterr().err


@respx.mock
def test_scp_download_that_is_refused_leaves_no_local_file(tmp_path) -> None:
    """It did not create one before, and paging must not start creating one.

    The whole-file read opened the destination only once it had the bytes. A
    download that truncated a file on its way to a 404 would be a regression
    dressed as an improvement.
    """
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        return_value=httpx.Response(404, json={"error": "no such file"})
    )
    dst = tmp_path / "out"

    assert _cli.main(["scp", "dev:/gone.txt", str(dst)]) == 1
    assert not dst.exists()


@pytest.mark.parametrize("remote_path", ["/tmp/.", "/tmp/..", "/"])
def test_scp_download_into_directory_rejects_a_non_file_basename(
    tmp_path, remote_path: str
) -> None:
    with pytest.raises(SystemExit, match="does not name a downloadable file"):
        _cli.main(["scp", f"dev:{remote_path}", str(tmp_path)])
    assert list(tmp_path.iterdir()) == []


def test_scp_both_local_dies(tmp_path) -> None:
    with pytest.raises(SystemExit, match="exactly one side"):
        _cli.main(["scp", str(tmp_path / "a"), str(tmp_path / "b")])


def test_scp_both_remote_dies() -> None:
    with pytest.raises(SystemExit, match="exactly one side"):
        _cli.main(["scp", "dev:/a", "dev:/b"])


def test_scp_remote_needs_a_path() -> None:
    with pytest.raises(SystemExit, match="say where"):
        _cli.main(["scp", __file__, "dev:"])


def test_scp_refuses_an_oversized_local_file_before_reading_it(tmp_path) -> None:
    src = tmp_path / "large.bin"
    with src.open("wb") as f:
        f.truncate(_cli.FILE_SIZE_LIMIT + 1)
    with pytest.raises(SystemExit, match="64 MiB"):
        _cli.main(["scp", str(src), "dev:/tmp/large.bin"])


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


@respx.mock
@pytest.mark.parametrize("argv", [["scp", "dev:/big.bin", "OUT"], ["webhooks", "list"]])
def test_ctrl_c_ends_a_command_with_a_message_rather_than_a_traceback(
    argv: list[str], tmp_path, capsys
) -> None:
    """Interrupting a transfer is a normal thing to do, not a crash.

    `_interact` has answered Ctrl-C with 130 on the `terminal` path since that path
    existed, so what the CLI owes a person pressing it was already settled;
    `scp` and every `webhooks` verb run outside that handler, and `main` caught
    `MandalaError`, `ValueError` and `OSError` — none of which a
    `KeyboardInterrupt` is — so they ended in a traceback and a 1 instead
    (adversarial review, OPL-4479). 130 is 128 plus SIGINT, which is what a
    calling script reads.
    """

    def interrupt(request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    # A callable rather than the class: respx refuses a side effect that is not
    # an `Exception`, and a `KeyboardInterrupt` is deliberately not one.
    respx.get(f"{BASE}/computers").mock(side_effect=interrupt)
    respx.get(f"{BASE}/webhooks").mock(side_effect=interrupt)

    # Caught here rather than left to propagate, because an interrupt reaching
    # pytest ends the whole session — the failure this pins would take the rest
    # of the suite with it and report as an abort rather than as itself.
    try:
        code = _cli.main([str(tmp_path / "out") if a == "OUT" else a for a in argv])
    except KeyboardInterrupt:
        pytest.fail(f"{argv[0]} let the interrupt through to a traceback")
    assert code == 130
    assert "interrupted" in capsys.readouterr().err


# --- terminal preflight ----------------------------------------------------


def _computer(payload: dict) -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(200, json=[{**payload, "id": "vm-9", "name": "dev"}])
    )
    respx.get(f"{BASE}/computers/vm-9").mock(
        return_value=httpx.Response(200, json={**payload, "id": "vm-9", "name": "dev"})
    )


_TERMINAL_COMPUTER = {
    "status": "running",
    "os": "linux",
    "vnc": {
        "url": "wss://control.test",
        "view_url": "wss://view.test",
        "token": "control",
        "view_token": "view",
        "embed_url": "https://embed.test",
        "terminal_url": "wss://terminal.test",
    },
}


@respx.mock
def test_terminal_windows_guest_dies_plainly() -> None:
    _computer({"status": "running", "os": "windows"})
    with pytest.raises(SystemExit, match="Windows"):
        _cli.main(["terminal", "dev"])


@respx.mock
def test_terminal_stopped_computer_says_start_it() -> None:
    _computer({"status": "stopped", "os": "linux"})
    with pytest.raises(SystemExit, match="start it"):
        _cli.main(["terminal", "dev"])


@respx.mock
def test_terminal_closes_the_api_client_before_interacting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _computer(_TERMINAL_COMPUTER)
    # Pinned rather than left to whatever pytest did with the streams: with
    # `-s` the real terminal is one, and the URL would carry its geometry.
    monkeypatch.setattr(_cli, "_terminal_fd", lambda: None)
    client = _cli._client()
    closed = False
    original_close = client.close

    def close() -> None:
        nonlocal closed
        closed = True
        original_close()

    monkeypatch.setattr(client, "close", close)
    monkeypatch.setattr(_cli, "_client", lambda: client)

    def interact(url: str) -> int:
        assert closed
        assert url == "wss://terminal.test"
        return 7

    monkeypatch.setattr(_cli, "_interact", interact)
    assert _cli.main(["terminal", "dev"]) == 7


@respx.mock
def test_terminal_sizes_the_session_from_the_terminal_even_with_stdout_piped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPL-4246: `mandala terminal dev | tee log` used to open the PTY at 80x24.

    The broker takes the PTY's initial geometry from `cols`/`rows` on the
    upgrade URL and only honours a `resize` frame afterwards, so the login
    prompt, the MOTD and any replayed scrollback are drawn at whatever the URL
    said. Nothing put them there, and the size the client would have measured
    came off stdout, which here is a pipe.
    """
    _computer(_TERMINAL_COMPUTER)
    monkeypatch.setattr(_cli, "_terminal_fd", lambda: 3)
    monkeypatch.setattr(_cli, "_terminal_size", lambda fd: (203, 51))
    seen: list[str] = []
    monkeypatch.setattr(_cli, "_interact", lambda url: seen.append(url) or 0)

    assert _cli.main(["terminal", "dev", "--session", "two"]) == 0
    assert seen == ["wss://terminal.test?session=two&cols=203&rows=51"]


@respx.mock
def test_terminal_without_a_terminal_anywhere_sizes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully non-interactive run has no window to report; the broker defaults."""
    _computer(_TERMINAL_COMPUTER)
    monkeypatch.setattr(_cli, "_terminal_fd", lambda: None)
    seen: list[str] = []
    monkeypatch.setattr(_cli, "_interact", lambda url: seen.append(url) or 0)

    assert _cli.main(["terminal", "dev"]) == 0
    assert seen == ["wss://terminal.test"]


# --- which fd is "the terminal" --------------------------------------------


class _Stream:
    def __init__(self, fd: int, tty: bool) -> None:
        self.fd = fd
        self.tty = tty

    def fileno(self) -> int:
        return self.fd

    def isatty(self) -> bool:
        return self.tty


def test_terminal_fd_prefers_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw mode is set from stdin, so stdin is the terminal being spoken to."""
    monkeypatch.setattr(_cli.sys, "stdin", _Stream(0, True))
    monkeypatch.setattr(_cli.sys, "stdout", _Stream(1, True))
    assert _cli._terminal_fd() == 0


def test_terminal_fd_falls_back_past_a_piped_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mandala terminal dev < script` still draws its output in a real window."""
    monkeypatch.setattr(_cli.sys, "stdin", _Stream(0, False))
    monkeypatch.setattr(_cli.sys, "stdout", _Stream(1, False))
    monkeypatch.setattr(_cli.sys, "stderr", _Stream(2, True))
    assert _cli._terminal_fd() == 2


def test_terminal_fd_is_none_when_nothing_is_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(_cli.sys, name, _Stream(-1, False))
    assert _cli._terminal_fd() is None


def test_terminal_fd_survives_a_stream_with_no_fileno(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytest's own captured stdout is one of these, and so is a pythonw stdin."""

    class Detached:
        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            raise ValueError("underlying buffer detached")

    monkeypatch.setattr(_cli.sys, "stdin", Detached())
    monkeypatch.setattr(_cli.sys, "stdout", None)
    monkeypatch.setattr(_cli.sys, "stderr", _Stream(2, True))
    assert _cli._terminal_fd() == 2


def test_terminal_size_measures_the_fd_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: not `sys.__stdout__`, which a pipe run does not own."""
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)
    sizes = {0: (203, 51), 1: (80, 24)}
    monkeypatch.setattr(_cli.os, "get_terminal_size", lambda fd: _cli.os.terminal_size(sizes[fd]))
    assert _cli._terminal_size(0) == (203, 51)


def test_terminal_size_falls_back_when_the_fd_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)

    def refuse(fd: int) -> object:
        raise OSError("not a terminal")

    monkeypatch.setattr(_cli.os, "get_terminal_size", refuse)
    assert _cli._terminal_size(7) == (80, 24)


@pytest.mark.parametrize(
    ("columns", "lines", "want"),
    [
        ("120", "40", (120, 40)),
        ("120", "", (120, 24)),  # each half falls back on its own, as shutil does
        ("nonsense", "40", (80, 40)),
        ("0", "0", (80, 24)),  # a zero is not a window
    ],
)
def test_terminal_size_honours_the_environment_override(
    monkeypatch: pytest.MonkeyPatch, columns: str, lines: str, want: tuple[int, int]
) -> None:
    """COLUMNS/LINES is how a user reports a size no ioctl can."""
    monkeypatch.setenv("COLUMNS", columns)
    monkeypatch.setenv("LINES", lines)

    def refuse(fd: int) -> object:
        raise OSError("not a terminal")

    monkeypatch.setattr(_cli.os, "get_terminal_size", refuse)
    assert _cli._terminal_size(7) == want


def test_terminal_on_a_local_windows_terminal_dies_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_cli, "LOCAL_WINDOWS", True)
    monkeypatch.setattr(_cli, "_client", lambda: pytest.fail("must not make an API request"))
    with pytest.raises(SystemExit, match="Unix-like local terminal"):
        _cli.main(["terminal", "dev"])


def test_top_level_help_lists_terminal_and_the_ssh_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        _cli.main(["--help"])
    assert caught.value.code == 0
    out = capsys.readouterr().out
    for command in ("terminal", "ssh", "ssh-key", "ssh-access", "ssh-config"):
        assert command in out


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


def test_an_unguarded_write_never_asks_select(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ordinary path stays one syscall per frame, not one per 4 KiB."""

    def refuse(fd: int, timeout: float) -> bool:
        raise AssertionError("select is only for a write that can strand a terminal")

    monkeypatch.setattr(_cli, "_writable", refuse)
    monkeypatch.setattr(_cli.os, "write", lambda fd, data: len(bytes(data)))
    _cli._write_all(1, b"guest output")


def test_a_stalled_write_calls_back_once_and_still_delivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback is a hand-back, not a give-up: no byte of output is lost."""
    answers = iter([False])
    monkeypatch.setattr(_cli, "_writable", lambda fd, timeout: next(answers, True))
    written = bytearray()
    monkeypatch.setattr(
        _cli.os, "write", lambda fd, data: written.extend(bytes(data)) or len(bytes(data))
    )
    stalls = []
    payload = b"guest output" * 1000
    _cli._write_all(1, payload, lambda: stalls.append(1))

    assert len(stalls) == 1
    assert bytes(written) == payload


def test_a_full_pipe_stalls_before_the_write_finishes() -> None:
    """OPL-4246, reproduced: a consumer that stops reading blocks the pump.

    Filling a real pipe is the whole bug — `_write_all` was an unbounded
    `os.write` loop on the recv path, so `mandala terminal dev | head -c 1` parked
    it here for good. What the guard changes is only the terminal: the write
    is still in progress when the callback runs, and it still completes once
    somebody drains the pipe.
    """
    original = _cli._STDOUT_STALL_GRACE
    _cli._STDOUT_STALL_GRACE = 0.05
    read_fd, write_fd = _cli.os.pipe()
    try:
        stalled = threading.Event()
        finished = threading.Event()
        payload = b"x" * (1 << 20)  # comfortably past any pipe buffer

        def pump() -> None:
            _cli._write_all(write_fd, payload, stalled.set)
            finished.set()

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        assert stalled.wait(5)
        assert not finished.is_set()  # still mid-write: nothing was dropped

        drained = 0
        while drained < len(payload):
            drained += len(_cli.os.read(read_fd, 1 << 16))
        assert finished.wait(5)
        thread.join(5)
    finally:
        _cli._STDOUT_STALL_GRACE = original
        _cli.os.close(read_fd)
        _cli.os.close(write_fd)


# --- control frames --------------------------------------------------------


@pytest.mark.parametrize(
    ("frame", "want"),
    [
        ('{"type": "exit", "code": 3}', 3),
        ('{"type": "exit", "code": 0}', 0),
        ('{"type": "exit"}', _cli.EXIT_STATUS_UNKNOWN),
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
        # An exit frame whose code is unreadable is still an exit — but its
        # status is UNKNOWN, and 0 is the one answer that would claim success.
        ('{"type": "exit", "code": [1]}', _cli.EXIT_STATUS_UNKNOWN),
        ('{"type": "exit", "code": "abc"}', _cli.EXIT_STATUS_UNKNOWN),
        ('{"type": "exit", "code": null}', _cli.EXIT_STATUS_UNKNOWN),
        ('{"type": "exit", "code": true}', _cli.EXIT_STATUS_UNKNOWN),
        ('{"type": "exit", "code": 3.9}', _cli.EXIT_STATUS_UNKNOWN),
        # A decimal string crosses a process boundary the way a pid does.
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

    def install(signum: object, handler: object) -> None:
        if signum == _cli.signal.SIGWINCH:
            raise RuntimeError("boom")

    monkeypatch.setattr(_cli.signal, "signal", install)

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
        _cli.select, "select", lambda readers, writers, errors: ([readers[0]], [], [])
    )
    monkeypatch.setattr(
        _cli.signal, "signal", lambda signum, handler: handlers.setdefault("resize", handler)
    )
    monkeypatch.setattr(_cli, "_terminal_size", lambda fd: (80, 24))

    assert _cli._interact("wss://terminal.test") == 0
    assert ws.sent == 3
    assert all(thread is not threading.main_thread() for thread in ws.send_threads)


def test_a_piped_stdout_still_sizes_the_guest_pty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPL-4246: the resize used to be gated on stdout being a TTY.

    Raw mode is set from stdin, so `mandala terminal dev | tee log` is an
    interactive session by every measure the terminal cares about — and it was
    the one session that never sent a resize at all.
    """
    import termios
    import tty

    class FakeFile:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return self.tty

    class FakeConnection:
        def __init__(self) -> None:
            self.sent: list[object] = []

        def recv(self) -> str:
            return '{"type": "exit", "code": 0}'

        def send(self, message: object) -> None:
            self.sent.append(message)

        def close(self) -> None:
            pass

    ws = FakeConnection()
    monkeypatch.setattr(_cli, "_connect", lambda url: ws)
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile(True))
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile(False))
    monkeypatch.setattr(_cli.sys, "stderr", FakeFile(False))
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: object())
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, state: None)
    monkeypatch.setattr(tty, "setraw", lambda fd: None)
    monkeypatch.setattr(_cli, "_terminal_size", lambda fd: (203, 51))
    monkeypatch.setattr(_cli.os, "read", lambda fd, size: b"")

    assert _cli._interact("wss://terminal.test") == 0
    assert ws.sent == ['{"type": "resize", "cols": 203, "rows": 51}']


def test_a_wedged_stdout_does_not_keep_the_local_terminal_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPL-4246: the recv path blocked in `os.write` with the tty still raw.

    `ISIG` is off in raw mode, so Ctrl-C was a byte to a pipe nobody was
    reading and Ctrl-\\ the same; the `finally` that puts the terminal back
    was never reached, and recovery meant a `kill` from another window. The
    terminal now comes back while the write is still outstanding — checked
    here by the count of restores standing at the *next* recv, not merely by
    the end of the session.
    """
    import termios
    import tty

    class FakeFile:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return self.tty

    saved = object()
    restored: list[object] = []
    restores_seen: list[int] = []
    frames = iter([b"guest output", '{"type": "exit", "code": 0}'])

    class FakeConnection:
        def recv(self) -> bytes | str:
            restores_seen.append(len(restored))
            return next(frames)

        def send(self, message: object) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile(True))
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile(False))
    monkeypatch.setattr(_cli.sys, "stderr", FakeFile(False))
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: saved)
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, state: restored.append(state))
    monkeypatch.setattr(tty, "setraw", lambda fd: None)
    monkeypatch.setattr(_cli, "_writable", lambda fd, timeout: False)
    monkeypatch.setattr(_cli, "_terminal_size", lambda fd: (80, 24))
    monkeypatch.setattr(_cli.os, "read", lambda fd, size: b"")
    monkeypatch.setattr(_cli.os, "write", lambda fd, data: len(bytes(data)))

    assert _cli._interact("wss://terminal.test") == 0
    assert restored == [saved]
    assert restores_seen == [0, 1]


def test_a_tty_stdout_is_written_unguarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal only stops taking bytes under flow control the user asked for.

    Guarding it would buy nothing and cost a `select` and a chunked write per
    frame on the path every interactive session takes.
    """
    import termios
    import tty

    class FakeFile:
        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return True

    frames = iter([b"guest output", '{"type": "exit", "code": 0}'])

    class FakeConnection:
        def recv(self) -> bytes | str:
            return next(frames)

        def send(self, message: object) -> None:
            pass

        def close(self) -> None:
            pass

    def refuse(fd: int, timeout: float) -> bool:
        raise AssertionError("a terminal stdout needs no stall guard")

    monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile())
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile())
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: object())
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, state: None)
    monkeypatch.setattr(tty, "setraw", lambda fd: None)
    monkeypatch.setattr(_cli, "_writable", refuse)
    monkeypatch.setattr(_cli, "_terminal_size", lambda fd: (80, 24))
    monkeypatch.setattr(_cli.os, "read", lambda fd, size: b"")
    monkeypatch.setattr(_cli.os, "write", lambda fd, data: len(bytes(data)))

    assert _cli._interact("wss://terminal.test") == 0


def test_stdin_reader_is_woken_and_joined_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeFile:
        def __init__(self, fd: int) -> None:
            self.fd = fd

        def fileno(self) -> int:
            return self.fd

        def isatty(self) -> bool:
            return False

    entered_select = threading.Event()
    real_select = _cli.select.select

    def watched_select(readers: object, writers: object, errors: object):
        entered_select.set()
        return real_select(readers, writers, errors)

    class FakeConnection:
        def recv(self) -> str:
            assert entered_select.wait(1)
            return '{"type": "exit", "code": 0}'

        def send(self, message: object) -> None:
            pass

        def close(self) -> None:
            pass

    stdin_read, stdin_write = _cli.os.pipe()
    try:
        monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
        monkeypatch.setattr(_cli.sys, "stdin", FakeFile(stdin_read))
        monkeypatch.setattr(_cli.sys, "stdout", FakeFile(-1))
        monkeypatch.setattr(_cli.select, "select", watched_select)
        reads: list[int] = []
        monkeypatch.setattr(_cli.os, "read", lambda fd, size: reads.append(fd) or b"")

        assert _cli._interact("wss://terminal.test") == 0
        assert not reads
    finally:
        _cli.os.close(stdin_read)
        _cli.os.close(stdin_write)


def test_sender_is_joined_before_the_websocket_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeFile:
        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return False

    real_thread = threading.Thread
    tracked: dict[str, TrackedThread] = {}

    class TrackedThread:
        def __init__(self, *, target: object, daemon: bool) -> None:
            assert callable(target)
            self.target_name = target.__name__
            self.thread = real_thread(target=target, daemon=daemon)
            self.joined = False
            tracked[self.target_name] = self

        def start(self) -> None:
            self.thread.start()

        def join(self, timeout: float | None = None) -> None:
            self.joined = True
            self.thread.join(timeout)

        def is_alive(self) -> bool:
            return self.thread.is_alive()

    class FakeConnection:
        def recv(self) -> str:
            return '{"type": "exit", "code": 0}'

        def send(self, message: object) -> None:
            pass

        def close(self) -> None:
            assert tracked["pump_outbound"].joined

    monkeypatch.setattr(_cli.threading, "Thread", TrackedThread)
    monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile())
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile())

    assert _cli._interact("wss://terminal.test") == 0
    assert tracked["pump_stdin"].joined


def test_a_send_the_peer_stopped_reading_does_not_wedge_the_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`websockets`' sync ``send`` is ``sendall`` on a blocking socket, so a
    peer that stops reading blocks it until the peer comes back — which a peer
    that has just sent ``exit`` never does. Shutdown then did a blocking
    ``put`` of the stop sentinel and an unbounded ``join``, both before
    ``restore_tty()``, and ``close()`` sits after that join by design so a close
    frame cannot overlap a send. Nothing was left that could break the
    deadlock: the process hung with the terminal in raw mode, where Ctrl-C is a
    byte to a reader that has already exited (adversarial review, OPL-4222).
    """

    class FakeFile:
        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return False

    blocked = threading.Event()
    released = threading.Event()
    aborted: list[str] = []

    class FakeSocket:
        def shutdown(self, how: int) -> None:
            aborted.append("shutdown")
            released.set()

    class FakeConnection:
        socket = FakeSocket()

        def recv(self) -> str:
            assert blocked.wait(2), "the sender never reached send()"
            return '{"type": "exit", "code": 0}'

        def send(self, message: object) -> None:
            blocked.set()
            # Held until something aborts the socket, which is the only thing
            # that ends a real sendall() the peer has stopped reading.
            assert released.wait(5), "shutdown never aborted the socket"

        def close(self) -> None:
            pass

    monkeypatch.setattr(_cli, "_SENDER_JOIN_TIMEOUT", 0.1)
    monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile())
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile())
    reads = iter((b"paste", b""))
    monkeypatch.setattr(_cli.os, "read", lambda fd, size: next(reads))
    monkeypatch.setattr(
        _cli.select, "select", lambda readers, writers, errors: ([readers[0]], [], [])
    )

    done: list[int] = []
    worker = threading.Thread(target=lambda: done.append(_cli._interact("wss://x")), daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "_interact never returned"
    assert done == [0]
    assert aborted == ["shutdown"]


def test_the_stop_sentinel_gets_in_even_when_the_queue_is_full() -> None:
    """A blocking ``put`` here is a second way to hang: the queue is bounded,
    and what fills it is exactly the case shutdown runs for. Nothing will ever
    call ``get`` again, so the put that ends the sender waits on the sender."""
    outbound = _cli._outbound_queue()
    for i in range(_cli._OUTBOUND_QUEUE_MAX):
        outbound.put_nowait(b"x")
    assert outbound.full()
    sentinel = object()
    _cli._stop_sender(outbound, sentinel)
    # Room was made by dropping queued bytes, which cost nothing: `closed` is
    # set by then and the sender skips every message it takes after that.
    drained = [outbound.get_nowait() for _ in range(outbound.qsize())]
    assert sentinel in drained


def test_aborting_a_connection_without_a_socket_is_not_an_error() -> None:
    """`_abort` runs while something is already wrong and everything after it
    is cleanup, so a transport that does not expose a socket must not turn a
    hung shutdown into a raised one."""
    _cli._abort(object())


def test_terminating_signal_restores_raw_tty_before_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import termios
    import tty

    class FakeFile:
        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return self is _cli.sys.stdin

    installed: dict[int, object] = {}
    restored: list[object] = []
    saved = object()

    class FakeConnection:
        def recv(self) -> str:
            handler = installed[int(_cli.signal.SIGTERM)]
            assert callable(handler)
            handler(int(_cli.signal.SIGTERM), None)
            raise AssertionError("terminating signal returned")

        def send(self, message: object) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile())
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile())
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: saved)
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, state: restored.append(state))
    monkeypatch.setattr(tty, "setraw", lambda fd: None)
    monkeypatch.setattr(_cli.signal, "getsignal", lambda signum: _cli.signal.SIG_DFL)
    monkeypatch.setattr(
        _cli.signal, "signal", lambda signum, handler: installed.__setitem__(int(signum), handler)
    )

    def terminate(pid: int, signum: int) -> None:
        assert restored == [saved]
        raise SystemExit(128 + signum)

    monkeypatch.setattr(_cli.os, "kill", terminate)
    with pytest.raises(SystemExit):
        _cli._interact("wss://terminal.test")
    assert restored


def test_non_status_websocket_failures_are_cli_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from websockets.exceptions import InvalidURI
    from websockets.sync import client as ws_client

    def invalid_uri(*args: object, **kwargs: object) -> None:
        raise InvalidURI("not-a-websocket", "scheme isn't ws or wss")

    monkeypatch.setattr(ws_client, "connect", invalid_uri)
    with pytest.raises(SystemExit, match="could not open the terminal"):
        _cli._connect("not-a-websocket")


def test_an_invalid_terminal_url_does_not_print_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from websockets.exceptions import InvalidURI
    from websockets.sync import client as ws_client

    def invalid_uri(*args: object, **kwargs: object) -> None:
        raise InvalidURI("wss://h/term?token=SECRET", "scheme isn't ws or wss")

    monkeypatch.setattr(ws_client, "connect", invalid_uri)
    with pytest.raises(SystemExit) as caught:
        _cli._connect("wss://h/term?token=SECRET")
    message = str(caught.value)
    assert "could not open the terminal" in message
    assert "SECRET" not in message
    assert "token=" not in message


def test_outbound_queue_is_bounded() -> None:
    """A stalled websocket must not grow without bound on piped stdin."""
    q = _cli._outbound_queue()
    assert q.maxsize == _cli._OUTBOUND_QUEUE_MAX
    assert q.maxsize > 0


def test_queued_input_is_discarded_once_the_terminal_has_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drain to the sentinel after close, but do not send the leftover frames.

    A peer that sent exit and then stopped reading would stall send() for the
    whole backlog; skipping those writes is what makes join() return.
    """

    class FakeFile:
        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return False

    first_send = threading.Event()
    release_send = threading.Event()
    queued = threading.Event()
    sent: list[object] = []
    outbound = _cli._outbound_queue()
    original_put = outbound.put

    def put(item: object, block: bool = True, timeout: float | None = None) -> None:
        if not isinstance(item, (bytes, str)):
            release_send.set()
        original_put(item, block=block, timeout=timeout)
        if outbound.qsize() >= 2:
            queued.set()

    outbound.put = put  # type: ignore[method-assign]

    class FakeConnection:
        def recv(self) -> str:
            assert first_send.wait(1)
            assert queued.wait(1)
            return '{"type": "exit", "code": 0}'

        def send(self, message: object) -> None:
            if not release_send.is_set():
                first_send.set()
                assert release_send.wait(1)
            sent.append(message)

        def close(self) -> None:
            pass

    monkeypatch.setattr(_cli, "_outbound_queue", lambda: outbound)
    monkeypatch.setattr(_cli, "_connect", lambda url: FakeConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile())
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile())
    reads = iter((b"one", b"two", b"three", b""))
    monkeypatch.setattr(_cli.os, "read", lambda fd, size: next(reads))
    monkeypatch.setattr(
        _cli.select, "select", lambda readers, writers, errors: ([readers[0]], [], [])
    )

    assert _cli._interact("wss://terminal.test") == 0
    assert sent == [b"one"]


def test_a_detach_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """A link that drops before the exit frame leaves the status unknown.

    The session really is still alive server-side and reattaching really does
    work — that part is a feature and still says so on stderr. What it is not is
    evidence that the command succeeded. The daemon sends its exit frame
    precisely "so a client can tell 'your command exited' from a dropped
    network", and answering 0 here throws away the distinction it drew: a
    script cannot reattach, and
    a wrapper gating a deploy on this status would ship on a build whose end
    nobody saw (OPL-4479 BUG-29).
    """
    from websockets.exceptions import ConnectionClosed

    class FakeFile:
        def fileno(self) -> int:
            return -1

        def isatty(self) -> bool:
            return False

    class DroppedConnection:
        def recv(self) -> str:
            raise ConnectionClosed(None, None)

        def send(self, message: object) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(_cli, "_connect", lambda url: DroppedConnection())
    monkeypatch.setattr(_cli.sys, "stdin", FakeFile())
    monkeypatch.setattr(_cli.sys, "stdout", FakeFile())
    monkeypatch.setattr(_cli.signal, "getsignal", lambda signum: _cli.signal.SIG_IGN)
    monkeypatch.setattr(_cli.signal, "signal", lambda signum, handler: None)

    assert _cli._interact("wss://terminal.test") == _cli.EXIT_STATUS_UNKNOWN
    # The notice has to name the status, or a user sees what reads as a benign
    # message and then an unexplained 255 from their shell.
    err = capsys.readouterr().err
    assert "detached" in err and str(_cli.EXIT_STATUS_UNKNOWN) in err


def test_an_unknown_status_is_not_reported_as_success(capsys) -> None:
    """`mandala terminal cmd && next` must not run `next` on a status nobody read.

    0 is the one value that claims the command succeeded, so it is the one
    value an unreadable frame must not produce. 255 is what ssh answers when it
    cannot report a remote status, which keeps a wrapper already written
    against ssh correct, and the reason goes to stderr so the operator is not
    left guessing why a green command reported a failure (OPL-4479 BUG-29).
    """
    said: list[str] = []
    assert _cli._exit_code('{"type":"exit","code":"nope"}', said.append) == 255
    assert "unreadable status" in said[-1]

    assert _cli._exit_code('{"type":"exit"}', said.append) == 255
    assert "without reporting a status" in said[-1]

    # A wait status is an unsigned byte, and `SystemExit(256)` exits 0 — so an
    # out-of-range status would report a failure as a success through the very
    # door this closes. Go answers -1 for a process killed by a signal.
    assert _cli._exit_code('{"type":"exit","code":256}', said.append) == 255
    assert "out-of-range" in said[-1]
    assert _cli._exit_code('{"type":"exit","code":-1}', said.append) == 255
    assert _cli._exit_code('{"type":"exit","code":255}', said.append) == 255

    # A real status still passes through, 0 included, and says nothing.
    before = len(said)
    assert _cli._exit_code('{"type":"exit","code":0}', said.append) == 0
    assert _cli._exit_code('{"type":"exit","code":3}', said.append) == 3
    assert len(said) == before

    # The reason is handed back rather than PRINTED: the pump reads this while
    # the terminal is raw, where `tty.setraw` has cleared OPOST and a bare
    # newline staircases the message down the screen. Pinned, because
    # reintroducing the print is the regression and it would not fail anything
    # else in this suite.
    assert _cli._exit_code('{"type":"exit"}') == 255
    # BOTH streams: a reintroduced `print` defaults to stdout, which staircases
    # in raw mode exactly as stderr does, and `readouterr()` drains the pair.
    assert capsys.readouterr() == ("", "")


def test_an_unreadable_exit_code_does_not_end_the_session_in_a_traceback() -> None:
    """The fifth `int()` site, missed by the sweep that fixed the other four.

    `json.loads("1e309")` is `inf`, and `int(inf)` raises `OverflowError` —
    neither of the two this caught. It escapes the pump's own handlers and
    `main()`, so one stray control frame ends an interactive terminal in a
    traceback, which is what this function's docstring says it exists to
    prevent: "no code to read is not a reason to invent a failure"
    (/code-review, OPL-4232).
    """
    assert _cli._exit_code('{"type":"exit","code":1e309}') == _cli.EXIT_STATUS_UNKNOWN
    # The frame's arrival is the news that the session ENDED. It is not news
    # that the command SUCCEEDED, and 0 is the one value that claims it.
    assert _cli._exit_code('{"type":"exit"}') == _cli.EXIT_STATUS_UNKNOWN
    assert _cli._exit_code('{"type":"exit","code":3}') == 3
    assert _cli._exit_code('{"type":"exit","code":0}') == 0
    # And a frame that is not an exit still says nothing.
    assert _cli._exit_code('{"type":"resize","cols":80}') is None


@respx.mock
@pytest.mark.parametrize("remote_path", ["/tmp/filename\\", "/tmp/a:b\\", "/tmp/back\\slash\\"])
def test_scp_upload_preserves_linux_filename_ending_in_backslash(tmp_path, remote_path) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=COMPUTERS))
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(return_value=httpx.Response(200))
    src = tmp_path / "notes.txt"
    src.write_bytes(b"payload")

    assert _cli.main(["scp", str(src), f"dev:{remote_path}"]) == 0
    assert put.calls.last.request.url.params["path"] == remote_path
    assert put.calls.last.request.content == b"payload"


@pytest.mark.parametrize("store", ["missing", "corrupt", "unsafe"])
@pytest.mark.parametrize(
    "argv", [["--help"], ["terminal", "--help"], ["scp", "--help"], ["webhooks", "--help"]]
)
def test_O01_offline(
    store: str,
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.test_credentials import observe_store_io, write_store

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MANDALA_API_KEY")
    monkeypatch.delenv("MANDALA_BASE_URL")
    monkeypatch.setenv("MANDALA_PROFILE", "../invalid")
    if store != "missing":
        path = write_store(tmp_path, payload=b"{broken")
        if store == "unsafe":
            path.chmod(0o644)
    with monkeypatch.context() as observation:
        observed = observe_store_io(observation, forbid_home=True)
        with pytest.raises(SystemExit) as caught:
            _cli.main(argv)
        assert caught.value.code == 0
        # argparse checks gettext catalogs while rendering help; those are
        # unrelated to credential IO. Every observed stat must be such a file.
        for call in observed["stat"].call_args_list:
            catalog = Path(call.args[0])
            assert "locale" in catalog.parts and catalog.suffix == ".mo"
        assert not any(spy.called for name, spy in observed.items() if name != "stat")
    assert "usage:" in capsys.readouterr().out


@respx.mock
@pytest.mark.parametrize("verb", ["webhooks", "terminal", "scp"])
@pytest.mark.parametrize("profile", [None, "Work"])
def test_O02_profile_cli_paths(
    verb: str,
    profile: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.test_credentials import BASE_DOCUMENT, KEYS, write_store

    monkeypatch.delenv("MANDALA_API_KEY")
    monkeypatch.delenv("MANDALA_BASE_URL")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MANDALA_PROFILE", raising=False)
    if profile is not None:
        monkeypatch.setenv("MANDALA_PROFILE", profile)
    write_store(tmp_path)
    entry = BASE_DOCUMENT["profiles"][profile or "default"]
    base = entry["base_url"]
    if verb == "webhooks":
        respx.get(base + "/webhooks").mock(httpx.Response(200, json=[]))
        argv = ["webhooks", "list", "--json"]
    else:
        respx.get(base + "/computers").mock(httpx.Response(200, json=COMPUTERS))
        if verb == "scp":
            respx.get(base + "/computers/vm-1/files").mock(httpx.Response(200, content=b"fixture"))
            argv = ["scp", "dev:/fixture.txt", str(tmp_path / "download.txt")]
        else:
            respx.get(base + "/computers/vm-1").mock(httpx.Response(200, json=_TERMINAL_COMPUTER))
            monkeypatch.setattr(_cli, "_terminal_fd", lambda: None)
            monkeypatch.setattr(_cli, "_interact", lambda url: 0)
            argv = ["terminal", "dev"]
    assert _cli.main(argv) == 0
    assert len(respx.calls) >= 1
    for call in respx.calls:
        assert call.request.headers["Authorization"] == "Bearer " + entry["api_key"]
        assert str(call.request.url).startswith(base + "/")
    output = capsys.readouterr()
    assert all(key not in output.out + output.err for key in KEYS)


# --- names with control characters (platform OPL-5264) ---------------------
#
# The platform now refuses a key name holding a control character, but a key
# minted before it did keeps the name it was given, and a workspace, person or
# account name reaches these printers too. Each is printed escaped, so a
# newline cannot forge a row and an escape sequence cannot drive the terminal.

HOSTILE_KEY = {
    "id": "key-a1b2c3d4e5f6",
    "name": "ci\x1b[2Jevil\nforged",
    "prefix": "com_1a2b3c4d…",
    "created_at": "2026-09-20T08:00:00.000Z",
    "last_used_at": None,
    "workspace_id": "wsp-1",
    "workspace_name": "tenant\r x",
    "manage_keys": False,
}


# What the TypeScript CLI escapes (its TERMINAL_UNSAFE: C0, DEL, C1 and the
# bidi marks, embeddings, overrides and isolates), as inclusive ranges.
TS_TERMINAL_UNSAFE = [
    (0x00, 0x1F),
    (0x7F, 0x9F),
    (0x061C, 0x061C),
    (0x200E, 0x200F),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
]
# What this CLI escapes: the same, plus the two Unicode line separators.
ESCAPED = [*TS_TERMINAL_UNSAFE, (0x2028, 0x2029)]


def _escaped(c: str) -> bool:
    return any(lo <= ord(c) <= hi for lo, hi in ESCAPED)


def _raw_controls(text: str) -> list[str]:
    """The characters ``_printable`` escapes in ``text``, the line breaks between lines aside."""
    return [c for line in text.split("\n") for c in line if _escaped(c)]


@pytest.mark.parametrize(
    ("given", "want"),
    [
        ("ci\x1b[2Jevil\nforged", "ci\\u001b[2Jevil\\u000aforged"),
        ("x\x7fy\x85z\x9b", "x\\u007fy\\u0085z\\u009b"),
        ("a b c", "a\\u2028b\\u2029c"),
        ("CI – déploiement ✓ \\u001b", "CI – déploiement ✓ \\u001b"),
        ("a\u061cb\u200ec\u200fd", "a\\u061cb\\u200ec\\u200fd"),
        ("ci\u202eevil\u2066x", "ci\\u202eevil\\u2066x"),
        ("東京 🙂 ñandú עברית العربية", "東京 🙂 ñandú עברית العربية"),
    ],
)
def test_printable_escapes_control_characters_and_nothing_else(given: str, want: str) -> None:
    assert _cli._printable(given) == want


def test_printable_escapes_the_typescript_set_and_the_line_separators() -> None:
    # Pins the set: both ends and the middle of each range are escaped as
    # \uXXXX (four lowercase digits, as the TypeScript CLI spells them), and
    # the character just outside each end is left alone, so a drift either
    # way shows up here.
    for lo, hi in ESCAPED:
        for cp in {lo, (lo + hi) // 2, hi}:
            assert _cli._printable(chr(cp)) == f"\\u{cp:04x}", hex(cp)
        for cp in (lo - 1, hi + 1):
            if cp >= 0 and not _escaped(chr(cp)):
                assert _cli._printable(chr(cp)) == chr(cp), hex(cp)
    # The neighbours by name, so the loop above cannot pass by skipping them.
    for cp in (0x20, 0xA0, 0x061B, 0x061D, 0x200D, 0x2010, 0x2027, 0x202F, 0x2065, 0x206A):
        assert _cli._printable(chr(cp)) == chr(cp), hex(cp)
    # Escaping twice changes nothing: an escape holds no escaped character.
    every = "".join(chr(cp) for lo, hi in ESCAPED for cp in range(lo, hi + 1))
    once = _cli._printable(every)
    assert _raw_controls(once) == [] and _cli._printable(once) == once


def test_changelog_shows_the_escapes_the_cli_prints() -> None:
    # The release notes quote the escapes; a raw U+2028 there renders as an
    # empty code span (or a line break) instead of the text the CLI prints.
    changelog = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    assert _raw_controls(text) == []
    for given in ("\n", "\x1b", " "):
        assert f"`{_cli._printable(given)}`" in text


@respx.mock
def test_api_keys_list_prints_a_hostile_name_on_its_own_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.get(f"{BASE}/api-keys").mock(return_value=httpx.Response(200, json=[HOSTILE_KEY]))
    assert _cli.main(["api-keys", "list"]) == 0
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 2  # the header and the one key
    assert _raw_controls(out) == []
    assert "ci\\u001b[2Jevil\\u000aforged" in out
    assert "workspace tenant\\u000d\\u2028x (wsp-1)" in out


@respx.mock
def test_api_keys_create_escapes_the_name_it_reports(capsys: pytest.CaptureFixture[str]) -> None:
    created = {**HOSTILE_KEY, "raw": "com_" + "ab" * 24}
    respx.post(f"{BASE}/api-keys").mock(return_value=httpx.Response(201, json=created))
    assert _cli.main(["api-keys", "create"]) == 0
    out, err = capsys.readouterr()
    assert out == created["raw"] + "\n"
    assert _raw_controls(err) == [] and err.count("\n") == 1
    assert "(ci\\u001b[2Jevil\\u000aforged, workspace tenant\\u000d\\u2028x (wsp-1))" in err


@respx.mock
def test_whoami_escapes_every_name(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/whoami").mock(
        return_value=httpx.Response(
            200,
            json={
                "user": {"id": "usr-1", "email": "dana\n@example.com", "name": "Dana\x1b]0;x\x07"},
                "account": {"id": "acc-1", "name": "Acme\x9b", "plan": "team", "status": "active"},
                "role": "owner",
                "workspace": {
                    "id": "wsp-1",
                    "name": "ws ",
                    "created_at": "2026-09-20T08:00:00.000Z",
                },
                "key": HOSTILE_KEY,
            },
        )
    )
    assert _cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert _raw_controls(out) == []
    assert out.splitlines() == [
        "Dana\\u001b]0;x\\u0007 <dana\\u000a@example.com> (usr-1)",
        "Account: Acme\\u009b (acc-1), plan team, active",
        "Role: owner",
        "Scope: workspace ws\\u2029 (wsp-1)",
        "Key: ci\\u001b[2Jevil\\u000aforged (key-a1b2c3d4e5f6, com_1a2b3c4d…); cannot manage API keys",
    ]


# Every whoami field comes from the platform, not only the names a person
# chose: an id, a status or a role holding a control character is escaped too.
HOSTILE_IDS = {
    "user": {"id": "usr-\x1b[2J\nforged", "email": "dana@example.com", "name": "Dana"},
    "account": {"id": "acc-\u202e1", "name": "Acme", "plan": "team", "status": "act\x1bive"},
    "role": "own\ner",
    "workspace": None,
    "key": {**HOSTILE_KEY, "id": "key-\x07\u200f1", "name": "ci", "workspace_id": None},
}


@respx.mock
def test_whoami_escapes_the_platforms_fields(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=HOSTILE_IDS))
    assert _cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert _raw_controls(out) == []
    assert out.splitlines() == [
        "Dana <dana@example.com> (usr-\\u001b[2J\\u000aforged)",
        "Account: Acme (acc-\\u202e1), plan team, act\\u001bive",
        "Role: own\\u000aer",
        "Scope: the whole account",
        "Key: ci (key-\\u0007\\u200f1, com_1a2b3c4d…); cannot manage API keys",
    ]


@respx.mock
def test_whoami_escapes_the_platforms_fields_for_a_workspace_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A workspace-scoped key: the names are withheld, so the ids stand alone.
    scoped = {
        **HOSTILE_IDS,
        "user": {"id": "usr-\x1b[2J\nforged", "email": None, "name": None},
        "account": {"id": "acc-\u202e1", "name": None, "plan": None, "status": "act\x1bive"},
        "workspace": {
            "id": "wsp-\n\u061c1",
            "name": "tenant",
            "created_at": "2026-09-20T08:00:00.000Z",
        },
    }
    respx.get(f"{BASE}/whoami").mock(return_value=httpx.Response(200, json=scoped))
    assert _cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert _raw_controls(out) == []
    assert out.splitlines() == [
        "User usr-\\u001b[2J\\u000aforged (name and email withheld from a workspace-scoped key)",
        "Account: acc-\\u202e1, act\\u001bive (name and plan withheld from a workspace-scoped key)",
        "Role: own\\u000aer",
        "Scope: workspace tenant (wsp-\\u000a\\u061c1)",
        "Key: ci (key-\\u0007\\u200f1, com_1a2b3c4d…); cannot manage API keys",
    ]


# --- workspaces (platform OPL-5057) ------------------------------------------

WSP = {"id": "wsp-0123456789ab", "name": "acme", "created_at": "2026-09-01T00:00:00.000Z"}
WSP_MEMBERS = [
    {
        "user_id": "usr-0123456789abcdef",
        "email": "dana@example.com",
        "name": "Dana\x1b[2J\nforged",
        "role": "owner",
        "accepted_at": "2026-09-02T00:00:00.000Z",
        "suspended": False,
    },
    {
        "user_id": "usr-fedcba9876543210",
        "email": "lee@example.com",
        "name": None,
        "role": "viewer",
        "accepted_at": "2026-09-03T00:00:00.000Z",
        "suspended": True,
    },
]


@respx.mock
def test_workspaces_list_prints_one_row_each(capsys: pytest.CaptureFixture[str]) -> None:
    hostile = {**WSP, "id": "wsp-ba9876543210", "name": "tenant\r\u202ex"}
    respx.get(f"{BASE}/workspaces").mock(return_value=httpx.Response(200, json=[WSP, hostile]))
    assert _cli.main(["workspaces", "list"]) == 0
    out = capsys.readouterr().out
    assert _raw_controls(out) == [] and "\u202e" not in out
    lines = out.splitlines()
    assert lines[0].split() == ["ID", "NAME", "CREATED"]
    assert lines[1].split() == ["wsp-0123456789ab", "acme", "2026-09-01T00:00:00.000Z"]
    assert lines[2].split() == [
        "wsp-ba9876543210",
        "tenant\\u000d\\u202ex",
        "2026-09-01T00:00:00.000Z",
    ]


@respx.mock
def test_workspaces_list_json_is_the_platforms_rows(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    respx.get(f"{BASE}/workspaces").mock(return_value=httpx.Response(200, json=[WSP]))
    assert _cli.main(["workspaces", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [WSP]


@respx.mock
def test_workspaces_get_prints_the_one(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    route = respx.get(f"{BASE}/workspaces/wsp-0123456789ab").mock(
        return_value=httpx.Response(200, json=WSP)
    )
    assert _cli.main(["workspaces", "get", "wsp-0123456789ab"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split() for line in lines] == [
        ["ID", "NAME", "CREATED"],
        ["wsp-0123456789ab", "acme", "2026-09-01T00:00:00.000Z"],
    ]
    assert _cli.main(["workspaces", "get", "wsp-0123456789ab", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == WSP
    assert route.call_count == 2


@respx.mock
def test_workspaces_get_of_one_it_cannot_see_is_not_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    respx.get(f"{BASE}/workspaces/wsp-000000000000").mock(
        return_value=httpx.Response(404, json={"error": "No such workspace."})
    )
    assert _cli.main(["workspaces", "get", "wsp-000000000000", "--json"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    error = json.loads(err)["error"]
    assert (error["code"], error["status"]) == ("not_found", 404)


@respx.mock
def test_workspaces_members_prints_each_person(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/workspaces/wsp-0123456789ab/members").mock(
        return_value=httpx.Response(200, json=WSP_MEMBERS)
    )
    assert _cli.main(["workspaces", "members", "wsp-0123456789ab"]) == 0
    out = capsys.readouterr().out
    assert _raw_controls(out) == []
    lines = out.splitlines()
    assert len(lines) == 3  # the header and two people: a newline forges no row
    assert lines[0].split() == ["USER", "ID", "EMAIL", "NAME", "ROLE", "ACCEPTED", "SUSPENDED"]
    assert lines[1].split() == [
        "usr-0123456789abcdef",
        "dana@example.com",
        "Dana\\u001b[2J\\u000aforged",
        "owner",
        "2026-09-02T00:00:00.000Z",
        "no",
    ]
    assert lines[2].split() == [
        "usr-fedcba9876543210",
        "lee@example.com",
        "-",
        "viewer",
        "2026-09-03T00:00:00.000Z",
        "yes",
    ]


@respx.mock
def test_workspaces_members_json_is_the_platforms_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    respx.get(f"{BASE}/workspaces/wsp-0123456789ab/members").mock(
        return_value=httpx.Response(200, json=WSP_MEMBERS)
    )
    assert _cli.main(["workspaces", "members", "wsp-0123456789ab", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == WSP_MEMBERS


@respx.mock
def test_workspaces_members_with_a_scoped_key_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    sentence = (
        "An API key confined to a workspace cannot list members: the list is the whole account's."
    )
    respx.get(f"{BASE}/workspaces/wsp-0123456789ab/members").mock(
        return_value=httpx.Response(403, json={"error": sentence})
    )
    assert _cli.main(["workspaces", "members", "wsp-0123456789ab"]) == 1
    out, err = capsys.readouterr()
    assert out == "" and err == f"mandala-py: {sentence}\n"
    assert _cli.main(["workspaces", "members", "wsp-0123456789ab", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert (error["code"], error["status"]) == ("permission_denied", 403)


# --- every value the CLI prints is escaped, not only whoami's (OPL-5367, OPL-5366) --
#
# A name another member of the account or workspace chose (a computer, a
# secret, an SSH key, a webhook's URL), and the ids, timestamps and error texts
# the platform sets, reach the terminal through a table, a line of their own or
# a refusal. Each hostile value below holds a right-to-left override, an escape
# sequence or a newline, and none may reach the terminal raw.

RLO = "\u202e"
EVIL_NAME = f"evil{RLO}exe.txt"
EVIL_STATUS = "run\x1b[31mning"


def _clean(text: str) -> None:
    """``text`` holds no character ``_printable`` escapes, line breaks aside."""
    assert _raw_controls(text) == [], text
    assert RLO not in text and "\x1b" not in text


def test_table_escapes_every_cell_but_the_header() -> None:
    table = _cli._table(("ID", "NAME"), [("vm-\x1b[2J1", "a\nforged  row"), ("vm-2", EVIL_NAME)])
    _clean(table)
    assert table.splitlines() == [
        "ID             NAME",
        "vm-\\u001b[2J1  a\\u000aforged  row",
        "vm-2           evil\\u202eexe.txt",
    ]


HOSTILE_COMPUTERS = [
    {"id": "vm-\u202e1", "name": EVIL_NAME, "status": EVIL_STATUS, "os": "linux"},
    {"id": "vm-2\nvm-forged  x  running", "name": "scratch", "status": "stopped", "os": "linux"},
]


def _escaped_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("  ")]


@respx.mock
def test_you_have_listing_escapes_each_value_and_keeps_its_own_newlines() -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=HOSTILE_COMPUTERS))
    with pytest.raises(_cli._Failure) as caught:
        _cli._resolve(_cli._client(), "nope")
    shown = str(caught.value.code)
    _clean(shown)
    # One real line per computer: a newline in an id forges none.
    assert _escaped_rows(shown) == [
        "  vm-\\u202e1  evil\\u202eexe.txt  run\\u001b[31mning",
        "  vm-2\\u000avm-forged  x  running  scratch  stopped",
    ]
    assert shown.startswith("mandala-py: no computer named 'nope'. You have:\n")
    # --json's message keeps the values as they are: JSON escapes them.
    assert caught.value.message == (
        "no computer named 'nope'. You have:\n"
        f"  vm-{RLO}1  {EVIL_NAME}  {EVIL_STATUS}\n"
        "  vm-2\nvm-forged  x  running  scratch  stopped"
    )


@respx.mock
def test_known_computers_listing_of_an_incomplete_fleet_is_escaped() -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(200, json=HOSTILE_COMPUTERS, headers={"X-GC-Incomplete": "1"})
    )
    with pytest.raises(_cli._Failure) as caught:
        _cli._resolve(_cli._client(), "nope")
    shown = str(caught.value.code)
    _clean(shown)
    assert "Known computers:\n" in shown
    assert len(_escaped_rows(shown)) == 2
    assert shown.endswith("\nretry when every host is reachable")


@respx.mock
def test_ambiguous_name_escapes_the_ids() -> None:
    twins = [
        {"id": "vm-\x1b]0;x\x07", "name": "scratch", "status": "running"},
        {"id": f"vm-{RLO}3", "name": "scratch", "status": "running"},
    ]
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=twins))
    with pytest.raises(_cli._Failure) as caught:
        _cli._resolve(_cli._client(), "scratch")
    shown = str(caught.value.code)
    _clean(shown)
    assert shown.endswith("use an id: vm-\\u001b]0;x\\u0007, vm-\\u202e3")
    assert caught.value.reason == "ambiguous_computer"


@respx.mock
def test_a_refusal_under_json_reports_the_values_as_they_are(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=HOSTILE_COMPUTERS))
    assert _cli.main(["ssh-access", "nope", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "not_found"
    assert f"  vm-{RLO}1  {EVIL_NAME}  {EVIL_STATUS}\n" in error["message"]


@pytest.mark.parametrize(
    ("payload", "want"),
    [
        ({"status": "running", "os": "windows"}, "evil\\u202eexe.txt is a Windows computer"),
        ({"status": EVIL_STATUS, "os": "linux"}, "evil\\u202eexe.txt is run\\u001b[31mning"),
        ({"status": "running", "os": "linux"}, "evil\\u202eexe.txt has no terminal endpoint"),
    ],
)
@respx.mock
def test_terminal_refusals_escape_the_computer(payload: dict, want: str) -> None:
    row = {**payload, "id": "vm-9", "name": EVIL_NAME}
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[row]))
    respx.get(f"{BASE}/computers/vm-9").mock(return_value=httpx.Response(200, json=row))
    with pytest.raises(SystemExit) as caught:
        _cli.main(["terminal", "vm-9"])
    shown = str(caught.value.code)
    _clean(shown)
    assert want in shown


SECRET_ROW = {
    "id": f"csec-{RLO}1",
    "name": f"OPENAI{RLO}KEY",
    "workspace_id": "wsp-\x1b[2J",
    "revision_id": "csr-\nforged",
    "created_at": "2026-09-20T12:00:00Z",
    "updated_at": "2026-09-20T12:00:00Z\x1b[31m",
    "last_used_at": f"2026-09-21{RLO}",
}
SECRETS_LISTING = {
    "secrets": [SECRET_ROW],
    "delivery": True,
    "limits": {
        "name_max_chars": 60,
        "value_max_bytes": 4096,
        "active_per_account": 100,
        "created_per_account": 1000,
    },
}
WEBHOOK_ROW = {
    "id": "whk-\x1b[2J",
    "url": f"https://ci.example.com/{RLO}moc.live",
    "description": "CI",
    "events": ["process.exited", f"computer{RLO}.ready"],
    "computers": [],
    "enabled": False,
    "disabled_reason": "fail\ning",
    "disabled_at": "2026-09-02T00:00:00.000Z",
    "last_success_at": None,
    "last_failure_at": None,
    "last_status": 503,
    "workspace_id": None,
    "created_at": "2026-09-01T11:00:00.000Z",
    "updated_at": "2026-09-02T00:00:00.000Z",
}
DELIVERY_ROW = {
    "id": "whd-1",
    "event_type": f"process{RLO}.exited",
    "computer": "vm-\x1b[2J",
    "cursor": "c",
    "state": "exhausted",
    "attempts": 8,
    "next_at": None,
    "attempted_at": "2026-09-02T04:00:00.000Z",
    "last_status": None,
    "last_error": "timeout\nwhd-2  delivered  1  forged",
    "delivered_at": None,
    "created_at": "2026-09-01T12:00:00.000Z",
}
SSH_KEY_ROW = {
    "id": f"sshk-{RLO}1",
    "name": f"laptop{RLO}\x1b[31m",
    "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGqsBlqrbipXh/7n81gKS46IyjJY7nVv8mGtIAE+v76w",
    "fingerprint": "SHA256:\x1b[2Jx",
    "key_type": "ssh-ed25519",
    "created_at": "2026-09-16T12:00:00Z",
    "last_used_at": "2026-09-17\n",
}
API_KEY_ROW = {
    **HOSTILE_KEY,
    "id": f"key-{RLO}1",
    "last_used_at": "2026-09-21T00:00:00Z\x1b[2J",
    "workspace_id": f"wsp-{RLO}\x1b[31m",
    "minted_by_key_id": "key-\n2",
}
WORKSPACE_ROW = {"id": f"wsp-{RLO}1", "name": "acme", "created_at": "2026-09-01\x1b[2J"}
MEMBER_ROW = {**WSP_MEMBERS[0], "user_id": f"usr-{RLO}1", "accepted_at": "2026-09-02\n\x1b[31m"}


@pytest.mark.parametrize(
    ("argv", "method", "path", "answer", "want", "lines"),
    [
        (
            ["secrets", "list"],
            "GET",
            "/secrets",
            SECRETS_LISTING,
            [
                "csec-\\u202e1",
                "OPENAI\\u202eKEY",
                "wsp-\\u001b[2J",
                "2026-09-21\\u202e",
                "2026-09-20T12:00:00Z\\u001b[31m",
            ],
            2,
        ),
        (
            ["webhooks", "list"],
            "GET",
            "/webhooks",
            [WEBHOOK_ROW],
            [
                "whk-\\u001b[2J",
                "off (fail\\u000aing)",
                "process.exited,computer\\u202e.ready",
                "https://ci.example.com/\\u202emoc.live",
            ],
            2,
        ),
        (
            ["webhooks", "deliveries", "whk-1"],
            "GET",
            "/webhooks/whk-1/deliveries",
            [DELIVERY_ROW],
            ["process\\u202e.exited", "vm-\\u001b[2J", "timeout\\u000awhd-2  delivered"],
            2,
        ),
        (
            ["ssh-key", "list"],
            "GET",
            "/ssh-keys",
            [SSH_KEY_ROW],
            [
                "sshk-\\u202e1",
                "SHA256:\\u001b[2Jx",
                "2026-09-17\\u000a",
                "laptop\\u202e\\u001b[31m",
            ],
            2,
        ),
        (
            ["api-keys", "list"],
            "GET",
            "/api-keys",
            [API_KEY_ROW],
            [
                "key-\\u202e1",
                "2026-09-21T00:00:00Z\\u001b[2J",
                "(wsp-\\u202e\\u001b[31m)",
                "key-\\u000a2",
            ],
            2,
        ),
        (
            ["workspaces", "list"],
            "GET",
            "/workspaces",
            [WORKSPACE_ROW],
            ["wsp-\\u202e1", "2026-09-01\\u001b[2J"],
            2,
        ),
        (
            ["workspaces", "members", "wsp-1"],
            "GET",
            "/workspaces/wsp-1/members",
            [MEMBER_ROW],
            ["usr-\\u202e1", "2026-09-02\\u000a\\u001b[31m"],
            2,
        ),
    ],
)
@respx.mock
def test_listings_escape_every_cell(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    method: str,
    path: str,
    answer: object,
    want: list[str],
    lines: int,
) -> None:
    respx.route(method=method, url=f"{BASE}{path}").mock(
        return_value=httpx.Response(200, json=answer)
    )
    assert _cli.main(argv) == 0
    out = capsys.readouterr().out
    _clean(out)
    assert len(out.splitlines()) == lines  # the header and the one row: nothing forged
    for escaped in want:
        assert escaped in out


@respx.mock
def test_secrets_set_and_rm_escape_the_line_they_print(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import sys

    class Stdin(io.TextIOWrapper):
        def isatty(self) -> bool:
            return False

    respx.get(f"{BASE}/secrets").mock(return_value=httpx.Response(200, json=SECRETS_LISTING))
    respx.put(f"{BASE}/secrets/{SECRET_ROW['id']}").mock(
        return_value=httpx.Response(200, json=SECRET_ROW)
    )
    respx.post(f"{BASE}/secrets").mock(return_value=httpx.Response(201, json=SECRET_ROW))
    respx.delete(f"{BASE}/secrets/{SECRET_ROW['id']}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    monkeypatch.setattr(sys, "stdin", Stdin(io.BytesIO(b"value\n"), encoding="utf-8"))
    assert _cli.main(["secrets", "set", "OPENAI_KEY"]) == 0
    out = capsys.readouterr().out
    _clean(out)
    assert out == "stored OPENAI\\u202eKEY  csec-\\u202e1  revision csr-\\u000aforged\n"
    assert _cli.main(["secrets", "rm", SECRET_ROW["id"]]) == 0
    out = capsys.readouterr().out
    _clean(out)
    assert out == "deleted OPENAI\\u202eKEY  csec-\\u202e1\n"


@respx.mock
def test_api_keys_create_escapes_the_id_it_reports(capsys: pytest.CaptureFixture[str]) -> None:
    created = {**API_KEY_ROW, "raw": "com_" + "ab" * 24}
    respx.post(f"{BASE}/api-keys").mock(return_value=httpx.Response(201, json=created))
    assert _cli.main(["api-keys", "create"]) == 0
    out, err = capsys.readouterr()
    assert out == created["raw"] + "\n"
    _clean(err)
    assert err.count("\n") == 1
    assert err.startswith("mandala-py: created key-\\u202e1 (")
    assert "(wsp-\\u202e\\u001b[31m))" in err


@respx.mock
def test_ssh_key_add_escapes_the_line_it_prints(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    pub = tmp_path / "id.pub"
    pub.write_text(SSH_KEY_ROW["public_key"] + "\n")
    respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=SSH_KEY_ROW))
    assert _cli.main(["ssh-key", "add", str(pub)]) == 0
    out = capsys.readouterr().out
    _clean(out)
    assert out == "added sshk-\\u202e1  SHA256:\\u001b[2Jx  laptop\\u202e\\u001b[31m\n"


@respx.mock
def test_ssh_access_escapes_the_name_and_the_hosts_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    row = {"id": "vm-9", "name": EVIL_NAME, "status": "running"}
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[row]))
    respx.get(f"{BASE}/computers/vm-9/ssh").mock(
        return_value=httpx.Response(
            200,
            json={
                "computer": "vm-9",
                "enabled": True,
                "available": True,
                "pending": False,
                "key_count": 1,
                "keys_pushed": 0,
                "error": "push failed\nSSH is on for prod",
            },
        )
    )
    assert _cli.main(["ssh-access", "vm-9"]) == 0
    out = capsys.readouterr().out
    _clean(out)
    assert out.splitlines() == [
        "SSH is on for evil\\u202eexe.txt",
        "  keys: 0 of 1 delivered",
        "  error: push failed\\u000aSSH is on for prod",
    ]


@pytest.mark.parametrize("kind", ["browser-proxy", "egress-proxy"])
@respx.mock
def test_proxy_get_escapes_the_name_server_and_ids(
    capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    field = kind.replace("-", "_")
    proxy: dict[str, object] = {
        "server": f"http://proxy.example.com:3128/{RLO}",
        "credentials_secret_id": "csec-\x1b[2J",
    }
    if kind == "browser-proxy":
        proxy["bypass"] = [f"a{RLO}.com", "b\n.com"]
    row = {"id": "vm-9", "name": EVIL_NAME, "status": "running", field: proxy}
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[row]))
    respx.get(f"{BASE}/computers/vm-9").mock(return_value=httpx.Response(200, json=row))
    assert _cli.main([kind, "get", "vm-9"]) == 0
    out = capsys.readouterr().out
    _clean(out)
    assert out.startswith("evil\\u202eexe.txt: ")
    assert "http://proxy.example.com:3128/\\u202e" in out
    assert "  credentials: secret csec-\\u001b[2J" in out
    if kind == "browser-proxy":
        assert "  bypass: a\\u202e.com, b\\u000a.com" in out


@respx.mock
def test_secrets_rm_ambiguity_escapes_the_secret_it_names() -> None:
    by_name = {**SECRET_ROW, "id": "csec-\u202ea", "name": "CSEC-B"}
    by_id = {**SECRET_ROW, "id": "csec-b", "name": "other"}
    respx.get(f"{BASE}/secrets").mock(
        return_value=httpx.Response(200, json={**SECRETS_LISTING, "secrets": [by_name, by_id]})
    )
    with pytest.raises(_cli._Failure) as caught:
        _cli.main(["secrets", "rm", "csec-b"])
    shown = str(caught.value.code)
    _clean(shown)
    assert "is both a name (CSEC-B, csec-\\u202ea) and another secret's id" in shown
    assert caught.value.reason == "ambiguous_secret"


@pytest.mark.parametrize("kind", ["browser-proxy", "egress-proxy"])
@respx.mock
def test_proxy_set_refusal_escapes_the_current_server_and_credentials(kind: str) -> None:
    proxy = {"server": "http://old.example.com:3128/\u202e", "credentials_secret_id": "csec-\n1"}
    row = {"id": "vm-9", "name": "dev", "status": "running", kind.replace("-", "_"): proxy}
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[row]))
    respx.get(f"{BASE}/computers/vm-9").mock(return_value=httpx.Response(200, json=row))
    with pytest.raises(_cli._Failure) as caught:
        _cli.main([kind, "set", "vm-9", "http://new.example.com:3128"])
    shown = str(caught.value.code)
    _clean(shown)
    assert "credentials (csec-\\u000a1) are for http://old.example.com:3128/\\u202e," in shown
