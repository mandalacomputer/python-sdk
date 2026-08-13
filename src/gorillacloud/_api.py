"""Paths and request bodies, shared by the sync and async clients.

Every route the SDK can reach and every payload it can send is built here. The
two clients differ only in awaits; if either built its own URLs or bodies, they
could drift apart silently — and the surface test that pins the SDK to the
platform's allowlist would only be checking one of them.
"""

from __future__ import annotations

import shlex
from typing import Any

# --- paths ----------------------------------------------------------------

TEMPLATES = "templates"
COMPUTERS = "computers"
SNAPSHOTS = "snapshots"


def computer(computer_id: str) -> str:
    return f"computers/{computer_id}"


def computer_action(computer_id: str, action: str) -> str:
    """start | stop | restart | clone | screenshot | input | exec | snapshots | schedule."""
    return f"computers/{computer_id}/{action}"


def snapshot(snapshot_id: str) -> str:
    return f"snapshots/{snapshot_id}"


def snapshot_action(snapshot_id: str, action: str) -> str:
    """restore | clone."""
    return f"snapshots/{snapshot_id}/{action}"


# --- bodies ---------------------------------------------------------------


def create_body(
    *,
    name: str | None,
    template: str | None,
    cpu: int | None,
    ram_mb: int | None,
    disk_gb: int | None,
    start: bool,
    resolution: str | None = None,
) -> dict[str, Any]:
    """Build a create payload, omitting anything unset.

    Omission is meaningful: the server applies the template's defaults only when
    a key is absent, so sending explicit nulls would override them with nothing.
    """
    body: dict[str, Any] = {"start": start}
    for key, value in (
        ("name", name),
        ("template", template),
        ("cpu", cpu),
        ("ram_mb", ram_mb),
        ("disk_gb", disk_gb),
        ("resolution", resolution),
    ):
        if value is not None:
            body[key] = value
    return body


def name_body(name: str | None) -> dict[str, Any]:
    return {} if name is None else {"name": name}


def rename_body(name: str) -> dict[str, Any]:
    """The rename payload.

    Empty is refused here rather than at the server, which refuses it too. On
    create an omitted name means "you pick one"; on rename it can only mean a
    caller cleared the field, and a round trip to be told so is a round trip
    that never had to happen.
    """
    if not name.strip():
        raise ValueError("name must not be empty")
    return {"name": name}


def exec_body(command: str, timeout_s: int, desktop: bool = False) -> dict[str, Any]:
    """Build an exec payload.

    ``session`` is omitted rather than sent empty when ``desktop`` is false: the
    server's default is the system context, and the only value it accepts is
    ``"desktop"``.
    """
    body: dict[str, Any] = {"command": command, "timeout_s": timeout_s}
    if desktop:
        body["session"] = "desktop"
    return body


def open_url_command(url: str) -> str:
    """Build the shell command that puts ``url`` on the guest's screen.

    The browser is named rather than asked for. ``xdg-open`` is the portable way
    to want this and is installed on the base template, along with ``exo-open``,
    ``sensible-browser`` and ``x-www-browser`` — and every one of them exits 0
    and launches nothing, because the image's default-browser association points
    at a desktop entry it does not ship. Exit 0 and an unchanged screen is the
    worst shape a failure can take, so this asks for Firefox, which is the only
    browser on the image anyway.

    This function is the one place that decision lives. When the platform fixes
    the association (OPL-3376), this is the line that changes, rather than every
    caller's prompt.

    Detached, because a browser does not exit on its own: in the foreground the
    call would block until the timeout killed it and come back as a failure,
    having opened the window anyway.
    """
    url = url.strip()
    if not url:
        raise ValueError("url must not be empty")
    # shlex.quote stops the URL reaching the shell as anything but one argument.
    # It cannot stop the *browser* reading a leading dash as a flag, and no URL
    # starts with one, so that is refused outright rather than quoted.
    if url.startswith("-"):
        raise ValueError(f"url must not start with '-': {url!r}")
    return f"nohup firefox {shlex.quote(url)} >/dev/null 2>&1 &"


