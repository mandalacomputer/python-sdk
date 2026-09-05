"""Paths and request bodies, shared by the sync and async clients.

Every route the SDK can reach and every payload it can send is built here. The
two clients differ only in awaits; if either built its own URLs or bodies, they
could drift apart silently — and the surface test that pins the SDK to the
platform's allowlist would only be checking one of them.
"""

from __future__ import annotations

import math
import re
import shlex
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote

# --- paths ----------------------------------------------------------------

TEMPLATES = "templates"
#: The JSON Schema for a ``mandala/v1`` document (platform OPL-3568).
TEMPLATE_SCHEMA = "templates/schema"
#: Check a document without publishing it. Side-effect free, and claims no ref.
TEMPLATE_VALIDATE = "templates/validate"
#: Every build this account has started (platform OPL-3791).
#:
#: A collection, like :data:`MOVES` and for the same reason: a build is a job
#: rather than a property of a computer, and it outlives the request that
#: started it.
BUILDS = "builds"
SIZES = "sizes"
COMPUTERS = "computers"
SNAPSHOTS = "snapshots"
#: Every move on the account, live and recently finished.
#:
#: A collection, and not ``computers/:id/move`` — which is the platform's own
#: decision and worth knowing when binding to it: a per-computer read could not
#: tell a computer with no move from an id that does not exist, so there is no
#: such route. :meth:`~mandala_computer.Computer.wait_for_move` filters this by
#: ``computer_id``.
MOVES = "moves"
#: What the account has used, over a window. Account-scoped, like :data:`MOVES`.
USAGE = "usage"
#: How long automatic snapshots are kept — the plan's retention window.
#:
#: Account-scoped like :data:`USAGE` and :data:`MOVES`, and answered by the
#: control plane rather than by a hypervisor, so it cannot come back short the
#: way a fleet listing can. Read-only: the plan owns retention, and there is no
#: write on any surface.
RETENTION = "retention"
#: Account webhooks (platform OPL-3923, OPL-4300): a standing instruction to
#: POST this account's events, signed, at an address the caller chose.
#: Account-scoped and answered by the control plane, like :data:`RETENTION`.
WEBHOOKS = "webhooks"


def canonical(value: object, what: str) -> str:
    """A real ``str``, whatever was handed in.

    Every guard in this file validates a string and then hands the ORIGINAL on
    to something that stringifies or encodes it again — and a ``str`` SUBCLASS
    can answer differently the second time (adversarial review, OPL-3835). Two
    were live:

    * ``template_version_params`` matched the regex against the buffer, then
      returned the object; httpx serialises a query value with ``str(value)``, so
      a subclass whose ``__str__`` answers ``""`` passed the check and sent
      ``?version=`` — the empty-version branch, which on a retire means every
      version of the name and cannot be undone.
    * ``seg`` checked ``strip(".")`` on the buffer, then called ``quote``, which
      calls the value's own ``encode()``; a subclass returning ``b".."`` became a
      dot segment that the client normalises into a different route.

    ``str.__str__`` reads the underlying buffer rather than any override, so what
    comes back cannot disagree with what was checked. A non-string is refused
    here rather than coerced: ``str(None)`` is ``"None"``, which is a plausible
    id and a nonsense one.
    """
    if not isinstance(value, str):
        # ValueError, not the TypeError ruff prefers, and deliberately: every
        # other refusal in this file is a ValueError — `seg` for an empty or
        # all-dots id, `template_version_params` for a malformed version — and a
        # caller wrapping a call in `except ValueError` should not catch "that is
        # not a version" while missing "that is not a string". One type for one
        # class of mistake beats the rule.
        raise ValueError(  # noqa: TRY004
            f"{what} must be a string, not {type(value).__name__}"
        )
    return str.__str__(value)


def flag(value: object, what: str) -> bool:
    """A real ``bool``, and nothing that merely behaves like one.

    The companion to :func:`canonical`, and here for the same class of reason
    (adversarial review, OPL-3835). The flags in this file were read with plain
    truthiness, which is the wrong rule for a parameter that ARMS something:
    ``"false"`` is a non-empty string and therefore true, so
    ``build_params("false")`` asked for a rebuild of a multi-gigabyte image and
    ``delete_params(purge_snapshots="false", ...)`` selected the branch that
    destroys a computer's snapshots along with it. Both are spellings a caller
    reaching in from a config file, an environment variable or a CLI argument
    produces by accident, and neither is a spelling of "no".

    ``1`` and ``0`` are refused with them. They read as true and false to a
    human and would work, but admitting them puts the coercion rule back and the
    next value through it is a string again.

    Only the flags that arm something, or that change which session a call runs
    as, go through here. The ones that merely widen a listing —
    ``allow_partial``, ``include=all`` — stay on truthiness: nothing is destroyed
    or paid for by reading one of those generously.
    """
    if not isinstance(value, bool):
        # ValueError, not the TypeError ruff prefers, for the reason `canonical`
        # gives: one exception type for one class of mistake, so a caller
        # guarding a call cannot catch half of them.
        raise ValueError(  # noqa: TRY004
            f"{what} must be True or False, not {type(value).__name__}"
        )
    return value


def whole(
    value: object, what: str, *, exc: type[Exception] = TypeError, message: str | None = None
) -> int:
    """A real ``int``, whatever was handed in.

    The numeric companion to :func:`canonical`, and here for its reason (Codex
    adversarial review, OPL-3869). Every guard below compares a number and then
    hands the ORIGINAL object on to be serialised, and an ``int`` SUBCLASS can
    answer one thing to ``>`` and another to whatever formats it. Both halves
    were live:

    * :func:`guest_pid` checked ``pid > 0`` and interpolated the object, so a
      subclass whose ``__format__`` answers ``"../stop"`` passed the check and
      addressed ``computers/vm-1/exec/../stop`` — a different route entirely.
    * every ``< 0`` and ``<= 0`` guard here is an overridable comparison, so a
      subclass answering ``False`` to them put ``w=-1``, ``max_steps=-1`` and
      ``Range: bytes=-1-`` on the wire, past the checks written to stop exactly
      those.

    ``int.__index__`` reads the underlying value rather than any override, the
    way ``str.__str__`` does for a string, so what comes back cannot disagree
    with what was checked — and the CHECK must be made on what comes back, not
    on the argument. ``bool`` is refused rather than read as 0/1: it is an
    ``int`` subclass, and ``json.dumps`` writes it as ``true``.

    The exception type and message are the caller's, because these guards
    predate this helper and their wording is what tests and users already read.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise exc(message or f"{what} must be a whole number, not {value!r}")
    return int.__index__(value)


def real(value: object, what: str, *, message: str | None = None) -> float:
    """A real finite number, whatever was handed in.

    :func:`whole` for the durations, which take a float as readily as an int.
    An ``int`` stays an ``int`` so that ``wait(5)`` still sends ``5`` rather
    than ``5.0``.

    Finiteness belongs here rather than with the caller: NaN fails every ordered
    comparison, so ``seconds <= 0`` alone lets it through and it becomes a
    timeout of ``nan``; infinity passes that comparison honestly and serialises
    as a bare ``Infinity`` that is not JSON.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # ValueError for the reason `canonical` gives: one exception type for
        # one class of mistake, so a caller guarding a call catches all of it.
        raise ValueError(  # noqa: TRY004
            message or f"{what} must be a number, not {value!r}"
        )
    number = float.__float__(value) if isinstance(value, float) else int.__index__(value)
    if not math.isfinite(number):
        raise ValueError(message or f"{what} must be a finite number, not {value!r}")
    return number


