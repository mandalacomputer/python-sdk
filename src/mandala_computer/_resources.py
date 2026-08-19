"""Resource collections hanging off the sync client."""

from __future__ import annotations

import builtins
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from . import _api
from ._client import Transport
from ._computer import Computer
from ._exceptions import MandalaError
from ._models import Listing, Size, Snapshot, Template

__all__ = ["Computers", "Sizes", "Snapshots", "Templates"]

EPHEMERAL_DOC = """Provision a computer for the duration of the block, then destroy it.

``create()`` deliberately does not do this. Deleting a computer destroys its
disk, so tying that to a ``with`` block is only safe when the block is
unambiguously the machine's whole lifetime — which is exactly what this method
declares and ``create()`` does not.

Cleanup runs even if the block raises, and does not displace the exception that
was on its way out: if the delete itself fails while the block is already
raising, the computer is reported with a warning rather than a second exception,
so what you catch is still your own error. That warning means a machine outlived
its block and is still billable.
"""


class Computers:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self, *, allow_partial: bool = False) -> Listing[Computer]:
        """Every computer on the account, or every one in the key's workspace.

        No ``vnc`` on these rows — fetch one computer to get its desktop
        credentials, or call :meth:`Computer.refresh` on a listed one.

        ``allow_partial`` accepts a listing the platform knows is short. Without
        it a hypervisor that cannot be reached makes this raise
        :class:`~mandala_computer.UnavailableError` rather than answering short,
        because a short list is not a smaller truth: it reads exactly like the
        missing computers were deleted, and the obvious next thing a script does
        with a computer that has disappeared is tidy up after it. With it, the
        returned :class:`~mandala_computer.Listing` says so —
        ``is_complete`` — and the rows that could not be read carry
        :attr:`Computer.unreachable` and nothing else.
        """
        data, incomplete = self._t.listing(
            _api.COMPUTERS, params=_api.partial_params(allow_partial)
        )
        return Listing.of([Computer(self._t, c) for c in data or []], incomplete)

    def get(self, computer_id: str) -> Computer:
        data = self._t.json("GET", _api.computer(computer_id))
        return Computer(self._t, _api.computer_payload(data))

    def create(
        self,
        *,
        name: str | None = None,
        size: str | None = None,
        template: str | None = None,
        cpu: int | None = None,
        ram_mb: int | None = None,
        disk_gb: int | None = None,
        start: bool = True,
        resolution: str | None = None,
    ) -> Computer:
        """Provision a computer.

        Anything omitted falls back to the template's defaults. Sizing is capped
        by the account's plan; exceeding a cap raises
        :class:`~mandala_computer.PlanLimitError` naming the limit.

        ``size`` is a named size from :meth:`Client.sizes` — a template and a
        CPU/RAM/disk shape together, and the shapes the platform keeps
        pre-booted, so naming one is the likeliest way to get a computer in
        about a second rather than a cold boot. It cannot be combined with
        ``template``, ``cpu``, ``ram_mb`` or ``disk_gb``; sending both raises
        :class:`ValueError` before any request is made.

        ``resolution`` is ``"WIDTHxHEIGHT"`` or ``"WIDTHxHEIGHTxDEPTH"`` and
        defaults to ``"1280x800x24"``. It is a create-time choice and only a
        create-time choice: the screen is part of the machine QEMU builds, so
        changing it needs a new one, and there is no method that resizes a
        computer's display. Pick it deliberately if a model is going to drive
        this desktop — computer-use accuracy is resolution-sensitive, and every
        coordinate the model produces is in this space.

        Returns as soon as the API does — the machine is starting, not ready.
        Follow with :meth:`Computer.wait_for_guest`.

        A create that builds a computer which then will not boot is *not* an
        error: it returns the computer, stopped, with
        :attr:`Computer.start_error` saying what went wrong. The machine exists
        and is billable either way, so it comes back rather than being thrown
        away with the exception — check ``start_error`` if it matters, and
        :meth:`Computer.start` may work on a second attempt.
        """
        body = _api.create_body(
            name=name,
            template=template,
            cpu=cpu,
            ram_mb=ram_mb,
            disk_gb=disk_gb,
            start=start,
            resolution=resolution,
            size=size,
        )
        data = self._t.json("POST", _api.COMPUTERS, json=body)
        return Computer(self._t, _api.computer_payload(data))

    @contextmanager
    def ephemeral(self, **kwargs: Any) -> Iterator[Computer]:
        computer = self.create(**kwargs)
        try:
            yield computer
        except BaseException:
            # Not a bare `finally`. A delete that fails here would replace the
            # exception on its way out, and the caller would see a
            # ConflictError about a snapshot in flight instead of the error
            # their own code raised. The machine is billable until it goes, so
            # a failed cleanup is still news — it is warned about rather than
            # raised, which says it without taking the exception's place.
            try:
                computer.delete()
            except MandalaError as cleanup_failed:
                warn_cleanup_failed(computer.id, cleanup_failed)
            raise
        else:
            computer.delete()

    ephemeral.__doc__ = EPHEMERAL_DOC


