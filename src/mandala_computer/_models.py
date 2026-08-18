"""Response objects.

Deliberately permissive: unknown fields are preserved in ``raw`` rather than
rejected, so a server that starts returning more does not break older clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ExecResult", "Snapshot", "Template", "VncConnect"]


@dataclass(frozen=True)
class VncConnect:
    """Everything needed to put a computer's live desktop on a page.

    Two credentials rather than one, and the difference is enforced by the
    platform rather than by the client asking politely:

    ``token``
        Full control — keyboard, pointer, clipboard. Root-equivalent on that one
        machine, so it belongs on a server or in a page you trust.
    ``view_token``
        Watch only. The daemon drops input on a socket opened with it, so a
        browser holding this one cannot type even from a patched client.

    Both are scoped to a single computer, and neither is the account API key —
    which is every computer on the account, forever, and must never reach a
    browser. Both end when the computer restarts.
    """

    #: Websocket URL carrying ``token``. Full control.
    url: str
    #: Websocket URL carrying ``view_token``. Watch only.
    view_url: str
    #: The credential inside :attr:`url`, for building your own noVNC URL.
    token: str
    #: The credential inside :attr:`view_url`.
    view_token: str
    #: The platform's hosted viewer, watch-only, for an ``<iframe>``. The
    #: credential is in the URL fragment, which browsers never send to a server —
    #: so it stays out of access logs and out of ``Referer`` on everything the
    #: page then loads.
    embed_url: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any] | None) -> VncConnect | None:
        """Build one, or ``None`` when the API did not supply a full set.

        Absent rather than partial is the platform's own rule: a URL built over
        a missing credential is a string indistinguishable from a working one
        that answers 401 forever. Anything short of both credentials is treated
        as no connect surface at all.
        """
        if not isinstance(d, Mapping):
            return None
        token = str(d.get("token") or "")
        view_token = str(d.get("view_token") or "")
        if not token or not view_token:
            return None
        return cls(
            url=str(d.get("url", "")),
            view_url=str(d.get("view_url", "")),
            token=token,
            view_token=view_token,
            embed_url=str(d.get("embed_url", "")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Template:
    name: str
    label: str
    os: str
    cpu: int
    ram_mb: int
    disk_gb: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Template:
        return cls(
            name=d.get("name", ""),
            label=d.get("label", ""),
            os=d.get("os", ""),
            cpu=int(d.get("cpu", 0)),
            ram_mb=int(d.get("ram_mb", 0)),
            disk_gb=int(d.get("disk_gb", 0)),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Size:
    """A named size: a template plus a CPU/RAM/disk shape, from ``GET /sizes``.

    These are the shapes the platform keeps pre-booted, so a create that passes
    ``id`` as ``size`` is typically answered from the warm pool in about a
    second where a custom shape boots cold.

    ``allowed`` is about the plan's per-computer ceilings only — what the
    account already holds is not counted, so a create at an allowed size can
    still be refused against the plan's pools. ``cheapest_plan`` is the plan to
    name when it is False, or ``None`` if no purchasable plan admits the row.
    """

    id: str
    label: str
    template: str
    cpu: int
    ram_mb: int
    disk_gb: int
    allowed: bool
    cheapest_plan: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Size:
        return cls(
            id=d.get("id", ""),
            label=d.get("label", ""),
            template=d.get("template", ""),
            cpu=int(d.get("cpu", 0)),
            ram_mb=int(d.get("ram_mb", 0)),
            disk_gb=int(d.get("disk_gb", 0)),
            allowed=bool(d.get("allowed", False)),
            cheapest_plan=d.get("cheapest_plan"),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Snapshot:
    id: str
    computer_id: str
    name: str
    kind: str
    state: str
    size_bytes: int
    created_at: str
    incremental: bool
    auto: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_memory(self) -> bool:
        """True for a live RAM+disk capture, which forks/restores without booting."""
        return self.kind == "memory"

    @property
    def is_durable(self) -> bool:
        """True once the snapshot has been replicated to backup storage."""
        return self.state == "durable"

    @property
    def is_scheduled(self) -> bool:
        """True if the scheduler took this, rather than a person.

        Also what makes it eligible for retention: snapshots you take yourself
        are never aged out automatically.
        """
        return self.auto

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Snapshot:
        return cls(
            id=d.get("id", ""),
            computer_id=d.get("computer_id", ""),
            name=d.get("name", ""),
            kind=d.get("kind", "disk"),
            state=d.get("state", ""),
            size_bytes=int(d.get("size_bytes", 0)),
            created_at=d.get("created_at", ""),
            incremental=bool(d.get("incremental", False)),
            auto=bool(d.get("auto", False)),
            raw=dict(d),
        )


@dataclass(frozen=True)
class ExecResult:
    """The outcome of a shell command run inside the guest."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    #: True when the guest agent stopped capturing stdout before the command
    #: stopped producing it. See :attr:`truncated`.
    out_truncated: bool = False
    #: The same for stderr.
    err_truncated: bool = False

    @property
    def ok(self) -> bool:
        """The command ran and exited zero.

        Deliberately says nothing about :attr:`truncated`: a command that
        succeeded and produced more output than the guest agent would carry is
        still a command that succeeded. Whether a short answer is acceptable
        depends on what you were going to do with it, so it is reported
        separately rather than folded in here.
        """
        return self.exit_code == 0 and not self.timed_out

    @property
    def truncated(self) -> bool:
        """True if either stream was cut short.

        The guest agent caps a command's captured output at 16 MiB. Past that it
        keeps running and keeps producing, and what comes back is the first
        16 MiB with no other sign that there was more — which is why this is
        worth checking before parsing the output of anything that could be
        large. Redirect to a file inside the guest and fetch it instead when it
        might be.
        """
        return self.out_truncated or self.err_truncated

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ExecResult:
        return cls(
            exit_code=int(d.get("exit_code", 0)),
            stdout=d.get("stdout", "") or "",
            stderr=d.get("stderr", "") or "",
            timed_out=bool(d.get("timed_out", False)),
            out_truncated=bool(d.get("out_truncated", False)),
            err_truncated=bool(d.get("err_truncated", False)),
        )