def seg(value: str) -> str:
    """One path segment, percent-encoded — including ``/``.

    Every id the caller can hand us goes through here. Computer and snapshot
    ids are minted by the platform in a known alphabet, so in the ordinary case
    this changes nothing; but ``get()`` takes whatever string it is given, and
    an id is the one part of these URLs that does not come from this file. An
    unescaped ``/`` would not merely 404 — it would re-point the request at a
    route nobody meant, with the account's bearer token on it, and a ``?``
    would put query keys on a request whose own parameters are what interlocks
    like the snapshot-purge fingerprint are carried in.

    Encoding cannot answer ``.`` and ``..``: ``quote`` leaves a dot alone
    whatever ``safe`` says, and the dot-segment removal in RFC 3986 is applied
    by the client to the assembled URL, so ``get("..")`` would climb a level
    and address ``/api/v1`` itself — and an id of ``..`` would turn one
    computer's ``/snapshots`` into the account's whole snapshot list, which is
    a bad thing to hand a purge loop. An empty id does the same to the
    collection route. Neither is a real id, so both are refused here.
    """
    value = canonical(value, "id")
    if not value.strip("."):
        raise ValueError(f"id must not be empty or all dots: {value!r}")
    return quote(value, safe="")


def computer(computer_id: str) -> str:
    return f"computers/{seg(computer_id)}"


def computer_action(computer_id: str, action: str) -> str:
    """agent | clipboard | clone | exec | input | move | restart | schedule |
    screenshot | snapshots | start | stop | suspend | windows."""
    return f"computers/{seg(computer_id)}/{action}"


def guest_pid(pid: object) -> int:
    """A real positive pid, not a bool and not zero.

    Interpolated into the path, so a value that is not a positive integer builds
    ``computers/:id/exec/True`` or ``.../exec/0`` — a 404 about a route rather
    than a sentence about the pid that was wrong. ``bool`` is an ``int``
    subclass, so ``True`` would otherwise become ``1``.
    """
    text = f"pid must be a positive integer, not {pid!r}"
    # ValueError throughout, not TypeError for the type half: `canonical`'s rule,
    # so `except ValueError` around a call catches every way a pid can be wrong.
    if isinstance(pid, bool):
        raise ValueError(text)  # noqa: TRY004
    if isinstance(pid, str):
        # A decimal string is accepted and converted, because this value is the
        # one that CROSSES A PROCESS BOUNDARY — a job id out of a queue, a pid
        # written down by the run before this one — and those arrive as text.
        # `background_command("4242")` worked before this guard existed, and
        # refusing it was a regression rather than a tightening: the platform's
        # own strconv.Atoi takes it. What is refused is a string that is not a
        # plain positive decimal.
        digits = str.__str__(pid).strip()
        if not digits.isdecimal():
            raise ValueError(text)
        try:
            number = int(digits)
        except ValueError:
            # Past CPython's integer-string limit `int` raises with its own
            # message about digit counts, which names neither this argument nor
            # what it should have been. The limit is configurable down to 640,
            # so the ceiling is the interpreter's rather than a number worth
            # writing here (second review pass, OPL-4479).
            raise ValueError(text) from None
    elif isinstance(pid, int):
        number = int.__index__(pid)
    else:
        raise ValueError(text)  # noqa: TRY004
    if number <= 0:
        raise ValueError(text)
    return number


def exec_handle(computer_id: str, pid: int) -> str:
    """A backgrounded command, addressed by the guest pid ``exec`` answered with.

    Not ``computer_action``: the pid is a second path segment, and the platform's
    own ``patternFor`` reduces it to ``:pid`` rather than to ``:id`` — it names
    something inside a computer rather than a thing the platform owns.
    """
    return f"computers/{seg(computer_id)}/exec/{guest_pid(pid)}"


def window(computer_id: str, window_id: str) -> str:
    """One window on the guest's desktop, addressed by its X id.

    The window id has the most room to surprise of any id here — it is whatever
    the guest's window manager called a window, rather than something the
    platform minted — but it is encoded by the same :func:`seg` every other id
    goes through, because the consequence of a stray separator does not depend
    on who chose the string.
    """
    return f"computers/{seg(computer_id)}/windows/{seg(window_id)}"


def template_ref(namespace: str, name: str) -> str:
    """One published template, by the two halves of its ref.

    Two segments and not one, because that is the shape of the route: the
    platform reduces ``templates/<a>/<b>`` to ``templates/:namespace/:name``, so
    a ref handed over whole — ``acc-1/devbox@1.0.0`` — would be percent-encoded
    into a single segment and reach a route that does not exist. The version is
    a *query* parameter on this path, not part of it; see
    :func:`template_version_params`.
    """
    return f"{TEMPLATES}/{seg(namespace)}/{seg(name)}"


#: ``MAJOR.MINOR.PATCH``, no leading zeros.
#:
#: Matched with ``fullmatch``, not ``match``: Python's ``$`` also matches just
#: before a trailing newline, so ``"1.0.0\n"`` satisfied the anchored pattern and
#: was sent as ``?version=1.0.0%0A``. The platform's own ``wellFormedVersion``
#: uses a JavaScript regex, where ``$`` is end-of-input, so it answers 400 — the
#: exact round trip this guard exists to save. Two languages, one grammar, and
#: the anchors are not the same.
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")


def template_version_params(version: str | None) -> dict[str, str]:
    """The ``version`` query parameter, refused when it is not a version.

    Absence and emptiness have to be different things here. The platform answers
    400 for one that is empty or malformed rather than defaulting, and that
    refusal exists because of a real defect: ``?version=`` — which is what most
    clients serialise for an unset optional string — read as "no version was
    named" and retired an entire template, irreversibly.

    This SDK cannot send that at all. ``None`` omits the parameter, and anything
    else has to be a version. Checked here rather than left to the platform
    because the two answers are not interchangeable on a retire: omitting the
    parameter means EVERY version, so a caller who meant one version and passed
    an empty string would, without the platform's refusal, have retired the lot.
    """
    if version is None:
        return {}
    version = canonical(version, "version")
    if not _VERSION.fullmatch(version):
        raise ValueError(
            f"version must be MAJOR.MINOR.PATCH with no leading zeros (got {version!r}); "
            "omit it entirely to name the whole template"
        )
    return {"version": version}


def template_document(document: str) -> bytes:
    """The document a publish, a validate or a build sends.

    Raw bytes, not a JSON envelope: the platform reads JSON or YAML off the body
    itself, so a wrapper would be a document the validator never sees — and one
    that parses, so the failure would be a complaint about the wrapper's fields.

    Refused when empty for the reason :func:`seg` refuses an empty id — the
    platform answers 400 for it, and that is a round trip that never had to
    happen.
    """
    # Canonical FIRST, which is what makes the check below binding on the bytes
    # that leave. Ordered the other way — as it was — a str subclass overriding
    # ``strip()`` passed the emptiness check and then encoded to nothing, so an
    # empty body went on the wire under a comment claiming it could not. The one
    # guard the previous pass said it had fixed and had not.
    text = canonical(document, "document")
    if not text.strip():
        raise ValueError("document must be a non-empty template document, as JSON or YAML")
    # Unpaired surrogates are not UTF-8; ``encode`` raises ``UnicodeEncodeError``,
    # which a caller wrapping this in ``except ValueError`` would miss, so it is
    # the same refusal as clipboard and env.
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("document must be valid UTF-8") from exc


def build(build_id: str) -> str:
    return f"{BUILDS}/{seg(build_id)}"


def build_action(build_id: str, action: str) -> str:
    """progress | events."""
    return f"{BUILDS}/{seg(build_id)}/{action}"


def build_params(no_reuse: bool) -> dict[str, str]:
    """``no_reuse``, sent only when it is asked for.

    Omitted rather than sent as ``false``, and the reason is the documented
    schema rather than a claim about the parser: ``lib/apidoc`` gives this
    parameter ``enum: ['true']``, so ``true`` is the only value the reference
    admits and a client sending ``false`` is sending something undocumented.

    An earlier comment here said the platform reads the key's PRESENCE, which is
    false — ``server/buildjob.go`` reads ``Get("no_reuse") == "true"`` — and the
    same false claim was repeated in the other two clients and pinned as a test
    docstring. The emitted request was right either way; the stated reason was
    not.

    The flag has to BE a bool rather than merely read as one — see :func:`flag`.
    A rebuild is the expensive branch, and ``"false"`` selected it.
    """
    return {"no_reuse": "true"} if flag(no_reuse, "no_reuse") else {}


