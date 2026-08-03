"""Paths and request bodies, shared by the sync and async clients.

Every route the SDK can reach and every payload it can send is built here. The
two clients differ only in awaits; if either built its own URLs or bodies, they
could drift apart silently — and the surface test that pins the SDK to the
platform's allowlist would only be checking one of them.
"""

from __future__ import annotations

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


def snapshot_body(memory: bool) -> dict[str, Any]:
    return {"memory": memory}


def schedule_body(*, enabled: bool, hour: int, minute: int, tz: str) -> dict[str, Any]:
    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0-23")
    if not 0 <= minute <= 59:
        raise ValueError("minute must be 0-59")
    return {"enabled": enabled, "hour": hour, "minute": minute, "tz": tz}


def pointer_body(action: str, x: int, y: int) -> dict[str, Any]:
    return {"action": action, "x": x, "y": y}


def scroll_body(x: int, y: int, direction: str, amount: int) -> dict[str, Any]:
    if direction not in ("up", "down"):
        raise ValueError('direction must be "up" or "down"')
    return {"action": "scroll", "x": x, "y": y, "button": direction, "amount": amount}


def type_body(text: str) -> dict[str, Any]:
    return {"action": "type", "text": text}


def key_body(keys: tuple[str, ...]) -> dict[str, Any]:
    if not keys:
        raise ValueError("key() needs at least one key")
    return {"action": "key", "keys": list(keys)}


def screenshot_params(width: int | None) -> dict[str, Any] | None:
    return {"w": width} if width else None