def warn_cleanup_failed(computer_id: str, error: MandalaError) -> None:
    """Report an ``ephemeral`` cleanup that did not happen, without raising.

    Shared by both halves. This runs only while another exception is already on
    its way out, so raising would take that exception's place — but a computer
    that outlived its block is billing, and silence about it is worse than a
    warning nobody reads.
    """
    warnings.warn(
        f"ephemeral: could not delete {computer_id}: {error}. "
        "It is still running and still billable.",
        stacklevel=2,
    )


class Snapshots:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(
        self, *, include_unfinished: bool = False, allow_partial: bool = False
    ) -> Listing[Snapshot]:
        """Every snapshot on the account that you can act on.

        Snapshots outlive the computers they came from, so this routinely
        contains rows whose :attr:`Snapshot.computer_id` resolves to nothing —
        those carry :attr:`Snapshot.orphaned`, and :meth:`clone` is the
        operation that still works on them.

        ``include_unfinished`` widens it to deletions that began and did not
        finish. Nothing can be restored or cloned from one, but they still hold
        objects and are still billed, so it is the flag for a question about
        storage rather than about what can be used.

        ``allow_partial`` is :meth:`Computers.list`'s, with the same warning.
        """
        data, incomplete = self._t.listing(
            _api.SNAPSHOTS,
            params=_api.snapshot_listing_params(
                include_unfinished=include_unfinished, allow_partial=allow_partial
            ),
        )
        return Listing.of([Snapshot.from_api(s) for s in data or []], incomplete)

    def restore(self, snapshot_id: str) -> None:
        """Roll a computer back to a snapshot, replacing its current disk."""
        self._t.request("POST", _api.snapshot_action(snapshot_id, "restore"))

    def clone(self, snapshot_id: str, name: str | None = None) -> Computer:
        """Create a new computer from a snapshot.

        Cloning a memory snapshot forks it: the new machine resumes from the
        captured RAM rather than booting, so it starts as a live twin of the
        original — same hostname and network identity until it is re-identified.

        Returns as soon as the computer exists, which is before its disk does.
        A snapshot has to be copied out — and a snapshot taken incrementally is
        collapsed out of its whole chain — which runs for minutes, so the
        computer comes back ``"building"``. Until that lands there is nothing to
        boot and starting it raises :class:`~mandala_computer.ConflictError`; wait
        with :meth:`Computer.wait_until_built`.
        """
        data = self._t.json(
            "POST", _api.snapshot_action(snapshot_id, "clone"), json=_api.name_body(name)
        )
        return Computer(self._t, _api.computer_payload(data))

    def delete(self, snapshot_id: str) -> None:
        self._t.request("DELETE", _api.snapshot(snapshot_id))


class Templates:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Template]:
        data = self._t.json("GET", _api.TEMPLATES) or []
        return [Template.from_api(t) for t in data]


class Sizes:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Size]:
        data = self._t.json("GET", _api.SIZES) or []
        return [Size.from_api(s) for s in data]