def build_stream_failed(build_id: str, data: Any) -> str:
    """What to say when a build's event stream ends with an ``error`` event.

    The stream's own failure, not the build's — named as such because a caller
    told "the build failed" would go and read a document that is fine. One
    function so the sync and async halves cannot word it differently.
    """
    detail = ""
    if isinstance(data, Mapping):
        detail = str(data.get("error") or "")
    elif data is not None:
        detail = str(data)
    return (
        f"the build event stream for {build_id} ended: {detail or 'no reason given'} "
        f"(this says nothing about the build itself — read builds.progress({build_id!r}))"
    )


def build_stream_truncated(build_id: str, *, malformed: bool) -> str:
    """What to say when a build's event stream stops without a final answer.

    Both halves of the same failure: the platform's contract is that ``done`` is
    the last event, so a stream that ends without one — or with one whose payload
    is not a record — has been cut rather than completed. One function so the
    sync and async halves cannot word it differently.
    """
    how = "with a malformed final event" if malformed else "without a final event"
    return (
        f"the build event stream for {build_id} ended {how}; the build is probably still "
        f"running — read progress({build_id!r}) for the outcome"
    )


def snapshot(snapshot_id: str) -> str:
    return f"snapshots/{seg(snapshot_id)}"


def snapshot_action(snapshot_id: str, action: str) -> str:
    """restore | clone."""
    return f"snapshots/{seg(snapshot_id)}/{action}"


def files(computer_id: str) -> str:
    return f"computers/{seg(computer_id)}/files"


def is_absolute_guest_path(path: str) -> bool:
    r"""Whether a guest would read this path as absolute, on either family.

    Nothing here knows which OS the guest runs — a ``Computer`` does not say —
    and the two families disagree about what absolute means. A leading ``/`` is
    absolute on Linux and *drive-relative* on Windows, where the daemon's own
    ``validGuestPath`` refuses it and wants ``C:\...`` or a ``\\`` UNC share. So
    both spellings pass here and the server, which does know which guest it is
    talking to, keeps the final say.

    What this still catches without a round trip is the mistake worth catching:
    a bare relative path, which has no working directory to be relative to on
    either family.
    """
    if path.startswith(("/", "\\\\")):
        return True
    # Drive-qualified: C:\ or C:/, both of which the daemon accepts.
    return (
        len(path) >= 3
        and path[0].isascii()
        and path[0].isalpha()
        and path[1] == ":"
        and path[2] in "\\/"
    )


def looks_windows_guest_path(path: str) -> bool:
    r"""Whether the path is spelled the way only a Windows guest spells it.

    Weaker than :func:`is_absolute_guest_path` on purpose: this asks which
    *family* a path belongs to, not whether it is absolute, so the
    drive-relative ``C:notes.txt`` counts here and does not there.

    What it is for is the rules that must not be applied to the other family. A
    ``\`` separates nothing on Linux and a ``:`` is an ordinary character in a
    Linux filename, so treating every path as possibly-Windows quietly renames
    ``/tmp/a:b.txt`` to ``b.txt``. A leading ``/`` says Linux; a drive or a UNC
    prefix says Windows; a bare relative name says neither, and is left to the
    permissive reading, where the two families agree anyway.
    """
    if path.startswith("\\\\"):  # UNC share
        return True
    return len(path) >= 2 and path[0].isascii() and path[0].isalpha() and path[1] == ":"


def files_params(path: str) -> dict[str, str]:
    """The query naming which guest file, checked before the round trip.

    The path must be absolute: nothing about a transfer runs in a shell, so a
    relative path has no working directory to be relative to. The daemon
    refuses it too, but this mistake is knowable without the round trip.
    """
    # Canonical first, for the reason :func:`canonical` gives. ``startswith`` is
    # overridable, so a str subclass could satisfy the absoluteness check and
    # then send something else entirely — here, an empty path.
    text = canonical(path, "guest path")
    if not is_absolute_guest_path(text):
        raise ValueError(f"guest path must be absolute: {text!r}")
    return {"path": text}


def files_range(offset: int, length: int | None) -> dict[str, str]:
    """The ``Range`` header for one window of a guest file.

    ``offset`` counts from the start of the file, or **from its end when it is
    negative** — the same reading Python gives an index, and the same thing the
    ``bytes=-N`` form of the header means. ``length`` is how many bytes to ask
    for, or ``None`` for "to the end".

    Deliberately does **not** refuse a window larger than
    :data:`~mandala_computer._client.FILE_SIZE_LIMIT`. Asking for more than one
    request moves is not a mistake: the platform trims the window to what it can
    carry and the ``Content-Range`` on the answer says exactly what came back and
    what is left, which is the whole paging loop and needs no advance knowledge
    of the ceiling to run. Refusing it here would put that knowledge back.

    Which *end* gets trimmed follows the end the caller anchored — a window
    counted from the start keeps its start, a tail keeps its end — so a negative
    ``offset`` takes no ``length``. Combining them would ask for a window
    anchored at both ends, and the header has no way to spell that; naming the
    two forms apart is better than picking one silently.
    """
    offset = whole(
        offset, "offset", message=f"offset must be an integer byte position, not {offset!r}"
    )
    if length is not None:
        length = whole(
            length,
            "length",
            message=f"length must be an integer byte count or None, not {length!r}",
        )
    if offset < 0:
        if length is not None:
            raise ValueError(
                "a tail is spelled by its offset alone: bytes=-N asks for the file's "
                f"last N bytes and has no length to give. Pass offset={offset} on its "
                "own, or count offset and length from the start of the file."
            )
        return {"Range": f"bytes={offset}"}
    if length is None:
        return {"Range": f"bytes={offset}-"}
    if length < 1:
        raise ValueError(f"length must be at least 1 byte, not {length}")
    return {"Range": f"bytes={offset}-{offset + length - 1}"}


#: An RFC 3339 timestamp carrying a time zone, which is the only kind the
#: platform takes on ``GET /usage``.
#:
#: Matched with ``fullmatch``, not ``match``: Python's ``$`` also matches just
#: before a trailing newline, so ``"2026-08-01T00:00:00Z\n"`` — a stamp read
#: from a config file — satisfied the anchored pattern and was sent as
#: ``from=...%0A``. Same trap as :data:`_VERSION`, and on the one call whose
#: output somebody checks against an invoice.
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})")


