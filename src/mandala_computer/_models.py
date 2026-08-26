"""Response objects.

Deliberately permissive: unknown fields are preserved in ``raw`` rather than
rejected, so a server that starts returning more does not break older clients.
"""

from __future__ import annotations

import builtins
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from operator import index as integer_index
from typing import Any, SupportsIndex, TypeVar, overload

from ._exceptions import MandalaError

__all__ = [
    "BuildProgress",
    "BuildStep",
    "ComputerUsage",
    "ExecResult",
    "ExecStatus",
    "FilePart",
    "Listing",
    "PublishedTemplate",
    "RetiredTemplates",
    "Size",
    "Snapshot",
    "SnapshotHoldings",
    "Template",
    "TemplateBuild",
    "TemplateCheck",
    "UsagePeriod",
    "UsageReport",
    "UsageTotals",
    "VncConnect",
    "Window",
    "WindowResult",
]

T = TypeVar("T")
S = TypeVar("S")


def _num(value: Any) -> int:
    """An integer field off the wire, or zero when it is unusable."""
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0
    return int(number) if math.isfinite(number) else 0


def _real(value: Any) -> float:
    """A fractional field off the wire, or zero when it is unusable.

    Its own helper rather than :func:`_num`, which truncates to ``int``. Every
    usage figure is fractional — 0.75 hours is a real session, and 0.13
    GB-months is a real charge — so truncating them would round most small
    accounts' usage to nothing.
    """
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _text(value: Any) -> str:
    """A string field off the wire, with JSON null represented as empty."""
    return "" if value is None else str(value)