def snapshot_body(memory: bool) -> dict[str, Any]:
    return {"memory": memory}


def schedule_body(*, enabled: bool, hour: int, minute: int, tz: str) -> dict[str, Any]:
    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0-23")
    if not 0 <= minute <= 59:
        raise ValueError("minute must be 0-59")
    return {"enabled": enabled, "hour": hour, "minute": minute, "tz": tz}


# --- input ----------------------------------------------------------------
#
# The verb set is Anthropic's computer tool, in full. The platform accepts both
# that vocabulary and this SDK's flatter one, so these bodies use whichever is
# clearer for each action — what matters is that every verb a computer-use model
# can emit has a method here, because the alternative is every user of this SDK
# writing the same seven stubs.


def pointer_body(action: str, x: int, y: int) -> dict[str, Any]:
    return {"action": action, "x": x, "y": y}


def click_body(
    action: str, x: int | None, y: int | None, modifiers: tuple[str, ...]
) -> dict[str, Any]:
    """A click, optionally at a point and optionally with keys held down.

    No coordinate means "where the pointer already is", which is a real and
    different request from clicking (0, 0) — so the keys are omitted rather than
    sent as zeros.
    """
    body: dict[str, Any] = {"action": action}
    if x is not None or y is not None:
        body["x"] = x or 0
        body["y"] = y or 0
    if modifiers:
        body["text"] = "+".join(modifiers)
    return body


def drag_body(from_x: int | None, from_y: int | None, to_x: int, to_y: int) -> dict[str, Any]:
    """A press, a move, and a release — one gesture, not two clicks.

    ``start_coordinate`` is omitted when the caller did not give one, which asks
    the platform to drag from wherever the pointer is. It refuses that if nothing
    has moved the pointer yet, rather than guessing at an origin.
    """
    body: dict[str, Any] = {"action": "left_click_drag", "coordinate": [to_x, to_y]}
    if from_x is not None and from_y is not None:
        body["start_coordinate"] = [from_x, from_y]
    return body


def button_body(action: str, x: int | None, y: int | None) -> dict[str, Any]:
    """left_mouse_down / left_mouse_up, optionally moving first."""
    body: dict[str, Any] = {"action": action}
    if x is not None or y is not None:
        body["x"] = x or 0
        body["y"] = y or 0
    return body


_SCROLL_DIRECTIONS = ("up", "down", "left", "right")


def scroll_body(
    x: int, y: int, direction: str, amount: int, modifiers: tuple[str, ...] = ()
) -> dict[str, Any]:
    if direction not in _SCROLL_DIRECTIONS:
        raise ValueError(f"direction must be one of {_SCROLL_DIRECTIONS}")
    body: dict[str, Any] = {
        "action": "scroll",
        "x": x,
        "y": y,
        "scroll_direction": direction,
        "amount": amount,
    }
    if modifiers:
        body["text"] = "+".join(modifiers)
    return body


def type_body(text: str) -> dict[str, Any]:
    return {"action": "type", "text": text}


def key_body(keys: tuple[str, ...]) -> dict[str, Any]:
    if not keys:
        raise ValueError("key() needs at least one key")
    return {"action": "key", "keys": list(keys)}


def hold_key_body(keys: tuple[str, ...], seconds: float) -> dict[str, Any]:
    if not keys:
        raise ValueError("hold_key() needs at least one key")
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    return {"action": "hold_key", "keys": list(keys), "duration": seconds}


def wait_body(seconds: float) -> dict[str, Any]:
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    return {"action": "wait", "duration": seconds}


def cursor_body() -> dict[str, Any]:
    return {"action": "cursor_position"}


def screenshot_params(width: int | None) -> dict[str, Any] | None:
    return {"w": width} if width else None
