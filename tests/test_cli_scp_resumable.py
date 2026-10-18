"""``mandala-py scp`` past 64 MiB: the resumable upload, as ``mandala scp`` does.

A file one request cannot carry is sent in chunks through the resumable upload,
its receipt removed once the file is published, and an interrupted one named by
its upload id so ``--upload-id`` can resume it (OPL-6080). The single-request
limit is shrunk here so the files stay small.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from mandala_computer import _cli

BASE = "https://api.test/api/v1"
LINUX = [{"id": "vm-1", "name": "dev", "status": "running", "os": "linux"}]
WINDOWS = [{"id": "vm-1", "name": "dev", "status": "running", "os": "windows"}]
UPLOADS = f"{BASE}/computers/vm-1/files/uploads"
ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)
    monkeypatch.setattr(_cli, "FILE_SIZE_LIMIT", 8)


def staged(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    offset = int(q["offset"]) + len(request.content)
    size = int(q["size"])
    return httpx.Response(
        200,
        json={
            "path": q["path"],
            "upload_id": q["upload_id"],
            "offset": offset,
            "size": size,
            "complete": offset == size,
        },
    )


def source(tmp_path: Path, data: bytes = b"0123456789") -> Path:
    path = tmp_path / "big.bin"
    path.write_bytes(data)
    return path


@respx.mock
def test_a_file_past_the_single_request_limit_goes_up_resumably(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=LINUX))
    single = respx.put(f"{BASE}/computers/vm-1/files")
    chunks = respx.put(UPLOADS).mock(side_effect=staged)
    cleanup = respx.delete(UPLOADS).mock(return_value=httpx.Response(200, json={}))
    src = source(tmp_path)
    assert _cli.main(["scp", str(src), "dev:/home/user/", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "source": str(src),
        "destination": "dev:/home/user/big.bin",
        "bytes": 10,
        "confirmed": True,
    }
    assert not single.called
    assert chunks.call_count == 1
    sent = chunks.calls[0].request
    assert sent.content == b"0123456789"
    assert re.fullmatch(r"[a-f0-9]{32}", sent.url.params["upload_id"])
    assert sent.url.params["overwrite"] == "true"
    # The receipt is removed once the file is in place, without waking anything.
    assert cleanup.call_count == 1
    assert cleanup.calls[0].request.url.params["upload_id"] == sent.url.params["upload_id"]
    assert cleanup.calls[0].request.url.params.get("no_wake") == "1"


@respx.mock
def test_a_receipt_that_could_not_be_removed_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=LINUX))
    respx.put(UPLOADS).mock(side_effect=staged)
    respx.delete(UPLOADS).mock(return_value=httpx.Response(503, json={"error": "away"}))
    src = source(tmp_path)
    assert _cli.main(["scp", str(src), "dev:/home/user/big.bin", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert re.fullmatch(r"[a-f0-9]{32}", out["cleanup_upload_id"])
    assert out["bytes"] == 10


@respx.mock
def test_upload_id_resumes_from_what_the_guest_holds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=LINUX))
    status = respx.get(UPLOADS).mock(
        return_value=httpx.Response(
            200,
            json={
                "path": "/home/user/big.bin",
                "upload_id": ID,
                "offset": 4,
                "size": 10,
                "complete": False,
            },
        )
    )
    chunks = respx.put(UPLOADS).mock(side_effect=staged)
    respx.delete(UPLOADS).mock(return_value=httpx.Response(200, json={}))
    src = source(tmp_path)
    assert _cli.main(["scp", "--upload-id", ID, str(src), "dev:/home/user/big.bin"]) == 0
    assert status.called
    assert chunks.calls[0].request.url.params["offset"] == "4"
    assert chunks.calls[0].request.content == b"456789"
    assert capsys.readouterr().err.startswith(f"{src} -> dev:/home/user/big.bin (10 bytes)")


@respx.mock
def test_an_interrupted_upload_names_its_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=LINUX))
    respx.put(UPLOADS).mock(side_effect=httpx.ConnectError("dropped"))
    src = source(tmp_path)
    assert _cli.main(["scp", str(src), "dev:/home/user/big.bin", "--json"]) == 1
    error: dict[str, Any] = json.loads(capsys.readouterr().err)["error"]
    assert re.fullmatch(r"[a-f0-9]{32}", error["upload_id"])
    assert _cli.main(["scp", str(src), "dev:/home/user/big.bin"]) == 1
    lines = capsys.readouterr().err.splitlines()
    assert any("--upload-id" in line and "upload id" in line for line in lines)


@respx.mock
def test_a_large_upload_to_a_windows_guest_is_refused_before_sending(tmp_path: Path) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=WINDOWS))
    chunks = respx.put(UPLOADS)
    with pytest.raises(SystemExit, match="larger uploads are Linux only"):
        _cli.main(["scp", str(source(tmp_path)), "dev:C:\\\\Users\\\\me\\\\big.bin"])
    assert not chunks.called


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["scp", "--upload-id", "ABC", "x", "dev:/tmp/x"], "32 lowercase hexadecimal"),
        (["scp", "--upload-id", ID, "dev:/tmp/x", "."], "applies to an upload"),
    ],
)
def test_upload_id_is_checked_before_anything_is_sent(argv: list[str], message: str) -> None:
    with pytest.raises(SystemExit, match=message):
        _cli.main(argv)


@pytest.mark.parametrize("as_json", [False, True])
@respx.mock
def test_ctrl_c_mid_upload_still_names_the_upload_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=LINUX))

    def stop(_request: httpx.Request) -> httpx.Response:
        raise KeyboardInterrupt

    respx.put(UPLOADS).mock(side_effect=stop)
    argv = ["scp", str(source(tmp_path)), "dev:/home/user/big.bin"]
    assert _cli.main(argv + (["--json"] if as_json else [])) == 130
    err = capsys.readouterr().err
    if as_json:
        error = json.loads(err)["error"]
        assert error["code"] == "cancelled"
        assert re.fullmatch(r"[a-f0-9]{32}", error["upload_id"])
    else:
        assert re.search(r"upload [a-f0-9]{32} interrupted; resume it .*--upload-id", err)


@respx.mock
def test_a_source_that_changes_mid_upload_still_names_the_upload_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=LINUX))
    src = source(tmp_path, b"x" * (4 * 1024 * 1024 + 10))

    def shrink(request: httpx.Request) -> httpx.Response:
        answer = staged(request)
        src.write_bytes(b"short")
        return answer

    respx.put(UPLOADS).mock(side_effect=shrink)
    assert _cli.main(["scp", str(src), "dev:/home/user/big.bin", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "invalid_arguments"
    assert re.fullmatch(r"[a-f0-9]{32}", error["upload_id"])