def _usage_stamp(value: datetime | str, what: str) -> str:
    """One bound of a usage window, in the spelling the platform reads.

    A ``datetime`` is the shape to prefer, and an AWARE one is the only kind
    accepted: a naive datetime has no zone, so rendering it would mean this SDK
    choosing between "the machine that called" and "UTC" — and being wrong about
    that shifts the window by hours on the one call whose output somebody checks
    against an invoice. ``datetime.now(timezone.utc)`` and
    ``datetime(2026, 8, 1, tzinfo=timezone.utc)`` are both fine; a bare
    ``datetime(2026, 8, 1)`` is refused, with the remedy in the message.

    A string is taken for the caller who already has one — out of a config file,
    or off a previous response — and is checked here rather than sent, for the
    same reason. The platform refuses a zoneless stamp too; being told locally
    means the complaint arrives with the argument that caused it.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"{what} must be an aware datetime — a naive one has no time zone, and "
                "guessing one would move the window. Try "
                f"{what}=your_datetime.replace(tzinfo=timezone.utc)."
            )
        # isoformat() renders the offset it carries; UTC comes out as +00:00,
        # which is RFC 3339 and what the platform parses. Not forced to "Z":
        # the offset IS the instant, and normalising it would be this SDK
        # deciding what the caller meant.
        return value.isoformat()
    # `canonical` before the regex, and the CANONICAL string is what goes back:
    # a `str` subclass can hold a valid 2026 stamp and answer a valid 2030 one
    # to httpx, which bills a window nobody asked about on the one call whose
    # output is compared against an invoice.
    if not isinstance(value, str):
        raise ValueError(  # noqa: TRY004
            f"{what} must be an RFC 3339 timestamp with a time zone, e.g. "
            f"2026-08-01T00:00:00Z (or pass an aware datetime): {value!r}"
        )
    text = canonical(value, what)
    if not _RFC3339.fullmatch(text):
        raise ValueError(
            f"{what} must be an RFC 3339 timestamp with a time zone, e.g. "
            f"2026-08-01T00:00:00Z (or pass an aware datetime): {value!r}"
        )
    return text


def usage_params(
    since: datetime | str | None, until: datetime | str | None
) -> dict[str, str] | None:
    """The window to read usage over, or ``None`` for the billing period.

    ``since``/``until`` because ``from`` is a Python keyword and cannot be a
    parameter name; they are sent as ``from``/``to``, which is what the platform
    and the other two clients call them.

    ``None`` rather than an empty dict when neither bound is given, so the
    default call builds a bare URL — and so that "I did not name a window" and
    "I named an empty one" cannot look the same on the wire.
    """
    params: dict[str, str] = {}
    if since is not None:
        params["from"] = _usage_stamp(since, "since")
    if until is not None:
        params["to"] = _usage_stamp(until, "until")
    return params or None


def partial_params(allow_partial: bool) -> dict[str, str] | None:
    """The opt-in to a knowingly short fan-out listing.

    Omitted rather than sent as ``0`` when the caller did not ask: the platform
    reads the key's presence, and an explicit falsey value is the kind of thing
    a proxy or a future server version could read either way. Nothing is the
    unambiguous spelling of "I did not ask for this".
    """
    return {"allow_partial": "1"} if allow_partial else None


def snapshot_listing_params(*, include_unfinished: bool, allow_partial: bool) -> dict[str, str]:
    """The query on ``GET /snapshots``.

    ``include=unfinished`` widens the listing to deletions that began and did
    not finish. They are not restorable or clonable — their state reads
    ``deleting`` — but they still hold objects and are still billed, so this is
    the flag for a question about storage rather than about what can be used.
    """
    params = dict(partial_params(allow_partial) or {})
    if include_unfinished:
        params["include"] = "unfinished"
    return params


def windows_params(include_all: bool) -> dict[str, str] | None:
    """``include=all`` to keep the desktop's own furniture in the listing.

    Off by default because panels, docks and the wallpaper window are not
    windows a caller acts on — a stock guest showing one terminal has five.
    """
    return {"include": "all"} if include_all else None


def delete_params(*, purge_snapshots: bool, expect: str | None) -> dict[str, str] | None:
    """The query that turns a delete into a delete-and-purge.

    ``expect`` is required here, and the platform's own rule is weaker — it
    accepts an unguarded purge, for callers with no way to read the holdings.
    This SDK has one call away, so the refusal costs nothing and buys the
    interlock: the fingerprint binds the sweep to the set that was actually
    looked at, and the daemon refuses it if a capture has landed since. Without
    it the purge is bound to whatever the set happens to be at the moment it
    fires, which is not the thing anybody agreed to destroy.

    ``expect`` is dropped rather than carried when nothing is being purged. A
    stale fingerprint on an ordinary delete would refuse it for a reason that
    has nothing to do with what was asked.
    """
    # A real bool, not anything truthy: this is the branch that destroys a
    # computer's snapshots along with it, and ``purge_snapshots="false"``
    # selected it. The ``expect`` interlock below does not cover that — it binds
    # the purge to the set that was looked at, not to whether a purge was meant.
    if not flag(purge_snapshots, "purge_snapshots"):
        return None
    # Canonical BEFORE the emptiness check, and this is the guard where that
    # ordering matters most (/code-review, OPL-3835). ``__bool__`` is overridable
    # too, so a str subclass answering True here and "" to ``str()`` passed the
    # check and put ``?expect=`` on the wire — and ``checkExpectation`` in
    # server/vm.go reads an empty expectation as NO expectation, so the interlock
    # this function exists to enforce was silently disarmed on the one route that
    # destroys a computer and its snapshots together.
    text = canonical(expect, "expect") if expect is not None else ""
    if not text:
        raise ValueError(
            "purging snapshots needs the fingerprint from snapshot_holdings(): "
            "read it, check the count and size are what you meant to destroy, "
            "and pass it as expect=. Nothing has been deleted."
        )
    return {"snapshots": "delete", "expect": text}


# --- responses ------------------------------------------------------------


def computer_payload(data: Any) -> dict[str, Any]:
    """Flatten a response that is one computer, in either shape it can arrive in.

    A create whose guest was made and then would not boot answers 201 with
    ``{"computer": {...}, "start_error": "..."}`` rather than an error alone —
    deliberately, so the caller learns the id of the machine it is now paying
    for instead of having to list to find it.

    Read as an ordinary computer that envelope is a computer with no id: every
    field reads off the wrapper, finds nothing, and the id the platform went out
    of its way to return is the one thing dropped. So it is unwrapped here, and
    the failure travels on the record beside the fields it belongs to — see
    :attr:`~mandala_computer.Computer.start_error`.

    Every response that is one computer goes through this, not just the create.
    The envelope is the platform's shape for "here is your machine, and here is
    what went wrong with it", and a second route answering that way should not
    need a second discovery of this function.
    """
    if not isinstance(data, Mapping):
        return {}
    inner = data.get("computer")
    if not isinstance(inner, Mapping):
        return dict(data)
    # start_error kept alongside the fields rather than in a parallel return, so
    # it survives into `raw` and cannot be dropped by a caller that only wanted
    # the computer. A refresh replaces the record and clears it, which is right:
    # it describes one start attempt, not the machine.
    return {**inner, "start_error": data.get("start_error")}


# --- bodies ---------------------------------------------------------------


def create_body(
    *,
    name: str | None,
    template: str | None,
    cpu: int | None,
    ram_mb: int | None,
    disk_gb: int | None,
    start: bool,
    resolution: str | None = None,
    size: str | None = None,
) -> dict[str, Any]:
    """Build a create payload, omitting anything unset.

    Omission is meaningful: the server applies the template's defaults only when
    a key is absent, so sending explicit nulls would override them with nothing.

    A ``size`` names a template and a shape together, so combining it with any
    of the four it stands in for is refused here — the server refuses it too,
    but this mistake is knowable without the round trip, and the server's
    refusal exists for callers who are not this SDK.
    """
    if size is not None and any(v is not None for v in (template, cpu, ram_mb, disk_gb)):
        raise ValueError(
            "size already names a template and a shape; send size alone, "
            "or template/cpu/ram_mb/disk_gb without it"
        )
    name = _require_optional_name(name)
    # ``name`` came back canonical from the check above; these three never had
    # a check at all, and go on the wire beside it.
    size = None if size is None else canonical(size, "size")
    template = None if template is None else canonical(template, "template")
    resolution = None if resolution is None else canonical(resolution, "resolution")
    body: dict[str, Any] = {"start": flag(start, "start")}
    for key, text in (
        ("name", name),
        ("size", size),
        ("template", template),
        ("resolution", resolution),
    ):
        if text is not None:
            body[key] = text
    # Bools are ints, so ``cpu=True`` is true and ``json.dumps`` then encodes
    # it as a boolean. The platform wants a count, not JSON ``true``.
    for key, count in (("cpu", cpu), ("ram_mb", ram_mb), ("disk_gb", disk_gb)):
        if count is not None:
            body[key] = whole(count, key, exc=ValueError)
    return body


def name_body(name: str | None) -> dict[str, Any]:
    """An optional name for a clone.

    Omission asks the platform to generate one. Empty text cannot express that
    omission and is refused consistently with create and rename.
    """
    name = _require_optional_name(name)
    return {} if name is None else {"name": name}


def _require_optional_name(name: str | None) -> str | None:
    """An optional name, canonicalised, or ``None`` when the caller omitted one.

    Empty text cannot express omission and is refused. Canonical first, for the
    reason :func:`canonical` gives: ``strip`` is overridable, so a subclass
    could pass the emptiness check and then send something else — here, an
    empty name. A non-string is a ``ValueError`` rather than the
    ``AttributeError`` ``strip`` would raise.
    """
    if name is None:
        return None
    text = canonical(name, "name")
    if not text.strip():
        raise ValueError("name must not be empty")
    return text


def rename_body(name: str) -> dict[str, Any]:
    """The rename payload.

    Empty is refused here rather than at the server, which refuses it too. On
    create an omitted name means "you pick one"; on rename it can only mean a
    caller cleared the field, and a round trip to be told so is a round trip
    that never had to happen.
    """
    checked = _require_optional_name(name)
    if checked is None:
        raise ValueError("name must not be empty")
    return {"name": checked}


def exec_body(
    command: str,
    timeout: int,
    desktop: bool = False,
    *,
    background: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build an exec payload.

    ``session`` is omitted rather than sent empty when ``desktop`` is false: the
    server's default is the system context, and the only value it accepts is
    ``"desktop"``.

    The wire field is ``timeout_s`` and the argument is ``timeout``: seconds is
    the only unit this SDK measures a wait in, so the suffix says nothing here
    that the surrounding code does not, while on the wire it is the platform's
    name for the field and not ours to shorten.

    The field is omitted alongside ``background`` rather than sent and ignored.
    The server does ignore it — not waiting is the whole request — but a payload
    carrying a deadline that means nothing is a payload somebody will later read
    as a promise the platform never made.

    ``cwd`` must be absolute for the reason a file transfer's path must be: the
    guest agent inherits whatever directory it was started in, so a relative one
    resolves somewhere nobody named.
    """
    body: dict[str, Any] = {"command": canonical(command, "command")}
    # A real bool for the same reason `desktop` is one below: `background="false"`
    # is truthy, and it selects the branch that sends NO timeout at all.
    if not flag(background, "background"):
        body["timeout_s"] = _positive_seconds(
            timeout, "timeout must be positive for a foreground exec"
        )
    # A real bool, not anything truthy: this is the switch from the system
    # context to the logged-in session, and ``desktop="false"`` selected it.
    if flag(desktop, "desktop"):
        body["session"] = "desktop"
    if background:
        body["background"] = True
    if cwd is not None:
        # Canonical first, for the reason :func:`canonical` gives. ``startswith``
        # is overridable, so a str subclass could satisfy the absoluteness check
        # and then send something else — here, a relative cwd.
        text = canonical(cwd, "cwd")
        if not is_absolute_guest_path(text):
            raise ValueError(f"cwd must be absolute: {text!r}")
        body["cwd"] = text
    if env:
        body["env"] = _env_object(env)
    return body


