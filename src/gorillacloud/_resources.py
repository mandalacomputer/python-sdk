"""Resource collections hanging off the sync client."""

from __future__ import annotations

import builtins
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import _api
from ._client import Transport
from ._computer import Computer
from ._models import Snapshot, Template

__all__ = ["Computers", "Snapshots", "Templates"]

EPHEMERAL_DOC = """Provision a computer for the duration of the block, then destroy it.

``create()`` deliberately does not do this. Deleting a computer destroys its
disk, so tying that to a ``with`` block is only safe when the block is
unambiguously the machine's whole lifetime — which is exactly what this method
declares and ``create()`` does not.

Cleanup runs even if the block raises.
"""


class Computers:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Computer]:
        data = self._t.json("GET", _api.COMPUTERS) or []
        return [Computer(self._t, c) for c in data]

    def get(self, computer_id: str) -> Computer:
        data = self._t.json("GET", _api.computer(computer_id))
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
        body = _api.create_body(
            name=name, template=template, cpu=cpu, ram_mb=ram_mb, disk_gb=disk_gb, start=start
        )
        data = self._t.json("POST", _api.COMPUTERS, json=body)
        return Computer(self._t, data or {})

    @contextmanager
    def ephemeral(self, **kwargs: Any) -> Iterator[Computer]:
        computer = self.create(**kwargs)
        try:
            yield computer
        finally:
            computer.delete()

    ephemeral.__doc__ = EPHEMERAL_DOC


class Snapshots:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Snapshot]:
        data = self._t.json("GET", _api.SNAPSHOTS) or []
        return [Snapshot.from_api(s) for s in data]

    def restore(self, snapshot_id: str) -> None:
        """Roll a computer back to a snapshot, replacing its current disk."""
        self._t.request("POST", _api.snapshot_action(snapshot_id, "restore"))

    def clone(self, snapshot_id: str, name: str | None = None) -> Computer:
        """Create a new computer from a snapshot.

        Cloning a memory snapshot forks it: the new machine resumes from the
        captured RAM rather than booting, so it starts as a live twin of the
        original — same hostname and network identity until it is re-identified.
        """
        data = self._t.json(
            "POST", _api.snapshot_action(snapshot_id, "clone"), json=_api.name_body(name)
        )
        return Computer(self._t, data or {})

    def delete(self, snapshot_id: str) -> None:
        self._t.request("DELETE", _api.snapshot(snapshot_id))


class Templates:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Template]:
        data = self._t.json("GET", _api.TEMPLATES) or []
        return [Template.from_api(t) for t in data]