def _exit_code(value: Any) -> int | None:
    """An exit code off the wire, preserving null and rejecting JSON booleans."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("exit_code must be an integer or null, not a boolean")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise MandalaError("exec answered with an invalid exit_code") from exc


class Listing(list[T]):
    """A collection read that the platform may have had to answer short.

    A ``list``, so everything written against the old return type keeps working
    and nothing has to be unwrapped. What it adds is the answer to a question a
    bare list cannot carry: whether this is all of them.

    ``GET /computers`` and ``GET /snapshots`` fan out across every hypervisor
    holding something of yours. One that cannot be reached makes the answer
    incomplete, and by default the platform refuses to send it —
    :class:`~mandala_computer.UnavailableError`. Passing ``allow_partial=True``
    takes the short answer instead, and this is where it says so::

        computers = client.computers.list(allow_partial=True)
        if not computers.is_complete:
            ...  # do not treat anything absent from this as deleted

    :attr:`incomplete` is ``None`` when the listing is whole. When it is not, it
    is how many rows the platform's placement cache could account for — which is
    legitimately ``0``, because a computer created during the outage was never
    cached against the host now holding it. So branch on
    :attr:`is_complete`, never on the number.
    """

    #: Rows missing, or ``None`` when nothing was missing. See the class note on
    #: why ``0`` is not the same as ``None``.
    incomplete: int | None = None

    @property
    def is_complete(self) -> bool:
        """False when the platform said this listing is short."""
        return self.incomplete is None

    def copy(self) -> Listing[T]:
        """Copy the rows without discarding whether the server answered short."""
        return Listing.of(list(self), self.incomplete)

    @overload
    def __getitem__(self, index: SupportsIndex, /) -> T: ...

    @overload
    def __getitem__(self, index: slice, /) -> Listing[T]: ...

    def __getitem__(self, index: SupportsIndex | slice, /) -> T | Listing[T]:
        if isinstance(index, slice):
            return Listing.of(super().__getitem__(index), self.incomplete)
        return super().__getitem__(index)

    @overload
    def __add__(self, other: list[T], /) -> Listing[T]: ...

    @overload
    def __add__(self, other: list[S], /) -> Listing[T | S]: ...

    def __add__(self, other: list[Any], /) -> Listing[Any]:
        """Concatenate without promoting a partial answer to a complete one."""
        incomplete = self.incomplete
        if isinstance(other, Listing) and other.incomplete is not None:
            incomplete = other.incomplete if incomplete is None else incomplete + other.incomplete
        return Listing.of(super().__add__(other), incomplete)

    def __radd__(self, other: list[Any], /) -> Listing[Any]:
        """Preserve partial state when an ordinary list is on the left."""
        incomplete = self.incomplete
        if isinstance(other, Listing) and other.incomplete is not None:
            incomplete = other.incomplete if incomplete is None else incomplete + other.incomplete
        return Listing.of(list.__add__(other, self), incomplete)

    def extend(self, values: Iterable[T], /) -> None:
        """Append rows without promoting a partial answer to a complete one."""
        other_incomplete = values.incomplete if isinstance(values, Listing) else None
        super().extend(values)
        if other_incomplete is not None:
            self.incomplete = (
                other_incomplete if self.incomplete is None else self.incomplete + other_incomplete
            )

    # This is list.__iadd__'s own signature. Mypy additionally compares it to
    # the cross-type __add__ overloads above, although in-place mutation cannot
    # change the generic type of the object it returns.
    def __iadd__(  # type: ignore[override, misc]
        self, other: Iterable[T], /
    ) -> Listing[T]:
        """The in-place spelling of :meth:`extend`, including partial state."""
        self.extend(other)
        return self

    def __mul__(self, value: SupportsIndex, /) -> Listing[T]:
        """Repeat rows without losing (or understating) missing rows."""
        count = integer_index(value)
        incomplete = self.incomplete
        if incomplete is not None:
            # Like an empty slice, multiplying by zero must not turn a partial
            # source into a confidently complete answer. Positive repetition,
            # on the other hand, repeats both the present and missing rows.
            incomplete *= max(count, 1)
        return Listing.of(super().__mul__(count), incomplete)

    def __rmul__(self, value: SupportsIndex, /) -> Listing[T]:
        """The reflected spelling of :meth:`__mul__`."""
        return self * value

    @classmethod
    def of(cls, items: list[T], incomplete: int | None = None) -> Listing[T]:
        listing = cls(items)
        listing.incomplete = incomplete
        return listing


@dataclass(frozen=True, repr=False)
class VncConnect:
    """Everything needed to put a computer's live desktop on a page.

    Two credentials rather than one, and the difference is enforced by the
    platform rather than by the client asking politely:

    ``token``
        Full control — keyboard and pointer. Root-equivalent on that one
        machine, so it belongs on a server or in a page you trust. NOT the
        clipboard, whatever a noVNC client offers on it: QEMU carries cut text
        only through a vdagent channel these guests are not started with, so a
        paste arrives and is dropped with no error. Move text with
        :meth:`Computer.exec` and ``desktop=True``. A write needs ``setsid``, so
        the holder outlives the command — an X selection belongs to a live
        process — AND ``>/dev/null 2>&1``, without which the resident xclip
        holds the pipe the guest agent is reading and the exec runs to its full
        timeout before answering. Send the text base64 rather than quoted, since
        an apostrophe would otherwise end the shell word, and poll rather than
        reading straight back: being granted a selection is asynchronous, so the
        next read can still be the old one.
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
    #: Websocket URL opening an interactive terminal — a PTY in the guest,
    #: carried on the same controlling credential as :attr:`url`, so treat it
    #: as that credential. ``""`` on a Windows guest, which has no terminal yet.
    #:
    #: Present and refused is the case to plan for, and it is about the COMPUTER
    #: rather than the server: the serial channel a terminal runs over is added
    #: to a guest's hardware at COLD boot, so a computer last started before
    #: terminals shipped has a URL here that answers 409 until it is stopped and
    #: started. A restart will not do it — that resets the same QEMU, and the
    #: command line only changes on a cold boot.
    terminal_url: str = ""
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
            terminal_url=str(d.get("terminal_url") or ""),
            raw=dict(d),
        )

    @staticmethod
    def _without_credential(url: str) -> str:
        """A URL with everything after the path dropped.

        Each of these URLs carries a token in its query or its fragment, so the
        origin and path are the whole of what can be shown.
        """
        if not url:
            return ""
        bare = url.split("?", 1)[0].split("#", 1)[0]
        return bare if bare == url else f"{bare}?<redacted>"

    def __repr__(self) -> str:
        """Deliberately hand-written, and lossy.

        The generated one printed both tokens and the three URLs carrying them.
        These credentials have no expiry — they last until the computer restarts
        or somebody rotates them — and ``token`` is root-equivalent on that
        machine, so a single log line or traceback rendering this object hands
        over the desktop for as long as it runs. Everything a repr is actually
        for survives: which computer this is, and whether each field is set.

        :attr:`raw` still holds the real values, and it is excluded from the
        repr for the same reason.
        """
        return (
            f"VncConnect(url={self._without_credential(self.url)!r}, "
            f"view_url={self._without_credential(self.view_url)!r}, "
            f"token=<redacted>, view_token=<redacted>, "
            f"embed_url={self._without_credential(self.embed_url)!r}, "
            f"terminal_url={self._without_credential(self.terminal_url)!r})"
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
            name=_text(d.get("name")),
            label=_text(d.get("label")),
            os=_text(d.get("os")),
            cpu=_num(d.get("cpu")),
            ram_mb=_num(d.get("ram_mb")),
            disk_gb=_num(d.get("disk_gb")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class PublishedTemplate:
    """A document this account published, from ``publish()`` or ``get()``.

    :class:`Template` is what a LISTING answers — a name, a size, enough to
    launch it. This is what a template IS, and the two are different shapes on
    purpose: the listing has to stay small enough to render a picker from, and
    the document carries build steps that can run to pages.
    """

    #: ``namespace/name@version``. What you pass as ``template`` to create.
    ref: str
    #: ``sha256:…`` of the document. Two publishes of the same digest are the
    #: same template, which is what makes republishing an unchanged document a
    #: no-op rather than a conflict.
    doc_digest: str
    #: The document itself, in canonical form — the bytes :attr:`doc_digest` is
    #: over. Key order and whitespace may differ from what was sent; nothing
    #: else does.
    document: Mapping[str, Any]
    #: The catalogue row this document describes.
    template: Template
    #: Every version of this name, newest first.
    versions: builtins.list[str]
    #: ``None`` on a template the platform publishes — nobody published it.
    published_at: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> PublishedTemplate:
        document = d.get("document")
        template = d.get("template")
        published = d.get("published_at")
        return cls(
            ref=_text(d.get("ref")),
            doc_digest=_text(d.get("doc_digest")),
            document=dict(document) if isinstance(document, Mapping) else {},
            template=Template.from_api(template if isinstance(template, Mapping) else {}),
            versions=[_text(v) for v in d.get("versions") or []],
            # None stays None rather than becoming "": a shipped template was
            # not published by anybody, and an empty timestamp reads as one that
            # is known and blank rather than one that does not apply.
            published_at=None if published is None else _text(published),
            raw=dict(d),
        )


@dataclass(frozen=True)
class TemplateCheck:
    """What ``validate()`` said about a document.

    Both outcomes are a 200 — an invalid document is an answer to the question,
    not a failed request — so nothing here raises for :attr:`valid` being False.
    That is the point of validating: :attr:`problems` lists EVERY problem at
    once, where publishing reports the first thing that stops it.
    """

    valid: bool
    #: Every problem with the document, not just the first. Empty when valid.
    problems: builtins.list[str]
    #: The ref the document claims, once it parsed far enough to have one.
    ref: str | None
    #: ``sha256:…`` of the whole document. Changes with any edit, a label included.
    doc_digest: str | None
    #: ``sha256:…`` of only what decides the IMAGE.
    #:
    #: A new label or a version bump leaves it alone, so comparing it against a
    #: previous run is how you tell whether an edit means a rebuild. ``None``
    #: for a document naming a parent in ``spec.from``, which cannot be computed
    #: without the parent's.
    build_digest: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> TemplateCheck:
        def maybe(key: str) -> str | None:
            value = d.get(key)
            return None if value is None else _text(value)

        return cls(
            valid=bool(d.get("valid", False)),
            problems=[_text(p) for p in d.get("problems") or []],
            ref=maybe("ref"),
            doc_digest=maybe("doc_digest"),
            build_digest=maybe("build_digest"),
            raw=dict(d),
        )


@dataclass(frozen=True)
class RetiredTemplates:
    """What a retire took away, from ``retire()``.

    Not a :class:`PublishedTemplate` with a flag on it: the document is gone, so
    there is nothing of that shape left to answer with.

    WHAT A RETIRE COSTS is worth knowing before calling it. It breaks
    RESOLUTION and nothing else — a computer is built from the image the ref
    resolved to and holds no reference to the document, so anything already
    running, stopped or suspended is untouched. What it does not give back is
    the NAME: a retired ref is refused for ever, identical bytes included, and
    :attr:`refs_claimed` does not go down.
    """

    #: The refs that went, newest version first. Never empty — an empty retire
    #: is a 404.
    retired: builtins.list[str]
    #: One value: everything in :attr:`retired` went in the same write.
    retired_at: str
    #: The versions of this name still published, newest first. Empty means the
    #: name is gone.
    versions: builtins.list[str]
    #: How many templates the account holds now — the number the per-account
    #: ceiling is against.
    templates: int
    #: How many refs this account has ever claimed, live and retired together.
    #:
    #: It does NOT go down when you retire, and there is a much larger ceiling
    #: on it than on :attr:`templates`. The two move differently, and somebody
    #: watching only the first would conclude that retiring is free.
    refs_claimed: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> RetiredTemplates:
        return cls(
            retired=[_text(r) for r in d.get("retired") or []],
            retired_at=_text(d.get("retired_at")),
            versions=[_text(v) for v in d.get("versions") or []],
            templates=_num(d.get("templates")),
            refs_claimed=_num(d.get("refs_claimed")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class TemplateBuild:
    """Compiling a document into an image (platform OPL-3791).

    Not to be confused with a computer's disk copy, which the platform also
    calls a build. This one is minutes long: ``start()`` answers immediately
    with a job, and ``wait()`` is what watches it.
    """

    #: ``bld-a1b2c3d4e5f6``-shaped.
    id: str
    #: The document this was built from, as ``namespace/name@version``.
    ref: str
    #: ``running``, ``succeeded`` or ``failed``.
    status: str
    #: Why it failed, when it did. For a failing ``run:`` step, the end of that
    #: step's own output.
    error: str
    started_at: str
    #: ``None`` while it is still running.
    finished_at: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> TemplateBuild:
        finished = d.get("finished_at")
        return cls(
            id=_text(d.get("id")),
            ref=_text(d.get("ref")),
            status=_text(d.get("status")),
            error=_text(d.get("error")),
            started_at=_text(d.get("started_at")),
            finished_at=None if finished is None else _text(finished),
            raw=dict(d),
        )


@dataclass(frozen=True)
class BuildStep:
    """One step of a build, in the order the document declares them."""

    #: Its position, 1-based.
    n: int
    #: ``apt``, ``run``, ``file``, ``mkdir``, ``env``, or ``finish`` for the
    #: cleanup every build ends with.
    kind: str
    #: What the step does, from the document — the packages, the path, or the
    #: first real line of the script.
    label: str
    #: ``pending``, ``running``, ``done``, ``failed``, or ``skipped`` for one an
    #: earlier failure meant we never reached.
    status: str
    started_at: str | None
    finished_at: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> BuildStep:
        started, finished = d.get("started_at"), d.get("finished_at")
        return cls(
            n=_num(d.get("n")),
            kind=_text(d.get("kind")),
            label=_text(d.get("label")),
            status=_text(d.get("status")),
            started_at=None if started is None else _text(started),
            finished_at=None if finished is None else _text(finished),
            raw=dict(d),
        )


@dataclass(frozen=True)
class BuildProgress:
    """What a build is DOING, as against what became of it (platform OPL-3794).

    A build is minutes long — most of it spent copying a multi-gigabyte base
    image and then running the document's steps — so this says which step of how
    many is running, and which one failed. It stays readable after the build has
    finished, so a program that was not attached at the time can still see where
    it stopped.
    """

    id: str
    #: The job's own status, restated so one poll answers both questions.
    status: str
    #: Whether to stop polling.
    #:
    #: Derived from :attr:`status` and not from :attr:`phase`: a phase is read
    #: out of the build's log, which the document's own ``run:`` steps write
    #: into, and only the job decides whether a build worked.
    done: bool
    #: ``planning``, ``staging``, ``copying``, ``building``, ``publishing``, and
    #: then ``published``, ``reused`` or ``failed``.
    #:
    #: ``unknown`` means the build finished without keeping a step-by-step
    #: record — every build from before the endpoint existed is one. It is not
    #: reported as ``published`` because a build that REUSED an existing image
    #: succeeds too, and that distinction lived in the record that is missing.
    #: :attr:`status` is still the answer.
    phase: str
    #: Which step is running, 1-based, or the one that failed. ``0`` before the
    #: first.
    step: int
    #: How many steps there are.
    of: int
    #: Every step, in order, whatever its status — so the whole list renders
    #: from the first read.
    steps: builtins.list[BuildStep]
    #: One line about the phase, or why a failed build failed.
    note: str
    #: Why it failed, when it did. The same value ``get()`` gives.
    error: str
    #: When the build last MOVED, and not when this was last read — a build
    #: whose steps have stopped advancing is one whose ``updated_at`` stops.
    updated_at: str
    #: True only where the fleet could not recognise its own build tool's
    #: output, so the per-step position is unavailable. The build itself is
    #: unaffected and :attr:`status` is still the answer.
    unmatched: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> BuildProgress:
        rows = d.get("steps") or []
        return cls(
            id=_text(d.get("id")),
            status=_text(d.get("status")),
            done=bool(d.get("done", False)),
            phase=_text(d.get("phase")),
            step=_num(d.get("step")),
            of=_num(d.get("of")),
            steps=[BuildStep.from_api(r) for r in rows if isinstance(r, Mapping)],
            note=_text(d.get("note")),
            error=_text(d.get("error")),
            updated_at=_text(d.get("updated_at")),
            unmatched=bool(d.get("unmatched", False)),
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
        cheapest_plan = d.get("cheapest_plan")
        return cls(
            id=_text(d.get("id")),
            label=_text(d.get("label")),
            template=_text(d.get("template")),
            cpu=_num(d.get("cpu")),
            ram_mb=_num(d.get("ram_mb")),
            disk_gb=_num(d.get("disk_gb")),
            allowed=bool(d.get("allowed", False)),
            cheapest_plan=None if cheapest_plan is None else _text(cheapest_plan),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Snapshot:
    id: str
    computer_id: str
    name: str
    kind: str
    #: Where these bytes have got to, and what may be done with them.
    #:
    #: ``"capturing"``
    #:     Still being taken, and NOT a snapshot yet. A listing puts these
    #:     first, their ids begin ``cap-``, and restore, clone and delete all
    #:     answer 404 on one. Acting on the newest row of a fresh listing is
    #:     exactly how this is met.
    #: ``"pending"``
    #:     On its host and usable. This is the point to act from.
    #: ``"durable"``
    #:     In backup storage as well. See :attr:`is_durable`.
    #: ``"deleting"``
    #:     A deletion that began and did not finish; only listed when asked for.
    state: str
    size_bytes: int
    created_at: str
    incremental: bool
    auto: bool
    #: For a computer that still exists, its current name — so a rename shows up
    #: here without re-reading anything. For an orphan, the name it had at
    #: capture, which is all that is left of it.
    computer_name: str = ""
    #: The computer this was captured from no longer exists, which decides which
    #: of the two things you can do with a snapshot still works:
    #: :meth:`~mandala_computer.Snapshots.clone` builds a new computer out of it
    #: and is fine, while :meth:`~mandala_computer.Snapshots.restore` puts the
    #: disk back on the source and has nowhere to put it. Snapshots outlive
    #: their computers on purpose, so an ordinary account's listing has these in
    #: it as a matter of course rather than as a fault.
    orphaned: bool = False
    #: This is a placeholder standing in for a snapshot nobody could read, seen
    #: only in a listing taken with ``allow_partial=True``. The platform does
    #: not merely omit what it could not reach — it appends one of these per
    #: missing row, carrying an id and nothing else, so that something short is
    #: visibly short rather than quietly smaller. Such a row has no
    #: :attr:`computer_id`: there was no daemon to say which computer it belongs
    #: to. Which is why filtering a partial listing by computer keeps these —
    #: dropping them removes precisely the markers saying the answer is
    #: incomplete, and then reports a confident count.
    unreachable: bool = False
    #: The shape the capture was taken at, which is the shape a
    #: :meth:`~mandala_computer.Snapshots.clone` of it comes up as. Worth
    #: reading before cloning: a snapshot carries its own sizing rather than
    #: the source computer's current one, so a computer resized after the
    #: capture clones back to what it was, not to what it is.
    os: str = ""
    template: str = ""
    cpu: int = 0
    ram_mb: int = 0
    disk_gb: int = 0
    resolution: str = ""
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
            id=_text(d.get("id")),
            computer_id=_text(d.get("computer_id")),
            name=_text(d.get("name")),
            kind=_text(d.get("kind", "disk")),
            state=_text(d.get("state")),
            size_bytes=_num(d.get("size_bytes")),
            created_at=_text(d.get("created_at")),
            incremental=bool(d.get("incremental", False)),
            auto=bool(d.get("auto", False)),
            computer_name=_text(d.get("computer_name")),
            orphaned=bool(d.get("orphaned", False)),
            unreachable=bool(d.get("unreachable", False)),
            os=_text(d.get("os")),
            template=_text(d.get("template")),
            cpu=_num(d.get("cpu")),
            ram_mb=_num(d.get("ram_mb")),
            disk_gb=_num(d.get("disk_gb")),
            resolution=_text(d.get("resolution")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class SnapshotHoldings:
    """What a computer would leave behind — and the interlock on destroying it.

    From ``GET /computers/{id}/snapshots``, which is not a listing: the
    snapshots themselves come from :meth:`~mandala_computer.Snapshots.list`,
    and these two routes answer different shapes on purpose.

    :attr:`fingerprint` is the reason to come here. It names the exact set the
    count and the size describe, it cannot be reconstructed from a listing, and
    it is what makes a purge binding — see
    :meth:`~mandala_computer.Computer.delete`. Read the numbers, decide, then
    pass the fingerprint you were shown; the daemon refuses the sweep if a
    capture has landed in between, which is exactly the race that would
    otherwise destroy something nobody agreed to.
    """

    count: int
    size_bytes: int
    fingerprint: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> SnapshotHoldings:
        return cls(
            count=_num(d.get("count")),
            size_bytes=_num(d.get("size_bytes")),
            fingerprint=_text(d.get("fingerprint")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Retention:
    """How long automatic snapshots are kept, as the plan grants it.

    From ``GET /retention``, and the other half of
    :meth:`~mandala_computer.Computer.set_schedule` — which decides when
    snapshots are TAKEN and deliberately has no field for how long they survive.

    A grandfather-father-son window rather than an age. What survives is the
    newest automatic snapshot in each of the last :attr:`daily` days *that have
    one*, the last :attr:`weekly` such ISO weeks and the last :attr:`monthly`
    such calendar months. Counting periods that contain a capture rather than
    periods on the calendar is what stops a computer switched off for a month
    losing the history it had: nothing ages out for the passage of time alone.

    Boundaries are cut in UTC, whatever timezone the schedule runs in. A capture
    at 23:30 on a Sunday in ``America/Chicago`` is Monday in UTC and counts
    toward the following ISO week.

    A zero turns that tier off. All three zero is what an account with no active
    subscription reads.

    Only snapshots with :attr:`Snapshot.auto` are ever touched. One you took by
    hand is yours until you delete it, whatever this says — which is also how
    you keep something past the window.
    """

    daily: int
    weekly: int
    monthly: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Retention:
        return cls(
            daily=_num(d.get("daily")),
            weekly=_num(d.get("weekly")),
            monthly=_num(d.get("monthly")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class UsagePeriod:
    """The period an account is billed on."""

    start: str
    end: str
    #: ``"subscription"`` when the boundary came from the plan's renewal date,
    #: which is what an invoice is anchored to. ``"calendar-month"`` when there
    #: is no live subscription to take it from, in which case the period is the
    #: current UTC month. Worth reading before quoting a figure at anybody: the
    #: two answer different questions about "this period".
    source: str

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> UsagePeriod:
        return cls(
            start=_text(d.get("start")),
            end=_text(d.get("end")),
            source=_text(d.get("source")),
        )


@dataclass(frozen=True)
class ComputerUsage:
    """One computer's share of a window."""

    id: str
    name: str
    run_hours: float
    vcpu_hours: float
    ram_gb_hours: float
    #: This computer is no longer on the fleet. It ran during the window and was
    #: deleted, which is why it is billed for and absent from
    #: :meth:`~mandala_computer.Computers.list` — the line is not stale, the
    #: machine is gone.
    gone: bool

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ComputerUsage:
        return cls(
            id=_text(d.get("id")),
            name=_text(d.get("name")),
            run_hours=_real(d.get("run_hours")),
            vcpu_hours=_real(d.get("vcpu_hours")),
            ram_gb_hours=_real(d.get("ram_gb_hours")),
            gone=bool(d.get("gone", False)),
        )


@dataclass(frozen=True)
class UsageTotals:
    """What an account used, with the per-computer breakdown behind it.

    The two storage figures stay separate because the remedies are: a computer's
    disk is provisioned at create and released at delete, and snapshots come and
    go under the retention policy the account sets. One summed number would be a
    figure nobody could act on.
    """

    run_hours: float
    vcpu_hours: float
    ram_gb_hours: float
    snapshot_gb_hours: float
    snapshot_gb_months: float
    disk_gb_hours: float
    disk_gb_months: float
    #: The breakdown, which is what makes a total checkable.
    #:
    #: EMPTY on a workspace-scoped API key, and empty rather than ``None`` so
    #: that iterating it never needs a check first. Usage is metered and billed
    #: per ACCOUNT, so these lines cover the whole account and would name
    #: computers outside such a key's scope; the platform withholds them and
    #: sends the account-wide totals either way.
    #: :attr:`UsageReport.breakdown` is how to tell "no computers ran" from
    #: "this key may not see which did".
    computers: tuple[ComputerUsage, ...]

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> UsageTotals:
        rows = d.get("computers")
        return cls(
            run_hours=_real(d.get("run_hours")),
            vcpu_hours=_real(d.get("vcpu_hours")),
            ram_gb_hours=_real(d.get("ram_gb_hours")),
            snapshot_gb_hours=_real(d.get("snapshot_gb_hours")),
            snapshot_gb_months=_real(d.get("snapshot_gb_months")),
            disk_gb_hours=_real(d.get("disk_gb_hours")),
            disk_gb_months=_real(d.get("disk_gb_months")),
            computers=tuple(
                ComputerUsage.from_api(c)
                for c in (rows if isinstance(rows, list) else [])
                if isinstance(c, Mapping)
            ),
        )


@dataclass(frozen=True)
class UsageReport:
    """What :meth:`~mandala_computer.Usage.read` answers.

    READ :attr:`degraded` AND :attr:`unmetered` BEFORE USING THE NUMBERS. Every
    figure is a sum across the hypervisors this account's computers are on, so a
    host that did not contribute does not leave a hole anybody could notice — it
    leaves a total that is quietly too small. The platform answers 200 with these
    two flags rather than refusing, because a caveat in the same object cannot be
    missed the way a missing row can, and because one of the two never clears by
    retrying.
    """

    #: The period this ACCOUNT is billed on — not necessarily the window that was
    #: measured. :attr:`from_` and :attr:`to` are that, and they differ whenever
    #: a window was named.
    period: UsagePeriod
    #: The start of the measured window. ``from_`` because ``from`` is a keyword.
    from_: str
    #: The end of it, and worth reading rather than assuming: a ``until`` in the
    #: future is answered as now, because the future holds no usage.
    to: str
    usage: UsageTotals
    #: A hypervisor could not be reached, so every figure may be too small. This
    #: one clears on its own — retry when the host is back.
    degraded: bool
    #: The same shortfall from the other cause: a hypervisor is up and running a
    #: daemon older than the meter, so it has no hours to report. Waiting does
    #: not fix this one, which is why it is a separate flag rather than the same.
    unmetered: bool
    #: Whether :attr:`UsageTotals.computers` is the real breakdown rather than a
    #: withheld one — False on a workspace-scoped key. Read off the payload's
    #: shape (the platform omits the field rather than sending an empty list), so
    #: an empty breakdown can be told from an invisible one.
    breakdown: bool
    #: The last UTC day (``YYYY-MM-DD``) whose usage has settled for billing — a
    #: contiguous prefix, so a day still being held back stops the count where it
    #: is. ``None`` when none of the window has settled yet.
    #:
    #: NOT a caveat on the totals, which are live from the ledger and true
    #: through :attr:`to`. It answers the other question, and it is the one to
    #: check before comparing these numbers against an invoice.
    reported_through: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> UsageReport:
        period = d.get("period")
        totals = d.get("usage")
        totals = totals if isinstance(totals, Mapping) else {}
        reported = d.get("reported_through")
        return cls(
            period=UsagePeriod.from_api(period if isinstance(period, Mapping) else {}),
            from_=_text(d.get("from")),
            to=_text(d.get("to")),
            usage=UsageTotals.from_api(totals),
            degraded=bool(d.get("degraded", False)),
            unmetered=bool(d.get("unmetered", False)),
            # Presence, not emptiness. The platform drops the key for a scoped
            # credential and sends ``[]`` for an account that ran nothing, and
            # those are different answers: one is "you may not see this", the
            # other is "there was nothing to see".
            breakdown=isinstance(totals.get("computers"), list),
            reported_through=None if reported is None else _text(reported),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Move:
    """A move in flight, or the outcome of one that has finished.

    A resize past what a computer's host can run is refused with an offer (see
    :class:`~mandala_computer.MoveRequiredError`);
    :meth:`~mandala_computer.Computer.relocate` takes it up, and the platform
    answers 202 with one of these while the disk copy runs behind it.
    :meth:`~mandala_computer.Moves.list` is where it is read afterwards.

    Two fields are absent because the platform does not send them: which host the
    computer is leaving and which it is going to. Both are recorded on its side
    for an operator; a tenant is told "another host in this region" and never
    which machine.
    """

    computer_id: str
    #: Where it has got to.
    #:
    #: ``staging``, ``moving`` and ``resizing`` are live. The four terminal
    #: states are four different situations, which is why they are four words:
    #:
    #: - ``done`` — on the new host at the new size.
    #: - ``moved`` — on the new host at its OLD size. The move landed and the
    #:   resize did not, so the computer HAS changed hardware and an ordinary
    #:   :meth:`~mandala_computer.Computer.resize` finishes the job where it now
    #:   is. Reading this as "the move failed" sends you looking for a machine
    #:   that has moved.
    #: - ``failed`` — nothing happened. The computer is where it was, untouched.
    #: - ``lost`` — we stopped watching. It may well have completed; read the
    #:   computer.
    state: str
    #: A sentence about the state, for a person. Empty while nothing has gone wrong.
    detail: str
    #: Still running. The flag to poll on, rather than comparing :attr:`state`
    #: against a list that will grow.
    live: bool
    #: Present only where the move is applying a new value for that dimension.
    #: ``None`` means "not being changed" and never "changed to nothing" — which
    #: is why these are optional rather than defaulting to 0 on the field this
    #: whole operation exists to grow.
    cpu: int | None = None
    ram_mb: int | None = None
    disk_gb: int | None = None
    started_at: str = ""
    #: ``None`` while :attr:`live`.
    finished_at: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Move:
        return cls(
            computer_id=_text(d.get("computer_id")),
            state=_text(d.get("state")),
            detail=_text(d.get("detail")),
            live=bool(d.get("live")),
            cpu=_num(d["cpu"]) if d.get("cpu") is not None else None,
            ram_mb=_num(d["ram_mb"]) if d.get("ram_mb") is not None else None,
            disk_gb=_num(d["disk_gb"]) if d.get("disk_gb") is not None else None,
            started_at=_text(d.get("started_at")),
            finished_at=_text(d["finished_at"]) if d.get("finished_at") is not None else None,
            raw=dict(d),
        )


@dataclass(frozen=True)
class Window:
    """One window on the guest's desktop.

    What a screenshot cannot tell you: a picture says what the desktop looks
    like, this says what any of it *is* — which is how a browser that failed to
    launch is told apart from one that has not painted yet.

    Match on :attr:`wm_class` rather than :attr:`title`. The class is the
    application and is stable; the title is whatever page or document it happens
    to be showing.
    """

    id: str
    title: str
    #: The X11 ``WM_CLASS`` — the application, e.g. ``"Firefox"``. Spelled with
    #: the prefix because ``class`` is a Python keyword and cannot be a field.
    wm_class: str
    #: The window manager's own type, e.g. ``"normal"``, ``"dock"``.
    type: str
    x: int
    y: int
    width: int
    height: int
    focused: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Window:
        return cls(
            id=_text(d.get("id")),
            title=_text(d.get("title")),
            wm_class=_text(d.get("class")),
            type=_text(d.get("type")),
            x=_num(d.get("x")),
            y=_num(d.get("y")),
            width=_num(d.get("width")),
            height=_num(d.get("height")),
            focused=bool(d.get("focused", False)),
            raw=dict(d),
        )


@dataclass(frozen=True)
class FilePart:
    """One window of a guest file, and where it sits in the whole.

    What :meth:`~mandala_computer.Computer.read_file_part` answers with. The
    bytes alone would not be enough to ask for the next window: the platform
    trims a request longer than one transfer moves rather than refusing it, so
    **you can get fewer bytes than you asked for on a success**, and where the
    window actually ended is a fact only the response carries::

        part = c.read_file_part("/home/user/out.tar", offset=0, length=1 << 20)
        while True:
            sink.write(part.data)
            if part.at_end:
                break
            part = c.read_file_part("/home/user/out.tar", offset=part.end, length=1 << 20)

    That loop is :meth:`~mandala_computer.Computer.download_file`, which is
    usually the thing to reach for. This record is for the reads that are not a
    download: the last 4 KiB of a log, a header off the front of an archive,
    a resumable transfer that has to remember where it stopped.

    :attr:`partial` is ``False`` in exactly one situation, and it is not "the
    file was small". A range is always sent, so the platform answering with the
    whole thing means it *ignored* the range — which it does for a file whose
    length the guest cannot report, a ``/proc`` entry being the usual one. There
    are no byte positions to name in such a file and no total to promise, so
    :attr:`total` is ``None`` and :attr:`at_end` is ``True``: everything there
    was arrived, and there is nothing to page through.
    """

    #: The bytes of this window.
    data: bytes
    #: Position in the file of this window's first byte.
    offset: int
    #: The file's total length, or ``None`` when the guest could not report one.
    total: int | None
    #: Whether this is a window of the file (a 206) rather than all of it.
    partial: bool

    @property
    def end(self) -> int:
        """One past this window's last byte — the offset to ask from next."""
        return self.offset + len(self.data)

    @property
    def at_end(self) -> bool:
        """Whether the file ends here, so there is nothing left to ask for."""
        if not self.partial:
            return True
        return self.total is not None and self.end >= self.total

    @property
    def remaining(self) -> int | None:
        """Bytes after this window, or ``None`` when the total is not known."""
        if self.total is None:
            return None
        return max(self.total - self.end, 0)


@dataclass(frozen=True)
class WindowResult:
    """What a window action left behind.

    :attr:`window` is the window *as it now is*, not an acknowledgement of what
    was asked. Believe it rather than the request: the window manager places the
    frame and applications snap to their own increments, so a move to (300, 200)
    routinely lands at (305, 229).

    It is ``None`` in two different situations, and :attr:`gone` is what tells
    them apart — ``True`` after a ``close``, which is the action succeeding, and
    ``False`` when the action happened but the guest could not describe the
    result.
    """

    window: Window | None
    gone: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> WindowResult:
        w = d.get("window")
        return cls(
            window=Window.from_api(w) if isinstance(w, Mapping) else None,
            gone=bool(d.get("gone", False)),
            raw=dict(d),
        )


@dataclass(frozen=True)
class ExecStatus:
    """A backgrounded command's state, and what it has printed since last time.

    The output is a **cursor, not a buffer**. Each read returns what has arrived
    since the previous read and advances the daemon's own offset, so output you
    receive and drop is gone, and two readers polling one pid split the stream
    between them rather than each seeing all of it. :attr:`stdout_offset` and
    :attr:`stderr_offset` report how far it has read — they are not parameters
    to send back.

    :attr:`more` is the flag to poll on: it says there is further output waiting
    right now.
    """

    pid: int
    #: The command line, echoed back.
    command: str
    running: bool
    exited: bool
    #: ``None`` until it has exited — ``None`` rather than ``0``, which is the
    #: one value that would be read as success by anything not checking first.
    exit_code: int | None
    #: What it has printed since the previous read. This read consumed it.
    stdout: str
    stderr: str
    #: How far the daemon has now read, reported rather than requested.
    stdout_offset: int
    stderr_offset: int
    #: There is further output waiting right now — poll again straight away
    #: rather than sleeping first.
    more: bool
    #: It was stopped by :meth:`~mandala_computer.BackgroundCommand.kill` rather
    #: than ending on its own.
    killed: bool
    started_at: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def done(self) -> bool:
        """True once the command has stopped, however it stopped.

        Read with :attr:`more`, not instead of it: a command can exit with
        output still queued, and a loop that stops at ``done`` alone drops
        whatever the last read did not reach.
        """
        return self.exited or not self.running

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ExecStatus:
        code = d.get("exit_code")
        return cls(
            pid=_num(d.get("pid")),
            command=_text(d.get("command")),
            running=bool(d.get("running", False)),
            exited=bool(d.get("exited", False)),
            exit_code=_exit_code(code),
            stdout=_text(d.get("stdout")),
            stderr=_text(d.get("stderr")),
            stdout_offset=_num(d.get("stdout_offset")),
            stderr_offset=_num(d.get("stderr_offset")),
            more=bool(d.get("more", False)),
            killed=bool(d.get("killed", False)),
            started_at=_text(d.get("started_at")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class ExecResult:
    """The outcome of a shell command run inside the guest."""

    #: ``None`` when the platform could not report an exit code, such as a
    #: timed-out command. It must not be coerced to zero and mistaken for success.
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    #: True when the guest agent stopped capturing stdout before the command
    #: stopped producing it. See :attr:`truncated`.
    out_truncated: bool = False
    #: The same for stderr.
    err_truncated: bool = False
    #: ``compare=False``, unlike the other models here. An ``ExecResult`` is a
    #: value, not a handle: callers assert on one against a result they built
    #: themselves, and put them in sets. Comparing the unknown fields the
    #: server happened to send would make ``res == ExecResult(0, "hi", "",
    #: False)`` false for a command that did exactly that, and comparing a
    #: ``dict`` at all makes the frozen dataclass unhashable.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
        code = d.get("exit_code")
        return cls(
            exit_code=_exit_code(code),
            stdout=_text(d.get("stdout")),
            stderr=_text(d.get("stderr")),
            timed_out=bool(d.get("timed_out", False)),
            out_truncated=bool(d.get("out_truncated", False)),
            err_truncated=bool(d.get("err_truncated", False)),
            raw=dict(d),
        )
