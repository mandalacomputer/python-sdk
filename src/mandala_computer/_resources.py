"""Resource collections hanging off the sync client."""

from __future__ import annotations

import builtins
import time
import warnings
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from datetime import datetime
from enum import Enum, auto
from typing import Any

from . import _api
from ._client import SNAPSHOT_DELETE_TIMEOUT, SNAPSHOT_POLL, Transport
from ._computer import Computer, _poll_delay, _ride_out, check_wait_args
from ._exceptions import (
    MandalaError,
    TimeoutError,
    _is_transient_for_poll,
)
from ._models import (
    BuildProgress,
    Listing,
    Move,
    PublishedTemplate,
    Retention,
    RetiredTemplates,
    Size,
    Snapshot,
    Template,
    TemplateBuild,
    TemplateCheck,
    UsageReport,
    Webhook,
    WebhookCreated,
    WebhookDelivery,
    build_contradiction,
    move_rows,
)
from ._sse import SSEEvent

__all__ = [
    "Builds",
    "Computers",
    "Moves",
    "Sizes",
    "Snapshots",
    "Templates",
    "Usage",
    "Webhooks",
]


class _LastPoll(Enum):
    """What became of the most recent poll, in three states rather than two.

    ``observed: bool`` forced a claim the SDK cannot make (adversarial review,
    OPL-3835). A timeout that is CONSISTENT with this wait's own cap is not
    proof it fired: a caller-supplied transport can raise ``ReadTimeout``
    itself, and httpcore maps a phase's ``socket.timeout`` — which IS the
    builtin ``TimeoutError`` — onto the same named class. Saying "was still
    running" there asserts the fleet is fine on the strength of a stopwatch.
    """

    #: It answered. Whatever it said is current.
    ANSWERED = auto()
    #: It demonstrably did not answer — a refusal, a dropped connection, or a
    #: timeout this wait's cap cannot have caused.
    FAILED = auto()
    #: A timeout consistent with this wait's own deadline and with a transport
    #: failure alike. Neither side can be blamed, so the message blames neither.
    UNRESOLVED = auto()


