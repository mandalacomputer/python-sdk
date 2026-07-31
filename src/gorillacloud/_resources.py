"""Resource collections hanging off the client."""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ._client import Transport
from ._computer import Computer
from ._models import Snapshot, Template

__all__ = ["Computers", "Snapshots", "Templates"]


class Computers:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Computer]:
        data = self._t.json("GET", "computers") or []
        return [Computer(self._t, c) for c in data]

    def get(self, computer_id: str) -> Computer:
        data = self._t.json("GET", f"computers/{computer_id}")
        return Computer(self._t, data or {})

    def create(
        self,
        *,
        name: str | None = None,
        template: str | None = None,
        cpu: int | None = None,
        ram_mb: int | None = None,
        disk_gb: int | None = None,
        start: bool = True,
    ) -> Computer:
        """Provision a computer.

        Anything omitted falls back to the template's defaults. Sizing is capped
        by the account's plan; exceeding a cap raises
        :class:`~gorillacloud.PlanLimitError` naming the limit.

        Returns as soon as the API does — the machine is starting, not ready.
        Follow with :meth:`Computer.wait_for_guest`.
        """
        body: dict[str, Any] = {"start": start}
        for k, v in (
            ("name", name),
            ("template", template),
            ("cpu", cpu),
            ("ram_mb", ram_mb),
            ("disk_gb", disk_gb),
        ):
            if v is not None:
                body[k] = v
        data = self._t.json("POST", "computers", json=body)
        return Computer(self._t, data or {})

    @contextmanager
    def ephemeral(self, **kwargs: Any) -> Iterator[Computer]:
        """Provision a computer for the duration of the block, then destroy it.

        ``create()`` deliberately does not do this. Deleting a computer destroys
        its disk, so tying that to a ``with`` block is only safe when the block
        is unambiguously the machine's whole lifetime — which is exactly what
        this method declares and ``create()`` does not.

            with client.computers.ephemeral(template="base") as c:
                c.wait_for_guest()
                c.type("hello")

        Cleanup runs even if the block raises.
        """
        computer = self.create(**kwargs)
        try:
            yield computer
        finally:
            computer.delete()


class Snapshots:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Snapshot]:
        data = self._t.json("GET", "snapshots") or []
        return [Snapshot.from_api(s) for s in data]

    def restore(self, snapshot_id: str) -> None:
        """Roll a computer back to a snapshot, replacing its current disk."""
        self._t.request("POST", f"snapshots/{snapshot_id}/restore")

    def clone(self, snapshot_id: str, name: str | None = None) -> Computer:
        """Create a new computer from a snapshot.

        Cloning a memory snapshot forks it: the new machine resumes from the
        captured RAM rather than booting, so it starts as a live twin of the
        original — same hostname and network identity until it is re-identified.
        """
        body: dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        data = self._t.json("POST", f"snapshots/{snapshot_id}/clone", json=body)
        return Computer(self._t, data or {})

    def delete(self, snapshot_id: str) -> None:
        self._t.request("DELETE", f"snapshots/{snapshot_id}")


class Templates:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Template]:
        data = self._t.json("GET", "templates") or []
        return [Template.from_api(t) for t in data]
