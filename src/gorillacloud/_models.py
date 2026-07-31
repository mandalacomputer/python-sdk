"""Response objects.

Deliberately permissive: unknown fields are preserved in ``raw`` rather than
rejected, so a server that starts returning more does not break older clients.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["ExecResult", "Snapshot", "Template"]


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
class Snapshot:
    id: str
    computer_id: str
    name: str
    kind: str
    state: str
    size_bytes: int
    created_at: str
    incremental: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_memory(self) -> bool:
        """True for a live RAM+disk capture, which forks/restores without booting."""
        return self.kind == "memory"

    @property
    def is_durable(self) -> bool:
        """True once the snapshot has been replicated to backup storage."""
        return self.state == "durable"

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
            raw=dict(d),
        )


@dataclass(frozen=True)
class ExecResult:
    """The outcome of a shell command run inside the guest."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ExecResult:
        return cls(
            exit_code=int(d.get("exit_code", 0)),
            stdout=d.get("stdout", "") or "",
            stderr=d.get("stderr", "") or "",
            timed_out=bool(d.get("timed_out", False)),
        )