def classify_poll_failure(
    err: BaseException, started: float, budget: float, ceiling: float | None
) -> _LastPoll:
    """Whether a failed poll says anything about the PLATFORM.

    ``ceiling`` is the client's own limit for the phase that timed out, or
    ``None`` where the phase cannot be named: "tighter" is a question about ONE
    phase, and a connect stall measured against a read timeout got it wrong in
    both directions (adversarial review, OPL-3835).

    Three things have to hold before a timeout is even a CANDIDATE for this
    wait's own cap: the phase must be nameable, the cap must have been the
    tighter of the two for that phase, and the request must have spent
    substantially all of the budget it was given — a timeout arriving at once
    exhausted nothing. Even then the answer is UNRESOLVED rather than "ours",
    because none of it rules out a transport raising the same class itself.
    """
    if not isinstance(err, TimeoutError):
        return _LastPoll.FAILED
    if ceiling is None or budget >= ceiling:
        return _LastPoll.FAILED
    if time.monotonic() - started < budget * 0.9:
        return _LastPoll.FAILED
    return _LastPoll.UNRESOLVED


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

    def list(self, *, allow_partial: bool = False, state: str | None = None) -> Listing[Computer]:
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
        :attr:`Computer.unreachable` and the identity the platform has on
        record, with nothing on them their host alone would know.

        ``state`` narrows to one :attr:`Computer.state`. Without it the listing
        is every computer that exists or may exist — ``live``, ``unreachable``
        and ``deleting``. ``deleted`` and ``lost`` are terminal, are answered
        from the platform's record alone since no host has them to list, and
        this is the ONLY way they are ever shown: a delete that was answered and
        a computer written off with its host are both invisible to the plain
        listing, which is why "it is not in the list" has never been the same
        statement as "it was deleted".

        ``state="unreachable"`` still needs ``allow_partial=True`` beside it.
        The platform marks any listing holding one of those rows short, and this
        method fails closed on that mark whatever was asked for — so the one
        narrowing that looks like it should not need the flag is the one that
        cannot do without it. The two terminal states need nothing: they are
        answered from the record alone and no host was asked, so there is no
        outage to acknowledge.
        """
        data, incomplete = self._t.listing(
            _api.COMPUTERS,
            params=_api.computer_listing_params(allow_partial=allow_partial, state=state),
        )
        return Listing.of([Computer(self._t, c) for c in data or []], incomplete)

    def get(self, computer_id: str) -> Computer:
        data = self._t.json_object("GET", _api.computer(computer_id))
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
        data = self._t.json_object("POST", _api.COMPUTERS, json=body)
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
            except Exception as cleanup_failed:  # noqa: BLE001
                # Every failure, not just MandalaError: a caller-supplied
                # transport or an unexpected local failure can still raise its
                # own exception. Nothing here is worth more than the exception
                # already going out, so nothing here may displace it.
                warn_cleanup_failed(computer.id, cleanup_failed)
            raise
        else:
            computer.delete()

    ephemeral.__doc__ = EPHEMERAL_DOC


def warn_cleanup_failed(computer_id: str, error: Exception) -> None:
    """Report an ``ephemeral`` cleanup that did not happen, without raising.

    Shared by both halves. This runs only while another exception is already on
    its way out, so raising would take that exception's place — but a computer
    that outlived its block is billing, and silence about it is worse than a
    warning nobody reads.
    """
    warnings.warn(
        f"ephemeral: could not delete {computer_id}: {error}. "
        "It is still running and still billable.",
        # warn_cleanup_failed -> generator context manager -> contextlib -> user.
        stacklevel=4,
    )


#: The state a deletion writes onto the row once it has detached the dependents.
#:
#: Left out of a bare listing, because a half-deleted snapshot is not one
#: anything can be restored or cloned from — which is the whole reason the wait
#: for a deletion polls with ``include=unfinished`` and the wait for a capture
#: does not. A poll that asked the plain listing would read a row that had got
#: this far as a row that had gone, and call a stalled deletion a finished one.
DELETING = "deleting"


def still_listed(rows: Sequence[Mapping[str, Any]], snapshot_id: str) -> Snapshot | None:
    """The row of the snapshot being deleted, while there still is one.

    ``None`` is the answer the wait is looking for: THE DELETION HAS NO STATE
    THAT MEANS DELETED, so the row leaving the listing is the deletion having
    finished, and that is the only thing that says so (platform OPL-4572).

    The exact opposite reading to :meth:`ComputerFields._captured`, on the same
    listing and the same id, which is why the two are written separately rather
    than shared: a capture that fails drops its row, so an absence there is a
    failure, and a deletion that succeeds drops its row, so an absence here is
    the success. Getting one of those backwards is silent in both directions.

    A SHORT LISTING MUST NEVER REACH THIS. A row missing because nobody could
    look is indistinguishable here from a row that has gone, so the caller is
    the one that decides: the poll asks without ``allow_partial``, which buys a
    503 it rides out, AND it refuses to read an absence off any answer carrying
    ``X-GC-Incomplete``. The second half is not redundant — the first is a
    promise the public API makes, the transport reads that header off any 200 it
    is handed, and the platform's own rule is that a reader tests for its
    presence (Codex adversarial review, OPL-4576).
    """
    for row in rows:
        if row.get("id") == snapshot_id:
            return Snapshot.from_api(row)
    return None


def deletion_timed_out(
    snapshot_id: str, timeout: float, state: str | None, *, short: bool = False
) -> str:
    """A row that outlasted the wait, said as the different things it can be.

    None of them is "the deletion timed out": nothing about the deletion stops
    when this wait does, and the remedy differs by which state the row was left
    in — which is why the state is in the sentence rather than only the id.

    ``state`` is the LAST STATE A POLL ANSWERED WITH, and the message says so
    rather than asserting what the row reads now: the poll that ended this wait
    may have been one that failed, and a state carried over from before it is
    evidence rather than a current fact — the distinction ``Builds.wait`` needed
    three states of its own to make (OPL-3835).

    It is ``None`` when no poll ever answered, which a ``timeout`` too small to
    make one request in produces. Saying nothing about how far the deletion got
    is the honest answer there; guessing a state would put a remedy in front of
    a caller that this wait has no evidence for.

    ``short`` is the same admission about a different failure: the last listing
    came back MARKED INCOMPLETE, so this wait was never able to ask its question
    at all — see :func:`still_listed`. It is neither of the two stalls, and it
    is not evidence that anything is stuck.
    """
    if short:
        return (
            f"{snapshot_id} could not be confirmed deleted within {timeout:g}s: the "
            "snapshot listing came back marked incomplete — a hypervisor could not be "
            "reached — and a short listing cannot say a row is gone, only that nobody "
            "looked. Nothing here says the deletion failed; retry once the fleet "
            "answers whole"
        )
    if state is None:
        return (
            f"{snapshot_id} was still being deleted after {timeout:g}s, and this wait "
            "ended without reading the listing, so it cannot say how far the deletion "
            "got. It has not stopped — only this wait has. Poll "
            "snapshots.list(include_unfinished=True) for the id: it is deleted when "
            "the row is gone"
        )
    if state == DELETING:
        return (
            f"{snapshot_id} was still listed after {timeout:g}s, last seen in state "
            "'deleting' (the dependents are detached and the stored objects are still going; "
            "the deletion has not stopped, only this wait has). The platform "
            "retries a deletion that stalled about every fifteen minutes, and "
            "deleting the id again asks for the same work by hand — a stalled "
            "deletion is picked up rather than refused"
        )
    return (
        f"{snapshot_id} was still listed after {timeout:g}s, last seen in state "
        f"{state or 'unknown'!r} — the state it had before the delete, so the deletion "
        "never reached the point where it marks the row: it is either still detaching "
        "dependent snapshots or it stopped before it could, which is what a dependent "
        "that is ITSELF being deleted does to it. The snapshot is intact either way, "
        "and this stall is not on the platform's own sweep, which looks for 'deleting' "
        "— so delete the id again, once any dependent's deletion has finished"
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

        CAPTURES IN FLIGHT ARE LISTED TOO, and they are not snapshots yet: a
        row in state ``capturing`` is a placeholder that restore, clone and
        delete all 404 on. Its id is nonetheless the id the snapshot will keep,
        which is what makes this listing the thing to poll after
        :meth:`Computer.snapshot` — see :attr:`Snapshot.is_capturing`. Check
        ``state`` on every row rather than on the newest one: this is one answer
        per host concatenated in a fixed host order, so it carries no
        account-wide ordering to read anything from.

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
        data = self._t.json_object(
            "POST", _api.snapshot_action(snapshot_id, "clone"), json=_api.name_body(name)
        )
        return Computer(self._t, _api.computer_payload(data))

    def delete(
        self,
        snapshot_id: str,
        *,
        wait: bool = True,
        timeout: float = SNAPSHOT_DELETE_TIMEOUT,
        poll: float = SNAPSHOT_POLL,
    ) -> None:
        """Destroy a snapshot, and wait for it to be gone.

        Later snapshots in the same chain are unaffected: a deletion detaches
        every dependent from this one before anything is removed, so deleting a
        link does not cost you the snapshots that were built on it.

        THE REQUEST NO LONGER WAITS FOR THE DELETION; this method does. ``DELETE
        snapshots/:id`` answers **202** with the snapshot's row the moment the
        deletion is accepted and destroys it afterwards (platform OPL-4572).
        Detaching those dependents, committing the index and walking both the
        local files and the bucket objects scales with the chain and with how
        much is stored — the same length that took the capture off its request
        in OPL-4562, and longer than an HTTP request survives.

        WHAT IS POLLED IS THE ROW'S ABSENCE. There is no state that means
        deleted: the snapshot no longer being listed is the deletion having
        finished, and that is the only thing that says so. The listing is read
        with ``include=unfinished``, because a deletion that has detached the
        dependents marks the row ``deleting`` and a bare listing leaves those
        out — polling without it would read a stalled deletion as a finished
        one.

        A ROW THAT STAYS IS ONE THAT STALLED, which is the opposite polarity to
        a capture: a capture that fails leaves no row at all, and a deletion
        that succeeds is what removes one. So this raises
        :class:`~mandala_computer.TimeoutError` naming the state the row was
        left in, rather than reporting a deletion that failed — the platform
        retries a ``deleting`` row on its own sweep, about every fifteen
        minutes, and sending the delete again picks one up by hand.

        ``wait=False`` returns as soon as the 202 lands, for a caller who would
        rather hold the id and poll on their own schedule — the shape
        :meth:`Computer.snapshot` has::

            client.snapshots.delete(snap.id, wait=False)
            gone = not any(
                s.id == snap.id
                for s in client.snapshots.list(include_unfinished=True)
            )

        EVERY REFUSAL IS STILL SYNCHRONOUS and still carries the status it did
        before — 404 for no such snapshot,
        :class:`~mandala_computer.ConflictError` for a capture reading through
        it, for a clone or a migration holding it, and for a deletion of this id
        already running. A 202 means the deletion started.

        ONE CONFLICT ARRIVES AFTER THE 202 and cannot be raised here: a
        dependent that is itself being deleted cannot be detached, so a delete
        that meets one fails once the work starts, having destroyed nothing.
        That is a row left in its ordinary state, which is what the timeout
        message distinguishes. Deleting a chain one link at a time — waiting for
        each row to go before starting the next, which is what this method does
        by default — never meets it.

        A SECOND DELETE IS NOT FATAL. While the first is working it is a
        ``ConflictError`` saying the snapshot is already being deleted, which is
        an answer about progress rather than a fault; against a row whose
        deletion stalled it is accepted and finishes the job.

        ``timeout`` and ``poll`` are checked before anything is deleted, and
        whether or not ``wait`` is going to use them — see
        :func:`check_wait_args`.

        Returns nothing under either ``wait``. The 202 carries the row, and the
        row is the thing that is on its way out: handing it back would be
        offering a record of a snapshot as the answer to destroying it, and the
        id it holds is the one that was passed in.
        """
        # BEFORE the DELETE, for the reason the capture gives: a number this
        # refuses is a mistake in the call, and finding it after a deletion has
        # started is finding it too late to be worth anything.
        check_wait_args(timeout, poll)
        self._t.request("DELETE", _api.snapshot(snapshot_id))
        if not wait:
            return
        self._await_deletion(snapshot_id, timeout, poll)

    def _await_deletion(self, snapshot_id: str, timeout: float, poll: float) -> None:
        """Poll the account's snapshots until this id stops being listed.

        The listing rather than a read of the snapshot, because a read is the
        one thing that cannot answer this: ``GET /snapshots/:id`` hides a row
        marked ``deleting``, so it would report the deletion as finished at the
        moment it committed its intent rather than at the moment it finished the
        work.

        A platform predating the 202 needs nothing special here, unlike the
        capture: it did the whole deletion inside the request, so the first poll
        finds no row and this returns after one listing.
        """
        deadline = time.monotonic() + timeout
        last: str | None = None
        short = False
        while True:
            # The deadline before the poll, which is `_await_capture`'s shape
            # and `Builds.wait`'s: an already-spent budget has no request worth
            # making.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(deletion_timed_out(snapshot_id, timeout, last, short=short))
            try:
                rows, incomplete = self._t.listing(
                    _api.SNAPSHOTS,
                    params=_api.snapshot_listing_params(
                        include_unfinished=True, allow_partial=False
                    ),
                    timeout_cap=remaining,
                )
            except MandalaError as err:
                # A deletion is minutes of one host's storage, and a hypervisor
                # briefly out of reach during it is ordinary (OPL-3724). It
                # matters more here than in any other wait: this is the loop
                # where a failure to read is one keystroke away from being read
                # as the row having gone.
                time.sleep(_ride_out(err, deadline, poll))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        deletion_timed_out(snapshot_id, timeout, last, short=short)
                    ) from err
                continue
            # THE MARK, not the flag that asks for one. `allow_partial=False`
            # buys the 503 the public API answers a short listing with, and that
            # is a promise made by one deployment rather than a property of this
            # loop: the transport reads `X-GC-Incomplete` off any 200 it is
            # handed, the platform's own rule is that a reader tests for the
            # header's PRESENCE, and a listing that carried it would be read
            # here as the row having gone — the one wrong answer this wait must
            # never give (Codex adversarial review, OPL-4576).
            row = still_listed(rows, snapshot_id)
            # Only the ABSENCE is unreadable. A row that is there is a fact
            # whatever else the answer was short by, so `short` is exactly "this
            # poll could not answer the question" rather than "this listing was
            # imperfect".
            short = row is None and incomplete is not None
            if row is None and not short:
                return
            # Carried out of the loop so the timeout can say which of the two
            # stalls this was, and read from the LAST poll rather than the
            # first: the row starts in the state it had and moves to `deleting`
            # once the dependents are off it.
            if row is not None:
                last = row.state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(deletion_timed_out(snapshot_id, timeout, last, short=short))
            time.sleep(min(poll, remaining))

    def retention(self) -> Retention:
        """How long the automatic ones are kept — your plan's retention window.

        The other half of :meth:`~mandala_computer.Computer.set_schedule`, which
        decides when snapshots are TAKEN and deliberately has no field for how
        long they survive. Without this a caller setting a daily schedule had to
        hardcode a number per plan tier or infer one by watching ``auto``
        snapshots disappear.

        On this collection rather than on a :class:`~mandala_computer.Computer`
        because the window belongs to the ACCOUNT — every computer you own is
        aged out on the same one, though each keeps its own set, so two
        computers on ``7/4/12`` keep up to twenty-three snapshots each rather
        than twenty-three between them.

        Read-only, and there is no write anywhere: the plan owns retention, so
        setting it would be granting yourself history you have not paid for. It
        changes when the subscription does. See :class:`Retention` for what the
        three numbers select.
        """
        return Retention.from_api(self._t.json_object("GET", _api.RETENTION))


class Templates:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Template]:
        data = self._t.json_array("GET", _api.TEMPLATES)
        return [Template.from_api(t) for t in data]

    def schema(self) -> Mapping[str, Any]:
        """The JSON Schema for a ``mandala/v1`` document.

        Returned as it arrives rather than wrapped in a type, because it is a
        schema: what a caller does with it is point an editor or a validator at
        it, and a shape of our own over the top would be a second, worse
        description of the same thing. Its ``$id`` is the URL it came from, so a
        ``$ref`` to it resolves.
        """
        return self._t.json_object("GET", _api.TEMPLATE_SCHEMA)

    def validate(self, document: str) -> TemplateCheck:
        """Check a document without publishing it.

        Side-effect free and claims no ref, so it is safe on a draft and safe to
        call repeatedly. Worth doing while iterating: a document that is wrong
        comes back with EVERY problem at once, where :meth:`publish` reports the
        first thing that stops it.

        Does not raise for an invalid document. That is not leniency — an
        invalid document is the answer to the question this method asks, and the
        platform says so with a 200. Read
        :attr:`~mandala_computer.TemplateCheck.valid`.

        The document goes as raw bytes, JSON or YAML, exactly as written. There
        is no envelope to build and none to get wrong.

        On a valid document, read
        :attr:`~mandala_computer.TemplateCheck.build_digest_needs` whenever
        :attr:`~mandala_computer.TemplateCheck.build_digest` is ``None``: the
        platform sends the two as alternatives, and a document naming a parent
        gets the sentence rather than the digest.
        """
        data = self._t.json_object(
            "POST", _api.TEMPLATE_VALIDATE, content=_api.template_document(document)
        )
        return TemplateCheck.from_api(data)

    def publish(self, document: str) -> PublishedTemplate:
        """Store a document under a ref of your own, so a create can launch it.

        THE NAMESPACE IS YOUR ACCOUNT. ``metadata.namespace`` has to be your
        account id — anything else is a
        :class:`~mandala_computer.PermissionDeniedError`, ``system`` included —
        and this SDK does not rewrite it, because silently relocating somebody's
        document would publish a ref that is not the one in the file they
        submitted.

        A REF IS IMMUTABLE. Publishing the identical document again succeeds and
        changes nothing, so a pipeline that republishes on every commit is safe.
        Publishing a DIFFERENT document under the same ref is a
        :class:`~mandala_computer.ConflictError`, and the fix is to bump
        ``metadata.version``. What counts as different is the digest, so a
        changed label is a change.

        A ref you have RETIRED stays spoken for and cannot be republished,
        identical bytes included. See :meth:`retire`.
        """
        data = self._t.json_object("POST", _api.TEMPLATES, content=_api.template_document(document))
        return PublishedTemplate.from_api(data)

    def get(self, namespace: str, name: str, *, version: str | None = None) -> PublishedTemplate:
        """Read one template back, as the document it was written as.

        Works for your own namespace and for ``system``, so you can see what you
        are layering onto. Another account's namespace is a
        :class:`~mandala_computer.NotFoundError`, the same answer a name that
        does not exist gets.

        Without ``version`` this is the newest published version of that name —
        which is also what a create naming the unpinned ``namespace/name``
        resolves to. :attr:`~mandala_computer.PublishedTemplate.versions` lists
        the rest.

        A ref you retired is a :class:`~mandala_computer.NotFoundError` whose
        message names the date it went, rather than claiming the template never
        existed. Read the message before concluding you mistyped something.
        """
        data = self._t.json_object(
            "GET",
            _api.template_ref(namespace, name),
            params=_api.template_version_params(version),
        )
        return PublishedTemplate.from_api(data)

    def retire(self, namespace: str, name: str, *, version: str | None = None) -> RetiredTemplates:
        """Retire a template you published, so it stops resolving and stops
        counting against your ceiling.

        WITH ``version`` this retires that one version. WITHOUT it, this retires
        EVERY version of the name — which is what "retire this template" means,
        and is deliberately not :meth:`get`'s "the newest": a delete that
        quietly took the latest one would let a loop walk backwards through a
        history it never asked about. An empty string is refused here rather
        than sent, for the same reason.

        COMPUTERS ARE NOT AFFECTED. A computer is built from the IMAGE the ref
        resolved to and holds no reference to the document, so anything already
        running, stopped or suspended is untouched. What a retire breaks is
        resolution: a NEW create naming the ref is refused.

        THE REF IS STILL SPOKEN FOR, AND STILL COUNTS ONCE. Publishing it again
        afterwards is a :class:`~mandala_computer.ConflictError`, identical
        bytes included, and
        :attr:`~mandala_computer.RetiredTemplates.refs_claimed` does not go
        down. Publish the next version instead.
        """
        data = self._t.json_object(
            "DELETE",
            _api.template_ref(namespace, name),
            params=_api.template_version_params(version),
        )
        return RetiredTemplates.from_api(data)


def _wait_timed_out(
    build_id: str, timeout: float, last: BuildProgress | None, poll: _LastPoll
) -> str:
    """What a build wait says when it gives up.

    Four sentences rather than one, because the situations differ in what the
    caller should do next: a build seen running, a wait whose last poll cannot
    be attributed to either side, a build that stopped answering, and one that
    never answered at all. Shared by the sync and async waits so they cannot
    word it differently.

    The middle one is the correction (adversarial review, OPL-3835). It used to
    be folded into the first, which asserted the fleet was fine on the strength
    of a stopwatch — a timeout consistent with this wait's own cap is not proof
    that the cap is what fired.
    """
    if last is not None and poll is _LastPoll.ANSWERED:
        return (
            f"build {build_id} was still running after {timeout:g}s "
            f"(phase {last.phase}, step {last.step} of {last.of}; "
            "the build has not stopped, only this wait has)"
        )
    if last is not None and poll is _LastPoll.UNRESOLVED:
        return (
            f"the {timeout:g}s wait on build {build_id} ran out with its last poll still "
            f"outstanding, so whether the fleet was answering cannot be told from here; "
            f"when it last answered it was in phase {last.phase}, step {last.step} of "
            f"{last.of}. The build has not stopped, only this wait has — read progress() "
            "for where it got to."
        )
    if last is not None:
        return (
            f"build {build_id} could not be reached for the last part of {timeout:g}s; "
            f"when it last answered it was in phase {last.phase}, step {last.step} of "
            f"{last.of}. The build has not stopped, only this wait has — read progress() "
            "for where it got to."
        )
    return f"build {build_id} could not be observed within {timeout:g}s: every poll failed"


class Builds:
    """Compiling template documents into images.

    Its own collection rather than methods on :class:`Templates`, because a
    build is not a property of a published template: ``POST /builds`` takes a
    DOCUMENT, not a ref, and the job it answers with outlives the request and is
    read back by its own id. Publishing and building are separate acts with very
    different costs, and the platform keeps them apart for that reason.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def start(self, document: str, *, no_reuse: bool = False) -> TemplateBuild:
        """Compile a document into a golden image, and return with a job.

        A build takes minutes — an agent image is roughly fifteen — so this
        never blocks. :meth:`wait` is what watches one, :meth:`progress` is the
        poll and :meth:`events` is the stream.

        THE NAMESPACE AND THE FAMILY BOTH HAVE TO BE YOURS, and either one is a
        :class:`~mandala_computer.PermissionDeniedError`. ``spec.family`` is what
        the built image is CALLED on a hypervisor, in a directory shared with
        every computer on that machine, so a build may only write into
        ``golden-<your account id>`` or that and a ``-`` and a name of your
        choosing.

        A :class:`~mandala_computer.ConflictError` means a hypervisor is busy —
        one build runs per host at a time — rather than that anything is wrong
        with the document, and is worth retrying.

        ``no_reuse`` builds again even when an image already carries this
        document's build digest. Identical documents normally share an image,
        which is what makes a repeated build cheap.
        """
        data = self._t.json_object(
            "POST",
            _api.BUILDS,
            content=_api.template_document(document),
            params=_api.build_params(no_reuse),
        )
        return TemplateBuild.from_api(data)

    def list(self, *, allow_partial: bool = False) -> Listing[TemplateBuild]:
        """Every build the fleet still holds a record of, newest first.

        A build lives on the hypervisor that ran it, so this is a fan-out — and
        like every other fan-out on this surface it FAILS CLOSED. A response
        carrying ``X-GC-Incomplete`` becomes a 503, so a hypervisor being away
        arrives as an :class:`~mandala_computer.UnavailableError` rather than as
        a short list.

        ``allow_partial`` is the way through, and it is
        :meth:`Computers.list`'s with one difference that matters. The platform
        honoured it here all along and did not DOCUMENT it until OPL-3840, which
        is why this method did not send it and why the text here used to say
        there was no way through at all — a build listing was strictly less
        available than a computer listing for no reason anybody decided.

        The difference is what a short answer LOOKS like. A short build listing
        appends nothing at all, because the platform keeps no record of which
        hypervisor ran which build: the missing builds are simply absent,
        ``incomplete`` is ``0`` rather than a count, and the
        :class:`~mandala_computer.Listing` saying ``is_complete`` is false is the
        only evidence there is.

        A short computer or snapshot listing appends one marked row per thing it
        could not reach — but only for a key that spans the account. A key
        scoped to one workspace gets no marked rows from any of the three, since
        the ids would come out of a placement cache with no workspace column.
        On such a key the Listing is the only evidence everywhere.
        """
        data, incomplete = self._t.listing(_api.BUILDS, params=_api.partial_params(allow_partial))
        return Listing.of([TemplateBuild.from_api(b) for b in data or []], incomplete)

    def get(self, build_id: str) -> TemplateBuild:
        """What became of one build. ``error`` says why a failed one failed."""
        return TemplateBuild.from_api(self._t.json_object("GET", _api.build(build_id)))

    def progress(self, build_id: str, *, timeout_cap: float | None = None) -> BuildProgress:
        """What a build is DOING, as against what became of it.

        The polling half; :meth:`events` is the same record as a stream. Use
        this for anything that reconnects, restarts, or cannot hold a socket
        open. It stays readable after the build has finished, so a program that
        was not attached at the time can still see which step failed.
        """
        data = self._t.json_object(
            "GET", _api.build_action(build_id, "progress"), timeout_cap=timeout_cap
        )
        return BuildProgress.from_api(data)

    def events(self, build_id: str) -> Iterator[BuildProgress]:
        """The same record as :meth:`progress`, as an event stream.

        Yields every ``progress`` and the final ``done``. A ``progress`` is sent
        only when something actually moved, so every one of them is news; the
        ``done`` is the last event of a build that finished, INCLUDING one that
        failed — a failed build is a ``done`` whose ``status`` says ``failed``,
        not an ``error`` event.

        An ``error`` event means the STREAM could not go on and says nothing
        about the build; it is raised, because a caller who kept reading would be
        told nothing more and a build they still care about needs
        :meth:`progress`. Attaching to a build that has already finished is not
        an error — one ``progress`` and one ``done`` arrive immediately.

        An account may hold eight of these open at once; the ninth is a
        :class:`~mandala_computer.RateLimitError`.


        TO STOP READING EARLY, CLOSE THE ITERATOR. A bare ``break`` leaves this
        generator suspended at its yield, and nothing inside it unwinds until it
        is closed or collected — so the stream stays checked out, and an account
        holds only eight at once (adversarial review, OPL-3835). The ``closing``
        wrapper inside cannot help with that: it runs when this generator ends,
        which a ``break`` does not do. :func:`contextlib.closing` around the call
        makes it concise, the same way :meth:`Computer.agent_stream` says::

            with closing(client.builds.events(build_id)) as stream:
                for progress in stream:
                    if progress.step == 3:
                        break

        A ``done`` that disagrees with itself — the event that ends the stream,
        carrying a payload that says the build is still running — is a truncated
        stream and raises rather than ending the iteration. See
        :attr:`~mandala_computer.BuildProgress.done`.
        """
        # Closed explicitly, the way `Computer.agent_stream` closes its own
        # stream and for the same reason (/code-review, OPL-3835). Every exit
        # from this loop abandons the inner generator rather than finishing it:
        # the NORMAL one is a `return` on the done event, and the three failures
        # are raises. Left bare, the `with self._http.stream(...)` inside
        # Transport.sse unwinds whenever the collector reaches it, and an
        # account may hold only eight of these open at once — the ninth is a
        # RateLimitError. A leaked stream costs a real slot.
        with closing(self._t.sse("GET", _api.build_action(build_id, "events"))) as stream:
            yield from self._events(build_id, stream)

    def _events(self, build_id: str, stream: Iterator[SSEEvent]) -> Iterator[BuildProgress]:
        for event in stream:
            if event.event == "error":
                raise MandalaError(_api.build_stream_failed(build_id, event.data))
            if event.event not in ("progress", "done"):
                continue
            if not isinstance(event.data, Mapping):
                # A malformed ``done`` is the end of the stream with the answer
                # missing, and skipping it left this waiting on a connection the
                # platform had finished with. A malformed ``progress`` is
                # different — it is news rather than an answer, so it is skipped
                # and the next one is read.
                if event.event == "done":
                    raise MandalaError(_api.build_stream_truncated(build_id, malformed=True))
                continue
            progress = BuildProgress.from_api(event.data)
            contradiction = build_contradiction(progress)
            if contradiction is not None:
                # A record that disagrees with itself, on either event. Raised
                # rather than yielded: a caller cannot act on a build that is
                # both finished and running, and the TypeScript SDK refuses the
                # same shape (OPL-3835).
                raise MandalaError(contradiction)
            if event.event == "done" and not progress.done:
                # A ``done`` whose payload says the build is still running is the
                # malformed case too — see BuildProgress.done. Raised BEFORE the
                # yield, so a caller cannot act on it as progress and then be
                # told the stream was never valid.
                raise MandalaError(_api.build_stream_truncated(build_id, malformed=True))
            yield progress
            if event.event == "done":
                return
        # The stream ended without saying so. Returning here is indistinguishable
        # from finishing, so a caller looping over this would report a build it
        # stopped watching as a build that ended.
        raise MandalaError(_api.build_stream_truncated(build_id, malformed=False))

    def wait(self, build_id: str, timeout: float = 1800.0, poll: float = 5.0) -> BuildProgress:
        """Block until a build stops running, and answer where it got to.

        Polls :meth:`progress` rather than holding the stream open, because a
        wait is the case the stream is worst at: it reconnects badly, it is
        bounded to eight per account, and a caller who only wants the outcome
        has no use for the events in between.

        It does NOT raise for a build that failed. ``succeeded`` and ``failed``
        are two situations with two remedies — one has an image and the other
        has a step to fix — and an exception flattens them into "something went
        wrong", which is the mistake the move work established the rule about.
        Read ``status``, ``error``, and ``steps`` to see which step stopped it.

        Raises :class:`~mandala_computer.TimeoutError` if the build is still
        going when ``timeout`` runs out. The build is not stopped by that; only
        the waiting is. ``timeout`` and ``poll`` must both be finite and
        non-negative — a ``ValueError`` before the first request otherwise, in
        both halves, for the reasons :func:`check_wait_args` gives.

        The default timeout is generous because the work is: most of a build is
        copying a multi-gigabyte base image before a single step of the document
        runs, and an agent image is roughly fifteen minutes in total.
        """
        check_wait_args(timeout, poll)
        deadline = time.monotonic() + timeout
        last: BuildProgress | None = None
        # Whether the MOST RECENT poll answered, as against whether any ever did.
        # Without it the timeout quotes a stale ``last`` and says the build "was
        # still running" — a claim about the present tense, made from a reading
        # that may be half an hour old and followed by nothing but failures.
        poll_state = _LastPoll.FAILED
        while True:
            # Reset every iteration, so a Retry-After raises THIS sleep and not
            # every later one. Left assigned to `poll` it ratcheted: one 429 with
            # Retry-After: 30 turned a five-second poll into a thirty-second one
            # for the rest of the wait (/code-review, OPL-3835). The TypeScript
            # twin keeps `pollMs` immutable for the same reason.
            delay = poll
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(_wait_timed_out(build_id, timeout, last, poll_state))
            started = time.monotonic()
            try:
                # The poll carries what is left of the wait, not the client's own
                # timeout. Without the cap a ``wait(timeout=1)`` could sit in one
                # request for the default sixty seconds — and for ever against a
                # caller-supplied client with no timeout at all. The same cap
                # Computer.wait_until_built passes to its refresh.
                last = self.progress(build_id, timeout_cap=remaining)
                poll_state = _LastPoll.ANSWERED
                # ``done`` and not a comparison against a list of statuses: the
                # platform derives it from the JOB rather than from the phase,
                # and the phase is read out of a log the document's own steps
                # write into.
                if last.done:
                    return last
            except MandalaError as err:
                # A hypervisor briefly away during a fifteen-minute build is
                # ordinary, and is what this loop is for. A 4xx other than
                # 408/409/429 is a request the platform refused on its merits,
                # and propagates now rather than in half an hour.
                #
                # This module had its own copy of that rule, arrived at over two
                # reviews, and its docstring called itself "the second front of
                # OPL-3724" because it no longer matched the TypeScript SDK. It
                # is now _is_transient_for_poll, which every wait in this package
                # and both sibling clients ask — and which took this copy's two
                # corrections with it: 408 is retryable per RFC 9110, and the
                # range test is `>= 500` rather than "not a 4xx" so that a 301
                # from a misconfigured base URL is not polled for half an hour.
                if not _is_transient_for_poll(err):
                    raise
                # What the MOST RECENT poll said about the platform, in three
                # states. A poll cut short by this wait's own cap did not fail
                # to answer — it was never given time to — but a timeout merely
                # CONSISTENT with that cap is not proof it fired, so the third
                # state exists and the message declines to blame either side
                # (adversarial review, OPL-3835).
                poll_state = classify_poll_failure(
                    err, started, remaining, self._t.phase_ceiling(err)
                )
                delay = _poll_delay(err, poll)
            if poll_state is _LastPoll.ANSWERED and last is not None:
                # OUTSIDE the handler above, which treats a bare MandalaError as
                # transient by design — raised inside it, this was swallowed and
                # retried until the deadline (OPL-3835).
                contradiction = build_contradiction(last)
                if contradiction is not None:
                    raise MandalaError(contradiction)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(_wait_timed_out(build_id, timeout, last, poll_state))
            time.sleep(min(delay, remaining))


