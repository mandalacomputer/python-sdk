"""The Computer handle — a cloud desktop and everything you can do to it."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from . import _api
from ._client import Transport
from ._exceptions import GorillaCloudError, TimeoutError
from ._models import ExecResult, Snapshot

__all__ = ["Computer"]

# The guest renders at a fixed 1280x800; coordinates are in that space.
SCREEN_WIDTH = 1280
SCREEN_HEIGHT = 800


class ComputerFields:
    """Read-only accessors over a computer payload.

    Shared by the sync and async handles — the field names are the API contract
    and there is no reason for two copies of them.
    """

    _data: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self._data.get("id", ""))

    @property
    def name(self) -> str:
        return str(self._data.get("name", ""))

    @property
    def status(self) -> str:
        """``"running"`` or ``"stopped"`` as of the last refresh."""
        return str(self._data.get("status", ""))

    @property
    def os(self) -> str:
        return str(self._data.get("os", ""))

    @property
    def template(self) -> str:
        return str(self._data.get("template", ""))

    @property
    def cpu(self) -> int:
        return int(self._data.get("cpu", 0))

    @property
    def ram_mb(self) -> int:
        return int(self._data.get("ram_mb", 0))

    @property
    def disk_gb(self) -> int:
        return int(self._data.get("disk_gb", 0))

    @property
    def created_at(self) -> str:
        return str(self._data.get("created_at", ""))

    @property
    def raw(self) -> Mapping[str, Any]:
        """The API response verbatim, including any fields this SDK predates."""
        return dict(self._data)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id} {self.name!r} {self.status}>"


class Computer(ComputerFields):
    """A cloud desktop.

    Obtain one from :class:`gorillacloud.Client` — ``client.computers.create()``,
    ``.get()``, or ``.list()`` — rather than constructing it directly.
    """

    def __init__(self, transport: Transport, data: Mapping[str, Any]) -> None:
        self._t = transport
        self._data = dict(data)

    # --- lifecycle ------------------------------------------------------

    def refresh(self) -> Computer:
        """Re-read this computer's state from the API."""
        self._data = dict(self._t.json("GET", _api.computer(self.id)) or {})
        return self

    def start(self) -> Computer:
        self._t.request("POST", _api.computer_action(self.id, "start"))
        return self.refresh()

    def stop(self) -> Computer:
        self._t.request("POST", _api.computer_action(self.id, "stop"))
        return self.refresh()

    def restart(self) -> Computer:
        self._t.request("POST", _api.computer_action(self.id, "restart"))
        return self.refresh()

    def clone(self, name: str | None = None) -> Computer:
        """Copy this computer into a new one. The source must be stopped."""
        data = self._t.json(
            "POST", _api.computer_action(self.id, "clone"), json=_api.name_body(name)
        )
        return Computer(self._t, data or {})

    def delete(self) -> None:
        """Destroy this computer and its disk. Snapshots taken from it survive."""
        self._t.request("DELETE", _api.computer(self.id))

    # --- readiness ------------------------------------------------------

    def wait_until_running(self, timeout: float = 120.0, poll: float = 2.0) -> Computer:
        """Block until the machine is running.

        This is the *machine*, not the desktop: it returns as soon as the VM is
        up, while the guest OS is still booting. Use :meth:`wait_for_guest` when
        you need something inside the guest to be ready.
        """
        deadline = time.monotonic() + timeout
        while True:
            self.refresh()
            if self.status == "running":
                return self
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.id} was still {self.status!r} after {timeout:g}s")
            time.sleep(poll)

    def wait_for_guest(self, timeout: float = 180.0, poll: float = 3.0) -> Computer:
        """Block until the guest OS answers, by running a trivial command in it.

        Linux only — it relies on the guest agent, which Windows images do not
        ship yet. On Windows use :meth:`wait_until_running` and poll
        :meth:`screenshot` instead.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                if self.exec("true", timeout_s=5).ok:
                    return self
            except GorillaCloudError:
                pass  # agent not up yet
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.id} guest did not respond within {timeout:g}s")
            time.sleep(poll)

    # --- observing ------------------------------------------------------

    def screenshot(self, width: int | None = None) -> bytes:
        """Capture the screen.

        Full-resolution PNG by default. Passing ``width`` returns a downscaled
        JPEG instead — much cheaper, and enough for a thumbnail or a quick
        "has anything changed" check.
        """
        resp = self._t.request(
            "GET",
            _api.computer_action(self.id, "screenshot"),
            params=_api.screenshot_params(width),
        )
        return resp.content

    # --- controlling ----------------------------------------------------

    def _input(self, body: dict[str, Any]) -> None:
        self._t.request("POST", _api.computer_action(self.id, "input"), json=body)

    def move(self, x: int, y: int) -> None:
        """Move the pointer to ``(x, y)`` in the guest's 1280x800 screen space."""
        self._input(_api.pointer_body("move", x, y))

    def click(self, x: int, y: int) -> None:
        self._input(_api.pointer_body("left_click", x, y))

    def right_click(self, x: int, y: int) -> None:
        self._input(_api.pointer_body("right_click", x, y))

    def middle_click(self, x: int, y: int) -> None:
        self._input(_api.pointer_body("middle_click", x, y))

    def double_click(self, x: int, y: int) -> None:
        self._input(_api.pointer_body("double_click", x, y))

    def scroll(self, x: int = 0, y: int = 0, *, direction: str = "down", amount: int = 3) -> None:
        """Scroll the wheel, first moving to ``(x, y)`` when either is non-zero."""
        self._input(_api.scroll_body(x, y, direction, amount))

    def type(self, text: str) -> None:
        """Type text as keystrokes.

        Characters with no key mapping are skipped rather than raising, so a
        stray emoji in a prompt cannot fail the whole call.
        """
        self._input(_api.type_body(text))

    def key(self, *keys: str) -> None:
        """Press a chord, e.g. ``key("ctrl", "c")`` or ``key("Return")``."""
        self._input(_api.key_body(keys))

    def exec(self, command: str, timeout_s: int = 30) -> ExecResult:
        """Run a shell command inside the guest.

        Uses the guest's native shell — bash on Linux, cmd.exe on Windows. A
        non-zero exit is returned, not raised; check :attr:`ExecResult.ok`.
        """
        data = self._t.json(
            "POST",
            _api.computer_action(self.id, "exec"),
            json=_api.exec_body(command, timeout_s),
        )
        return ExecResult.from_api(data or {})

    # --- snapshots ------------------------------------------------------

    def snapshot(self, *, memory: bool = False) -> Snapshot:
        """Capture a snapshot of this computer.

        Works while it is running. ``memory=True`` also captures live RAM and
        device state, so a restore or fork resumes exactly where it was instead
        of booting — the computer must be running for that.
        """
        data = self._t.json(
            "POST",
            _api.computer_action(self.id, "snapshots"),
            json=_api.snapshot_body(memory),
        )
        return Snapshot.from_api(data or {})

    def snapshots(self) -> list[Snapshot]:
        """This computer's snapshots, in the order the API returns them."""
        data = self._t.json("GET", _api.SNAPSHOTS) or []
        return [Snapshot.from_api(s) for s in data if s.get("computer_id") == self.id]

    def schedule(self) -> Mapping[str, Any]:
        """The automatic daily snapshot schedule."""
        return self._t.json("GET", _api.computer_action(self.id, "schedule")) or {}

    def set_schedule(
        self,
        *,
        enabled: bool,
        hour: int = 4,
        minute: int = 0,
        tz: str = "UTC",
    ) -> Mapping[str, Any]:
        """Set the automatic daily snapshot window, in the given IANA timezone."""
        self._t.request(
            "PUT",
            _api.computer_action(self.id, "schedule"),
            json=_api.schedule_body(enabled=enabled, hour=hour, minute=minute, tz=tz),
        )
        return self.schedule()
