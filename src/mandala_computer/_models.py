"""Response objects.

Deliberately permissive: unknown fields are preserved in ``raw`` rather than
rejected, so a server that starts returning more does not break older clients.
"""

from __future__ import annotations

import base64
import binascii
import builtins
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from operator import index as integer_index
from typing import Any, Literal, NoReturn, SupportsIndex, TypedDict, TypeVar, overload

from ._api import SECRET_ENV, SECRET_FILE
from ._exceptions import MandalaError

__all__ = [
    "AccountCapabilities",
    "AccountCompleteness",
    "AccountLimits",
    "AccountPerComputer",
    "AccountPlan",
    "AccountQuota",
    "AccountRemaining",
    "AccountUsage",
    "Activity",
    "ActivityHealth",
    "ActivityPage",
    "ActivityResults",
    "ApiKey",
    "ApiKeyCreated",
    "BrowserProxy",
    "BrowserProxyArgs",
    "BuildProgress",
    "BuildStep",
    "ComputerDeletion",
    "ComputerUsage",
    "DirectoryEntry",
    "ExecResult",
    "ExecStatus",
    "FilePart",
    "GuestDirectory",
    "LifecycleAck",
    "Listing",
    "Move",
    "Operation",
    "OperationError",
    "OperationPage",
    "PublishedTemplate",
    "Retention",
    "RetiredTemplates",
    "Secret",
    "SecretBinding",
    "SecretBindingArgs",
    "SecretBindings",
    "SecretLimits",
    "SecretList",
    "SecretsReceipt",
    "SignalPage",
    "Size",
    "Snapshot",
    "SnapshotHoldings",
    "SnapshotPurge",
    "SshAccess",
    "SshKey",
    "Template",
    "TemplateBuild",
    "TemplateCheck",
    "UsagePeriod",
    "UsageReport",
    "UsageTotals",
    "VncConnect",
    "Webhook",
    "WebhookCreated",
    "WebhookDelivery",
    "Whoami",
    "WhoamiAccount",
    "WhoamiUser",
    "WhoamiWorkspace",
    "Window",
    "WindowResult",
]

T = TypeVar("T")
S = TypeVar("S")


def _num(value: Any) -> int:
    """An integer field off the wire, or zero when it is unusable.

    A JSON boolean is not one of the usable ones. ``float(True)`` is ``1.0``,
    so ``"cpu": true`` decoded to a one-vCPU machine — a plausible number
    invented out of a value this promises to answer zero for, which is the
    reading a caller can at least see is wrong. :func:`_opt_num` and
    :func:`_exit_code` both refuse booleans explicitly and say why; this and
    :func:`_real` were the two that never learned the rule (adversarial review,
    OPL-4479).
    """
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0
    return int(number) if math.isfinite(number) else 0


def _opt_num(value: Any) -> int | None:
    """An integer field whose ABSENCE means something, or ``None``.

    :func:`_num`'s zero is the wrong floor for such a field, and ``Window.pid``
    is the case it was written for: a guest is free to advertise
    ``_NET_WM_PID`` 0, so a decoder that turns "the window did not say" into
    ``0`` has invented a process for it. A value this client cannot read is not
    a number either, and gets the same ``None`` rather than a zero that would
    be indistinguishable from a real one.

    Not :func:`_exit_code`, which raises: an unreadable exit code is a failed
    command mistaken for a successful one, and there is no safe value to carry
    on with. An unreadable pid is one window out of a listing missing an
    incidental field, and refusing the whole desktop over it would be the
    larger loss.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) else None


#: The most digits a decimal string may carry and still be converted here.
#:
#: The bound is not about plausibility — every integer field on this surface is
#: an exit code, a pid, a byte count or an offset, and none of them is twenty
#: digits — it is about arithmetic. ``int(text, 10)`` builds an integer of any
#: size out of a value the platform wrote, and past 4300 digits that call
#: itself raises ``ValueError`` on CPython's integer-string conversion limit:
#: a bare builtin out of a helper whose two callers promise otherwise, ``None``
#: from :func:`_opt_whole` and a :class:`~mandala_computer.MandalaError` from
#: :func:`_exit_code`, and which on the event path ends a live stream rather
#: than dropping one field (adversarial review, OPL-4479). Length is the one
#: property that can be tested before the conversion that is the hazard, so it
#: is tested first.
#:
#: 310 rather than something tighter because refusing here must not itself
#: become a lost value: a string this turns down falls through to ``float()``
#: in both callers, and only from 310 significant digits is that fall always
#: ``inf``, which they already refuse. Anything shorter would hand them a
#: finite float of a number too large to be one, which is the silent rounding
#: this function exists to prevent. 310 rather than 4300 because the limit is
#: tunable: ``sys.set_int_max_str_digits`` and ``PYTHONINTMAXSTRDIGITS`` reach
#: down to ``sys.int_info.str_digits_check_threshold``, 640, so the bound has
#: to hold under the floor rather than at today's default.
_MAX_EXACT_DIGITS = 310


def _exact_int(value: Any) -> int | None:
    """A whole number that ``float()`` would not be needed for, or ``None``.

    ``float`` is lossy past 2**53, so a Python ``int`` or a decimal string in
    that range must not be routed through it. Callers fall through to ``float``
    for values this cannot speak for — notably ``1.0`` and ``"3.0"``.

    ``str.isdecimal`` and not ``str.isdigit``, because the second is a wider
    grammar than ``int()``'s and the difference is exactly the characters
    ``int()`` refuses: ``"²"`` is a digit by that test and not a literal
    ``int()`` will read, so the guard passed and the conversion raised
    ``ValueError`` — from a decoder reading a field the caller never named
    (adversarial review, OPL-4479). ``isdecimal`` is the predicate that matches
    what ``int()`` accepts, Unicode decimal digits and all.

    Leading zeros come off before the bound is applied. They carry no magnitude
    but they do count against the conversion limit :data:`_MAX_EXACT_DIGITS`
    exists to stay under, so ``"0" * 5000 + "1"`` is ``1`` here rather than
    another ``ValueError``. Only a string over the bound is stripped at all —
    below it there is no padding to keep off a limit it already clears, and the
    conversion reads the same number either way.

    They are stripped by VALUE and not by character, because ``isdecimal``
    admits every decimal script and ``str.lstrip("0")`` knows only the ASCII
    one. Stripping by character left an Arabic-Indic ``"٠" * 400 + "٩" * 20``
    padded, over the bound, and refused — and a refusal here falls through to
    ``float``, which answers a finite ``1e20`` for it. The bound's whole
    premise is that what it turns down is too large for ``float`` to hold, so
    that padding turned an exactly readable number into a silently rounded one,
    and made the same value decode two ways depending on which script wrote its
    zeros (second review pass, OPL-4479).
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip()
        sign = text[:1] if text[:1] in ("+", "-") else ""
        digits = text[len(sign) :]
        if not digits.isdecimal():
            return None
        if len(digits) > _MAX_EXACT_DIGITS:
            lean = digits.lstrip("0")
            if not lean.isascii():
                # ASCII zeros came off above; what is left may still open with
                # another script's, and those have to be measured one at a
                # time. The test is on what REMAINS, not on whether `lstrip`
                # found anything: a single "0" in front of Arabic-Indic padding
                # takes the fast path away from the padding it exists for.
                lead = 0
                while lead < len(lean) - 1 and int(lean[lead]) == 0:
                    lead += 1
                lean = lean[lead:]
            digits = lean or "0"
            if len(digits) > _MAX_EXACT_DIGITS:
                return None
        return int(sign + digits, 10)
    return None


def _opt_whole(value: Any) -> int | None:
    """:func:`_opt_num`, refusing a number that was never a whole one.

    :func:`_opt_num` truncates, which is right for the fields it was written
    for — a window's geometry is whole on the wire and a fractional one is a
    rounding to live with. It is wrong for an EXIT CODE, where ``int(0.9)`` is
    ``0`` and ``0`` is the one value that reads as success. That is the misread
    :func:`_exit_code` exists to refuse, and the event stream reached it
    through ``_opt_num`` instead: a ``process.exited`` carrying ``0.9`` decoded
    to a clean pass, with ``lost`` false, so nothing said the code was
    unreadable.

    Not :func:`_exit_code` itself, which RAISES. This runs inside
    :func:`~mandala_computer._events.to_computer_event`, whose policy for a
    frame it cannot read is to hand it over with the field unset rather than
    end the connection over it — the same split :meth:`Window.from_api` draws.
    So an unreadable code is ``None``: "the platform sent one and this client
    could not read it", which is a thing a caller can branch on, where ``0``
    is a lie.
    """
    # An `int` is already whole and is answered without going through `float`,
    # which is `_exit_code`'s own fast path and is there for the same reason:
    # past 2**53 the conversion is lossy and the comparison below cannot see it,
    # because the float being compared IS a whole number. Routed through
    # `_opt_num` alone, 9007199254740993 came back as ...992 — a silently
    # different exit code, which is the class of wrong answer this function
    # exists to refuse (/code-review, OPL-4232). The same hole remained on the
    # decimal-string path (`"9007199254740993"`), which this SDK accepts.
    exact = _exact_int(value)
    if exact is not None:
        return exact
    number = _opt_num(value)
    if number is None:
        return None
    # `_opt_num` has already ruled out the non-finite and the unparseable, so
    # the only thing left to refuse is a finite number that was not whole.
    return number if float(value) == number else None


def _real(value: Any) -> float:
    """A fractional field off the wire, or zero when it is unusable.

    Its own helper rather than :func:`_num`, which truncates to ``int``. Every
    usage figure is fractional — 0.75 hours is a real session, and 0.13
    GB-months is a real charge — so truncating them would round most small
    accounts' usage to nothing.

    Booleans are refused for the reason :func:`_num` gives, and it lands on a
    billing figure here: ``"run_hours": true`` billed as one hour.
    """
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


class _Wire(Enum):
    """What a boolean field on the wire actually was, before anyone decided what
    it MEANS.

    Five states, because five is how many there are. ``_flag`` returned a
    ``bool`` and took an ``unknown=`` fallback to pick one, and that shape was
    the source of four rounds of defects on this branch (adversarial review,
    OPL-3835): every fallback boolean is wrong somewhere, because the right
    answer needs context the decoder cannot see. ``Move.live`` needs
    ``Move.state``; ``ExecStatus.more`` is both the sleep switch and half the
    break condition, so neither value is safe; a snapshot's ``unreachable``
    means opposite things on a sparse row and a full one. Handing callers a
    classification and letting each decide is the fix, and the reason this is an
    enum rather than another knob.
    """

    #: The key was not there. An older host that never heard of the field.
    ABSENT = auto()
    #: ``null``. NOT APPLICABLE in this API's own convention — ``cpu``,
    #: ``finished_at`` and ``exit_code`` all use it that way — rather than
    #: "cannot tell".
    NULL = auto()
    TRUE = auto()
    FALSE = auto()
    #: Present, and not anything this client can read.
    MALFORMED = auto()


def _wire(d: Mapping[str, Any], key: str) -> _Wire:
    """Classify one boolean field. It decides nothing.

    ``true``/``false`` and ``1``/``0`` are recognised however they are spelled —
    as JSON booleans, as numbers, or as strings. The original bug was
    TRUTHINESS, not recognition: ``bool("false")`` was True, which is wrong, but
    ``"false"`` still plainly means false, and a backend that encodes its
    booleans that way must not be told its every flag is unreadable.

    Integral floats are recognised with the ints. ``json.loads`` gives ``1`` for
    ``1`` and ``1.0`` for ``1.0``, so accepting one and not the other made the
    same wire value decode two ways (adversarial review, OPL-3835).
    """
    if key not in d:
        return _Wire.ABSENT
    value = d[key]
    # Before the numeric branch: `True == 1` in Python, and a real boolean must
    # not be classified by the int rule.
    if isinstance(value, bool):
        return _Wire.TRUE if value else _Wire.FALSE
    if value is None:
        return _Wire.NULL
    if isinstance(value, (int, float)) and value in (0, 1):
        return _Wire.TRUE if value == 1 else _Wire.FALSE
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1"):
            return _Wire.TRUE
        if text in ("false", "0"):
            return _Wire.FALSE
    return _Wire.MALFORMED


#: The documented shape of an unreachable placeholder row: an id, the flag, and
#: no ``computer_id``, because there was no daemon to say which computer it
#: belonged to. Shared rather than written twice — the sync and async filters had
#: identical copies, which is a drift waiting to happen and a test that only ever
#: covered one of them (adversarial review, OPL-3835).
def _opt_flag(d: Mapping[str, Any], key: str) -> bool | None:
    """A boolean the platform may leave out: ``None`` unless it plainly said."""
    said = _wire(d, key)
    return True if said is _Wire.TRUE else False if said is _Wire.FALSE else None


def is_unreachable_stub(row: Mapping[str, Any]) -> bool:
    """Whether a snapshot row stands in for one nobody could read.

    ROW SHAPE, not the flag alone. ``unreachable`` means opposite things on the
    two rows it can appear on: on a stub it is the marker saying a listing is
    short, and dropping it reports a confident count over an incomplete answer;
    on a FULL row belonging to another computer, admitting it hands back
    somebody else's snapshots from a method read before an irreversible delete.

    The discriminator is the PRESENCE of ``computer_id``, not its truthiness. A
    row carrying ``"computer_id": null`` or ``""`` is a full row that failed to
    fill the field in, and reading it as a stub admitted it into every
    computer's list (adversarial review, OPL-3835).
    """
    if "computer_id" in row:
        return False
    if _wire(row, "unreachable") in (_Wire.FALSE, _Wire.ABSENT):
        return False
    # THE SHAPE IS REQUIRED WHATEVER THE FLAG SAYS, and applying it only to the
    # unreadable values left the same hole one branch over (adversarial review,
    # OPL-3835): a full row with no `computer_id` and `unreachable: true` was
    # admitted into EVERY computer's filtered list. `POST
    # /computers/{id}/snapshots` answers without a `computer_id` too — it is in
    # the path — so the missing key alone cannot mean placeholder. The
    # documented stub is an id and this flag and nothing more.
    # A TOLERANT test, not an exact whitelist. `set(row) <= {"id",
    # "unreachable"}` stopped recognising a stub the moment the platform added a
    # `created_at` or a `kind` to it, and filtering those out drops precisely
    # the markers saying an answer is short (/code-review, OPL-3835). `state` is
    # what every real snapshot carries and no placeholder does — the same shape
    # of test `Computer.unreachable` makes with `status`.
    return "state" not in row


def _texts(value: Any) -> builtins.list[str]:
    """A list-of-strings field off the wire, or empty when it is unusable.

    ``d.get("x") or []`` reads as a safe default and is not one (adversarial
    review, OPL-3835). It guards ``None`` and nothing else: a NUMBER raises a
    bare ``TypeError`` out of the comprehension — from a public method, about a
    field the caller never named — and a STRING iterates by character, so
    ``"1.2.3"`` silently became ``['1', '.', '2', '.', '3']``. A version list
    that decodes to punctuation is worse than one that decodes to nothing.

    Degrading rather than raising is this module's stated contract: unknown and
    malformed fields are preserved in ``raw`` and never rejected, so a platform
    that starts answering differently does not break older clients.
    """
    if not isinstance(value, builtins.list):
        return []
    return [_text(v) for v in value]


def _text(value: Any) -> str:
    """A string field off the wire, with JSON null represented as empty."""
    return "" if value is None else str(value)


#: Whitespace inside a base64 field, which carries nothing and is stripped
#: before decoding. Line-wrapped base64 is ordinary; every other character
#: outside the alphabet is not, and is refused rather than discarded.
#:
#: ASCII SPELLED OUT rather than ``\s``, which is Unicode on a ``str`` pattern:
#: that stripped U+00A0, U+2028 and U+3000 as well, so a field mangled by an
#: entity-rewriting proxy or a mojibake round trip decoded to confident bytes
#: with nothing said about it (/code-review, OPL-4544) — the one outcome this
#: function is written to prevent. The platform encodes with Go's
#: ``base64.StdEncoding``, which never wraps at all, so narrowing costs nothing.
_SPACE = re.compile(r"[ \t\r\n\f\v]+")