class Moves:
    """The moves on this account, live and recently finished.

    Its own collection because ``GET /moves`` is its own route, account-scoped
    rather than hanging off a computer — which is the platform's decision and the
    right one: a move is a fact about a computer that is currently on one host
    and about to be on another, and during the window that matters that is
    exactly what nobody can say.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Move]:
        """Every move worth reading: the ones still running, and the ones that
        finished within the last day and have not been dismissed.

        Two things to get from a listing rather than a per-computer read. A move
        you started is found by its ``computer_id`` —
        :meth:`~mandala_computer.Computer.wait_for_move` does exactly that. And a
        move you did NOT start is what the "another computer on this account is
        being moved right now" refusal is about: one runs per account at a time,
        and this is where you find out which and how far along.

        A finished move stays here for a day so that an outcome is still readable
        by somebody who went away while it ran. Read ``live``, not the row's
        absence.

        An API key issued against a workspace sees the moves of computers in that
        workspace only.
        """
        # The platform answers ``{"moves": [...]}``; a caller gets the list. The
        # envelope exists because the route is account-scoped and could grow a
        # sibling field, and unwrapping it here is what keeps that from being
        # every caller's problem. ``move_rows`` is the row-shape check every
        # other listing gets from ``json_array``.
        return [Move.from_api(m) for m in move_rows(self._t.json_object("GET", _api.MOVES))]


class Sizes:
    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Size]:
        data = self._t.json_array("GET", _api.SIZES)
        return [Size.from_api(s) for s in data]


class Usage:
    """What this account has used.

    Its own collection because ``GET /usage`` is its own route, account-scoped
    like ``GET /moves`` rather than hanging off a computer — which it could not
    be: the figures include computers that have since been deleted, and those are
    exactly the ones an unexplained line on an invoice belongs to.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def read(
        self,
        *,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
    ) -> UsageReport:
        """Running hours weighted by cores and memory, the storage held, and
        the per-computer breakdown behind the totals.

        The read to build a spend check around: a loop that launches computers is
        the caller that can run up a bill without noticing, and this is the same
        figure the dashboard shows the person who will ask about it.

        With no arguments the window is the account's current billing period,
        which is what makes the numbers comparable with an invoice. Name
        ``since``/``until`` for a window that has CLOSED — the billing period is
        always the current one, and by the time an invoice arrives the period it
        covers is not. Both take an aware ``datetime`` or an RFC 3339 string with
        a zone; a naive datetime is refused rather than guessed at, because the
        zone that would have to be assumed is not necessarily yours. They are
        sent as ``from``/``to``, which ``from`` being a keyword is the whole
        reason for the other spelling.

        Check :attr:`~mandala_computer.UsageReport.degraded` and
        :attr:`~mandala_computer.UsageReport.unmetered` on the way out. Each
        figure is a sum across the fleet, so a hypervisor that did not answer
        leaves a total that is quietly short rather than an obviously missing
        row, and those two flags are the only thing that says so.
        """
        data = self._t.json_object("GET", _api.USAGE, params=_api.usage_params(since, until))
        return UsageReport.from_api(data)


