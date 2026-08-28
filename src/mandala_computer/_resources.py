"""Resource collections hanging off the sync client."""

from __future__ import annotations

import builtins
import math
import time
import warnings
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from datetime import datetime
from enum import Enum, auto
from typing import Any

from . import _api
from ._client import Transport
from ._computer import Computer, _poll_delay
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
    build_contradiction,
)
from ._sse import SSEEvent

__all__ = ["Builds", "Computers", "Moves", "Sizes", "Snapshots", "Templates", "Usage"]


def check_wait_args(timeout: float, poll: float) -> None:
    """The two numbers :meth:`Builds.wait` is steered by, checked once.

    Shared so the halves cannot diverge, and they had (adversarial review,
    OPL-3835): ``poll=-1`` reached ``time.sleep(-1)`` and raised a bare
    ``ValueError`` out of the sync half, while ``asyncio.sleep(-1)`` returned at
    once and turned the async half into a tight loop against a metered endpoint.
    ``timeout=float("nan")`` was worse in both — every ``remaining <= 0``
    comparison is false against a NaN, so the deadline this method's docstring
    promises never arrived and the wait ran for ever.

    NEGATIVE and non-finite only. Zero was refused too for one commit, and that
    was a compatibility break wider than the bug it was fixing (second
    adversarial review, OPL-3835): every sibling wait in this SDK —
    ``wait_until_built``, ``wait_until_running``, ``wait_for_guest`` — takes
    ``poll=0``, and this repository's own tests pass it at twenty-odd call
    sites. ``timeout=0`` is an already-expired deadline, and the sibling waits
    answer it with the ``TimeoutError`` their callers are catching; raising a
    ``ValueError`` instead would go past that handler.

    ``poll=0`` DOES mean no delay between polls, and against a live endpoint
    that is a hammering loop — ``time.sleep(0)`` and ``await asyncio.sleep(0)``
    both return at once (third adversarial review, OPL-3835). It is allowed
    anyway because it is not this method's property to fix: all eight sibling
    wait loops in _computer.py and _async_computer.py do the same thing with the
    same argument and there is no poll floor anywhere in the SDK, so refusing it
    HERE would make one wait stricter than the eight beside it while leaving the
    hammering reachable through any of them. A floor is worth having; it is
    worth having everywhere at once, not smuggled in through the one wait a
    review happened to look at.
    """
    for value, what in ((timeout, "timeout"), (poll, "poll")):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{what} must be a finite, non-negative number of seconds")


#: The least a wait will sleep after a 429, when `poll` and ``Retry-After``
#: both fail to supply one. See ``_poll_delay`` in _computer.py.
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
        data = self._t.json_object(
            "POST", _api.snapshot_action(snapshot_id, "clone"), json=_api.name_body(name)
        )
        return Computer(self._t, _api.computer_payload(data))

    def delete(self, snapshot_id: str) -> None:
        self._t.request("DELETE", _api.snapshot(snapshot_id))

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
        data = self._t.json_object("GET", _api.MOVES)
        rows = data.get("moves")
        # The platform answers ``{"moves": [...]}``; a caller gets the list. The
        # envelope exists because the route is account-scoped and could grow a
        # sibling field, and unwrapping it here is what keeps that from being
        # every caller's problem.
        return [Move.from_api(m) for m in rows] if isinstance(rows, list) else []


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