def _b64(value: Any) -> bytes | None:
    """One base64 output field as bytes, or ``None`` when it cannot be read.

    ``stdout_b64`` and ``stderr_b64`` carry a command's output on every exec
    response and are base64 on all of them, never conditionally (platform
    OPL-4403). A JSON string is UTF-8 by definition and a command's output is
    bytes, so the ``stdout``/``stderr`` these replaced could only carry the
    bytes that happened to be valid UTF-8 and rewrote every other one into
    ``U+FFFD``, irreversibly. The obvious casualty is a tarball or a PNG; the
    reachable one is plainer, because a poll is cut at 1 MiB on a BYTE offset,
    so an ordinary UTF-8 build log longer than that had a multi-byte character
    split across the cut and both halves replaced.

    ``None`` rather than ``b""`` for a value this cannot decode, because on a
    consuming read those are opposite facts: empty means the command printed
    nothing, and unreadable means it printed something this client has just
    consumed and thrown away. The models keep the empty bytes and report the
    difference through ``output_unreadable``, the same way :attr:`ExecStatus.
    output_uncertain` reports a ``more`` that could not be read.

    STRICT about the alphabet, unlike the stdlib default, and TOLERANT of
    whitespace, unlike ``validate=True`` on its own. Whitespace carries nothing
    in base64 and encoders that wrap their output at a column are ordinary, so a
    newline must not cost a caller a megabyte of build log. Every OTHER
    character outside the alphabet says this field is not what this client
    thinks it is — and the stdlib default DISCARDS those and decodes what is
    left, which answers a corrupt field with confident bytes rather than
    admitting it could not read one. ``b64decode("YWJj!ZGVm")`` is ``b"abcdef"``
    with no complaint.
    """
    if value is None:
        return b""
    if not isinstance(value, str):
        return None
    if not value:
        return b""
    try:
        # `binascii.Error` subclasses `ValueError`; both are named because a
        # non-ASCII input raises the plain one on some interpreters.
        return base64.b64decode(_SPACE.sub("", value), validate=True)
    except (binascii.Error, ValueError):
        return None


def _output(d: Mapping[str, Any]) -> tuple[bytes, bytes, bool]:
    """Both output fields, decoded, and whether either was unreadable.

    Shared by :meth:`ExecResult.from_api` and :meth:`ExecStatus.from_api`
    because the pair is one decision made twice otherwise, and the four routes
    that answer these two shapes must not disagree about what an undecodable
    field means.

    A BODY IN THE OLD SHAPE counts as unreadable, and that is the second half of
    the case for not falling back to ``stdout``/``stderr``. Reading them would
    put back the ``U+FFFD`` rewriting they were renamed to end; not reading them
    and saying nothing was no better, because empty bytes beside ``ok`` is a
    clean successful run that printed nothing, which is what a caller parsing
    build output would have believed (/code-review, OPL-4544). The rename is
    supposed to fail loudly on a missing field and this decoder is what has to
    do the failing. The evidence is in the payload: neither ``*_b64`` key
    present and an old one that is, which is a host whose daemon predates
    platform OPL-4403 — reachable during a fleet rollout or a rollback, since
    exec has no projector in front of it and the daemon's shape is the public
    one.

    Absent everything is NOT that. ``ExecStatus.from_api({"pid": 1})`` is a
    sparse object, and a body that mentions no output at all is not a body
    claiming there was none.
    """
    out = _b64(d.get("stdout_b64"))
    err = _b64(d.get("stderr_b64"))
    old_shape = not ("stdout_b64" in d or "stderr_b64" in d) and ("stdout" in d or "stderr" in d)
    return out or b"", err or b"", out is None or err is None or old_shape


def _exit_code(value: Any) -> int | None:
    """An exit code off the wire, preserving null and rejecting JSON booleans.

    A WHOLE number, and nothing that merely survives ``int()``. That call
    truncates towards zero, so ``0.9`` became ``0`` and a command that failed
    was reported as one that succeeded — the single outcome this field's own
    docstring says must never happen. It also raises ``OverflowError`` on an
    infinity, which JSON produces for ``1e309`` and which is neither of the two
    exceptions caught below: that escaped ``exec()`` as a bare ``OverflowError``,
    past the :class:`~mandala_computer.MandalaError` this SDK promises.

    :func:`_num` and :func:`_opt_num` next door both test ``math.isfinite``
    before converting and both catch ``OverflowError``. This does the same, and
    then insists the number was an integer to begin with, because unlike those
    two it has no safe value to degrade to.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # `MandalaError`, like every other refusal here, and it was the one
        # `TypeError` among them. The README's promise is that everything the
        # PLATFORM answers with derives from `MandalaError`, and it scopes its
        # `TypeError` carve-out to arguments refused before anything is sent —
        # naming four, none of them a response field. So a JSON `true` here
        # escaped `exec()` as a bare builtin, past the handler this SDK tells
        # callers to write, while the malformed value one line down was
        # correctly refused (adversarial review, OPL-4232). It is the same
        # argument the paragraph above makes about `OverflowError`.
        raise MandalaError("exec answered with an invalid exit_code: a boolean is not one")
    # An `int` is already the answer, and going through `float` would stop it
    # being one: past 2**53 the conversion is lossy and `number != int(number)`
    # cannot see it, because the float it is comparing is a whole number — so
    # 9007199254740993 came back as ...992, silently, which is the same class of
    # wrong code this function exists to refuse (/code-review, OPL-4222). The
    # decimal-string path had the same hole; `_exact_int` closes both. No
    # exit code is anywhere near that; the point is that the check below is only
    # sound for values it can represent.
    exact = _exact_int(value)
    if exact is not None:
        return exact
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise MandalaError("exec answered with an invalid exit_code") from exc
    if not math.isfinite(number) or number != int(number):
        raise MandalaError("exec answered with an invalid exit_code")
    return int(number)


class Listing(list[T]):
    """A collection read that the platform may have had to answer short.

    A ``list``, so everything written against the old return type keeps working
    and nothing has to be unwrapped. What it adds is the answer to a question a
    bare list cannot carry: whether this is all of them.

    ``GET /computers``, ``GET /snapshots`` and ``GET /builds`` fan out across
    every hypervisor holding something of yours. One that cannot be reached
    makes the answer incomplete, and by default the platform refuses to send it
    —
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

    A SHORT build listing puts ``0`` here rather than a count — never a number,
    the way a computer listing sometimes can — and this object is then the only
    thing that says the answer was short: there is no placement cache for
    builds, so the platform has neither a count to give nor a marked row to
    append. A whole one is ``None`` like any other.

    Template catalogues also carry this metadata, but return a partial answer
    with HTTP 200 without requiring ``allow_partial``. As with builds, a zero
    count still means incomplete: templates on an unreachable host cannot be
    counted.

    The same is true of the other two whenever the key is scoped to one
    workspace: the platform withholds the marked rows from such a credential
    rather than name it ids from workspaces it cannot see, so this object is
    again all there is.
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
        machine, so it belongs on a server or in a page you trust. The
        CLIPBOARD crosses this socket where the bridge was provisioned, and
        :attr:`clipboard` is the field that says whether it was on this
        computer. Everything below used to be written so that a caller could
        infer that answer; it is now read rather than inferred, and what is
        left is what to do with it.

        ``True`` means the transport is open, not that a copy or a paste
        succeeds. The first paste of a session is often dropped, because the
        guest PULLS the text and vdagent may not own the selection yet, and a
        browser will not hand over the guest's clipboard without focus and
        permission. ``False`` means text a client pastes reaches QEMU and
        stops, silently, with no error to catch.

        A ``False`` is sometimes fixable and sometimes not, because the bridge
        has two halves. The channel is hardware and comes from a COLD start —
        stop the computer and start it again; restarting a RUNNING one does
        not do it, since that resets the guest rather than rebuilding the
        machine QEMU was given. The agent is ``spice-vdagent`` in the guest and
        comes from the IMAGE, which a computer keeps for life, so one built
        before the agent shipped needs it installed there — which you may do,
        having root — or replacing with a newly created computer. Windows
        guests never have it, whatever the hardware says.

        Keep the route below whichever you get.

        :meth:`Computer.clipboard` and :meth:`Computer.set_clipboard` are the
        route to build on — the reliable one, not merely the fallback — because
        they need nothing of the HARDWARE: no cold boot, and no permission from
        a browser. What they do want is a Linux guest with a display and
        ``xclip`` in the image, since they drive the guest's own desktop
        session; Windows is refused outright, and a computer built from a golden
        that predates ``xclip`` gets a permanent 400 that says so. That is a
        much smaller set than the socket's two conditions, and unlike them it is
        stated in the answer rather than left to be inferred. Where the socket
        does carry the clipboard the two do not fight over it — those methods
        write the same X CLIPBOARD selection the agent then offers onward.

        They replace what this SDK documented here as a recipe over
        :meth:`Computer.exec` with ``desktop=True``, and going back to it is a
        mistake worth naming. Public ``exec`` runs a LOGIN shell, which sources
        the desktop user's own profile onto the same stdout your command prints
        to, ahead of it — wanted when you asked to run a command the way the
        user would, and fatal for reading a value, since an ``echo`` left in a
        ``.profile`` corrupts the answer and a deliberate one forges it. No
        framing you add fixes that: a profile that prints your frame owns
        everything after it. The clipboard endpoints do not share that stream.
        The write was worse — an X selection belongs to a live process, so the
        holder had to outlive the exec under ``setsid`` and have its output
        redirected or the call hung to its full timeout; the text had to be
        base64 and quoted or an apostrophe ended the shell word; and the result
        had to be polled for in a loop bounded in ATTEMPTS, each one billable,
        because being granted a selection is asynchronous.
        :meth:`Computer.set_clipboard` does all of it, confirms the selection
        was taken before it returns, and bills once.
    ``view_token``
        Watch only. The daemon drops input on a socket opened with it, so a
        browser holding this one cannot type even from a patched client. The
        guest's CLIPBOARD does not come back over it either, and that is
        enforced rather than asked for: the daemon takes the clipboard
        capability out of the connection as it is negotiated. Worth knowing if
        you embed this — whatever the person using the desktop copies,
        including a password, is not visible to anyone holding this URL.

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
    #: Websocket URL streaming what this computer does without being asked —
    #: what :meth:`Computer.events` opens. Carries the same controlling
    #: credential as :attr:`url`, so treat it as that credential: a window title
    #: is content, which is why a watch-only connection is given no URL here at
    #: all. ``""`` on a Windows guest, which has nowhere to run the watcher the
    #: guest half of the stream needs.
    #:
    #: Re-read it rather than keeping it. The credential in it is rotated by a
    #: restart, and a restart is one of the ordinary reasons the socket dropped
    #: in the first place — :class:`~mandala_computer.EventStream` asks the
    #: platform again on every reconnect for exactly that reason.
    # Keyword-only for the reason `clipboard` below is, and the promise is the
    # same one `Window` learned to keep on OPL-4191: this class is exported, so
    # its field order IS its constructor, and `raw` has been the seventh
    # positional argument since this SDK shipped.
    events_url: str = field(default="", kw_only=True)
    #: Whether this socket was provisioned with the platform-controlled halves
    #: of the guest clipboard bridge (OPL-3870) — the vdagent channel QEMU was
    #: given at cold boot, and whether the image this computer was built from
    #: was verified to ship ``spice-vdagent``.
    #:
    #: A PROVISIONING signal rather than current availability, and the
    #: distinction is not pedantic: somebody with root in the guest can install,
    #: remove, disable or stop the agent afterwards and this field will not
    #: move. It is also always ``False`` on a socket opened with
    #: :attr:`view_token`, because the daemon takes the extended-clipboard
    #: capability out of a watch-only connection as it is negotiated — there the
    #: ``False`` is about the credential rather than about the computer.
    #:
    #: ``False`` when the platform does not send it at all, which is the
    #: conservative reading and deliberately not "unknown": the two ways to be
    #: wrong are not symmetric. A ``False`` about a working bridge costs a
    #: caller nothing but the socket, since :meth:`Computer.clipboard` and
    #: :meth:`Computer.set_clipboard` work there too; a ``True`` about an absent
    #: one is the silently dropped paste this field exists to end.
    # Keyword-only so adding this field does not move the long-established
    # positional slot for ``raw``. Besides breaking construction, accepting an
    # old positional ``raw`` value here would make the hand-written repr render
    # that payload (and any credentials in it) as ``clipboard``.
    clipboard: bool = field(default=False, kw_only=True)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any] | None) -> VncConnect | None:
        """Build one, or ``None`` when the API did not supply a full set.

        Absent rather than partial is the platform's own rule: a URL built over
        a missing credential is a string indistinguishable from a working one
        that answers 401 forever. Anything short of both credentials is treated
        as no connect surface at all.

        Which is why every URL here is read as ``or ""`` rather than through a
        ``dict.get`` default: a default answers a missing key and a JSON
        ``null`` defeats it, and ``str(None)`` is ``"None"`` — a five-character
        string that is not a URL and does not read as an absent one either, so
        it would be handed to a connect call and fail obscurely instead of
        being reported as nothing to connect to (adversarial review, OPL-4479).
        ``terminal_url`` and ``events_url`` were already spelled this way; the
        rule now holds for all five.
        """
        if not isinstance(d, Mapping):
            return None
        token = str(d.get("token") or "")
        view_token = str(d.get("view_token") or "")
        if not token or not view_token:
            return None
        return cls(
            url=str(d.get("url") or ""),
            view_url=str(d.get("view_url") or ""),
            token=token,
            view_token=view_token,
            embed_url=str(d.get("embed_url") or ""),
            terminal_url=str(d.get("terminal_url") or ""),
            events_url=str(d.get("events_url") or ""),
            # `is True` rather than truthiness, and absent lands on False: the
            # platform sends this present-and-false rather than omitting it, so
            # anything that is not a literal true — a missing key on an older
            # deployment, a string, a null — is not an assertion that the bridge
            # is there, and this is the field where guessing yes is the costly
            # direction.
            clipboard=d.get("clipboard") is True,
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
            f"terminal_url={self._without_credential(self.terminal_url)!r}, "
            f"events_url={self._without_credential(self.events_url)!r}, "
            # Not a credential and not derived from one, so it is printed
            # whole. It is also the field somebody reading a repr to work out
            # why a paste went nowhere is looking for.
            f"clipboard={self.clipboard!r})"
        )