#: The most environment entries an exec may carry, and the longest one entry
#: may be. Both are the platform's bounds (``execMaxEnv`` / ``execMaxEnvLen`` in
#: ``server/execbg.go``), and a value past either is a request the guest agent
#: would refuse after the round trip.
MAX_ENV_ENTRIES = 64
MAX_ENV_ENTRY_BYTES = 4096


def _env_object(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` whose names and values the guest can actually use.

    JSON will carry a number, a null, an empty name. The guest agent will not:
    it turns this into a ``KEY=value`` list, so a non-string becomes a JSON type
    the agent never asked for, an empty name or a ``=`` inside one splits the
    entry at the wrong place, and a NUL is dropped rather than refused. Both
    produce a command that runs with an environment nobody asked for and reports
    success. Mirrored from the platform's ``execEnvList``, for the same reason
    :func:`canonical` is.

    A copy because the body is built once and sent later: a caller that mutates
    the object it passed would otherwise change what goes on the wire after the
    checks below have already passed over it.
    """
    names = list(env)
    if len(names) > MAX_ENV_ENTRIES:
        raise ValueError(
            f"env has {len(names)} entries; the platform accepts at most {MAX_ENV_ENTRIES}"
        )
    out: dict[str, str] = {}
    for name in names:
        key = canonical(name, "env name")
        if not key:
            raise ValueError("env has an entry with an empty name")
        if "=" in key or "\0" in key:
            raise ValueError(f"env name {key!r} must not contain '=' or a NUL")
        text = canonical(env[name], f"env value for {key!r}")
        if "\0" in text:
            raise ValueError(f"env value for {key!r} must not contain a NUL")
        try:
            size = len(key.encode("utf-8")) + len(text.encode("utf-8")) + 1
        except UnicodeEncodeError as exc:
            raise ValueError(f"env entry {key!r} must be valid UTF-8") from exc
        if size > MAX_ENV_ENTRY_BYTES:
            raise ValueError(
                f"env entry {key!r} is {size} bytes; the platform accepts "
                f"at most {MAX_ENV_ENTRY_BYTES}"
            )
        out[key] = text
    return out


def open_url_command(url: str) -> str:
    """Build the shell command that puts ``url`` on the guest's screen.

    The browser is named rather than asked for: Firefox, not ``xdg-open`` or one
    of the other portable wrappers. Naming it keeps the choice in one place —
    this function is the only thing that decides which browser the guest opens,
    so a change of image, or of which browser we want, is a change here rather
    than in every caller's prompt.

    Detached, because a browser does not exit on its own: in the foreground the
    call would block until the timeout killed it and come back as a failure,
    having opened the window anyway.
    """
    # Canonical first, then strip the canonical string — not the original.
    # ``str.strip`` returns ``self`` when there is nothing to strip, so a
    # subclass could override ``startswith`` and sneak a leading dash past the
    # guard below; ``shlex.quote`` would then quote the real buffer. A
    # non-string is a ``ValueError`` rather than the ``AttributeError``
    # ``strip`` would raise.
    url = canonical(url, "url").strip()
    if not url:
        raise ValueError("url must not be empty")
    # shlex.quote stops the URL reaching the shell as anything but one argument.
    # It cannot stop the *browser* reading a leading dash as a flag, and no URL
    # starts with one, so that is refused outright rather than quoted.
    if url.startswith("-"):
        raise ValueError(f"url must not start with '-': {url!r}")
    return f"nohup firefox {shlex.quote(url)} >/dev/null 2>&1 &"


def snapshot_body(memory: bool, name: str | None = None) -> dict[str, Any]:
    """A capture request. An omitted name asks the platform to generate one.

    ``memory`` is a real bool: it decides whether RAM is captured alongside the
    disk, which is what the snapshot costs and how long it takes.
    """
    memory = flag(memory, "memory")
    name = _require_optional_name(name)
    body: dict[str, Any] = {"memory": memory}
    if name is not None:
        body["name"] = name
    return body


# The eight things the window manager will do to one window. Checked here rather
# than left to the server, because a typo'd action is knowable without the round
# trip and the error naming the set is more use than a 400 naming the field.
WINDOW_ACTIONS = (
    "focus",
    "raise",
    "minimize",
    "maximize",
    "unmaximize",
    "close",
    "move",
    "resize",
)


def window_body(
    action: str,
    *,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """One action on one window, with the geometry the action needs.

    Half a point and half a size are refused rather than completed with a zero,
    for the reason ``drag_body`` refuses half an origin: a caller naming only
    ``x`` meant to name a position, and quietly filling the other half moves the
    window to the edge of the screen while the call reports success. The action
    happens, in the wrong place, and nothing says so.
    """
    # Canonical BEFORE the membership test: `in` asks the value's own __eq__,
    # so a `str` subclass can compare equal to "move" and serialise something
    # else entirely.
    action = canonical(action, "action")
    if action not in WINDOW_ACTIONS:
        raise ValueError(f"action must be one of {WINDOW_ACTIONS}")
    if (x is None) != (y is None):
        raise ValueError("give both x and y, or neither")
    if (width is None) != (height is None):
        raise ValueError("give both width and height, or neither")
    if action == "move" and x is None:
        raise ValueError("move needs both x and y")
    if action == "resize" and width is None:
        raise ValueError("resize needs both width and height")
    if action != "move" and x is not None:
        raise ValueError("x and y are only valid for move")
    if action != "resize" and width is not None:
        raise ValueError("width and height are only valid for resize")
    body: dict[str, Any] = {"action": action}
    if x is not None and y is not None:
        body["x"] = _coordinate(x, "x", exc=ValueError)
        body["y"] = _coordinate(y, "y", exc=ValueError)
    if width is not None and height is not None:
        body["width"] = whole(width, "width", exc=ValueError)
        body["height"] = whole(height, "height", exc=ValueError)
    return body


#: The most text ``PUT /computers/{id}/clipboard`` carries INTO a guest, in bytes.
#:
#: Mirrored rather than left to the server, so a request that can only fail is
#: not made, and kept in step by ``scripts/check_surface.py`` like
#: :data:`MAX_STEPS`. The platform states this one in Go — ``clipboardWriteMax``
#: in its ``server/clipboard.go`` — which the checker refused to read at first;
#: the docstring here said "NOT machine-checked" instead, which is an admission
#: rather than a check, so the reader learned Go and the sentence became true.
#: The number is not ours and is not arbitrary: the
#: platform puts the text inside one argument of one command, Linux caps a single
#: argv string at 128 KiB, and two layers of base64 stand between the text and
#: that ceiling — so each byte costs about 1.8 of it and 64 KiB is where the
#: platform stops. Past it ``execve`` fails with E2BIG rather than truncating.
#:
#: The READ cap is 128 KiB, a different bound on a different channel, and is
#: deliberately not mirrored: nothing here can meet it, since that text comes
#: from the guest.
MAX_CLIPBOARD_BYTES = 64 * 1024


def clipboard_body(text: str) -> dict[str, Any]:
    """Text for the desktop's clipboard, checked before it costs a round trip.

    Three refusals, each of which the platform also makes. The NUL is the one
    worth explaining: the platform confirms a write by reading the selection
    back through a command substitution, and a shell truncates that at the first
    NUL — so the write would land, the read-back would disagree, and the answer
    would be a 409 inviting a retry at something that had already worked.

    The cap is counted in UTF-8 BYTES rather than characters. An emoji is four of
    them, so a ``len(text)`` check would pass four times the legal payload to an
    ``execve`` that answers E2BIG.
    """
    # `canonical` rather than an isinstance check, for the reason it documents:
    # every check below reads `text`, and the request then encodes the ORIGINAL
    # object again. A `str` subclass can answer differently the second time — an
    # override that reports empty here and a full buffer to the serialiser walks
    # past all three refusals. It also makes this a ValueError like every other
    # refusal in this file, rather than the one TypeError among them.
    text = canonical(text, "clipboard text")
    # Empty is refused rather than sent, matching the platform: clearing the
    # clipboard is not what that endpoint does, and a caller who meant to clear
    # it should hear so rather than read a status code.
    if not text:
        raise ValueError("clipboard text must not be empty")
    if "\0" in text:
        raise ValueError("clipboard text must not contain a NUL")
    # UTF-8 bytes, not characters — see the docstring. Unpaired surrogates are
    # not UTF-8; ``encode`` raises ``UnicodeEncodeError``, which a caller wrapping
    # this in ``except ValueError`` would miss, so it is the same refusal as the
    # rest.
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("clipboard text must be valid UTF-8") from exc
    if size > MAX_CLIPBOARD_BYTES:
        raise ValueError(
            f"clipboard text is {size} bytes; the platform accepts at most {MAX_CLIPBOARD_BYTES}"
        )
    return {"text": text}


def resize_body(*, cpu: int | None, ram_mb: int | None, disk_gb: int | None) -> dict[str, Any]:
    """A new shape for a stopped computer.

    Its own body rather than a field on a general update, because the platform
    refuses a resize in combination with a rename or an idle window and is right
    to: a resize needs the computer stopped and the other two do not, so one
    request cannot honour both without applying half of it. Three methods that
    each send one group is the shape that cannot ask for the refused thing.

    A disk grows only. That is the server's rule, not checked here — shrinking
    is a coherent request that this SDK has no way to know is refused for this
    computer, and guessing at the current size to reject it would be a client
    inventing a limit.
    """
    # Bools are ints; see :func:`create_body`.
    body = {
        key: whole(value, key, exc=ValueError)
        for key, value in (("cpu", cpu), ("ram_mb", ram_mb), ("disk_gb", disk_gb))
        if value is not None
    }
    if not body:
        raise ValueError("resize() needs at least one of cpu, ram_mb or disk_gb")
    return body


def move_body(*, ram_mb: int, cpu: int | None, disk_gb: int | None) -> dict[str, Any]:
    """The sizing group for a move, which is a resize the current host cannot run.

    ``ram_mb`` is REQUIRED here and optional on :func:`resize_body`, and that is
    the one difference worth explaining. A move exists to escape a RAM ceiling:
    the platform fills an omitted ``ram_mb`` from the computer's current size and
    then refuses the move for not needing one, so a call without it can only ever
    be refused. Required in the signature turns a guaranteed 409 into a
    ``TypeError`` at the call site.

    There is no ``name`` and no ``idle_suspend_min``. The platform reads only
    these three fields off a move body and ignores the rest, so accepting either
    would be a rename that copies a multi-gigabyte disk between hosts and then
    does not happen.
    """
    # Bools are ints; see :func:`create_body`.
    body: dict[str, Any] = {"ram_mb": whole(ram_mb, "ram_mb", exc=ValueError)}
    if cpu is not None:
        body["cpu"] = whole(cpu, "cpu", exc=ValueError)
    if disk_gb is not None:
        body["disk_gb"] = whole(disk_gb, "disk_gb", exc=ValueError)
    return body


def idle_suspend_body(minutes: int | None) -> dict[str, Any]:
    """How long this computer may sit untouched before its host suspends it.

    ``None`` is sent, not omitted, and that is the whole reason this is not
    folded into a generic body builder that drops falsey values: an explicit
    null is how the override is cleared, returning the computer to whatever its
    host is sweeping at. Dropped, it would mean "change nothing", which is the
    opposite request.

    ``0`` is the third state and the only one that is not a duration: it pins
    the computer against the sweep entirely. That is what a long job started
    inside the guest needs — a build or a batch run sends nothing from outside,
    so it is idle by every measure the host can take, and it would otherwise be
    suspended under its own feet. So ``0`` and ``None`` are opposites here
    rather than two spellings of "no setting", and only a negative is refused.

    The platform requires this to be the only field in the PATCH, which is why
    it has a method of its own rather than a keyword on ``rename``.
    """
    # Floats are refused rather than rounded, and that also shuts the NaN door:
    # `nan < 0` is False, so a NaN would otherwise sail past the negative check
    # and onto the wire as an idle window. `whole` also settles the subclass
    # question — the value compared below is the value that goes out.
    if minutes is not None:
        minutes = whole(
            minutes,
            "idle_suspend_min",
            message=f"idle_suspend_min must be an integer minute count or None, not {minutes!r}",
        )
    if minutes is not None and minutes < 0:
        raise ValueError(
            f"idle_suspend_min cannot be negative: {minutes!r}. Send 0 to stop this computer "
            "being suspended for idleness, or None to follow its host's own window"
        )
    return {"idle_suspend_min": minutes}


def schedule_body(*, enabled: bool, hour: int, minute: int, tz: str) -> dict[str, Any]:
    # Bools are ints, so ``0 <= True <= 23`` is true and ``json.dumps`` then
    # encodes them as booleans. The platform wants 0–23, not a JSON ``true``.
    hour = whole(hour, "hour", message=f"hour must be an integer, not {hour!r}")
    minute = whole(minute, "minute", message=f"minute must be an integer, not {minute!r}")
    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0-23")
    if not 0 <= minute <= 59:
        raise ValueError("minute must be 0-59")
    # `enabled` ARMS the schedule, so it is a flag rather than anything truthy.
    return {
        "enabled": flag(enabled, "enabled"),
        "hour": hour,
        "minute": minute,
        "tz": canonical(tz, "tz"),
    }


# --- input ----------------------------------------------------------------
#
# The verb set is Anthropic's computer tool, in full. The platform accepts both
# that vocabulary and this SDK's flatter one, so these bodies use whichever is
# clearer for each action — what matters is that every verb a computer-use model
# can emit has a method here, because the alternative is every user of this SDK
# writing the same seven stubs.


def _coordinate(value: Any, name: str, *, exc: type[Exception] = TypeError) -> int:
    """Reject values JSON can carry but the guest cannot use as coordinates.

    The TypeError default is what the pointer guards have always raised and what
    their tests read. ``window_body`` passes ``ValueError`` instead, because its
    width and height were guarded for the first time in OPL-4210 and took the
    type the rest of this file uses — one function refusing its two halves
    differently is worse than either type on its own.
    """
    return whole(value, name, exc=exc, message=f"{name} must be an integer coordinate")


def pointer_body(action: str, x: int, y: int) -> dict[str, Any]:
    x = _coordinate(x, "x")
    y = _coordinate(y, "y")
    return {"action": action, "x": x, "y": y}


def _whole_point(x: int | None, y: int | None) -> None:
    """Refuse half a coordinate, for the reason :func:`drag_body` refuses half
    an origin.

    Omitting both is a real request — "wherever the pointer already is" — and a
    different one from (0, 0). Giving one of the two is neither: it reads as a
    caller who meant to name a point, and zero-filling the half they left out
    produces a click or a scroll that succeeds somewhere else entirely. Nothing
    reports that, which is what makes it worth a ``ValueError`` here.
    """
    if (x is None) != (y is None):
        raise ValueError("give both x and y, or neither")
    if x is not None and y is not None:
        _coordinate(x, "x")
        _coordinate(y, "y")


def click_body(
    action: str, x: int | None, y: int | None, modifiers: tuple[str, ...]
) -> dict[str, Any]:
    """A click, optionally at a point and optionally with keys held down.

    No coordinate means "where the pointer already is", which is a real and
    different request from clicking (0, 0) — so the keys are omitted rather than
    sent as zeros.
    """
    _whole_point(x, y)
    body: dict[str, Any] = {"action": action}
    if x is not None and y is not None:
        body["x"] = x
        body["y"] = y
    if modifiers:
        body["text"] = "+".join(modifiers)
    return body


def drag_body(from_x: int | None, from_y: int | None, to_x: int, to_y: int) -> dict[str, Any]:
    """A press, a move, and a release — one gesture, not two clicks.

    ``start_coordinate`` is omitted when the caller did not give one, which asks
    the platform to drag from wherever the pointer is. It refuses that if nothing
    has moved the pointer yet, rather than guessing at an origin.

    Half an origin is refused here rather than dropped. ``drag(90, 80,
    from_x=10)`` reads as a caller who meant to name a starting point, and
    silently ignoring the half they gave produces a drag that succeeds while
    selecting a different region — the worst shape a mistake can take, because
    nothing reports it.
    """
    if (from_x is None) != (from_y is None):
        raise ValueError("give both from_x and from_y, or neither")
    _coordinate(to_x, "to_x")
    _coordinate(to_y, "to_y")
    if from_x is not None and from_y is not None:
        _coordinate(from_x, "from_x")
        _coordinate(from_y, "from_y")
    body: dict[str, Any] = {"action": "left_click_drag", "coordinate": [to_x, to_y]}
    if from_x is not None and from_y is not None:
        body["start_coordinate"] = [from_x, from_y]
    return body


def button_body(action: str, x: int | None, y: int | None) -> dict[str, Any]:
    """left_mouse_down / left_mouse_up, optionally moving first."""
    _whole_point(x, y)
    body: dict[str, Any] = {"action": action}
    if x is not None and y is not None:
        body["x"] = x
        body["y"] = y
    return body


_SCROLL_DIRECTIONS = ("up", "down", "left", "right")


def scroll_body(
    x: int | None, y: int | None, direction: str, amount: int, modifiers: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A wheel scroll, optionally at a point and optionally with keys held.

    The coordinate is omitted when the caller did not give one, which scrolls
    whatever is under the pointer. Sending zeros instead would move the pointer
    to the top-left corner first and scroll whatever happens to be there — which
    is what a defaulted ``scroll()`` did before the coordinate keys became
    optional.
    """
    direction = canonical(direction, "direction")
    if direction not in _SCROLL_DIRECTIONS:
        raise ValueError(f"direction must be one of {_SCROLL_DIRECTIONS}")
    amount = whole(amount, "amount", exc=ValueError, message="amount must be positive")
    if amount <= 0:
        raise ValueError("amount must be positive")
    _whole_point(x, y)
    body: dict[str, Any] = {
        "action": "scroll",
        "scroll_direction": direction,
        "amount": amount,
    }
    if x is not None and y is not None:
        # The tool-native spelling, not the flat pair. The platform reads a flat
        # x/y of 0,0 on a scroll as "no position" — it has to, because that is
        # what this SDK sent for every defaulted scroll before the arguments
        # became optional — so a caller who genuinely means the top-left corner
        # cannot say so that way. `coordinate` has no such history and is
        # unambiguous, which makes scroll(0, 0) mean the corner again.
        body["coordinate"] = [x, y]
    if modifiers:
        body["text"] = "+".join(modifiers)
    return body


def type_body(text: str) -> dict[str, Any]:
    return {"action": "type", "text": canonical(text, "text")}


def key_body(keys: tuple[str, ...]) -> dict[str, Any]:
    if not keys:
        raise ValueError("key() needs at least one key")
    return {"action": "key", "keys": [canonical(key, "key") for key in keys]}


def _positive_seconds(seconds: object, message: str = "seconds must be positive") -> float:
    # NaN fails every ordered comparison, so `seconds <= 0` alone would let it
    # through and become an HTTP timeout of nan + slack. Infinity passes that
    # comparison honestly and is worse: it serialises as a bare `Infinity` that
    # is not JSON, and as an httpx timeout it means a hung guest hangs the
    # caller with no deadline at all. Finiteness, not just sign, is what makes
    # a duration sendable.
    number = real(seconds, "seconds", message=message)
    if number <= 0:
        raise ValueError(message)
    return number


def hold_key_body(keys: tuple[str, ...], seconds: float) -> dict[str, Any]:
    if not keys:
        raise ValueError("hold_key() needs at least one key")
    seconds = _positive_seconds(seconds)
    return {
        "action": "hold_key",
        "keys": [canonical(key, "key") for key in keys],
        "duration": seconds,
    }


def wait_body(seconds: float) -> dict[str, Any]:
    seconds = _positive_seconds(seconds)
    return {"action": "wait", "duration": seconds}


def cursor_body() -> dict[str, Any]:
    return {"action": "cursor_position"}


def screenshot_params(width: int | None, fresh: bool = False) -> dict[str, Any] | None:
    """``w`` downscales, ``fresh`` skips the cache.

    A bare screenshot may be served from a frame up to 1.5 seconds old, which is
    right for a thumbnail and wrong for a loop: a model shown the frame from
    before its own click concludes the click missed and clicks again, and the
    second one lands on whatever the first one opened. ``fresh`` is therefore
    not an optimisation to reach for when a screenshot looks stale — it is what
    every screenshot feeding a decision wants, and the cost of it is one capture.

    Sent as ``1`` rather than ``true``: the platform documents the parameter as
    that single value and matches on it.
    """
    params: dict[str, Any] = {}
    if width is not None:
        width = whole(width, "width", exc=ValueError, message="width must be positive")
        if width <= 0:
            raise ValueError("width must be positive")
        params["w"] = width
    if flag(fresh, "fresh"):
        params["fresh"] = 1
    return params or None


#: The platform's ceiling on ``max_steps``, mirrored.
#:
#: ``MAX_MAX_STEPS`` in the platform's ``web/lib/agent.ts``, and kept in step by
#: ``scripts/check_surface.py`` — a mirror nobody compares is a comment, and one
#: that drifts refuses a run the platform would have taken.
#:
#: Capped rather than obeyed for the reason the platform gives: each step is a
#: model call plus a screenshot on the caller's own key, so a ``max_steps`` of
#: ten thousand is a request to spend their money for an hour on a task that has
#: plainly gone wrong.
MAX_STEPS = 100


def agent_body(
    prompt: str,
    *,
    stream: bool,
    system: str | None = None,
    max_steps: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """One agent run's request.

    An empty prompt is refused here rather than sent. The platform would answer
    400, but a run is the one call on this surface where a round trip is not the
    whole cost of getting it wrong: the request that comes back is billed
    against the caller's own model key, and nothing else in this file lets a
    caller spend money to be told they typed nothing.

    ``max_steps`` is checked for the same reason — it is the spending bound, and
    a zero or a negative is a request to do no work, which is not what anybody
    means by it. It is checked the way the platform checks it, rather than only
    at the near end of the range: a whole number, at least 1, and no more than
    :data:`MAX_STEPS`. A ``2.5`` or a ``10000`` that got through here would come
    back as the 400 this function exists to save the caller.
    """
    prompt = canonical(prompt, "prompt")
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if max_steps is not None:
        max_steps = whole(
            max_steps, "max_steps", exc=ValueError, message="max_steps must be a whole number"
        )
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if max_steps > MAX_STEPS:
            raise ValueError(f"max_steps may not exceed {MAX_STEPS}")
    body: dict[str, Any] = {"prompt": prompt, "stream": stream}
    for key, value in (("system", system), ("max_steps", max_steps), ("model", model)):
        if value is not None:
            body[key] = value
    return body


def stop_params(force: bool) -> dict[str, Any] | None:
    """``force=true`` pulls the power instead of asking the guest to shut down.

    Absent, the guest is asked and given time to do it. Present, it is not —
    the equivalent of holding the button in, and anything the guest had not
    written to disk is lost with it. Kept off by default for that reason: this
    is what to reach for when a guest will not come down on its own, not the
    ordinary way to stop one.

    A real bool, for the reason :func:`flag` gives. This one was missed when the
    other two arming flags were hardened (second adversarial review, OPL-3835),
    and it is the same defect: ``stop(force="false")`` pulled the power and lost
    whatever the guest had not written to disk.
    """
    return {"force": "true"} if flag(force, "force") else None


# --- webhooks -------------------------------------------------------------


def webhook(webhook_id: str) -> str:
    return f"webhooks/{seg(webhook_id)}"


def webhook_action(webhook_id: str, action: str) -> str:
    """rotate | test | deliveries."""
    return f"webhooks/{seg(webhook_id)}/{action}"


#: The platform's ceiling on a subscription's ``description``, mirrored so a
#: caller is refused here rather than after a round trip — and checked against
#: ``DESCRIPTION_MAX`` in ``web/lib/webhooks.ts`` by ``scripts/check_surface.py``,
#: so the copy cannot drift unnoticed.
WEBHOOK_DESCRIPTION_MAX = 200
#: The ceiling on ``computers``, mirrored from ``COMPUTERS_MAX`` the same way.
WEBHOOK_COMPUTERS_MAX = 64
#: The replay window, mirrored from ``REPLAY_WINDOW_S`` in
#: ``web/lib/webhooksign.ts``. Lives in ``_webhooks`` as the verifier's default
#: and is named here so the drift check, which reads every mirrored number off
#: this module, sees it.
WEBHOOK_REPLAY_WINDOW_S = 300


def _ids(value: object, what: str, *, limit: int | None = None) -> list[str]:
    """A list of strings for a filter, or a ``ValueError``.

    A bare ``str`` is refused rather than wrapped: ``str`` is a ``Sequence`` of
    its own characters, so ``events="process.exited"`` would otherwise reach the
    platform as fourteen one-letter event types and a 400 naming the vocabulary
    — the same defect ``watch=`` on the event stream had (OPL-4220). Wrapping
    it would be kinder and would also decide, on the caller's behalf, that a
    string in a list position was one item rather than a mistake.
    """
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValueError(  # noqa: TRY004 — one exception type for one class of mistake
            f"{what} must be a list of strings, not {type(value).__name__}"
        )
    items = [canonical(v, f"{what} entry") for v in value]
    if any(not item.strip() for item in items):
        raise ValueError(f"{what} must not contain an empty string")
    if limit is not None and len(items) > limit:
        raise ValueError(f"{what} may name at most {limit}, not {len(items)}")
    return items


def _webhook_url(url: object) -> str:
    """An endpoint, as the platform will accept it: ``https://``, and a host.

    Only the shape that can never pass is refused here. The platform's own
    checks — no credentials in the URL, a public address behind the name — need
    a resolver and are its to make; a caller sees those as a 400 naming the
    rule, exactly as it would have without this SDK.
    """
    text = canonical(url, "url")
    if not text.startswith("https://") or len(text) == len("https://"):
        raise ValueError(f"url must be https:// and name a host: {text!r}")
    return text


def _description(value: object) -> str:
    text = canonical(value, "description")
    if len(text) > WEBHOOK_DESCRIPTION_MAX:
        raise ValueError(
            f"description must be at most {WEBHOOK_DESCRIPTION_MAX} characters, not {len(text)}"
        )
    return text


_OMITTED: Any = object()


def webhook_body(
    *,
    url: object = _OMITTED,
    description: object = _OMITTED,
    events: object = _OMITTED,
    computers: object = _OMITTED,
    enabled: object = _OMITTED,
    create: bool,
) -> dict[str, Any]:
    """The create or update payload, with only the fields the caller named.

    One builder for both verbs because the fields are the same five and the
    platform reads them the same way; the difference is that a create needs
    ``url`` and an update needs SOMETHING. An update naming nothing is a 400
    upstream and is refused here for the same reason: it is never what a
    caller meant, and ``update(id)`` reading as a harmless no-op would hide a
    call whose keyword got dropped.

    ``events=[]`` and ``computers=[]`` are sent, not dropped: an empty list is
    how a filter is CLEARED on an update — "every type", "every computer in
    scope" — and a builder that stripped empties would leave a caller no way to
    say it.
    """
    body: dict[str, Any] = {}
    if url is not _OMITTED:
        body["url"] = _webhook_url(url)
    elif create:
        raise ValueError("url is required")
    if description is not _OMITTED:
        body["description"] = _description(description)
    if events is not _OMITTED:
        body["events"] = _ids(events, "events")
    if computers is not _OMITTED:
        body["computers"] = _ids(computers, "computers", limit=WEBHOOK_COMPUTERS_MAX)
    if enabled is not _OMITTED:
        body["enabled"] = flag(enabled, "enabled")
    if not body:
        raise ValueError("an update must name at least one field to change")
    return body