class Webhooks:
    """Account webhooks: signed POSTs of this account's events, to an endpoint
    you chose (platform OPL-3923, OPL-4300).

    The second transport for events, beside the socket. The socket is for a
    caller that is attached and waiting; a webhook is for one that wants to be
    WOKEN — CI, a queue worker, anything that would otherwise poll. What
    arrives is the event object exactly as the socket frames it, under three
    Standard Webhooks headers that :func:`mandala_computer.verify` checks.

    A subscription is a standing instruction to make the platform send HTTP to
    an address you chose, which is why there are ten per account on every paid
    plan and none without one, and why the endpoint must be ``https://`` and
    resolve to a public address. The secret is answered ONCE, on
    :meth:`create` and :meth:`rotate`, and is never readable again.
    """

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    def list(self) -> builtins.list[Webhook]:
        """Every subscription on the account, oldest first, with its health.

        No secret is ever in this list. An API key issued against a workspace
        sees the subscriptions confined to that workspace only.
        """
        return [Webhook.from_api(w) for w in self._t.json_array("GET", _api.WEBHOOKS)]

    def create(
        self,
        url: str,
        *,
        description: str | None = None,
        events: Sequence[str] | None = None,
        computers: Sequence[str] | None = None,
        enabled: bool | None = None,
    ) -> WebhookCreated:
        """Subscribe an HTTPS endpoint to this account's events.

        THE ANSWER CARRIES THE SECRET, ONCE. Read
        :attr:`~mandala_computer.WebhookCreated.secret` off the return value
        and store it; it is not readable again, and :meth:`rotate` is how you
        get a new one.

        ``events`` narrows to some types — the socket's vocabulary less
        ``file.changed``; an unknown one is a 400 that lists them — and
        ``computers`` to some machines, up to 64, which need not exist yet.
        Omit either, or pass ``[]``, for everything. Both take a LIST: a bare
        string is refused rather than read as its letters. ``enabled=False``
        creates it switched off, to enable later.

        The endpoint is checked upstream at create — ``https://``, no
        credentials in it, a public address behind the name — and a 400 names
        the rule it broke. The eleventh subscription on an account is a
        :class:`~mandala_computer.ConflictError` naming the cap.
        """
        body = _api.webhook_body(
            create=True,
            url=url,
            **_named(description=description, events=events, computers=computers, enabled=enabled),
        )
        return WebhookCreated.from_api(self._t.json_object("POST", _api.WEBHOOKS, json=body))

    def get(self, webhook_id: str) -> Webhook:
        """One subscription, with its health: when the endpoint last accepted a
        delivery, when one last failed, the status of the newest attempt, and
        whether the platform has disabled it. Never the secret."""
        return Webhook.from_api(self._t.json_object("GET", _api.webhook(webhook_id)))

    def update(
        self,
        webhook_id: str,
        *,
        url: str | None = None,
        description: str | None = None,
        events: Sequence[str] | None = None,
        computers: Sequence[str] | None = None,
        enabled: bool | None = None,
    ) -> Webhook:
        """Change the endpoint, the description, the filters, or ``enabled``.

        Only what you name is sent; the rest is left as it is. Naming nothing
        is refused here, because an update that changes nothing is never what
        a caller meant. ``events=[]`` or ``computers=[]`` CLEARS that filter
        back to everything — an empty list is sent, not dropped.

        ``enabled=True`` clears a ``failing`` disable and starts fresh;
        ``enabled=False`` stops deliveries and records that you chose to. A new
        ``url`` is checked exactly as on create.
        """
        body = _api.webhook_body(
            create=False,
            **_named(
                url=url,
                description=description,
                events=events,
                computers=computers,
                enabled=enabled,
            ),
        )
        return Webhook.from_api(self._t.json_object("PATCH", _api.webhook(webhook_id), json=body))

    def delete(self, webhook_id: str) -> None:
        """Remove the subscription and every delivery record it holds, pending
        ones included. Nothing more is sent to the endpoint."""
        self._t.request("DELETE", _api.webhook(webhook_id))

    def rotate(self, webhook_id: str) -> WebhookCreated:
        """Mint a new secret and answer it — once, like a create.

        The old one goes on being honoured for 24 hours: every delivery in
        that window carries two signatures on the one header, new first, and
        :func:`mandala_computer.verify` accepts either, so a receiver can be
        moved to the new secret at any point in the window without refusing a
        delivery. Rotating again inside the window replaces the previous secret
        rather than keeping three.
        """
        data = self._t.json_object("POST", _api.webhook_action(webhook_id, "rotate"))
        return WebhookCreated.from_api(data)

    def test(self, webhook_id: str) -> WebhookDelivery:
        """Queue one signed delivery of a synthetic ``webhook.test`` event.

        Through the ordinary path, so it is signed, retried and recorded
        exactly as a real one. The answer is the delivery ACCEPTED, not
        finished: what the endpoint said comes from :meth:`deliveries`, once
        :attr:`~mandala_computer.WebhookDelivery.is_finished`. A disabled
        subscription is a :class:`~mandala_computer.ConflictError`; enable it
        first.
        """
        data = self._t.json_object("POST", _api.webhook_action(webhook_id, "test"))
        return WebhookDelivery.from_api(data)

    def deliveries(self, webhook_id: str) -> builtins.list[WebhookDelivery]:
        """The newest hundred deliveries, newest first, each with its state,
        its attempt count and the status or one-line error of its newest
        attempt.

        Finished deliveries are kept for seven days; pending ones until they
        finish. This is where an ``exhausted`` delivery shows up — nothing is
        dropped silently.
        """
        data = self._t.json_array("GET", _api.webhook_action(webhook_id, "deliveries"))
        return [WebhookDelivery.from_api(d) for d in data]


def _named(**fields: Any) -> dict[str, Any]:
    """The keyword arguments a caller actually gave, ``None`` meaning omitted.

    ``None`` is the omission marker on every optional here because none of the
    five fields can mean anything by it: a filter is cleared with ``[]``, a
    description with ``""``, and ``enabled`` is a bool. Dropping them before
    :func:`_api.webhook_body` is what lets that builder tell "not mentioned"
    from a value, and refuse an update that mentions nothing.
    """
    return {name: value for name, value in fields.items() if value is not None}