@dataclass(frozen=True)
class Template:
    name: str
    label: str
    os: str
    cpu: int
    ram_mb: int
    disk_gb: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    #: The pinned ``namespace/name@version``, when the platform sent one.
    #:
    #: ``None`` only from a host too old to advertise refs. It matters more than
    #: it looks: since OPL-3789 a template an account PUBLISHED is named by its
    #: ref and by nothing else — the short ``name`` still resolves to the
    #: platform's own catalogue — so a listing without this cannot tell a caller
    #: how to launch their own template. The platform publishes it for exactly
    #: that reason, and this model was dropping it on the floor.
    #:
    #: KEYWORD-ONLY, after the original slots, rather than second where it reads best. This
    #: class is exported, so its field order is its constructor: added ahead of
    #: ``label`` it broke every ``Template("ubuntu", "Ubuntu", ...)`` that worked
    #: on the previous release, in fixtures and downstream code alike
    #: (adversarial review, OPL-3835). Moving it past those six then quietly
    #: broke the seventh — ``raw`` was the seventh positional slot, so
    #: ``Template(..., disk_gb, raw_dict)`` bound the mapping to ``ref`` and left
    #: ``raw`` empty, without raising (second adversarial review). ``kw_only`` is
    #: what leaves every existing position alone, and this comment sits under
    #: ``raw`` so that Sphinx attaches it to the field it describes rather than
    #: to the one above it (/code-review). Decoding never noticed any of it —
    #: ``from_api`` passes by keyword.
    ref: str | None = field(default=None, kw_only=True)
    #: Desktop protocol advertised by the template, or ``None`` when absent.
    #: ``raw`` preserves the response; the SDK does not synthesize a protocol.
    #: For Linux, the server uses X11 when the value is omitted or empty;
    #: only explicit ``wayland`` selects Wayland. The SDK uses the same public
    #: window and clipboard routes for both protocols.
    desktop: str | None = field(default=None, kw_only=True)
    #: Icon advertised by the template, or ``None`` when absent.
    icon: str | None = field(default=None, kw_only=True)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Template:
        ref = d.get("ref")
        desktop = d.get("desktop")
        icon = d.get("icon")
        return cls(
            name=_text(d.get("name")),
            ref=None if ref is None else _text(ref),
            desktop=None if desktop is None else _text(desktop),
            icon=None if icon is None else _text(icon),
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
            versions=_texts(d.get("versions")),
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

    :attr:`build_digest` and :attr:`build_digest_needs` are ALTERNATIVES. A
    document with no parent gets the digest; one naming a parent in ``spec.from``
    gets the sentence saying what could not be computed and where to compute it.
    Reading only the first leaves a whole class of document looking like a
    failure with no reason attached, which is what it did here until OPL-4193.
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
    #: without the parent's — and then :attr:`build_digest_needs` says so in
    #: words, because the platform sends the two as alternatives.
    build_digest: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # All three keyword-only at the end rather than beside the fields they
    # belong with: this class is exported, so its field order is its
    # constructor, and `Template.ref` already cost a release learning what a
    # mid-order insertion does to every positional construction. Same reasoning
    # as `Window.visible` (OPL-4191).

    #: Why there is no :attr:`build_digest`, in a sentence meant for a person.
    #:
    #: It REPLACES :attr:`build_digest` rather than accompanying it — the
    #: platform sends one or the other, never both — so this is the field that
    #: turns "the digest is missing" into an answer::
    #:
    #:     check = client.templates.validate(doc)
    #:     if check.build_digest is None and check.build_digest_needs:
    #:         print(check.build_digest_needs)
    #:
    #: which prints, for a document naming a parent:
    #:
    #:     the contents of acme/base's image, which only a host holding it can
    #:     supply. Run ``gorillad -build-template <file> -dry-run`` there to see
    #:     this document's build digest
    #:
    #: ``None`` on an invalid document, on one with no parent — where
    #: :attr:`build_digest` is the answer instead — and from a host too old to
    #: send it, which is every host before OPL-4179 documented the field.
    build_digest_needs: str | None = field(default=None, kw_only=True)
    #: The document as the digests were taken over it, as a string of compact
    #: JSON with no trailing newline.
    #:
    #: Key order and whitespace are normalised, which is the whole point: two
    #: YAML files differing only in comments and key order are the same document
    #: and hash the same. This is what lets you check :attr:`doc_digest` rather
    #: than trust it::
    #:
    #:     import hashlib
    #:     mine = "sha256:" + hashlib.sha256(check.canonical.encode()).hexdigest()
    #:     assert mine == check.doc_digest
    #:
    #: A STRING and not a parsed object, and that is what makes the check
    #: possible — :attr:`PublishedTemplate.document` is the same document as a
    #: mapping, and a mapping cannot be re-serialised back to the exact bytes
    #: that were hashed. ``None`` on an invalid document, which has no digests
    #: to be canonical for.
    canonical: str | None = field(default=None, kw_only=True)
    #: The catalogue row this document describes, in the shape
    #: :meth:`~mandala_computer.Templates.list` answers.
    #:
    #: A real :class:`Template`, which it could not be until OPL-4190: this
    #: route was the one place on the surface where `template` meant the
    #: daemon's own wider row — the one carrying `family` — rather than the
    #: projected shape every other route sends. It is projected here now, so
    #: there is one `Template` shape again and this is it.
    #:
    #: ``None`` on an invalid document, which never parsed far enough to
    #: describe a row. A deployment from before that projector still answers
    #: with the wider row and still decodes to a :class:`Template` — the extra
    #: ``family`` lands in ``raw`` there, as any unmodelled key does.
    template: Template | None = field(default=None, kw_only=True)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> TemplateCheck:
        def maybe(key: str) -> str | None:
            value = d.get(key)
            return None if value is None else _text(value)

        # `template` is the seventh key a valid answer carries, and since
        # OPL-4190 it is the same row `GET /templates` lists — so it decodes
        # through the same machinery as everywhere else. `None` rather than an empty `Template` when it is
        # absent: an invalid document describes no row, and a row of zeroes
        # would be a size, an OS and a name asserted about a file that has none.
        template = d.get("template")
        return cls(
            valid=_wire(d, "valid") is _Wire.TRUE,
            problems=_texts(d.get("problems")),
            ref=maybe("ref"),
            doc_digest=maybe("doc_digest"),
            build_digest=maybe("build_digest"),
            build_digest_needs=maybe("build_digest_needs"),
            canonical=maybe("canonical"),
            template=Template.from_api(template) if isinstance(template, Mapping) else None,
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> RetiredTemplates:
        return cls(
            retired=_texts(d.get("retired")),
            retired_at=_text(d.get("retired_at")),
            versions=_texts(d.get("versions")),
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

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


#: What a build stops on. ``running`` is the only other status the platform
#: sends. Here rather than in _resources, so the field and everything that reads
#: it cannot drift apart — they have twice.
BUILD_TERMINAL = ("succeeded", "failed")


def _terminal_status(value: Any) -> bool:
    """Whether the wire sent a status a build STOPS on.

    Reads the RAW value and requires a string. This SDK is safe from the
    coercion that caught the TypeScript one — ``str(["succeeded"])`` is
    ``"['succeeded']"`` where ``String(["succeeded"])`` is ``"succeeded"``,
    because a JavaScript array of one joins to its element — but it is safe by
    accident of formatting rather than by rule (adversarial review, OPL-3835).
    Accident is not agreement, so both clients say it outright.
    """
    return isinstance(value, str) and value in BUILD_TERMINAL


def _build_done(d: Mapping[str, Any]) -> bool:
    """Whether a build record says the build is OVER.

    A RECOGNISED ``done`` is authoritative; absent, null or unreadable falls
    through to the status. The order is the platform's own: ``done`` is derived
    from the JOB, where the status is derived from a phase read out of a log the
    document's own steps write into.

    On the FIELD rather than beside it, and that is the correction (adversarial
    review, OPL-3835). This rule lived in ``build_ended`` in _resources while
    ``BuildProgress.done`` decoded the key alone, so ``{"status": "succeeded",
    "done": null}`` made ``wait()`` return an object whose own documented
    "whether to stop polling" field said False. Two spellings of one question
    drifted apart twice; now there is one.

    A ``done`` OF TRUE AGAINST A RUNNING STATUS IS NOT TERMINAL. The record
    contradicts itself, and the reading that trusts the flag turns an active
    build into a settled one. This half comes from the TypeScript SDK, whose own
    review of the same surface caught it while this one did not — see
    :func:`build_contradiction` for the other half of the answer, which is that
    a wait says so rather than polling a self-contradicting record in silence.
    """
    said = _wire(d, "done")
    if said is _Wire.TRUE:
        return _terminal_status(d.get("status"))
    if said is _Wire.FALSE:
        return False
    return _terminal_status(d.get("status"))


def build_contradiction(progress: BuildProgress) -> str | None:
    """Why this record cannot be believed, or ``None`` if it can.

    Only one shape qualifies: a ``done`` the wire actually said was true,
    against a status that is not terminal. Absent, null and unreadable are NOT
    contradictions — they are a host that said nothing, and the status answers
    for them.

    Separate from :func:`_build_done` because the two answer different
    questions and only one of them belongs on a model whose contract is that
    malformed fields are preserved and never rejected. The field stays lenient;
    the wait and the stream, which would otherwise poll a record like this until
    their deadline, raise.
    """
    if _wire(progress.raw, "done") is not _Wire.TRUE:
        return None
    # The RAW status, for the reason `_terminal_status` gives: `progress.status`
    # has been through `_text` and a coerced value cannot classify.
    if _terminal_status(progress.raw.get("status")):
        return None
    return (
        f"build {progress.id or '?'} reports done with status "
        f"{progress.status!r}, which is not one a build stops on "
        f"({' or '.join(BUILD_TERMINAL)}). The record contradicts itself, so "
        "neither half of it can be trusted — read progress() again."
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> BuildProgress:
        rows = d.get("steps")
        rows = rows if isinstance(rows, builtins.list) else []
        return cls(
            id=_text(d.get("id")),
            status=_text(d.get("status")),
            done=_build_done(d),
            phase=_text(d.get("phase")),
            step=_num(d.get("step")),
            of=_num(d.get("of")),
            steps=[BuildStep.from_api(r) for r in rows if isinstance(r, Mapping)],
            note=_text(d.get("note")),
            error=_text(d.get("error")),
            updated_at=_text(d.get("updated_at")),
            unmatched=_wire(d, "unmatched") in (_Wire.TRUE, _Wire.MALFORMED),
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
            allowed=_wire(d, "allowed") is _Wire.TRUE,
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
    #:     Still being taken, and NOT a snapshot yet: restore, clone and delete
    #:     all answer 404 on one, and a listing puts these first. THE ID IS
    #:     ALREADY THE SNAPSHOT'S OWN — allocated before the copy starts, kept
    #:     when it lands — so this is the row to poll rather than a stand-in
    #:     that gets replaced by another (platform OPL-4562). It used to be
    #:     ``cap-`` and the computer's id, which named the work and not the
    #:     thing, leaving a caller nothing to match on but "the newest row of a
    #:     fresh listing" — a guess a scheduled capture landing in the same
    #:     window gets wrong.
    #:
    #:     A capture that FAILS leaves nothing: this row disappears and no
    #:     snapshot takes its place, which is the only signal there is.
    #: ``"pending"``
    #:     On its host and usable. This is the point to act from.
    #: ``"durable"``
    #:     In backup storage as well. See :attr:`is_durable`.
    #: ``"deleting"``
    #:     A deletion got as far as detaching this snapshot's dependents and
    #:     did not finish. ONLY LISTED WHEN ASKED FOR —
    #:     ``include_unfinished=True`` — because a half-deleted snapshot is not
    #:     one anything can be restored or cloned from; it is still holding
    #:     objects and still billed, which is why it is askable at all. See
    #:     :attr:`is_deleting`.
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
    #:
    #: ``True`` only when a complete inventory of the fleet found the source
    #: computer absent. The platform omits the field when it cannot establish
    #: that — see :attr:`computer_unreachable` — and an omitted one reads
    #: ``False`` here, so ``False`` is not proof that the computer exists.
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # New fields go AFTER every existing one, so a positional construction
    # written against an earlier version keeps its meaning.
    #: The source computer's presence could not be established, so
    #: :attr:`orphaned` was not decided. The snapshot's bytes may well still be
    #: usable.
    computer_unreachable: bool = False
    #: Whether this snapshot has a usable copy on the host its computer is on
    #: now, which is what :meth:`~mandala_computer.Snapshots.restore` needs.
    #: ``False`` for a copy left on a computer's old host after a move: restore
    #: is refused, and :meth:`~mandala_computer.Snapshots.clone` still works
    #: while the snapshot itself is reachable. ``None`` when the platform did
    #: not say.
    restore_available: bool | None = None

    @property
    def is_memory(self) -> bool:
        """True for a live RAM+disk capture, which forks/restores without booting."""
        return self.kind == "memory"

    @property
    def is_capturing(self) -> bool:
        """True while this is a capture in flight rather than a snapshot.

        What :meth:`~mandala_computer.Computer.snapshot` hands back under
        ``wait=False``, and what a listing shows for a capture somebody else
        started. Restore, clone and delete all 404 on one; :attr:`id` is
        nonetheless the id the snapshot will keep, so it is what to poll on.
        """
        return self.state == "capturing"

    @property
    def is_deleting(self) -> bool:
        """True while a deletion of this snapshot is under way, or stalled.

        The opposite polarity to :attr:`is_capturing`, and the trap in it: a
        capture that fails leaves NO row, so absence is failure there, while a
        deletion that finishes is what removes the row, so absence is success
        here. A row in this state is the deletion still working or the deletion
        stopped, and nothing distinguishes those two from outside — the platform
        retries a stalled one on its own sweep, and
        :meth:`~mandala_computer.Snapshots.delete` sent again picks it up rather
        than being refused.

        Seen only in a listing taken with ``include_unfinished=True``.
        """
        return self.state == "deleting"

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
            # `or "disk"` rather than a `dict.get` default, which answers a
            # missing key and nothing else. The default is there to classify
            # rows from hosts that predate the field, and an explicit null
            # defeated it: the same wire value decoded two ways, and the null
            # left `is_memory` and every `kind ==` comparison reading an
            # unclassified snapshot (adversarial review, OPL-4479).
            kind=_text(d.get("kind")) or "disk",
            state=_text(d.get("state")),
            size_bytes=_num(d.get("size_bytes")),
            created_at=_text(d.get("created_at")),
            incremental=_wire(d, "incremental") is _Wire.TRUE,
            auto=_wire(d, "auto") is _Wire.TRUE,
            computer_name=_text(d.get("computer_name")),
            orphaned=_wire(d, "orphaned") is _Wire.TRUE,
            computer_unreachable=_wire(d, "computer_unreachable") is _Wire.TRUE,
            restore_available=_opt_flag(d, "restore_available"),
            # The same reading `is_unreachable_stub` uses, and it did not match:
            # the filter kept a row whose flag was null or unreadable and then
            # this decoded it False, so a caller told to check `unreachable`
            # before believing anything else read a placeholder — empty
            # computer_id, empty state, zero bytes — as a real snapshot, and
            # summed it into a total or passed its id to a delete
            # (/code-review, OPL-3835). Row shape decides an unreadable flag
            # here too: only a row that could not be anything but a stub.
            unreachable=(
                _wire(d, "unreachable") is _Wire.TRUE
                or (
                    _wire(d, "unreachable") in (_Wire.NULL, _Wire.MALFORMED)
                    and is_unreachable_stub(d)
                )
            ),
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

    #: Physical snapshot copies across the whole fleet — a snapshot held on two
    #: hosts counts twice — including captures and unfinished deletions.
    count: int
    size_bytes: int
    fingerprint: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # New fields go after ``raw``, so an earlier positional call keeps its meaning.
    #: Whether the source computer is present now. Bound into
    #: :attr:`fingerprint`; ``None`` when the platform did not say.
    computer_present: bool | None = None
    #: Captures in flight. A purge is refused while any remain.
    capturing: int = 0
    #: Copies whose deletion has not finished.
    deleting: int = 0

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> SnapshotHoldings:
        return cls(
            count=_num(d.get("count")),
            size_bytes=_num(d.get("size_bytes")),
            fingerprint=_text(d.get("fingerprint")),
            computer_present=_opt_flag(d, "computer_present"),
            capturing=_num(d.get("capturing")),
            deleting=_num(d.get("deleting")),
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Retention:
        return cls(
            daily=_num(d.get("daily")),
            weekly=_num(d.get("weekly")),
            monthly=_num(d.get("monthly")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class AccountPlan:
    """Effective plan catalogue identity, without account or billing identifiers."""

    id: str
    label: str


@dataclass(frozen=True)
class AccountLimits:
    """Account pool ceilings. Zero is a real ceiling, never unlimited."""

    max_computers: int
    vcpu_pool: int
    ram_pool_mb: int
    disk_pool_gb: int
    snapshot_storage_bytes: int


@dataclass(frozen=True)
class AccountPerComputer:
    """Per-computer shape maxima in vCPU, RAM MB and disk GB."""

    max_vcpu: int
    max_ram_mb: int
    max_disk_gb: int


@dataclass(frozen=True)
class AccountCapabilities:
    """Features permitted by the plan."""

    windows: bool


@dataclass(frozen=True)
class AccountCompleteness:
    """Independent inventory observations; False means that group's figures are None."""

    computers: bool
    snapshots: bool


@dataclass(frozen=True)
class AccountUsage:
    """Current consumption, with None for every figure of an incomplete group."""

    kept_computers: int | None
    #: Configured CPU and disk include stopped computers. Disk is provisioned GB.
    configured_vcpu: int | None
    configured_disk_gb: int | None
    running_or_reserved_computers: int | None
    running_or_reserved_vcpu: int | None
    #: Running guest RAM plus pending reservations, in MB.
    running_or_reserved_ram_mb: int | None
    #: Indexed stored bytes, excluding in-flight capture reservations.
    snapshot_storage_bytes: int | None


@dataclass(frozen=True)
class AccountRemaining:
    """Headroom clamped at zero; usage stays visible even above plan ceilings."""

    kept_computers: int | None
    configured_vcpu: int | None
    configured_disk_gb: int | None
    running_or_reserved_ram_mb: int | None
    #: Indexed-byte headroom does not predict snapshot capture admission.
    snapshot_storage_bytes: int | None


@dataclass(frozen=True)
class AccountQuota:
    """Instantaneous quota for the whole account, including workspace-scoped keys.

    Read ``complete`` before using numbers. This observation is advisory,
    reserves nothing, and is separate from historical metering in UsageReport.
    Unknown future fields remain in ``raw``, using the same shallow copy as Usage.
    """

    scope: Literal["account"]
    advisory: Literal[True]
    #: UTC collection completion time, not a consistency token.
    observed_at: str
    plan: AccountPlan
    limits: AccountLimits
    per_computer: AccountPerComputer
    capabilities: AccountCapabilities
    complete: AccountCompleteness
    usage: AccountUsage
    remaining: AccountRemaining
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, value: object) -> AccountQuota:
        """Decode required fields without coercing unknown consumption into zero."""

        def refuse(field: str, expected: str) -> NoReturn:
            raise MandalaError(f"expected an account quota report: {field} must be {expected}")

        def record(v: object, field: str) -> Mapping[str, Any]:
            return v if isinstance(v, Mapping) else refuse(field, "an object")

        def text(v: object, field: str) -> str:
            return v if isinstance(v, str) and v else refuse(field, "a nonempty string")

        def flag(v: object, field: str) -> bool:
            return v if isinstance(v, bool) else refuse(field, "a boolean")

        def quantity(v: object, field: str) -> int:
            if (
                isinstance(v, (int, float))
                and not isinstance(v, bool)
                and 0 <= v <= 2**53 - 1
                and int(v) == v
            ):
                return int(v)
            return refuse(field, "a nonnegative safe integer")

        def observed(v: object, field: str, complete: bool) -> int | None:
            if complete:
                return quantity(v, field)
            return None if v is None else refuse(field, "null when incomplete")

        d = record(value, "response")
        if d.get("scope") != "account":
            refuse("scope", "account")
        if d.get("advisory") is not True:
            refuse("advisory", "true")
        observed_at = text(d.get("observed_at"), "observed_at")
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z", observed_at):
            refuse("observed_at", "a UTC timestamp")
        try:
            datetime.fromisoformat(observed_at[:-1] + "+00:00")
        except ValueError:
            refuse("observed_at", "a UTC timestamp")
        plan = record(d.get("plan"), "plan")
        limits = record(d.get("limits"), "limits")
        per = record(d.get("per_computer"), "per_computer")
        caps = record(d.get("capabilities"), "capabilities")
        complete = record(d.get("complete"), "complete")
        usage = record(d.get("usage"), "usage")
        remaining = record(d.get("remaining"), "remaining")
        computers = flag(complete.get("computers"), "complete.computers")
        snapshots = flag(complete.get("snapshots"), "complete.snapshots")
        # A missing nullable field is malformed, not an explicit null.
        missing = object()
        return cls(
            scope="account",
            advisory=True,
            observed_at=observed_at,
            plan=AccountPlan(
                id=text(plan.get("id"), "plan.id"), label=text(plan.get("label"), "plan.label")
            ),
            limits=AccountLimits(
                max_computers=quantity(limits.get("max_computers"), "limits.max_computers"),
                vcpu_pool=quantity(limits.get("vcpu_pool"), "limits.vcpu_pool"),
                ram_pool_mb=quantity(limits.get("ram_pool_mb"), "limits.ram_pool_mb"),
                disk_pool_gb=quantity(limits.get("disk_pool_gb"), "limits.disk_pool_gb"),
                snapshot_storage_bytes=quantity(
                    limits.get("snapshot_storage_bytes"), "limits.snapshot_storage_bytes"
                ),
            ),
            per_computer=AccountPerComputer(
                max_vcpu=quantity(per.get("max_vcpu"), "per_computer.max_vcpu"),
                max_ram_mb=quantity(per.get("max_ram_mb"), "per_computer.max_ram_mb"),
                max_disk_gb=quantity(per.get("max_disk_gb"), "per_computer.max_disk_gb"),
            ),
            capabilities=AccountCapabilities(
                windows=flag(caps.get("windows"), "capabilities.windows")
            ),
            complete=AccountCompleteness(computers=computers, snapshots=snapshots),
            usage=AccountUsage(
                kept_computers=observed(
                    usage.get("kept_computers", missing), "usage.kept_computers", computers
                ),
                configured_vcpu=observed(
                    usage.get("configured_vcpu", missing), "usage.configured_vcpu", computers
                ),
                configured_disk_gb=observed(
                    usage.get("configured_disk_gb", missing), "usage.configured_disk_gb", computers
                ),
                running_or_reserved_computers=observed(
                    usage.get("running_or_reserved_computers", missing),
                    "usage.running_or_reserved_computers",
                    computers,
                ),
                running_or_reserved_vcpu=observed(
                    usage.get("running_or_reserved_vcpu", missing),
                    "usage.running_or_reserved_vcpu",
                    computers,
                ),
                running_or_reserved_ram_mb=observed(
                    usage.get("running_or_reserved_ram_mb", missing),
                    "usage.running_or_reserved_ram_mb",
                    computers,
                ),
                snapshot_storage_bytes=observed(
                    usage.get("snapshot_storage_bytes", missing),
                    "usage.snapshot_storage_bytes",
                    snapshots,
                ),
            ),
            remaining=AccountRemaining(
                kept_computers=observed(
                    remaining.get("kept_computers", missing), "remaining.kept_computers", computers
                ),
                configured_vcpu=observed(
                    remaining.get("configured_vcpu", missing),
                    "remaining.configured_vcpu",
                    computers,
                ),
                configured_disk_gb=observed(
                    remaining.get("configured_disk_gb", missing),
                    "remaining.configured_disk_gb",
                    computers,
                ),
                running_or_reserved_ram_mb=observed(
                    remaining.get("running_or_reserved_ram_mb", missing),
                    "remaining.running_or_reserved_ram_mb",
                    computers,
                ),
                snapshot_storage_bytes=observed(
                    remaining.get("snapshot_storage_bytes", missing),
                    "remaining.snapshot_storage_bytes",
                    snapshots,
                ),
            ),
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
            gone=_wire(d, "gone") is _Wire.TRUE,
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

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
            degraded=_wire(d, "degraded") in (_Wire.TRUE, _Wire.MALFORMED),
            unmetered=_wire(d, "unmetered") in (_Wire.TRUE, _Wire.MALFORMED),
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
    #: The move's lifecycle operation, on the answer to
    #: :meth:`~mandala_computer.Computer.relocate` only — never on a row of
    #: :meth:`~mandala_computer.Moves.list`. ``client.operations.wait(id)`` is
    #: the same wait as :meth:`~mandala_computer.Computer.wait_for_move`.
    #: ``None`` where the platform could not record one; the move was accepted
    #: either way.
    operation_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    #: The states this model's own docstring calls live. Consulted ONLY when the
    #: wire did not give a readable ``live`` — the flag stays the thing to poll
    #: on, exactly as documented, and this is the fallback for a payload that
    #: cannot answer.
    LIVE_STATES = frozenset({"staging", "moving", "resizing"})

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Move:
        state = _text(d.get("state"))
        # `live` needs `state`, which is why this cannot be one decoder call
        # (adversarial review, OPL-3835). Reading an unreadable `live` as False
        # ended `wait_for_move` on a computer whose state said `moving`; reading
        # it as True polled a FINISHED move to its deadline and raised. Neither
        # is answerable without the other field.
        #
        # ABSENT defers with the rest, and the first version of this had it
        # wrong: a host that omits the flag and says `state: "moving"` IS
        # describing a live move, so returning False ended the wait on a disk
        # still copying. Only a value the wire actually gave overrides the state.
        said = _wire(d, "live")
        if said in (_Wire.TRUE, _Wire.FALSE):
            live = said is _Wire.TRUE
        else:
            live = state in cls.LIVE_STATES
        return cls(
            computer_id=_text(d.get("computer_id")),
            state=state,
            detail=_text(d.get("detail")),
            live=live,
            # `_opt_num`, not `_num`, for the reason the three fields are
            # optional at all: `_num` answers 0 for anything it cannot read, and
            # 0 is a real answer here — "resized to nothing" — on the fields
            # this operation exists to grow. `_opt_num` keeps a genuine 0 and
            # turns junk back into "not being changed", which is the only thing
            # an unreadable size can honestly mean.
            cpu=_opt_num(d.get("cpu")),
            ram_mb=_opt_num(d.get("ram_mb")),
            disk_gb=_opt_num(d.get("disk_gb")),
            started_at=_text(d.get("started_at")),
            finished_at=_text(d["finished_at"]) if d.get("finished_at") is not None else None,
            operation_id=operation_id_of(d),
            raw=dict(d),
        )


def move_rows(data: Any) -> builtins.list[Mapping[str, Any]]:
    """The rows out of ``GET /moves``, or a refusal naming what arrived instead.

    ``GET /moves`` is the one collection on this surface answered by an envelope
    — ``{"moves": [...]}`` — because it is account-scoped and could grow a
    sibling field. Everything else goes through ``json_array`` / ``listing``,
    which insist on an array of objects and complain by name when they do not
    get one. This route had no equivalent, and both ways of being wrong were
    reachable: a non-list ``moves`` silently became ``[]``, and a row that was
    not an object reached :meth:`Move.from_api` and came back out as
    ``AttributeError: 'str' object has no attribute 'get'`` — a bare builtin
    escaping a public method, past the :class:`~mandala_computer.MandalaError`
    this SDK promises.

    The empty list is the worse of the two, and is why this raises rather than
    degrading the way the field decoders above do. This is the listing a caller
    polls to find out whether a machine is still moving, and no rows there means
    something specific: :meth:`~mandala_computer.Computer.wait_for_move` reads
    it as a move the platform reaped along with its computer. An envelope
    nobody could parse is not that.

    A MISSING ``moves`` key is refused with the rest, deliberately (asked at
    /code-review, OPL-4222). Reading absence as ``[]`` would be forward-
    compatible with a route that someday omits the key when there is nothing —
    and would put back exactly the silent empty listing this exists to remove,
    on the one route where an empty listing is itself a claim. The platform
    always emits the array, so the trade is a hypothetical break against a
    misdiagnosis that has to be looked for to be found.
    """
    rows = data.get("moves") if isinstance(data, Mapping) else None
    if not isinstance(rows, builtins.list) or not all(isinstance(row, Mapping) for row in rows):
        raise MandalaError(f"GET moves did not answer with an array of objects: {rows!r:.200}")
    return rows


@dataclass(frozen=True)
class Window:
    """One window on the guest's desktop.

    What a screenshot cannot tell you: a picture says what the desktop looks
    like, this says what any of it *is* — which is how a browser that failed to
    launch is told apart from one that has not painted yet.

    Match on :attr:`wm_class` rather than :attr:`title`. The class is the
    application and is stable; the title is whatever page or document it happens
    to be showing.

    Read :attr:`visible` before acting on :attr:`x` and :attr:`y`. A minimised
    window stays on this list keeping the coordinates it had, so on one of those
    the pair is a place on the desktop rather than a place the window is.
    """

    id: str
    title: str
    #: The X11 ``WM_CLASS`` — the application, e.g. ``"Firefox"``. Spelled with
    #: the prefix because ``class`` is a Python keyword and cannot be a field.
    wm_class: str
    #: The window manager's own type, e.g. ``"normal"``, ``"dock"``.
    type: str
    #: Where the window is, and how big it is — or ``None``, where the wire did
    #: not say.
    #:
    #: ``None`` rather than ``0``, for the reason :attr:`pid` is not a
    #: :func:`_num` either and with more behind it: ``_num``'s floor for an
    #: absent, null or unreadable field is zero, and zero here is not a missing
    #: answer but A PLACE. A window really can be at ``x: 0``, so a coordinate
    #: this client could not read came back indistinguishable from the top-left
    #: corner of the screen — and the corner is where an agent then clicks.
    #: ``cursor_position`` refuses exactly this one route along; the window
    #: decoder went on inventing it until OPL-4200.
    #:
    #: **The daemon already refuses it at the origin**, which is what makes the
    #: floor a divergence rather than a house rule. The platform requires all
    #: four for the same reason this does: a window whose position cannot be
    #: read is a window a caller cannot click, and reporting it at the origin
    #: with no size is the plausible-but-wrong answer rather than a missing one.
    #: A row that fails it is left out of the listing and the answer then
    #: carries an error, and
    #: the guest broker's own decoder drops a window event the same way. So the
    #: zero was this client putting back the answer the platform declines to
    #: give.
    #:
    #: All four are sent on every window, so ``None`` means something is already
    #: wrong — schema drift, a truncated body, a proxy answering in the
    #: platform's place. ``w.x or 0`` is the repair to avoid: there is no
    #: fallback for a place, and the one this replaced is the bug.
    #:
    #: Still POSITIONAL and still required to construct, unlike :attr:`visible`
    #: and :attr:`pid` below: the type widened, the field order did not. See the
    #: note under :attr:`raw` for what moving one of these would cost.
    x: int | None
    #: As :attr:`x`: ``None`` where the wire did not say.
    y: int | None
    #: As :attr:`x`: ``None`` where the wire did not say.
    width: int | None
    #: As :attr:`x`: ``None`` where the wire did not say.
    height: int | None
    focused: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # The wire sends `pid` after `type` and `visible` after `focused`, and both
    # arrive here at the END instead, keyword-only. This class is exported, so
    # its field order IS its constructor, and `Template.ref` cost a release
    # learning what inserting a field into that order does: every positional
    # construction that worked before binds one argument along and either raises
    # or, worse, does not. Decoding would never have noticed — `from_api` passes
    # by keyword. Wire order is the docstrings' job; the positions are a
    # promise already made.

    #: On the screen rather than minimised, and the only field here that can
    #: tell the two apart. A minimised window stays on the client list, keeps
    #: the coordinates it had and can still be the :attr:`focused` one — so an
    #: agent that reads :attr:`x`/:attr:`y` off one and clicks there is clicking
    #: at whatever is actually in front.
    #:
    #: Defaults to ``False`` on a hand-built window, which is the same direction
    #: the decoder takes an answer it cannot read, and for the same reason: a
    #: window wrongly called minimised is one a caller skips.
    visible: bool = field(default=False, kw_only=True)
    #: The guest process that owns this window, where the window says so.
    #:
    #: ``None`` rather than ``0`` when it does not, which is why this is the one
    #: optional integer here: a guest is free to advertise ``_NET_WM_PID`` 0, so
    #: absent and zero have to stay different things. The daemon sends a pointer
    #: for the same reason.
    #:
    #: **It does not identify the window.** An application that keeps one
    #: process for several windows — ``xfce4-terminal`` is one, and so is every
    #: browser — reports the same pid on all of them, so killing this pid can
    #: take windows you never asked about. A stock desktop demonstrates it
    #: before any application does: the three ``Xfce4-panel`` docks in
    #: ``windows(include_all=True)`` are three windows on one pid.
    pid: int | None = field(default=None, kw_only=True)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Window:
        """One window row, decoded and never refused.

        TOTAL, unlike :meth:`TemplateBuild.from_api` next door, which raises on
        a record whose identity is missing. It cannot join it, and the reason is
        where it is called from rather than what it decodes: this also runs on
        the EVENT stream (:func:`_events.to_computer_event`,
        :func:`_events.to_hello`), whose policy for a frame it cannot read is to
        skip it and read the next one rather than end the connection over it.

        So a row this cannot make sense of is reported rather than refused, and
        the refusing is done one layer out by whoever hands a caller something
        to act on: :func:`_computer._windows_from_response` for the listing, and
        :func:`window_contradiction` at ``window_action``. The same split
        :func:`build_contradiction` already draws — the lenient reading here,
        the raise at the call site that acts on it (OPL-4200).
        """
        return cls(
            id=_text(d.get("id")),
            title=_text(d.get("title")),
            wm_class=_text(d.get("class")),
            type=_text(d.get("type")),
            pid=_opt_num(d.get("pid")),
            # `_opt_num` and not `_num`, for the reason the fields' own note
            # gives: `_num` answers 0, and 0 is an origin a window really has
            # rather than one it failed to report (OPL-4200).
            x=_opt_num(d.get("x")),
            y=_opt_num(d.get("y")),
            width=_opt_num(d.get("width")),
            height=_opt_num(d.get("height")),
            focused=_wire(d, "focused") is _Wire.TRUE,
            # TRUE ONLY, and this is the decision OPL-4191 asked for rather than
            # the line above it copied. `_Wire` exists here because every
            # fallback boolean is wrong somewhere, and the fallback bites on
            # this field in particular: an answer that did not say decodes as
            # MINIMISED, so a build that stopped sending `visible` would report
            # a whole desktop as off the screen.
            #
            # That is the harmless half, which is what settles it. A window
            # wrongly called minimised is one a caller skips; a window wrongly
            # called on-screen is a click landing on whatever is really at those
            # coordinates. Loudly conservative beats silently wrong, and the
            # unreadable case stays legible in `raw` for a caller who needs to
            # tell "no" from "did not say". The TypeScript SDK reads it the same
            # way (OPL-4176), deliberately.
            #
            # Nothing has ever exercised the fallback: `visible` has been on the
            # wire since the route shipped and no build has omitted it. It was
            # missing HERE because the published reference listed nine of the
            # eleven fields until OPL-4179 — a documentation gap this SDK copied
            # faithfully, not a decoding bug.
            visible=_wire(d, "visible") is _Wire.TRUE,
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> WindowResult:
        w = d.get("window")
        return cls(
            window=Window.from_api(w) if isinstance(w, Mapping) else None,
            gone=_wire(d, "gone") is _Wire.TRUE,
            raw=dict(d),
        )


def window_contradiction(result: WindowResult) -> str | None:
    """Why this result cannot be believed, or ``None`` if it can.

    One shape qualifies: a ``gone`` the wire actually said was true, beside a
    window object describing the window it says is gone. The two halves are read
    by different callers — one drives ``result.window``, another branches on
    ``result.gone`` — so a body carrying both is answered differently by two
    correct programs. The first keeps clicking at a window that is not there;
    the second throws away a window that is.

    The READING rather than the raw record, unlike :func:`build_contradiction`:
    ``gone`` is already classified through ``_Wire`` and :attr:`WindowResult.window`
    is already "a mapping was there", so neither has been through a coercion
    this could be fooled by.

    Absent, null and unreadable are NOT contradictions, for the same reason they
    are not on a build: they are a host that said nothing. ``gone`` false with no
    window is a documented outcome — the action happened and the guest could not
    describe it.

    A REPORT rather than a refusal, and deliberately: the live close is
    ``{"gone": true, "ok": true, "window": null}``, so nothing sends this today,
    and the shape has an obvious legitimate future — "closed, and here is what
    it was". The day the platform documents that, this stops being a
    contradiction and the change is deleting one raise at each of the two call
    sites, not unpicking a rule from the decoder. Until then the caller is told
    rather than quietly handed the half of the body it happened to read first
    (OPL-4200). The TypeScript SDK reads it the same way, deliberately.
    """
    if not result.gone or result.window is None:
        return None
    return (
        f"the window action reports the window gone and describes window "
        f"{result.window.id!r} in the same body. The record contradicts itself, "
        "so neither half of it can be trusted — read windows() for what is on "
        "the desktop."
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

    :attr:`stdout` and :attr:`stderr` are **bytes**, decoded from the wire's
    ``stdout_b64``/``stderr_b64``. :attr:`stdout_text` is the text reading when
    you want one. The offsets count DECODED bytes — they line up with
    ``len(status.stdout)``, never with the length of the base64 that carried it,
    which is about four thirds as long. Nothing here asks you to do that
    arithmetic; the note is for anyone tempted to keep a cursor of their own
    beside the daemon's.
    """

    pid: int
    #: The command line, echoed back.
    command: str
    running: bool
    exited: bool
    #: ``None`` until it has exited — ``None`` rather than ``0``, which is the
    #: one value that would be read as success by anything not checking first.
    exit_code: int | None
    #: What it has printed since the previous read, as bytes. This read consumed
    #: it. See :attr:`stdout_text` for the text reading.
    stdout: bytes
    stderr: bytes
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
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    #: Whether this came off the wire. Keyword-only, so it changes no existing
    #: construction and stays out of ``__match_args__``.
    #:
    #: IN THE REPR as well as in ``==``, because it is the field most likely to
    #: be the only difference between two statuses — it is exactly the
    #: hand-built-versus-decoded discriminator — and hiding it left a failed
    #: ``assert status == ExecStatus(...)`` printing two identical reprs with no
    #: hint why (/code-review, OPL-3835).
    #:
    #: ``raw`` cannot answer it: ``from_api`` sets ``raw=dict(d)``, so a decoded
    #: ``{}`` — a proxy hiccup, a daemon answering 200 with an empty object — is
    #: indistinguishable from an object built by hand, and using its truthiness
    #: let that payload declare a command finished with nothing exited, nothing
    #: killed and no exit code (/code-review, OPL-3835). The escape hatch
    #: reopened the very hole it was written beside.
    #: IN ``==``, because it changes :attr:`done`. Excluded at first, which
    #: made ``from_api({})`` compare equal to a hand-built status that reports
    #: the opposite of it (adversarial review, OPL-3835) — two objects equal to
    #: each other and behaviourally different is the wrong thing for an
    #: expected-value assertion or a change check to be handed.
    decoded: bool = field(default=False, kw_only=True)
    #: The output on this read could not be read. See
    #: :attr:`ExecResult.output_unreadable`, which means the same thing, and
    #: means it more sharply here: this read consumed the daemon's cursor, so
    #: whatever was in that field is gone rather than fetchable again.
    #:
    #: NOT :attr:`output_uncertain`, which is about ``more`` — that one says
    #: this client cannot tell whether there is FURTHER output; this one says
    #: output it already had was lost. Keyword-only with a default, so it
    #: changes no existing construction.
    output_unreadable: bool = field(default=False, kw_only=True)

    # Normalize only the wire evidence that changes completion or draining.
    # init=False preserves positional construction and pattern matching;
    # __post_init__ recomputes it when dataclasses.replace changes raw/decoded.
    _running_stopped: bool = field(init=False)
    _more_unreadable: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_running_stopped", self.decoded and _wire(self.raw, "running") is _Wire.FALSE
        )
        object.__setattr__(self, "_more_unreadable", _wire(self.raw, "more") is _Wire.MALFORMED)

    @property
    def stdout_text(self) -> str:
        """:attr:`stdout` as text, with undecodable bytes replaced.

        ``errors="replace"``, so this never raises on the binary a command is
        entitled to print — the U+FFFD the wire format was changed to stop
        producing is fine HERE, where the bytes are still on the object beside
        it and the caller chose the lossy reading. What was not fine was the
        wire doing it before anything reached this SDK, with no way back.

        PER CHUNK, and that is the one place it is the wrong accessor. A poll is
        cut at 1 MiB on a BYTE offset, so a multi-byte character lands across
        two reads, and decoding each one on its own replaces both halves —
        exactly the corruption the base64 format exists to stop, put back one
        layer up (/grok-review, OPL-4544). Bytes join and text does not, so a
        loop assembling a log appends :attr:`stdout` and decodes once at the
        end. This is for a whole output small enough to have arrived in one
        read, and for printing a chunk you are not keeping.
        """
        return self.stdout.decode("utf-8", "replace")

    @property
    def stderr_text(self) -> str:
        """:attr:`stderr` as text, on the same terms as :attr:`stdout_text`."""
        return self.stderr.decode("utf-8", "replace")

    @property
    def done(self) -> bool:
        """True once the command has stopped, however it stopped.

        Read with :attr:`drained`, not instead of it: a command can exit with
        output still queued, and a loop that stops at ``done`` alone drops
        whatever the last read did not reach.

        AFFIRMATIVE EVIDENCE ONLY. ``exited``, ``killed``, or a ``running`` the
        wire actually said was false. A ``running`` that is present and
        unreadable decodes False like anything else this client cannot read, and
        ``not running`` then declared a command finished on no evidence at all —
        no exit code, nothing exited, nothing killed (adversarial review,
        OPL-3835). It says nothing about whether the command stopped, so it is
        not allowed to end the poll.
        """
        # ABSENT belongs with null and malformed and was left out (adversarial
        # review, OPL-3835): a payload with no `running` at all decodes it False
        # like anything else missing, and `not running` then ended the poll with
        # nothing exited, nothing killed and no exit code. Only a `running` the
        # wire actually said was FALSE is evidence of stopping.
        #
        # An object built directly rather than decoded has no payload to
        # consult, and then its fields ARE the evidence: a caller who wrote
        # `running=False` has said it stopped.
        if self.decoded and not self._running_stopped:
            return self.exited or self.killed
        return self.exited or self.killed or not self.running

    @property
    def output_uncertain(self) -> bool:
        """The host sent a :attr:`more` this client could not read.

        Its own field cannot carry this. ``more`` is TWO things to a polling
        loop — the sleep switch and half the break condition — so neither value
        is safe when it is unreadable: True spins with no delay, False breaks and
        drops output that a consuming read can never fetch again. It reads False
        so the loop sleeps, and this says why, so the loop can decline to stop.
        """
        return self._more_unreadable

    @property
    def drained(self) -> bool:
        """Safe to stop reading: stopped, nothing queued, and ``more`` readable.

        What a polling loop actually wants, and the reason it is a property
        rather than two conditions a caller has to remember to write. Spelled as
        ``done and not more`` it silently dropped queued output whenever ``more``
        could not be read (adversarial review, OPL-3835).

        ABOUT ``more`` ALONE. It says nothing about whether the output this read
        carried was readable — :attr:`output_unreadable` is that, and it is
        deliberately not folded in here: re-polling cannot recover a chunk the
        daemon's cursor has already passed, so refusing to drain would spin one
        more empty read and still have lost the bytes. A loop that must not
        continue past lost output checks that flag itself (/grok-review,
        OPL-4544). The word "unreadable" in this docstring used to imply
        otherwise, before there was a field by that name to confuse it with.
        """
        return self.done and not self.more and not self.output_uncertain

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ExecStatus:
        # stdout/stderr first. `poll`/`kill` have already consumed the daemon
        # cursor by the time this runs, so raising on a malformed `exit_code`
        # would drop output that cannot be fetched again — the same loss
        # `output_uncertain` exists to prevent for `more`. An unreadable code
        # is None, which this field already uses for "the platform could not
        # report one".
        #
        # `stdout_b64`/`stderr_b64`, and the old `stdout`/`stderr` are NOT read
        # as a fallback. The platform renamed them rather than adding an
        # `encoding` discriminator precisely so a client that has not been
        # updated fails on a missing field instead of silently reading base64
        # as text (platform OPL-4403); reading the old names here would put
        # that silence back, and would put back the U+FFFD rewriting they were
        # renamed to end.
        stdout, stderr, unreadable = _output(d)
        try:
            exit_code = _exit_code(d.get("exit_code"))
        except MandalaError:
            exit_code = None
        return cls(
            pid=_num(d.get("pid")),
            command=_text(d.get("command")),
            running=_wire(d, "running") in (_Wire.TRUE, _Wire.MALFORMED),
            exited=_wire(d, "exited") is _Wire.TRUE,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_offset=_num(d.get("stdout_offset")),
            stderr_offset=_num(d.get("stderr_offset")),
            more=_wire(d, "more") is _Wire.TRUE,
            killed=_wire(d, "killed") is _Wire.TRUE,
            started_at=_text(d.get("started_at")),
            raw=dict(d),
            decoded=True,
            output_unreadable=unreadable,
        )


@dataclass(frozen=True)
class ExecResult:
    """The outcome of a shell command run inside the guest.

    :attr:`stdout` and :attr:`stderr` are **bytes**, because a command's output
    is bytes: ``tar``, ``convert`` and a latin-1 build log are all things a
    guest is entitled to print, and a ``str`` surface here would decide for you
    that they are not. The wire carries them base64 for the same reason
    (platform OPL-4403). :attr:`stdout_text` is the text reading, which is what
    you want for anything you were going to ``strip()`` or compare.
    """

    #: ``None`` when the platform could not report an exit code, such as a
    #: timed-out command. It must not be coerced to zero and mistaken for success.
    exit_code: int | None
    #: What the command printed, as bytes. See :attr:`stdout_text`.
    stdout: bytes
    stderr: bytes
    timed_out: bool
    #: True when the guest agent stopped capturing stdout before the command
    #: stopped producing it. See :attr:`truncated`.
    out_truncated: bool = False
    #: The same for stderr.
    err_truncated: bool = False
    #: ``compare=False``, unlike the other models here. An ``ExecResult`` is a
    #: value, not a handle: callers assert on one against a result they built
    #: themselves, and put them in sets. Comparing the unknown fields the
    #: server happened to send would make ``res == ExecResult(0, b"hi", b"",
    #: False)`` false for a command that did exactly that, and comparing a
    #: ``dict`` at all makes the frozen dataclass unhashable.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    #: The output on this response could not be read, and empty bytes are
    #: standing in for it — either a ``*_b64`` field this client could not
    #: decode, or a body in the pre-OPL-4403 shape, whose ``stdout``/``stderr``
    #: this SDK deliberately does not read. Check it before concluding a command
    #: printed nothing: the two are the same value and opposite facts.
    #: Keyword-only with a default, so it changes no existing construction and
    #: stays out of ``__match_args__``.
    output_unreadable: bool = field(default=False, kw_only=True)

    #: A confirmed retained version, absent when optional capture was unconfirmed.
    #: Keyword-only and excluded from equality/hash and positional matching.
    result_id: str | None = field(default=None, kw_only=True, compare=False)

    @property
    def stdout_text(self) -> str:
        """:attr:`stdout` as text, with undecodable bytes replaced.

        ``errors="replace"``, so this never raises on the binary a command is
        entitled to print. The lossy reading is fine here, where the bytes are
        still on the object beside it and the caller asked for text; what was
        not fine was the wire format doing it before anything reached this SDK.
        """
        return self.stdout.decode("utf-8", "replace")

    @property
    def stderr_text(self) -> str:
        """:attr:`stderr` as text, on the same terms as :attr:`stdout_text`."""
        return self.stderr.decode("utf-8", "replace")

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
        # The old `stdout`/`stderr` are deliberately not read as a fallback —
        # see the note in `ExecStatus.from_api`.
        stdout, stderr, unreadable = _output(d)
        return cls(
            exit_code=_exit_code(code),
            stdout=stdout,
            stderr=stderr,
            timed_out=_wire(d, "timed_out") in (_Wire.TRUE, _Wire.MALFORMED),
            out_truncated=_wire(d, "out_truncated") in (_Wire.TRUE, _Wire.MALFORMED),
            err_truncated=_wire(d, "err_truncated") in (_Wire.TRUE, _Wire.MALFORMED),
            raw=dict(d),
            output_unreadable=unreadable,
        )


def _optional_result_id(
    data: Mapping[str, Any], stdout: bytes, stderr: bytes, unreadable: bool
) -> str | None:
    """Validate optional payload evidence after the exec caller has checked HTTP 200.

    ``from_api`` has no HTTP status, so it cannot confirm a retained version.
    This helper never changes the already executed action's legacy interpretation.
    """
    identity = data.get("result_id")
    code = data.get("exit_code")
    if (
        not isinstance(identity, str)
        or not re.fullmatch(r"res_[a-f0-9]{32}", identity)
        or type(code) is not int
        or not -2147483648 <= code <= 2147483647
        or _wire(data, "timed_out") is not _Wire.FALSE
        # The legacy classifier recognizes numeric/string booleans too. A
        # retained ID additionally requires the actual JSON boolean evidence.
        or type(data["timed_out"]) is not bool
        or unreadable
        or type(data.get("out_truncated")) is not bool
        or type(data.get("err_truncated")) is not bool
        or len(stdout) > 16 * 1024 * 1024
        or len(stderr) > 16 * 1024 * 1024
        or base64.b64encode(stdout).decode("ascii") != data.get("stdout_b64")
        or base64.b64encode(stderr).decode("ascii") != data.get("stderr_b64")
    ):
        return None
    return str.__str__(identity)


def _opt_text(value: Any) -> str | None:
    """A string field whose JSON ``null`` MEANS something, kept as ``None``.

    :func:`_text` folds null into the empty string, which is right for a name
    and wrong for ``disabled_reason``, ``last_error`` and the timestamps on a
    webhook: there, ``null`` is the platform saying "not applicable" — never
    failed, still enabled, no attempt yet — and a decoder that turned it into
    ``""`` would leave a caller unable to tell an endpoint that has never
    answered from one that answered with an empty error line.
    """
    return None if value is None else str(value)


@dataclass(frozen=True)
class Webhook:
    """A subscription: POST this account's events, signed, at :attr:`url`.

    From the ``webhooks`` resource (platform OPL-3923, OPL-4300), and the shape
    every read answers. The signing secret is NEVER here — :class:`WebhookCreated`
    is the one shape that carries it, and only from
    :meth:`~mandala_computer.Webhooks.create` and ``rotate``.

    What arrives at :attr:`url` is the event object exactly as the socket
    frames it, byte for byte — ``type``, ``at``, ``computer``, ``seq``,
    ``cursor``, ``source``, ``data`` — under the three Standard Webhooks
    headers. :func:`mandala_computer.verify` checks them.
    """

    #: ``whk-`` and sixteen hex characters.
    id: str
    #: Where deliveries are POSTed. ``https://`` only.
    url: str
    #: Free text, for your listing. Empty when you gave none.
    description: str
    #: The event types this subscription receives. EMPTY MEANS EVERY TYPE. The
    #: vocabulary is the socket's less ``file.changed``.
    events: builtins.list[str]
    #: The computer ids it receives events for. Empty means every computer in
    #: scope. Not checked against your computers when set — a subscription may
    #: name a computer you are about to create.
    computers: builtins.list[str]
    #: Whether deliveries are made. Set ``False`` by the platform when an
    #: endpoint has failed for a day — see :attr:`disabled_reason` — and back
    #: to ``True`` by you with an update, which starts fresh.
    enabled: bool
    #: Why :attr:`enabled` is false: ``"customer"`` when you disabled it,
    #: ``"failing"`` when the platform did — a delivery ran out of attempts and
    #: nothing had been accepted for 24 hours. ``None`` while enabled.
    disabled_reason: str | None
    #: RFC 3339, or ``None`` while enabled.
    disabled_at: str | None
    #: When the endpoint last answered 2xx to any delivery. ``None`` until it has.
    last_success_at: str | None
    #: When a delivery attempt last failed. ``None`` until one has.
    last_failure_at: str | None
    #: The HTTP status of the newest attempt, whatever it was. ``None`` before
    #: any attempt, or when the newest got no answer.
    last_status: int | None
    created_at: str
    updated_at: str
    #: The workspace this subscription is confined to, when it was created with
    #: a workspace-scoped API key. Empty on an account-wide subscription — the
    #: platform omits the field rather than sending null, on the pattern
    #: :class:`Computer` follows for the same one.
    workspace_id: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_failing(self) -> bool:
        """The platform switched it off because the endpoint kept failing.

        The one disable a caller did not ask for, and the one to alarm on: the
        endpoint was down for a day, every pending delivery was dropped, and
        nothing more will be sent until it is re-enabled.
        """
        return not self.enabled and self.disabled_reason == "failing"

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Webhook:
        return cls(**cls._fields(d))

    @classmethod
    def _fields(cls, d: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": _text(d.get("id")),
            "url": _text(d.get("url")),
            "description": _text(d.get("description")),
            "events": _texts(d.get("events")),
            "computers": _texts(d.get("computers")),
            # Absent decodes as ENABLED, which is the platform's default for a
            # create that did not say — and the safe reading of a row that did
            # not either: a subscription read as disabled by mistake is one a
            # caller stops watching.
            "enabled": _wire(d, "enabled") is not _Wire.FALSE,
            "disabled_reason": _opt_text(d.get("disabled_reason")),
            "disabled_at": _opt_text(d.get("disabled_at")),
            "last_success_at": _opt_text(d.get("last_success_at")),
            "last_failure_at": _opt_text(d.get("last_failure_at")),
            "last_status": _opt_whole(d.get("last_status")),
            "created_at": _text(d.get("created_at")),
            "updated_at": _text(d.get("updated_at")),
            "workspace_id": _text(d.get("workspace_id")),
            "raw": dict(d),
        }


@dataclass(frozen=True)
class WebhookCreated(Webhook):
    """A :class:`Webhook` with its :attr:`secret` — answered ONCE.

    From ``create`` and ``rotate`` and nowhere else. The secret is not readable
    again: store it now, and if it is lost, rotate. After a rotate the old
    secret goes on being honoured for 24 hours, during which every delivery
    carries two signatures, so a receiver moved to the new one at any point
    in that window never refuses a delivery.
    """

    #: ``whsec_`` and 44 characters of base64. Hand it to
    #: :func:`mandala_computer.verify` on the receiving side.
    secret: str = field(kw_only=True, repr=False)

    def __repr__(self) -> str:
        """The generated repr, with the secret named and not shown.

        A log line or a traceback that carried it would be enough to forge
        deliveries at the customer's endpoint — the leak :class:`VncConnect`
        closes for its tokens, and the same answer: say the field is there,
        never what it holds.
        """
        return f"{super().__repr__()[:-1]}, secret=<redacted>)"

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> WebhookCreated:
        return cls(secret=_text(d.get("secret")), **cls._fields(d))


#: The states a delivery is finished in. Nothing more will happen to one of these.
DELIVERY_FINAL_STATES = frozenset({"delivered", "exhausted", "dropped"})


@dataclass(frozen=True)
class WebhookDelivery:
    """One event to one subscription, and what became of it.

    From ``deliveries`` (the newest hundred, newest first) and from ``test``
    (the one it queued). A delivery is minted once per event per subscription
    and keeps its :attr:`id` across every attempt, which is why a receiver can
    dedupe on it.
    """

    #: ``whd-`` and sixteen hex characters: the ``webhook-id`` header this
    #: delivery carried, fixed across attempts.
    id: str
    #: The event's ``type``. ``"gap"`` for a gap frame; ``"webhook.test"`` for
    #: a test delivery.
    event_type: str
    #: The computer the event is about. Empty on a test delivery.
    computer: str
    #: The event's own ``cursor`` — what to pass as ``since=`` to the socket to
    #: read on from it.
    cursor: str
    #: ``"pending"`` (an attempt is scheduled), ``"in_flight"`` (one is running),
    #: ``"delivered"`` (a 2xx came back), ``"exhausted"`` (eight attempts
    #: failed) or ``"dropped"`` (the subscription was disabled or deleted first).
    state: str
    #: How many times it has been sent. Eight is the last.
    attempts: int
    #: When the next attempt is due, RFC 3339. ``None`` once the delivery is finished.
    next_at: str | None
    #: When the newest attempt started. ``None`` before the first.
    attempted_at: str | None
    #: The HTTP status of the newest attempt, or ``None`` when it got no answer.
    last_status: int | None
    #: One line about the newest failure, for a person to read. ``None`` after a
    #: success and before any attempt.
    #:
    #: An **open** set, not an enumeration. What a delivery failed on is most
    #: often the transport — ``timeout``, ``dns``, ``refused``, ``reset``,
    #: ``unreachable``, ``tls``, ``redirect``, ``address refused``, ``status
    #: NNN`` — and a ``dropped`` delivery says why it was dropped. The platform
    #: documents that other values are possible, and some read as a prefix and a
    #: cause (``attempt failed: …``, ``settle failed: …``). New
    #: wordings arrive without warning, because this field describes an operator's
    #: failure rather than a client's contract. Branch on :attr:`state`,
    #: :attr:`last_status` and :attr:`attempts`, which do not move; show this one.
    last_error: str | None
    #: When the 2xx came back. ``None`` otherwise.
    delivered_at: str | None
    #: When the event reached the queue.
    created_at: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def is_delivered(self) -> bool:
        return self.state == "delivered"

    @property
    def is_finished(self) -> bool:
        """Nothing more will happen to it — delivered, exhausted or dropped.

        The condition to poll :meth:`~mandala_computer.Webhooks.deliveries`
        on after a ``test``: a pending or in-flight delivery has not said yet.
        """
        return self.state in DELIVERY_FINAL_STATES

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> WebhookDelivery:
        return cls(
            id=_text(d.get("id")),
            event_type=_text(d.get("event_type")),
            computer=_text(d.get("computer")),
            cursor=_text(d.get("cursor")),
            state=_text(d.get("state")),
            attempts=_num(d.get("attempts")),
            next_at=_opt_text(d.get("next_at")),
            attempted_at=_opt_text(d.get("attempted_at")),
            last_status=_opt_whole(d.get("last_status")),
            last_error=_opt_text(d.get("last_error")),
            delivered_at=_opt_text(d.get("delivered_at")),
            created_at=_text(d.get("created_at")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class SshKey:
    """One of your SSH public keys, from the ``ssh_keys`` resource.

    A key belongs to a person, not to an account: the list is the same
    whichever account the API key acts on, and the key reaches the computers of
    every account where you are an owner or member.
    """

    #: ``sshk-`` and sixteen hex characters.
    id: str
    #: Your label. Defaults to the comment that followed the key when it was added.
    name: str
    #: ``<type> <base64>``, re-encoded by the platform: no options, no comment.
    public_key: str
    #: ``SHA256:`` and 43 characters, exactly as ``ssh-keygen -l`` prints it.
    fingerprint: str
    #: ``ssh-ed25519``, ``ecdsa-sha2-nistp256`` and so on.
    key_type: str
    created_at: str
    #: When this key last opened a connection to a computer. ``None`` until it has.
    last_used_at: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> SshKey:
        return cls(
            id=_text(d.get("id")),
            name=_text(d.get("name")),
            public_key=_text(d.get("public_key")),
            fingerprint=_text(d.get("fingerprint")),
            key_type=_text(d.get("key_type")),
            created_at=_text(d.get("created_at")),
            last_used_at=_opt_text(d.get("last_used_at")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class SshAccess:
    """Whether SSH is on for one computer, and whether it can work there.

    From :meth:`~mandala_computer.Computer.ssh_access` and
    :meth:`~mandala_computer.Computer.set_ssh_access`.
    """

    #: The computer this is about.
    computer: str
    #: Whether SSH is switched on. Off until somebody switches it on; a field
    #: that is missing or unreadable reads as off.
    enabled: bool
    #: Whether the computer can run SSH at all. ``False`` for one made from a
    #: template image that predates SSH: create a new computer to use it.
    #: ``None`` when the computer has not been asked yet; it is asked when it
    #: next starts.
    available: bool | None
    #: The computer's host has yet to receive the current setting and key list.
    #: It is sent again automatically, and a connection attempt sends it first.
    pending: bool
    #: How many keys may log in: every key of every owner and member of the
    #: account. ``0`` while SSH is off.
    key_count: int
    #: How many of those the computer is given. Fewer than :attr:`key_count`
    #: only when the account holds more keys than one computer accepts.
    keys_pushed: int
    #: Set when the computer's host refused the current setting. ``None`` otherwise.
    error: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> SshAccess:
        available = _wire(d, "available")
        return cls(
            computer=_text(d.get("computer")),
            enabled=_wire(d, "enabled") is _Wire.TRUE,
            available=(
                True if available is _Wire.TRUE else False if available is _Wire.FALSE else None
            ),
            pending=_wire(d, "pending") is _Wire.TRUE,
            key_count=_num(d.get("key_count")),
            keys_pushed=_num(d.get("keys_pushed")),
            error=_opt_text(d.get("error")),
            raw=dict(d),
        )


# --- browser proxy ----------------------------------------------------------


class _BrowserProxyServer(TypedDict):
    server: str


class BrowserProxyArgs(_BrowserProxyServer, total=False):
    """A proxy for a computer's browsers, for
    :meth:`~mandala_computer.Computers.create` and
    :meth:`~mandala_computer.Computer.set_browser_proxy`.

    ``server`` is the proxy's URL, such as ``http://proxy.example.com:3128`` or
    ``socks5://127.0.0.1:1080``. Which schemes and hosts are accepted is the
    platform's rule, not this client's: a value it refuses raises
    :class:`~mandala_computer.BadRequestError` with its sentence. ``bypass``
    names the hosts the browsers reach directly — ``example.com``,
    ``*.example.com``, an address or a range, or ``<local>``.
    """

    bypass: Sequence[str]


@dataclass(frozen=True)
class BrowserProxy:
    """The proxy a computer's browsers are sent through, as the platform reports it.

    ``server`` is in its stored spelling (lower-cased), and ``bypass`` is the
    list as stored, duplicates dropped; empty when there is none. Can be passed
    back to :meth:`~mandala_computer.Computer.set_browser_proxy` as it is.
    """

    server: str
    bypass: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, d: object, where: str = "computer") -> BrowserProxy | None:
        """Refuses rather than guesses, for :meth:`SecretBinding.from_api`'s reason.

        A change replaces the setting whole, so what is read here is what a
        caller edits and sends back: a bypass entry dropped or a server coerced
        on the read is one the caller never saw, removed by an update that
        looked like it kept everything. Keys this client does not know are
        left out rather than refused, so a platform that grows the setting is
        still readable. ``None`` when there is none.
        """
        if d is None:
            return None
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: browser_proxy is not an object")
        server = d.get("server")
        if not isinstance(server, str) or not server:
            raise MandalaError(f"{where}: browser_proxy does not name its server")
        bypass = d.get("bypass")
        if bypass is None:
            return cls(server=str.__str__(server))
        if not isinstance(bypass, list) or not all(isinstance(e, str) and e for e in bypass):
            raise MandalaError(f"{where}: browser_proxy bypass is not a list of hosts")
        return cls(server=str.__str__(server), bypass=tuple(str.__str__(e) for e in bypass))


# --- secret bindings --------------------------------------------------------


class _SecretBindingId(TypedDict):
    secret_id: str


class SecretBindingArgs(_SecretBindingId, total=False):
    """One secret to bind, by id, published under exactly one of ``env`` and ``file``.

    For :meth:`~mandala_computer.Computers.create` and
    :meth:`~mandala_computer.Computer.set_secrets`.

    ``env`` is the environment variable the value is published under in the
    desktop session: letters, digits and underscores, not starting with a digit,
    at most 64 characters.

    ``file`` is the file the value is published as,
    ``/run/mandala-secrets/user/files/<file>``: lowercase letters, digits, ``-``
    and ``_``, starting with a letter, at most 48 characters. For what a program
    reads from a path. A file may hold any bytes. A replaced value is also sent
    to a running computer's file, asynchronously and on a best-effort basis:
    :attr:`~mandala_computer.Computer.secrets_pending` says whether it landed,
    and the next start or restart always delivers the latest.

    ``revision_id`` is for a rebind only: naming the revision the computer holds
    now keeps it; leaving it out records the latest. Every start and restart
    delivers each secret's latest value regardless.
    """

    env: str
    file: str
    revision_id: str


def _binding_text(row: Mapping[str, Any], key: str, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise MandalaError(f"{where}: a secret binding has no usable {key}")
    return str.__str__(value)


def _binding_name(
    row: Mapping[str, Any], key: str, pattern: re.Pattern[str], where: str
) -> str | None:
    """``env`` or ``file`` off a row: absent or null is ``None``; anything else
    must be a name the platform could have stored, or the answer is refused."""
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise MandalaError(f"{where}: a secret binding's {key} is not a name: {value!r}")
    return str.__str__(value)


@dataclass(frozen=True)
class SecretBinding:
    """One secret a computer is bound to, as the platform records it.

    ``revision_id`` is the revision last delivered to the computer: every start
    and restart delivers the secret's latest value and moves it there, and so
    does a replaced value that reaches the running computer live — sent
    asynchronously and best-effort, so it may not. Exactly one of ``env`` and
    ``file`` is set. Names and revisions only: no value is ever returned.
    """

    secret_id: str
    revision_id: str
    #: The environment variable the value is published under, or ``None``.
    env: str | None = None
    #: The file under ``/run/mandala-secrets/user/files`` the value is
    #: published as, or ``None``.
    file: str | None = None

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "secret bindings") -> SecretBinding:
        """Refuses rather than guesses: a row it cannot read raises.

        A binding list read short would look complete, and a caller replacing
        the list from it would unbind a secret it never saw.
        """
        secret_id = _binding_text(d, "secret_id", where)
        revision_id = _binding_text(d, "revision_id", where)
        env = _binding_name(d, "env", SECRET_ENV, where)
        file = _binding_name(d, "file", SECRET_FILE, where)
        if (env is None) == (file is None):
            raise MandalaError(f"{where}: {secret_id} must name exactly one of env and file")
        return cls(secret_id=secret_id, revision_id=revision_id, env=env, file=file)


@dataclass(frozen=True)
class SecretBindings:
    """A computer's secret bindings, and the ``version`` a change sends back.

    From :meth:`~mandala_computer.Computer.secrets` and
    :meth:`~mandala_computer.Computer.set_secrets`.
    """

    secrets: builtins.list[SecretBinding]
    #: Send this with :meth:`~mandala_computer.Computer.set_secrets` to change
    #: only the list you read.
    version: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "secret bindings") -> SecretBindings:
        """Refuses an answer that is not a whole binding list with its version.

        Every row must read, and ``version`` must be a non-negative whole
        number: one invented here would be sent back by a replace as if a read
        had answered it.
        """
        rows = d.get("secrets")
        if not isinstance(rows, list):
            raise MandalaError(f"expected secret bindings from {where}")
        bindings = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise MandalaError(f"{where}: a secret binding is not an object")
            bindings.append(SecretBinding.from_api(row, where))
        version = d.get("version")
        number = (
            int.__index__(version)
            if isinstance(version, int) and not isinstance(version, bool)
            else -1
        )
        if number < 0:
            raise MandalaError(f"{where}: version must be a non-negative whole number")
        return cls(secrets=bindings, version=number, raw=dict(d))


@dataclass(frozen=True)
class SecretsReceipt:
    """The confirmation that a computer's bound values reached its desktop session.

    :attr:`~mandala_computer.Computer.secrets_applied`. A start whose delivery
    did not land has no receipt for its generation, and
    :attr:`~mandala_computer.Computer.secrets_error` says why.
    """

    #: The delivering start this receipt is for. Behind
    #: :attr:`~mandala_computer.Computer.secrets_generation` means it is from an
    #: earlier start.
    generation: int
    #: RFC 3339: when the values reached the desktop session.
    applied_at: str
    #: Secret id to the revision id that was delivered, for every binding.
    revisions: dict[str, str]

    @classmethod
    def from_api(cls, value: object) -> SecretsReceipt | None:
        if not isinstance(value, Mapping):
            return None
        revisions = value.get("revisions")
        return cls(
            generation=_num(value.get("generation")),
            applied_at=_text(value.get("applied_at")),
            revisions=(
                {str(k): _text(v) for k, v in revisions.items()}
                if isinstance(revisions, Mapping)
                else {}
            ),
        )


# --- the account's secret store ---------------------------------------------


def _store_text(d: Mapping[str, Any], key: str, where: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise MandalaError(f"{where}: a secret has no usable {key}")
    return str.__str__(value)


@dataclass(frozen=True)
class Secret:
    """One secret in the account's store — never its value.

    From :attr:`mandala_computer.Client.secrets`. No route ever returns a value:
    a value goes in with :meth:`~mandala_computer.Secrets.create` or
    :meth:`~mandala_computer.Secrets.replace` and comes out only inside the
    computers bound to it.
    """

    #: ``csec-`` and sixteen hex characters. What a binding names as ``secret_id``.
    id: str
    #: Unique within its scope. Up to 60 characters.
    name: str
    #: The workspace it belongs to, or ``None`` for one available account-wide.
    workspace_id: str | None
    #: ``csr-`` and twenty-four hex characters. Moves every time the value is
    #: replaced; a replace or a delete sends back the one it read, and is
    #: refused with a :class:`~mandala_computer.ConflictError` if it has moved.
    revision_id: str
    created_at: str
    #: The last replace, or the create.
    updated_at: str
    #: When it was last delivered to a computer. ``None`` until it has been.
    last_used_at: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "secret") -> Secret:
        """Refuses a row without a usable id, name or revision.

        A revision this invented would be sent back by a replace or a delete
        as if a read had answered it.
        """
        workspace = d.get("workspace_id")
        return cls(
            id=_store_text(d, "id", where),
            name=_store_text(d, "name", where),
            workspace_id=str.__str__(workspace)
            if isinstance(workspace, str) and workspace
            else None,
            revision_id=_store_text(d, "revision_id", where),
            created_at=_text(d.get("created_at")),
            updated_at=_text(d.get("updated_at")),
            last_used_at=_opt_text(d.get("last_used_at")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class SecretLimits:
    """The store's limits, as ``GET /secrets`` reports them."""

    #: The longest name, in characters.
    name_max_chars: int
    #: The largest value, in bytes of UTF-8.
    value_max_bytes: int
    #: How many secrets the account may hold at once, across every workspace.
    #: Deleting one frees a place.
    active_per_account: int
    #: How many the account may create over its lifetime, deleted ones included.
    #: Deleting does not free a place; support can raise it.
    created_per_account: int

    @classmethod
    def from_api(cls, value: object) -> SecretLimits | None:
        if not isinstance(value, Mapping):
            return None
        return cls(
            name_max_chars=_num(value.get("name_max_chars")),
            value_max_bytes=_num(value.get("value_max_bytes")),
            active_per_account=_num(value.get("active_per_account")),
            created_per_account=_num(value.get("created_per_account")),
        )


@dataclass(frozen=True)
class SecretList:
    """The secrets in one scope, and the store's limits.

    From :meth:`~mandala_computer.Secrets.list`. Names, ids and revisions only.
    """

    secrets: builtins.list[Secret]
    #: Whether a secret can be bound to a computer on this platform. While
    #: ``False`` secrets can be stored, but a binding is refused. ``None`` when
    #: the platform did not say.
    delivery: bool | None
    limits: SecretLimits | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "GET secrets") -> SecretList:
        """Refuses an answer without a whole list: a short one would look complete."""
        rows = d.get("secrets")
        if not isinstance(rows, builtins.list):
            raise MandalaError(f"expected a secret list from {where}")
        secrets = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise MandalaError(f"{where}: a secret is not an object")
            secrets.append(Secret.from_api(row, where))
        return cls(
            secrets=secrets,
            delivery=_opt_flag(d, "delivery"),
            limits=SecretLimits.from_api(d.get("limits")),
            raw=dict(d),
        )


# --- guest directories ------------------------------------------------------


@dataclass(frozen=True)
class DirectoryEntry:
    """One name in a guest directory."""

    #: The exact filename. Keep its Unicode, spaces and punctuation when
    #: building a path from it.
    name: str
    #: ``"file"``, ``"directory"``, ``"symlink"``, ``"special"`` or
    #: ``"unavailable"``. Read as an open set.
    type: str
    #: A regular file's size, when known. ``None`` for every other type.
    size_bytes: int | None = None


@dataclass(frozen=True)
class GuestDirectory:
    """A bounded listing of one directory inside a guest.

    From :meth:`~mandala_computer.Computer.list_directory`. Not a page: when
    :attr:`truncated` is set, :attr:`entries` is an unordered sample with no
    continuation, and the way to see more is to list a narrower directory.
    """

    #: The directory that was listed.
    path: str
    entries: builtins.list[DirectoryEntry]
    #: The directory was larger than one listing examines, so :attr:`entries`
    #: is partial.
    truncated: bool
    #: Names left out because their encoding or control characters cannot be
    #: used by the file API.
    skipped: int
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> GuestDirectory:
        """Refuses an answer without an entry list, which would read as empty."""
        rows = d.get("entries")
        if not isinstance(rows, builtins.list):
            raise MandalaError("expected directory entries from GET computers/:id/files/list")
        entries = [
            DirectoryEntry(
                name=_text(row.get("name")),
                type=_text(row.get("type")),
                size_bytes=_opt_whole(row.get("size_bytes")),
            )
            for row in rows
            if isinstance(row, Mapping)
        ]
        # A partial listing read as complete is the one misreading that matters,
        # so anything but a plain "false" leaves the flag set.
        return cls(
            path=_text(d.get("path")),
            entries=entries,
            truncated=_wire(d, "truncated") is not _Wire.FALSE,
            skipped=_num(d.get("skipped")),
            raw=dict(d),
        )


# --- passive signals and API activity ---------------------------------------


@dataclass(frozen=True)
class SignalPage:
    """One read of a computer's passive platform signals.

    From :meth:`~mandala_computer.Computer.signals`. Ephemeral observations
    the host made — a computer started, stopped, suspended or went idle, a
    background process exited — not durable history, and not proof that a task
    succeeded. Keep :attr:`cursor` and pass it as ``since`` on the next read.
    """

    computer: str
    #: The checkpoint this page starts from, or the current head on a baseline
    #: or a reset. Named ``from`` on the wire.
    from_cursor: str
    #: The next checkpoint. Pass it as ``since`` to read on.
    cursor: str
    #: The events, each exactly as the platform sent it: ``type``, ``seq``,
    #: ``cursor``, ``at``, ``computer``, ``source`` and ``data``.
    events: builtins.list[Mapping[str, Any]]
    #: Another event is owed: read again with :attr:`cursor`.
    more: bool
    #: The first read, which starts at the current head with no history.
    baseline: bool
    #: Present when the cursor had expired, was malformed, or its host
    #: restarted or moved: events were missed, and :attr:`cursor` is a new head.
    gap: Mapping[str, Any] | None
    #: The signal types this host produces.
    supported: builtins.list[str]
    #: ``"ephemeral"``.
    retention: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> SignalPage:
        """Refuses a page without a cursor or an event list.

        A failure must never read as an empty history: the platform keeps that
        promise on its side, and a decoder that filled in an empty list would
        break it on this one.
        """
        cursor = d.get("cursor")
        events = d.get("events")
        if not isinstance(cursor, str) or not isinstance(events, builtins.list):
            raise MandalaError("expected a signal page from GET computers/:id/signals")
        gap = d.get("gap")
        return cls(
            computer=_text(d.get("computer")),
            from_cursor=_text(d.get("from")),
            cursor=str.__str__(cursor),
            events=[dict(e) for e in events if isinstance(e, Mapping)],
            more=_wire(d, "more") is _Wire.TRUE,
            baseline=_wire(d, "baseline") is _Wire.TRUE,
            gap=dict(gap) if isinstance(gap, Mapping) else None,
            supported=_texts(d.get("supported")),
            retention=_text(d.get("retention")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class Activity:
    """One request sent through the computer API, as the platform retained it.

    From :meth:`~mandala_computer.Computer.activities` and
    :meth:`~mandala_computer.Computer.activity`. Safe metadata only: no command,
    output, input text or error prose is ever retained. It does not identify an
    agent, and it does not prove what happened in the guest.

    ``channel``, ``route``, ``action``, ``state`` and ``reason`` are the
    platform's vocabularies; read them as open sets.
    """

    #: Immutable. A request's identity, never an idempotency key.
    activity_id: str
    account_id: str
    computer_id: str
    #: The workspace when the request was admitted, or ``None``.
    workspace_id: str | None
    channel: str
    route: str
    action: str
    state: str
    #: When the platform received the request, UTC.
    received_at: str
    #: When the platform observed its outcome, UTC.
    observed_at: str
    #: Moves on every change to the row, late result links included.
    revision: int | None = None
    #: A hint that :meth:`~mandala_computer.Computer.activity_results` may
    #: have something.
    has_results: bool = False
    #: The first transport attempt, when one was observed. Not proof that the
    #: guest ran anything.
    dispatched_at: str | None = None
    #: The request's elapsed time, network and admission included — not how
    #: long the guest took.
    elapsed_ms: int | None = None
    reason: str | None = None
    #: The response status, when known. An error after dispatch does not prove
    #: the action had no effect.
    http_status: int | None = None
    #: A known synchronous command exit.
    exit_code: int | None = None
    #: The execution id the original request returned.
    execution_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> Activity:
        workspace = d.get("workspace_id")
        return cls(
            activity_id=_text(d.get("activity_id")),
            account_id=_text(d.get("account_id")),
            computer_id=_text(d.get("computer_id")),
            workspace_id=str(workspace) if isinstance(workspace, str) and workspace else None,
            channel=_text(d.get("channel")),
            route=_text(d.get("route")),
            action=_text(d.get("action")),
            state=_text(d.get("state")),
            received_at=_text(d.get("received_at")),
            observed_at=_text(d.get("observed_at")),
            revision=_opt_whole(d.get("revision")),
            has_results=_wire(d, "has_results") is _Wire.TRUE,
            dispatched_at=_opt_text(d.get("dispatched_at")),
            elapsed_ms=_opt_whole(d.get("elapsed_ms")),
            reason=_opt_text(d.get("reason")),
            http_status=_opt_whole(d.get("http_status")),
            exit_code=_opt_whole(d.get("exit_code")),
            execution_id=_opt_text(d.get("execution_id")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class ActivityHealth:
    """How much of an account's API activity history is there to read."""

    #: When recording began. Earlier requests are not reconstructed.
    recording_started_at: str
    #: The earliest retained row in this scope, or ``None``.
    earliest_retained_at: str | None
    #: History has crossed a row-count retention boundary.
    count_truncated: bool
    #: History has crossed an age retention boundary.
    age_truncated: bool
    #: ``"available"`` or ``"degraded"``.
    capture: str
    #: ``"best-effort"``: operational history, not an audit record.
    completeness: str
    #: The most recent capture gap within retention, or ``None``.
    gap_at: str | None
    #: The last recovery from a capture gap, or ``None``.
    recovered_at: str | None

    @classmethod
    def from_api(cls, value: object) -> ActivityHealth | None:
        if not isinstance(value, Mapping):
            return None
        return cls(
            recording_started_at=_text(value.get("recording_started_at")),
            earliest_retained_at=_opt_text(value.get("earliest_retained_at")),
            count_truncated=_wire(value, "count_truncated") is _Wire.TRUE,
            age_truncated=_wire(value, "age_truncated") is _Wire.TRUE,
            capture=_text(value.get("capture")),
            completeness=_text(value.get("completeness")),
            gap_at=_opt_text(value.get("gap_at")),
            recovered_at=_opt_text(value.get("recovered_at")),
        )


@dataclass(frozen=True)
class ActivityPage:
    """A page of a computer's API activity, newest first, or a page of changes.

    From :meth:`~mandala_computer.Computer.activities`. Follow
    :attr:`next_cursor` for older rows; pass :attr:`changes_cursor` with
    ``changes=True`` later to receive new rows and rows that have since
    finished. :attr:`gap` on a change page means the cursor expired: discard it
    and read the history again.
    """

    items: builtins.list[Activity]
    next_cursor: str | None
    changes_cursor: str
    gap: bool
    health: ActivityHealth | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ActivityPage:
        """Refuses a page without its rows, which would read as no activity."""
        rows = d.get("items")
        if not isinstance(rows, builtins.list):
            raise MandalaError("expected an activity page from GET computers/:id/activities")
        next_cursor = d.get("next_cursor")
        return cls(
            items=[Activity.from_api(r) for r in rows if isinstance(r, Mapping)],
            next_cursor=next_cursor if isinstance(next_cursor, str) and next_cursor else None,
            changes_cursor=_text(d.get("changes_cursor")),
            gap=_wire(d, "gap") is _Wire.TRUE,
            health=ActivityHealth.from_api(d.get("health")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class ActivityResults:
    """The retained results one activity links to, as references only.

    From :meth:`~mandala_computer.Computer.activity_results`. Each item is a
    retained result or artifact, as the platform sent it — ``id``, ``kind``,
    ``association``, ``availability`` and, when available, its sizes and
    expiry. Read the content itself with
    :meth:`~mandala_computer.Computer.result` or the artifact
    methods. An artifact linked here does not prove that anything succeeded.
    """

    activity_id: str
    revision: int | None
    #: More versions exist than the newest eight shown.
    more: bool
    items: builtins.list[Mapping[str, Any]]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any]) -> ActivityResults:
        rows = d.get("items")
        if not isinstance(rows, builtins.list):
            raise MandalaError(
                "expected activity results from GET computers/:id/activities/:activity/results"
            )
        return cls(
            activity_id=_text(d.get("activity_id")),
            revision=_opt_whole(d.get("revision")),
            more=_wire(d, "more") is _Wire.TRUE,
            items=[dict(r) for r in rows if isinstance(r, Mapping)],
            raw=dict(d),
        )


# --- deleting a computer ------------------------------------------------------


@dataclass(frozen=True)
class SnapshotPurge:
    """What a delete with ``purge_snapshots=True`` did to the selected copies.

    Counts of physical copies: a snapshot held on two hosts counts twice.
    """

    #: Copies the confirmation selected.
    selected: int
    #: Selected copies confirmed gone.
    confirmed: int
    #: Selected copies still listed as being deleted. Unknown outcome.
    queued: int
    #: Selected copies whose deletion was refused.
    failed: int
    #: Selected copies with an unknown outcome.
    unknown: int
    #: Selected copies still visible in the final inventory, or ``None`` when
    #: that inventory could not be read.
    remaining: int | None
    #: New copies this request did not select, or ``None`` when unknown. They
    #: need a new confirmation to delete.
    unselected: int | None
    #: Every selected copy was confirmed gone in a complete final inventory.
    complete: bool

    @classmethod
    def from_api(cls, value: object) -> SnapshotPurge | None:
        if not isinstance(value, Mapping):
            return None
        return cls(
            selected=_num(value.get("selected")),
            confirmed=_num(value.get("confirmed")),
            queued=_num(value.get("queued")),
            failed=_num(value.get("failed")),
            unknown=_num(value.get("unknown")),
            remaining=_opt_whole(value.get("remaining")),
            unselected=_opt_whole(value.get("unselected")),
            complete=_wire(value, "complete") is _Wire.TRUE,
        )


@dataclass(frozen=True)
class ComputerDeletion:
    """Everything a computer delete answered.

    From ``Computer.delete(..., detailed=True)``. The platform answers 202
    when work remains queued, so read :attr:`ok`, :attr:`computer_deleted` and
    :attr:`purge` even on success.
    """

    #: Whether the requested cleanup completed. ``None`` when not said.
    ok: bool | None
    #: Physical snapshot copies confirmed deleted, or ``None`` when not said.
    snapshots_deleted: int | None
    #: On a purge: ``True`` records a confirmed deletion, ``False`` means it is
    #: unconfirmed or the computer was seen again, and ``None`` means unknown.
    computer_deleted: bool | None
    #: An explanation of a partial, queued, refused or unknown outcome.
    error: str | None
    purge: SnapshotPurge | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any] | None) -> ComputerDeletion:
        d = d or {}
        deleted = d.get("snapshots_deleted")
        error = d.get("error")
        return cls(
            ok=_opt_flag(d, "ok"),
            snapshots_deleted=None if deleted is None else _opt_whole(deleted),
            computer_deleted=_opt_flag(d, "computer_deleted"),
            error=error if isinstance(error, str) and error else None,
            purge=SnapshotPurge.from_api(d.get("purge")),
            raw=dict(d),
        )


# --- API keys and whoami (platform OPL-5053) ----------------------------------


def _key_text(d: Mapping[str, Any], key: str, where: str) -> str:
    value = d.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise MandalaError(f"{where}: no usable {key}")
    return str.__str__(value)


def _nullable_text(d: Mapping[str, Any], key: str, where: str) -> str | None:
    value = d.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise MandalaError(f"{where}: {key} is neither a string nor null")
    return str.__str__(value)


@dataclass(frozen=True)
class ApiKey:
    """One API key, as the platform lists it — never the key itself.

    From :attr:`mandala_computer.Client.api_keys`. The raw key is on
    :class:`ApiKeyCreated` and nowhere else, answered once by the mint.
    """

    #: ``key-`` and twelve hex characters. What :meth:`~mandala_computer.ApiKeys.revoke` takes.
    id: str
    #: The label it was minted with; ``None`` when it has none.
    name: str | None
    #: The first characters of the key and an ellipsis, for telling keys apart.
    #: Display only. ``oauth`` for a Connected app's key.
    prefix: str
    created_at: str
    #: When it last authenticated a request. ``None`` until it has.
    last_used_at: str | None
    #: The workspace it is confined to, or ``None`` for one that acts on the whole account.
    workspace_id: str | None
    workspace_name: str | None
    #: Whether it may list, mint and revoke keys. Only a dashboard session turns
    #: this on, and a key minted over the API never has it.
    manage_keys: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "API key") -> ApiKey:
        """Refuses a row without a usable id, or that cannot say whether it manages keys.

        The id is what a revoke sends back, and ``manage_keys`` is a
        permission: a key this cannot read it on is not one to report as
        holding none.
        """
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        manage = d.get("manage_keys")
        if not isinstance(manage, bool):
            raise MandalaError(f"{where}: manage_keys is not true or false")
        last_used = d.get("last_used_at")
        return cls(
            id=_key_text(d, "id", where),
            name=_nullable_text(d, "name", where),
            prefix=_text(d.get("prefix")),
            created_at=_text(d.get("created_at")),
            last_used_at=str(last_used) if isinstance(last_used, str) and last_used else None,
            workspace_id=_nullable_text(d, "workspace_id", where),
            workspace_name=_nullable_text(d, "workspace_name", where),
            manage_keys=manage,
            raw=dict(d),
        )


@dataclass(frozen=True)
class ApiKeyCreated(ApiKey):
    """:class:`ApiKey` with the one thing only the mint answers."""

    #: The key: ``com_`` and 48 hex characters. SHOWN HERE AND NEVER AGAIN —
    #: store it now. Named ``key`` because ``raw`` is the wire object on every
    #: model here; on the wire it is ``raw``.
    key: str = field(default="", repr=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "POST api-keys") -> ApiKeyCreated:
        """Refused when the key is not in it: shown once, so an answer without
        it is a key nobody can ever use, reported as made."""
        base = ApiKey.from_api(d, where)
        secret = d.get("raw")
        if not isinstance(secret, str) or not secret.startswith("com_"):
            raise MandalaError(
                f"{where}: expected the new key, which the platform answers once, and it is not here"
            )
        return cls(**{f: getattr(base, f) for f in base.__dataclass_fields__}, key=secret)


@dataclass(frozen=True)
class WhoamiUser:
    """The person a credential was issued to."""

    id: str
    email: str
    name: str | None


@dataclass(frozen=True)
class WhoamiAccount:
    """The account a credential acts on."""

    id: str
    name: str | None
    plan: str
    #: ``active`` or ``suspended``. A suspended account can still ask who it is.
    status: str


@dataclass(frozen=True)
class WhoamiWorkspace:
    """The workspace a credential is confined to."""

    id: str
    name: str
    created_at: str


@dataclass(frozen=True)
class Whoami:
    """Who a credential is: :meth:`mandala_computer.Account.whoami`."""

    user: WhoamiUser
    account: WhoamiAccount
    #: ``owner``, ``member`` or ``viewer`` — as it is NOW, not as it was when
    #: the key was minted. An open set: show one this client does not know.
    role: str
    #: ``None`` for a key that acts on the whole account.
    workspace: WhoamiWorkspace | None
    #: The key itself. ``None`` is possible but not expected. On a Connected
    #: app's access token this is the app's hidden key: ``prefix`` is ``oauth``.
    key: ApiKey | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "GET whoami") -> Whoami:
        user = d.get("user")
        account = d.get("account")
        if not isinstance(user, Mapping) or not isinstance(account, Mapping):
            raise MandalaError(f"{where}: expected a user and an account")
        role = d.get("role")
        if not isinstance(role, str) or not role:
            raise MandalaError(f"{where}: no role")
        ws = d.get("workspace")
        workspace: WhoamiWorkspace | None = None
        if ws is not None:
            if not isinstance(ws, Mapping):
                raise MandalaError(f"{where}: the workspace is neither an object nor null")
            workspace = WhoamiWorkspace(
                id=_key_text(ws, "id", f"{where} workspace"),
                name=_text(ws.get("name")),
                created_at=_text(ws.get("created_at")),
            )
        key = d.get("key")
        return cls(
            user=WhoamiUser(
                id=_key_text(user, "id", f"{where} user"),
                email=_text(user.get("email")),
                name=_nullable_text(user, "name", f"{where} user"),
            ),
            account=WhoamiAccount(
                id=_key_text(account, "id", f"{where} account"),
                name=_nullable_text(account, "name", f"{where} account"),
                plan=_text(account.get("plan")),
                status=_text(account.get("status")),
            ),
            role=str.__str__(role),
            workspace=workspace,
            key=None if key is None else ApiKey.from_api(key, f"{where} key"),
            raw=dict(d),
        )


# --- lifecycle operations -----------------------------------------------------


def operation_id_of(data: Any) -> str | None:
    """The ``operation_id`` a lifecycle answer carried, or ``None`` for none.

    Absent is ordinary and never an error: the platform leaves it out, with the
    call still done, when it could not record the operation, and an older
    platform never sends one. Anything that is not a non-empty string reads as
    absent for the same reason — the call it came back on already happened.
    """
    if not isinstance(data, Mapping):
        return None
    value = data.get("operation_id")
    return str.__str__(value) if isinstance(value, str) and value else None


def answered_operation_id(resp: Any) -> str | None:
    """:func:`operation_id_of`, off a lifecycle acknowledgement's raw response.

    The start, stop, suspend and restart calls never read their answer, and a
    body this cannot parse is no reason to fail a call that has already been
    done — so anything unreadable is ``None`` here rather than an error.
    """
    if not getattr(resp, "content", b""):
        return None
    try:
        return operation_id_of(resp.json())
    except ValueError:
        return None


@dataclass(frozen=True)
class OperationError:
    """Why an operation failed."""

    #: The part to act on: ``start_failed``, ``build_failed``,
    #: ``computer_gone``, ``move_failed``, ``resize_not_applied``, ``lost``, and
    #: more may be added.
    code: str
    #: A sentence about it, for a person.
    message: str


@dataclass(frozen=True)
class Operation:
    """One lifecycle operation (platform OPL-5055): what an accepted create,
    clone, start, stop, suspend, restart, restore, resize or move started, and
    how it ended. :meth:`~mandala_computer.Operations.wait` polls one to its end.

    ``succeeded`` MEANS THE PLATFORM FINISHED ITS STEP, not that the desktop
    inside has booted: a create or a start that succeeded is a guest that was
    started. :meth:`~mandala_computer.Computer.wait_for_guest` is still the wait
    for a desktop that answers.

    Most operations are already ``succeeded`` when the call that started them
    answers, because the platform did the step before answering. A clone is
    ``running`` until its disk is copied, and a move until it lands.
    """

    #: ``op_`` and 24 hex characters: the ``operation_id`` a lifecycle call answered.
    id: str
    #: The call that started it. An open set: the platform adds kinds, and one
    #: this version does not know is a kind, not a malformed answer.
    kind: str
    #: The computer it is about — for a create or a clone, the new one.
    #: ``None`` only for a restore whose computer could not be named.
    computer_id: str | None
    #: ``pending`` or ``running`` while live; ``succeeded`` or ``failed`` once
    #: final. Open, for the reason :attr:`kind` is.
    state: str
    #: Why a ``failed`` one failed; ``None`` otherwise.
    error: OperationError | None
    created_at: str
    #: When :attr:`state` or :attr:`error` last changed.
    updated_at: str
    #: ``None`` while it is live.
    finished_at: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "operation") -> Operation:
        """Strict on the three fields a wait decides on — the id it polls, the
        state it ends on, and the error it raises — and on the error's shape,
        because a ``failed`` operation whose reason was silently dropped would
        be raised as a failure with nothing to say about why."""
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        state = d.get("state")
        if not isinstance(state, str) or not state:
            raise MandalaError(f"{where}: no usable state")
        kind = d.get("kind")
        if not isinstance(kind, str) or not kind:
            raise MandalaError(f"{where}: no usable kind")
        error: OperationError | None = None
        raw_error = d.get("error")
        if raw_error is not None:
            if (
                not isinstance(raw_error, Mapping)
                or not isinstance(raw_error.get("code"), str)
                or not isinstance(raw_error.get("message"), str)
            ):
                raise MandalaError(f"{where}: error is neither null nor a code and a message")
            error = OperationError(
                code=str.__str__(raw_error["code"]), message=str.__str__(raw_error["message"])
            )
        finished = d.get("finished_at")
        return cls(
            id=_key_text(d, "id", where),
            kind=str.__str__(kind),
            computer_id=_nullable_text(d, "computer_id", where),
            state=str.__str__(state),
            error=error,
            created_at=_text(d.get("created_at")),
            updated_at=_text(d.get("updated_at")),
            finished_at=_text(finished) if finished else None,
            raw=dict(d),
        )


@dataclass(frozen=True)
class OperationPage:
    """One page of :meth:`~mandala_computer.Operations.list`, newest first."""

    operations: builtins.list[Operation]
    #: Pass as ``cursor`` for the page after this one; ``None`` on the last page.
    next_cursor: str | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Mapping[str, Any], where: str = "GET operations") -> OperationPage:
        """Refuses a page whose cursor could not be walked: one that is not a
        string would be sent back as its ``repr``, and one silently read as
        ``None`` would end the walk early and call the listing complete."""
        rows = d.get("operations") if isinstance(d, Mapping) else None
        if not isinstance(rows, builtins.list):
            raise MandalaError(f"{where}: expected a list of operations")
        nxt = d.get("next_cursor")
        if nxt is not None and (not isinstance(nxt, str) or not nxt):
            raise MandalaError(f"{where}: next_cursor is neither a string nor null")
        return cls(
            operations=[
                Operation.from_api(row, f"operation {i} from {where}") for i, row in enumerate(rows)
            ],
            next_cursor=str.__str__(nxt) if nxt is not None else None,
            raw=dict(d),
        )


@dataclass(frozen=True)
class LifecycleAck:
    """What a lifecycle call that finishes before it answers returns:
    ``{"ok": true}`` and the operation it recorded."""

    #: The operation this call recorded, already ``succeeded``. ``None`` where
    #: the platform could not record one or predates operations; the call is
    #: done either way.
    operation_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_response(cls, resp: Any) -> LifecycleAck:
        data: Any = None
        if getattr(resp, "content", b""):
            try:
                data = resp.json()
            except ValueError:
                data = None
        return cls(
            operation_id=operation_id_of(data),
            raw=dict(data) if isinstance(data, Mapping) else {},
        )
