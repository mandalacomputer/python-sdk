"""How ``mandala-py terminal`` reads a refused upgrade (OPL-6080).

A suspended computer is told to start, not to be stopped and started: the
terminal route does not resume it, and its 409 carries ``resume_required``.
A ``503 contention`` (another terminal part way through its setup) is opened
again. Anything else is the platform's sentence with its status and code.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
import respx
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from mandala_computer import _cli

BASE = "https://api.test/api/v1"
VNC = {
    "url": "wss://control.test",
    "view_url": "wss://view.test",
    "token": "control",
    "view_token": "view",
    "embed_url": "https://embed.test",
    "terminal_url": "wss://terminal.test",
}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)
    monkeypatch.setattr(_cli, "_terminal_fd", lambda: None)
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def refused(status: int, body: dict[str, Any] | None, **headers: str) -> InvalidStatus:
    content = json.dumps(body).encode() if body is not None else b""
    return InvalidStatus(Response(status, "refused", Headers(headers), content))


def connecting(monkeypatch: pytest.MonkeyPatch, *answers: object) -> list[str]:
    """``websockets``' connect, answering each attempt with the next of ``answers``."""
    import websockets.sync.client as ws_client

    seen: list[str] = []
    queue = list(answers)

    def connect(url: str, **_: Any) -> object:
        seen.append(url)
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(ws_client, "connect", connect)
    return seen


def test_a_suspended_409_says_start_it_not_stop_and_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connecting(
        monkeypatch,
        refused(
            409,
            {
                "error": "this computer is suspended; resume it and reconnect",
                "reason": "unavailable",
                "resume_required": True,
            },
        ),
    )
    with pytest.raises(SystemExit) as caught:
        _cli._connect("wss://terminal.test")
    message = str(caught.value.code)
    assert "suspended" in message and "start it, then retry" in message
    assert "stop it" not in message


@respx.mock
def test_terminal_refuses_a_suspended_computer_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {"id": "vm-9", "name": "dev", "status": "suspended", "os": "linux", "vnc": VNC}
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[row]))
    respx.get(f"{BASE}/computers/vm-9").mock(return_value=httpx.Response(200, json=row))
    seen = connecting(monkeypatch)
    with pytest.raises(SystemExit, match="dev is suspended, and opening a terminal does not"):
        _cli.main(["terminal", "dev"])
    assert seen == []


def test_a_contention_503_is_opened_again(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = object()
    busy = {
        "error": "another terminal is still being set up on this computer; try again shortly",
        "reason": "contention",
    }
    seen = connecting(monkeypatch, refused(503, busy), refused(503, busy), ws)
    assert _cli._connect("wss://terminal.test") is ws
    assert len(seen) == 3


def test_contention_that_does_not_clear_ends_with_its_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    busy = {"error": "another terminal is still being set up", "reason": "contention"}
    attempts = [refused(503, busy, **{"X-Request-ID": "req_9"}) for _ in range(4)]
    seen = connecting(monkeypatch, *attempts)
    with pytest.raises(_cli.UnavailableError) as caught:
        _cli._connect("wss://terminal.test")
    assert len(seen) == 4
    assert caught.value.status == 503
    assert caught.value.reason == "contention"
    assert caught.value.request_id == "req_9"


@respx.mock
def test_any_other_refusal_prints_the_platforms_sentence_and_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    row = {"id": "vm-9", "name": "dev", "status": "running", "os": "linux", "vnc": VNC}
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[row]))
    respx.get(f"{BASE}/computers/vm-9").mock(return_value=httpx.Response(200, json=row))
    connecting(
        monkeypatch,
        refused(
            409,
            {
                "error": "this computer was last started before interactive terminals "
                "existed and its hardware lacks the channel; stop and start it to add one",
                "reason": "unsupported",
                "request_id": "req_1",
            },
        ),
    )
    assert _cli.main(["terminal", "dev"]) == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("mandala-py: this computer was last started before")
    assert lines[0].endswith("(HTTP 409)")
    assert "request id req_1" in lines[1]


def test_a_refusal_without_a_body_still_names_its_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connecting(monkeypatch, refused(502, None))
    with pytest.raises(_cli.APIError) as caught:
        _cli._connect("wss://terminal.test")
    assert caught.value.status == 502
    assert "terminal refused" in str(caught.value)


def test_a_retry_after_longer_than_the_wait_is_not_shortened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    busy = {"error": "another terminal is still being set up", "reason": "contention"}
    seen = connecting(monkeypatch, refused(503, busy, **{"Retry-After": "60"}), object())
    with pytest.raises(_cli.UnavailableError) as caught:
        _cli._connect("wss://terminal.test")
    assert len(seen) == 1
    assert caught.value.retry_after == 60


def test_a_short_retry_after_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    busy = {"error": "another terminal is still being set up", "reason": "contention"}
    ws = object()
    connecting(monkeypatch, refused(503, busy, **{"Retry-After": "7"}), ws)
    assert _cli._connect("wss://terminal.test") is ws
    assert slept == [7.0]
