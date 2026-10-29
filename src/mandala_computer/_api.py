"""Paths and request bodies, shared by the sync and async clients.

Every route the SDK can reach and every payload it can send is built here. The
two clients differ only in awaits; if either built its own URLs or bodies, they
could drift apart silently — and the surface test that pins the SDK to the
platform's allowlist would only be checking one of them.
"""

from __future__ import annotations

import ipaddress
import math
import re
import shlex
import time
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import quote

import httpx

from ._exceptions import APIError, MandalaError
from ._timestamps import parse_rfc3339

if TYPE_CHECKING:
    from ._models import ApiKeyCreated

T = TypeVar("T")

# Browser session bounds are mirrored in the platform surface manifest.
BROWSER_MIN_LEASE_SECONDS = 60
BROWSER_DEFAULT_LEASE_SECONDS = 1800
BROWSER_MAX_SESSION_SECONDS = 7200

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
#: Instantaneous account-wide quota, including for workspace-scoped keys.
ACCOUNT = "account"
#: The account's AI credit ledger: balance, live lots, the plan's grant and a
#: page of journal entries. Account-wide, so a key confined to a workspace is
#: refused with 403.
CREDITS = "credits"
#: The most journal entries one ``GET credits`` page holds, and how many it
#: holds when ``limit`` is left out.
CREDIT_PAGE_MAX = 100
CREDIT_PAGE_DEFAULT = 50
#: The largest ``before`` the platform accepts: JavaScript's safe-integer
#: ceiling, because the journal's ids are numbers on the other side.
CREDIT_CURSOR_MAX = 9007199254740991
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
#: The caller's own SSH public keys. A key belongs to a person rather than to an
#: account, so the collection is the same whichever account a key acts on.
SSH_KEYS = "ssh-keys"
#: Who the credential is (platform OPL-5053): the person, the account and role
#: it acts with, the workspace it is confined to, and the key itself. Needs no
#: permission, and answers a suspended account too.
WHOAMI = "whoami"
#: The holder's own API keys (platform OPL-5053). Every verb needs the key to be
#: allowed to manage keys, an opt-in only a dashboard session turns on;
#: without it the platform answers 403 with a sentence that says so.
API_KEYS = "api-keys"
#: Lifecycle operations (platform OPL-5055): what each accepted create, clone,
#: start, stop, suspend, restart, restore, resize, move and delete started, and
#: how it ended. Read only.
OPERATIONS = "operations"
#: The account's workspaces: list, read, create, rename and delete.
WORKSPACES = "workspaces"
#: The most one page of ``GET operations`` holds; the platform refuses more.
OPERATIONS_PAGE_MAX = 100


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
        # Padding carries no magnitude, and refusing a zero-padded pid that
        # `_exact_int` reads exactly would have the two helpers disagree about
        # the same string.
        digits = digits.lstrip("0") or "0"
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

    Not ``computer_action``: the pid is a second path segment, and the platform
    reduces it to ``:pid`` rather than to ``:id`` — it names something inside a
    computer rather than a thing the platform owns.
    """
    return f"computers/{seg(computer_id)}/exec/{guest_pid(pid)}"


EXECUTION_MAX_OFFSET = 9_007_199_254_740_991
EXECUTION_READ_DEFAULT = 65_536
EXECUTION_READ_MAX = 1_048_576


def execution_id(value: str) -> str:
    """An opaque execution identity; never a PID or a path fragment."""
    value = canonical(value, "execution_id")
    if not re.fullmatch(r"exec_[0-9a-f]{32}", value):
        raise ValueError("execution_id must be exec_ followed by 32 lowercase hex digits")
    return value


def execution(computer_id: str, identity: str) -> str:
    return f"{computer(computer_id)}/executions/{execution_id(identity)}"


def execution_output(computer_id: str, identity: str) -> str:
    return f"{execution(computer_id, identity)}/output"


def execution_output_params(stdout_offset: int, stderr_offset: int, limit: int) -> dict[str, int]:
    """Independent byte positions, with room for a full read in the safe range."""
    limit = whole(limit, "limit", exc=ValueError)
    if not 1 <= limit <= EXECUTION_READ_MAX:
        raise ValueError(f"limit must be from 1 to {EXECUTION_READ_MAX}")
    params = {"limit": limit}
    for name, value in (("stdout_offset", stdout_offset), ("stderr_offset", stderr_offset)):
        value = whole(value, name, exc=ValueError)
        if not 0 <= value <= EXECUTION_MAX_OFFSET - limit:
            raise ValueError(f"{name} plus limit must be within the safe integer range")
        params[name] = value
    return params


RESULT_PAGE_MAX = 65_536
RESULT_STREAM_MAX = 4 * 1024 * 1024
RESULT_RETENTION_MAX = 604_800
ARTIFACT_DEFAULT_BYTES = 8 * 1024 * 1024
ARTIFACT_MAX_BYTES = 64 * 1024 * 1024


def result_id(value: str) -> str:
    value = canonical(value, "result_id")
    if not re.fullmatch(r"res_[0-9a-f]{32}", value):
        raise ValueError("result_id must be res_ followed by 32 lowercase hex digits")
    return value


def artifact_id(value: str) -> str:
    value = canonical(value, "artifact_id")
    if not re.fullmatch(r"art_[0-9a-f]{32}", value):
        raise ValueError("artifact_id must be art_ followed by 32 lowercase hex digits")
    return value


def retained_result(computer_id: str, identity: str) -> str:
    return f"{computer(computer_id)}/results/{result_id(identity)}"


def artifacts(computer_id: str) -> str:
    return f"{computer(computer_id)}/artifacts"


def artifact(computer_id: str, identity: str) -> str:
    return f"{artifacts(computer_id)}/{artifact_id(identity)}"


def bounded_integer(value: object, name: str, minimum: int, maximum: int) -> int:
    number = whole(value, name, exc=ValueError)
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be from {minimum} to {maximum}")
    return number


def retained_options(
    max_bytes_per_stream: int | None, retention_seconds: int | None
) -> dict[str, int]:
    body: dict[str, int] = {}
    if max_bytes_per_stream is not None:
        body["max_bytes_per_stream"] = bounded_integer(
            max_bytes_per_stream, "max_bytes_per_stream", 1, RESULT_STREAM_MAX
        )
    if retention_seconds is not None:
        body["retention_seconds"] = bounded_integer(
            retention_seconds, "retention_seconds", 1, RESULT_RETENTION_MAX
        )
    return body


def retain_output_option(value: object) -> bool | dict[str, int]:
    if isinstance(value, bool):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("retain_output must be a boolean or retention options object")
    body: dict[str, int] = {}
    for key, number in value.items():
        name = canonical(key, "retention option")
        maximum = {
            "max_bytes_per_stream": RESULT_STREAM_MAX,
            "retention_seconds": RESULT_RETENTION_MAX,
        }.get(name)
        if maximum is None or name in body:
            raise ValueError("unknown or duplicate retention option")
        body[name] = bounded_integer(number, name, 1, maximum)
    return body


def result_output_params(stream: str, offset: int, limit: int) -> dict[str, Any]:
    stream = canonical(stream, "stream")
    if stream not in ("stdout", "stderr", "diagnostic"):
        raise ValueError("stream must be stdout, stderr or diagnostic")
    count = bounded_integer(limit, "limit", 1, RESULT_PAGE_MAX)
    start = bounded_integer(offset, "offset", 0, EXECUTION_MAX_OFFSET - count)
    return {"stream": stream, "offset": start, "limit": count}


def artifact_body(
    path: str,
    *,
    expected_size: int,
    expected_sha256: str,
    association: str | None,
    max_bytes: int | None,
    retention_seconds: int | None,
) -> dict[str, Any]:
    text = canonical(path, "path")
    try:
        path_bytes = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("path must be valid UTF-8") from None
    if (
        not text
        or path_bytes > 4096
        or re.search(r"[\x00-\x1f\x7f]", text)
        or not is_absolute_guest_path(text)
    ):
        raise ValueError(
            "path must be an absolute guest path of at most 4096 UTF-8 bytes without controls"
        )
    size = bounded_integer(expected_size, "expected_size", 0, ARTIFACT_MAX_BYTES)
    maximum = (
        ARTIFACT_DEFAULT_BYTES
        if max_bytes is None
        else bounded_integer(max_bytes, "max_bytes", 1, ARTIFACT_MAX_BYTES)
    )
    if size > maximum:
        raise ValueError("expected_size exceeds the artifact capture max_bytes")
    digest = canonical(expected_sha256, "expected_sha256")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters")
    body: dict[str, Any] = {"path": text, "expected_size": size, "expected_sha256": digest}
    if association is not None:
        body["execution_id"] = execution_id(association)
    if max_bytes is not None:
        body["max_bytes"] = maximum
    if retention_seconds is not None:
        body["retention_seconds"] = bounded_integer(
            retention_seconds, "retention_seconds", 1, RESULT_RETENTION_MAX
        )
    return body


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
#: was sent as ``?version=1.0.0%0A``. The platform anchors the same grammar in
#: JavaScript, where ``$`` is end-of-input, so it answers 400 — the
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
    schema rather than a claim about the parser: the API reference gives this
    parameter ``enum: ['true']``, so ``true`` is the only value it
    admits and a client sending ``false`` is sending something undocumented.

    An earlier comment here said the platform reads the key's PRESENCE, which is
    false — the platform compares the value against ``"true"`` — and the
    same false claim was repeated in the other two clients and pinned as a test
    docstring. The emitted request was right either way; the stated reason was
    not.

    The flag has to BE a bool rather than merely read as one — see :func:`flag`.
    A rebuild is the expensive branch, and ``"false"`` selected it.
    """
    return {"no_reuse": "true"} if flag(no_reuse, "no_reuse") else {}


def build_stream_failed(build_id: str, data: Any, *, status: int | None = None) -> str:
    """What to say when a build's event stream ends with an ``error`` event.

    The stream's own failure, not the build's — named as such because a caller
    told "the build failed" would go and read a document that is fine. One
    function so the sync and async halves cannot word it differently.

    A frame with a 4xx ``status`` is a refusal of the caller (a revoked key, a
    role that no longer reaches the build), and reading ``builds.progress``
    would be refused the same way, so that advice is left off it.
    """
    detail = ""
    if isinstance(data, Mapping):
        detail = str(data.get("error") or "")
    elif data is not None:
        detail = str(data)
    said = f"the build event stream for {build_id} ended: {detail or 'no reason given'}"
    if status is not None and status < 500:
        return f"{said} (this says nothing about the build itself)"
    return f"{said} (this says nothing about the build itself — read builds.progress({build_id!r}))"


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
    absolute on Linux and *drive-relative* on Windows, where the daemon refuses
    it and wants ``C:\...`` or a ``\\`` UNC share. So
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


def files_params(path: str, no_wake: bool = False) -> dict[str, str]:
    """The query naming which guest file, checked before the round trip.

    The path must be absolute: nothing about a transfer runs in a shell, so a
    relative path has no working directory to be relative to. The daemon
    refuses it too, but this mistake is knowable without the round trip.

    ``no_wake`` sends ``no_wake=1``: the platform then requires a running
    computer and refuses one that is not with a 409 rather than resuming it —
    and consuming running capacity. Checked with :func:`flag`, like every arming flag.
    """
    # Canonical first, for the reason :func:`canonical` gives. ``startswith`` is
    # overridable, so a str subclass could satisfy the absoluteness check and
    # then send something else entirely — here, an empty path.
    text = canonical(path, "guest path")
    if not is_absolute_guest_path(text):
        raise ValueError(f"guest path must be absolute: {text!r}")
    params = {"path": text}
    if flag(no_wake, "no_wake"):
        params["no_wake"] = "1"
    return params


def files_list(computer_id: str) -> str:
    """A guest directory listing, beside the file transfer route."""
    return f"computers/{seg(computer_id)}/files/list"


def list_dir_params(path: str) -> dict[str, str]:
    """``GET computers/:id/files/list``: one absolute directory path.

    Only ``path``: the listing never resumes a computer, so it has no
    ``no_wake`` to send.
    """
    return {"path": files_params(path)["path"]}


#: The most signal events one page may hold. The platform's default is 50.
SIGNAL_PAGE_MAX = 100


def signals_params(since: object, limit: object) -> dict[str, str] | None:
    """``GET computers/:id/signals``: an opaque ``since`` and a page ``limit``.

    An empty or absent ``since`` asks for a baseline at the current head. The
    platform refuses a query it does not understand, so only what was given is
    sent.
    """
    params: dict[str, str] = {}
    if since is not None:
        cursor = canonical(since, "since")
        if cursor:
            params["since"] = cursor
    if limit is not None:
        number = whole(limit, "limit", exc=ValueError)
        if not 1 <= number <= SIGNAL_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {SIGNAL_PAGE_MAX}, not {number}")
        params["limit"] = str(number)
    return params or None


#: The longest activity cursor the platform accepts.
ACTIVITY_CURSOR_MAX = 2048


def activities(computer_id: str) -> str:
    return f"computers/{seg(computer_id)}/activities"


def activity(computer_id: str, activity_id: str) -> str:
    return f"computers/{seg(computer_id)}/activities/{seg(activity_id)}"


def activities_params(cursor: object, changes: object) -> dict[str, str] | None:
    """``GET computers/:id/activities``: a cursor, and ``changes=1`` to read changes.

    A change read needs a ``changes_cursor`` to read from; one without is
    refused here, where the platform would refuse it after the round trip.
    """
    params: dict[str, str] = {}
    if cursor is not None:
        text = canonical(cursor, "cursor")
        if not text or len(text) > ACTIVITY_CURSOR_MAX:
            raise ValueError(f"cursor must be 1 to {ACTIVITY_CURSOR_MAX} characters")
        params["cursor"] = text
    if flag(changes, "changes"):
        if "cursor" not in params:
            raise ValueError("changes=True needs the changes_cursor a page answered")
        params["changes"] = "1"
    return params or None


def upload_params(path: str, overwrite: bool, no_wake: bool = False) -> dict[str, str]:
    """The query for an upload: :func:`files_params`, plus create-only if asked.

    ``overwrite=False`` sends ``overwrite=false``, which makes the platform
    write the file only if nothing is at ``path`` (OPL-4994). ``True`` sends
    nothing: absent means replace on every platform version, so the default
    request is the one this client always sent. Checked with :func:`flag`,
    because ``"false"`` is a non-empty string and would read as true — replacing
    the very file the caller asked to keep.
    """
    params = files_params(path, no_wake)
    if not flag(overwrite, "overwrite"):
        params["overwrite"] = "false"
    return params


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
#: output somebody uses to compare usage across periods.
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})")


def _usage_stamp(value: object, what: str) -> str:
    """One bound of a usage window, in the spelling the platform reads.

    A ``datetime`` is the shape to prefer, and an AWARE one is the only kind
    accepted: a naive datetime has no zone, so rendering it would mean this SDK
    choosing between "the machine that called" and "UTC" — and being wrong about
    that shifts the window by hours on the one call whose output somebody checks
    across periods. ``datetime.now(timezone.utc)`` and
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
                "guessing one could move it by hours. Try "
                f"{what}=your_datetime.replace(tzinfo=timezone.utc)."
            )
        # isoformat() renders the offset it carries; UTC comes out as +00:00,
        # which is RFC 3339 and what the platform parses. Not forced to "Z":
        # the offset IS the instant, and normalising it would be this SDK
        # deciding what the caller meant.
        return value.isoformat()
    # `canonical` before the regex, and the CANONICAL string is what goes back:
    # a `str` subclass can hold a valid 2026 stamp and answer a valid 2030 one
    # to httpx, which measures a window nobody asked about on the one call whose
    # output is used to compare usage across periods.
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


def computer_listing_params(
    *, allow_partial: bool, state: str | None, workspace_id: str | None = None
) -> dict[str, str] | None:
    """The query on ``GET /computers``.

    ``state`` narrows the listing to one lifecycle state — the control plane's
    own record of whether a computer exists, which is a different question from
    the ``status`` its host reports. It is read by the control plane and
    stripped before anything is forwarded to a host, so it is on the same
    footing as ``allow_partial`` rather than being a filter a hypervisor
    applies.

    THE VOCABULARY IS THE PLATFORM'S, not this table's. ``live``,
    ``unreachable``, ``deleting``, ``deleted`` and ``lost`` are what it accepts
    today, and it answers 400 naming all five for anything else — so a value
    this SDK has never heard of reaches a caller as the platform's own refusal
    rather than as a ``ValueError`` from a client that shipped before the state
    existed. Nothing is silently dropped either way: the platform validates
    before it filters, so a typo cannot come back as an unnarrowed listing
    wearing the caller's assumption that it was narrowed.

    An EMPTY string is refused here, because that is the one spelling the
    platform's refusal would be the wrong answer to. ``?state=`` is what most
    clients serialise for an unset optional string, and a caller who reaches
    this by forwarding an absent CLI argument meant ``None`` — "every computer
    that exists or may exist" — rather than a request that fails.

    ``workspace_id`` (platform OPL-5543) narrows it to one workspace, or with
    ``"unassigned"`` to the computers in none; it is checked as a secret's
    scope is, and also read and stripped by the control plane.

    ``None`` rather than an empty dict when none is asked for, so the default
    listing builds a bare URL, the same way :func:`usage_params` does.
    """
    params = dict(partial_params(allow_partial) or {})
    workspace = _workspace(workspace_id)
    if workspace is not None:
        params["workspace_id"] = workspace
    if state is not None:
        # Canonical BEFORE the emptiness check, for the reason `canonical`
        # documents: httpx serialises a query value with `str(value)`, so a str
        # subclass answering non-empty here and "" there would put `?state=` on
        # the wire past a check that had just accepted it.
        text = canonical(state, "state").strip()
        if not text:
            raise ValueError(
                "state must name a lifecycle state — live, unreachable, deleting, "
                "deleted or lost — or be None for every computer that exists or "
                "may exist. An empty string is neither."
            )
        params["state"] = text
    return params or None


def snapshot_listing_params(*, include_unfinished: bool, allow_partial: bool) -> dict[str, str]:
    """The query on ``GET /snapshots``.

    ``include=unfinished`` widens the listing to deletions that began and did
    not finish. They are not restorable or clonable — their state reads
    ``deleting`` — but they still hold objects and still count against storage, so this is
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
    # check and put ``?expect=`` on the wire — and the platform reads an empty
    # expectation as NO expectation, so the interlock
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
    #
    # ``operation_id`` the same way (platform OPL-5055): on a create that would
    # not boot it is on the envelope, beside ``start_error``, not on the computer.
    out = {**inner, "start_error": data.get("start_error")}
    if "operation_id" in data and "operation_id" not in inner:
        out["operation_id"] = data["operation_id"]
    return out


def names_a_computer(payload: Mapping[str, Any]) -> bool:
    """Whether a flattened answer names a computer: a non-empty string ``id``."""
    ident = payload.get("id")
    return isinstance(ident, str) and bool(ident)


def computer_record(data: Any, method: str, path: str) -> dict[str, Any]:
    """The one computer a route promised, refused when the answer was not one.

    Wrapped unchecked, an empty or id-less answer becomes a handle whose
    :attr:`~mandala_computer.Computer.id` is ``""``, and everything that handle
    can do then fails somewhere else entirely — a ``ValueError`` out of a path
    builder that blames the caller and names neither the request that came back
    empty nor the route it came from. So the route is named here instead.
    """
    payload = computer_payload(data)
    if not names_a_computer(payload):
        raise MandalaError(f"expected a computer from {method} {path}")
    return payload


def workspace_move_record(
    response: httpx.Response, computer_id: str, workspace_id: str | None
) -> dict[str, Any]:
    """Validate the move's completed result before replacing a cached computer."""
    message = (
        f"expected the moved computer from PUT {computer_action(computer_id, 'workspace')}; "
        "the move's outcome is unconfirmed and this handle still has its previous state"
    )
    if response.status_code != 200:
        raise MandalaError(message)
    try:
        data = response.json()
    except (ValueError, UnicodeError, RecursionError):
        raise MandalaError(message) from None
    record = data.get("computer") if isinstance(data, Mapping) else None
    if (
        not isinstance(record, Mapping)
        or record.get("id") != computer_id
        or (
            record.get("workspace_id") not in (None, "")
            if workspace_id is None
            else record.get("workspace_id") != workspace_id
        )
    ):
        raise MandalaError(message)
    return dict(record)


def computer_records(rows: Sequence[Any], method: str, path: str) -> list[dict[str, Any]]:
    """:func:`computer_record` over a listing, refusing it WHOLE on one id-less row.

    Refused rather than dropped, as a windows listing is: a listing quietly one
    shorter is a complete-looking answer that is wrong.
    """
    out: list[dict[str, Any]] = []
    for position, row in enumerate(rows):
        payload = computer_payload(row)
        if not names_a_computer(payload):
            raise MandalaError(
                f"expected a computer from {method} {path} "
                f"(row {position} of {len(rows)} has no id)"
            )
        out.append(payload)
    return out


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
    template_transfer: str | None = None,
    secrets: object = None,
    browser_proxy: object = None,
    egress_proxy: object = None,
    workspace_id: object = None,
    ends_at: object = None,
    ends_in_hours: object = None,
) -> dict[str, Any]:
    """Build a create payload, omitting anything unset.

    Omission is meaningful: the server applies the template's defaults only when
    a key is absent, so sending explicit nulls would override them with nothing.

    A ``size`` names a template and a shape together, so combining it with any
    of the four it stands in for is refused here — the server refuses it too,
    but this mistake is knowable without the round trip, and the server's
    refusal exists for callers who are not this SDK.

    ``secrets`` goes through :func:`secret_bindings_body`, and no ``secrets``
    key is sent when it is ``None``. ``browser_proxy`` goes through
    :func:`browser_proxy_body` the same way, and ``egress_proxy`` through
    :func:`egress_proxy_body`. ``browser_proxy`` alone has an explicit "none":
    :data:`~mandala_computer.NO_BROWSER_PROXY` sends ``null``, which opts out
    of a template's default proxy that an omitted key inherits.

    ``workspace_id`` (platform OPL-5543) is the workspace to create in, checked
    as a secret's scope is; ``None`` sends no key, the credential's own scope.

    ``ends_at`` and ``ends_in_hours`` go through :func:`end_time_fields`, and
    neither key is sent when both are ``None``.
    """
    from ._models import NO_BROWSER_PROXY  # a cycle at import time; not at call time

    if size is not None and any(
        v is not None for v in (template, template_transfer, cpu, ram_mb, disk_gb)
    ):
        raise ValueError(
            "size already names a template and a shape; send size alone, "
            "or template/template_transfer/cpu/ram_mb/disk_gb without it"
        )
    name = _require_optional_name(name)
    # ``name`` came back canonical from the check above; these three never had
    # a check at all, and go on the wire beside it.
    size = None if size is None else canonical(size, "size")
    template = None if template is None else canonical(template, "template")
    template_transfer = (
        None if template_transfer is None else canonical(template_transfer, "template_transfer")
    )
    if template_transfer is not None:
        if not template_transfer.strip():
            raise ValueError("template_transfer must be a nonempty string")
        if template is None or not template.strip():
            raise ValueError("template_transfer requires the original nonempty template")
    resolution = None if resolution is None else canonical(resolution, "resolution")
    body: dict[str, Any] = {"start": flag(start, "start")}
    for key, text in (
        ("name", name),
        ("size", size),
        ("template", template),
        ("template_transfer", template_transfer),
        ("resolution", resolution),
    ):
        if text is not None:
            body[key] = text
    # Bools are ints, so ``cpu=True`` is true and ``json.dumps`` then encodes
    # it as a boolean. The platform wants a count, not JSON ``true``.
    for key, count in (("cpu", cpu), ("ram_mb", ram_mb), ("disk_gb", disk_gb)):
        if count is not None:
            body[key] = whole(count, key, exc=ValueError)
    if secrets is not None:
        body["secrets"] = secret_bindings_body(secrets, revisions=False)
    if browser_proxy is NO_BROWSER_PROXY:
        body["browser_proxy"] = None
    elif browser_proxy is not None:
        body["browser_proxy"] = browser_proxy_body(browser_proxy)
    if egress_proxy is not None:
        body["egress_proxy"] = egress_proxy_body(egress_proxy)
    workspace = _workspace(workspace_id)
    if workspace is not None:
        body["workspace_id"] = workspace
    body.update(end_time_fields(ends_at, ends_in_hours, "ends_at", "ends_in_hours"))
    return body


def _positive_hours(value: object, what: str) -> float:
    hours = real(value, what, message=f"{what} must be a number of hours, not {value!r}")
    if hours <= 0:
        raise ValueError(f"{what} must be more than 0, not {value!r}")
    return hours


def end_time_fields(at: object, hours: object, at_key: str, hours_key: str) -> dict[str, Any]:
    """A moment as either an absolute time or a number of hours from now — at
    most one of them, and nothing when neither was given.

    The platform turns hours into a time itself, so the two are one setting
    spelt two ways, and it refuses a request carrying both. So does this, before
    anything is sent. Whether the time is far enough ahead, and not too far, is
    the platform's to judge against its own clock: this one may be wrong.

    A time follows :func:`_usage_stamp`'s rules, for its reason: an aware
    ``datetime`` or an RFC 3339 string with a zone, never a naive datetime,
    whose zone this SDK would have to guess — and a guess moves the moment by
    hours.
    """
    if at is not None and hours is not None:
        raise ValueError(f"send {at_key} or {hours_key}, not both")
    if at is not None:
        return {at_key: _usage_stamp(at, at_key)}
    if hours is not None:
        return {hours_key: _positive_hours(hours, hours_key)}
    return {}


def end_time_body(ends_at: object, ends_in_hours: object) -> dict[str, Any]:
    """The PATCH that sets a computer's end time, or clears it.

    Neither given clears it: ``ends_at`` is sent as an explicit null, for
    :func:`idle_suspend_body`'s reason — dropped, it would mean "change
    nothing", the opposite request. The platform requires this to be the only
    field in the PATCH, which is why it has a method of its own.
    """
    return end_time_fields(ends_at, ends_in_hours, "ends_at", "ends_in_hours") or {"ends_at": None}


#: How much later than asked an answered moment may be and still count as the
#: one asked for: the platform stores times at its own precision, and may
#: round. Generous next to that, and nothing next to what it guards against.
_MOMENT_SLACK = timedelta(seconds=1)
#: For a duration, which the platform turns into a time on its own clock after
#: the request arrives: the trip there, and the two clocks disagreeing.
_HOURS_SLACK = timedelta(seconds=60)


def _moment(text: str) -> datetime:
    """A stamp :func:`_usage_stamp` let through, as an aware datetime."""
    return parse_rfc3339(text.upper(), fraction_digits=None)


def utcnow() -> datetime:
    """Now, aware. One spelling, so tests and callers bracket a request alike."""
    return datetime.now(timezone.utc)


#: Without the server's own clock to go by, a relative time is held to the
#: client's, widened by this much either way for a clock set wrong.
_NO_DATE_SLACK = timedelta(minutes=10)
#: A replay without a ``Date`` is held to the client's clock, which reads it a
#: little after the platform measured it: a few seconds more for that.
_REPLAY_NO_DATE_SLACK = timedelta(seconds=10)


@dataclass(frozen=True)
class Exchange:
    """One request, as far as judging a time it answered with needs.

    ``began`` and ``ended`` are the client's clock around it, ``seconds`` how
    long it took by a monotonic one, ``server_now`` the answer's ``Date``
    header — the platform's own clock — when it carried a readable one, and
    ``replayed`` whether the answer was the stored one of an earlier call.
    """

    began: datetime
    ended: datetime
    seconds: float
    server_now: datetime | None = None
    replayed: bool = False


def exchange_start() -> tuple[datetime, float]:
    """The moment a request goes out, by both clocks; :func:`exchange_end` takes it."""
    return utcnow(), time.monotonic()


def exchange_end(start: tuple[datetime, float], headers: Mapping[str, str]) -> Exchange:
    """The :class:`Exchange` a request made, from :func:`exchange_start` and the
    answer's headers."""
    began, mono = start
    server_now: datetime | None
    try:
        server_now = parsedate_to_datetime(headers.get("date", ""))
    except (TypeError, ValueError, IndexError):
        server_now = None
    if server_now is not None and server_now.tzinfo is None:
        server_now = server_now.replace(tzinfo=timezone.utc)
    ex = Exchange(
        began=began,
        ended=utcnow(),
        seconds=max(0.0, time.monotonic() - mono),
        server_now=server_now,
        replayed=is_replay(headers),
    )
    return ex


def _relative_window(hours: float, ex: Exchange) -> tuple[datetime, datetime]:
    """Where hours from now may land, as the platform turned them into a time.

    By the platform's clock when the answer says what it is: its ``Date``,
    give or take a minute, and earlier by however long the request took. The
    client's clock is not trusted for this — it may be minutes out — and is
    used only when there is no ``Date``, widened by :data:`_NO_DATE_SLACK`
    either way.

    A replay (``Idempotent-Replayed``) is the first call's answer, measured
    from whenever that call was sent, which this SDK does not know. So it is
    held to the upper bound alone — not later than hours from now — and no
    lower bound is established: a replayed relative time is only confirmed
    to be no later than asked.
    """
    span = timedelta(hours=hours)
    if ex.server_now is not None:
        low = ex.server_now + span - _HOURS_SLACK - timedelta(seconds=ex.seconds)
        high = ex.server_now + span + _HOURS_SLACK
    else:
        low = ex.began + span - _MOMENT_SLACK - _NO_DATE_SLACK
        high = ex.ended + span + _HOURS_SLACK + _NO_DATE_SLACK
    if ex.replayed:
        low = datetime.min.replace(tzinfo=timezone.utc)
        if ex.server_now is None:
            high += _REPLAY_NO_DATE_SLACK
    return low, high


def _end_time_window(body: Mapping[str, Any], ex: Exchange) -> tuple[datetime, datetime] | None:
    """Where an answered end time must fall to be the one asked for.

    A time is stored to the second, so it comes back as the request truncated,
    give or take one — exactly, whatever any clock says. Hours go through
    :func:`_relative_window`. ``None``: nothing to compare against.
    """
    at = body.get("ends_at")
    if isinstance(at, str):
        asked = _moment(at).replace(microsecond=0)
        return asked - _MOMENT_SLACK, asked + _MOMENT_SLACK
    hours = body.get("ends_in_hours")
    if hours is not None:
        return _relative_window(hours, ex)
    return None


def wants_end_time(body: Mapping[str, Any]) -> bool:
    """Whether a create or PATCH body asks for an end time."""
    return body.get("ends_at") is not None or body.get("ends_in_hours") is not None


def end_time_not_started(computer_id: str, status: str) -> MandalaError:
    """The error for a launch that found its end-timed computer not running."""
    return MandalaError(
        f"{computer_id} is {status}, and launch does not start a computer created with an "
        "end time: its end time may have passed and stopped it. Call start() if you still "
        "want it running"
    )


def is_replay(headers: Mapping[str, str]) -> bool:
    """Whether a keyed call's answer is the stored answer to an earlier call
    (``Idempotent-Replayed: true``) rather than one measured against this one."""
    return headers.get("idempotent-replayed", "").strip().lower() == "true"


#: How long a compensating call may take — the revoke of a key minted without
#: its limits. Its own budget, not the caller's: what it undoes is the very
#: thing it exists to prevent.
CLEANUP_TIMEOUT_SECONDS = 30.0


def end_time_refusal(
    computer_id: str,
    body: Mapping[str, Any],
    answered: object,
    ex: Exchange,
    *,
    launching: bool = False,
) -> MandalaError | None:
    """The error to raise when an answer does not confirm the end time asked for.

    ``answered`` is the ``ends_at`` the answer carried, unread; ``ex`` is the
    request that asked.

    A platform that predates end times ignores the fields it does not know and
    answers as if they were never sent, so a computer meant to stop on its own
    would run until somebody noticed. The answer is the only proof the setting
    took, so its absence — or a value that is not the time asked for, such as
    the one it had before — is a failure, never a deletion or a stop: the
    computer is the caller's, and what to do with it is theirs to decide.

    A create answered from its idempotency record is the first call's answer,
    and that computer's end time may since have come and gone — the host
    stopped it and cleared the setting. A launch must not then start it again:
    that would leave a running computer with no end time at all.
    """
    replayed = ex.replayed
    wanted = wants_end_time(body)
    then = "so launch did not start it" if launching else "nothing was deleted or stopped"
    if wanted and answered is None:
        if replayed:
            at = body.get("ends_at")
            passed = isinstance(at, str) and _moment(at) <= utcnow()
            why = (
                "its end time has passed: the host has stopped it and cleared the setting"
                if passed
                else "its end time has most likely passed, or was never applied"
            )
            return MandalaError(
                f"{computer_id}: this create was answered from an earlier request with the "
                f"same idempotency key, and the computer has no end time — {why}; {then}"
            )
        return MandalaError(
            f"{computer_id}: the end time was not applied — the server answered without "
            f"ends_at, so it may not support end times; {then}"
        )
    if wanted:
        try:
            got = _moment(answered) if isinstance(answered, str) else None
        except ValueError:
            got = None
        if got is None:
            return MandalaError(
                f"{computer_id}: the end time was not applied — the server answered an "
                f"unreadable ends_at {answered!r}; {then}"
            )
        window = _end_time_window(body, ex)
        if window is not None and not window[0] <= got <= window[1]:
            return MandalaError(
                f"{computer_id}: the end time was not applied — the server answers ends_at "
                f"{answered}, not the time asked for; {then}"
            )
        return None
    if "ends_at" in body and answered is not None:
        return MandalaError(
            f"{computer_id}: the end time was not cleared — the server still answers "
            f"ends_at {answered!r}"
        )
    return None


def key_limits_unapplied(
    body: Mapping[str, Any],
    expires_by: datetime | None,
    expires_at: str | None,
    computer_ids: Sequence[str] | None,
) -> str | None:
    """What a minted key's answer fails to confirm of the limits asked for, or ``None``.

    A platform that predates key limits ignores the fields it does not know and
    mints a key that never expires and reaches every computer — the opposite of
    what was asked, and a key is a credential. ``expires_by`` is the latest
    the expiry asked for may come back as.
    """
    problems = []
    if expires_by is not None:
        try:
            at = None if expires_at is None else _moment(expires_at)
        except ValueError:
            at = None
        if at is None:
            problems.append("it has no expiry")
        elif at > expires_by:
            problems.append(f"it expires at {expires_at}, later than asked")
    asked = body.get("computer_ids")
    if asked is not None:
        if computer_ids is None:
            problems.append("it is not confined to the computers named")
        elif not set(computer_ids) <= set(asked):
            problems.append("it reaches computers that were not named")
    return "; ".join(problems) or None


def minted_key(
    body: Mapping[str, Any], data: Mapping[str, Any], ex: Exchange
) -> tuple[ApiKeyCreated | None, str | None, str | None]:
    """A mint's answer read against what was asked: ``(key, id, problem)``.

    With no problem, ``key`` is the key to hand back. With one, ``key`` is
    ``None`` — a key minted without the limits asked for is never returned —
    and ``id`` is what to revoke, when the answer named one.
    """
    from ._models import ApiKeyCreated  # a cycle at import time; not at call time

    expires_by: datetime | None = None
    if isinstance(body.get("expires_at"), str):
        expires_by = _moment(body["expires_at"]) + _MOMENT_SLACK
    elif body.get("expires_in_hours") is not None:
        expires_by = _relative_window(body["expires_in_hours"], ex)[1]
    limited = expires_by is not None or body.get("computer_ids") is not None
    if not limited:
        return ApiKeyCreated.from_api(data), None, None
    try:
        created = ApiKeyCreated.from_api(data)
    except MandalaError as e:
        raw_id = data.get("id") if isinstance(data, Mapping) else None
        key_id = raw_id if isinstance(raw_id, str) and raw_id.startswith("key-") else None
        return None, key_id, f"its answer could not be read: {e}"
    problem = key_limits_unapplied(body, expires_by, created.expires_at, created.computer_ids)
    if problem is not None:
        return None, created.id, problem
    return created, None, None


def key_limits_refusal(key_id: str | None, problem: str, revoked: bool | None) -> MandalaError:
    """The error for a key minted without the limits asked for. Never the key itself."""
    if revoked:
        fate = f"the new key {key_id} has been revoked"
    elif key_id is None:
        fate = "the answer did not name the new key, so it could not be revoked: list your keys"
    else:
        fate = f"revoking the new key {key_id} FAILED — revoke it with api_keys.revoke({key_id!r})"
    return MandalaError(
        f"POST api-keys: the server did not apply the requested limits ({problem}); "
        f"it may not support them. {fate}"
    )


_BROWSER_PROXY_KEYS = frozenset({"server", "bypass", "credentials_secret_id"})

#: A secret's id: ``csec-`` and sixteen lowercase hex characters.
_SECRET_ID = re.compile(r"csec-[0-9a-f]{16}")


def browser_proxy_body(proxy: object, what: str = "browser_proxy") -> dict[str, Any]:
    """A browser proxy as the wire takes it, checked for shape only.

    A :class:`~mandala_computer.BrowserProxyArgs` mapping, or a
    :class:`~mandala_computer.BrowserProxy` read off another computer. The
    shape is this client's to know; the rules on the values are not. Which
    schemes a proxy may use, which hosts it may name and how many bypass
    entries there may be are the platform's, and they are growing, so they are
    left to its 400, which names the rule that was broken. A copy here would
    refuse a value the platform has since learned to accept. The credentials
    id is checked here because its form is an id's, not a rule that grows.

    A key this does not know is refused rather than dropped, as a secret
    binding's is: a misspelt ``bypass`` left out would send the browsers
    through the proxy for every host.
    """
    from ._models import BrowserProxy  # a cycle at import time; not at call time

    if isinstance(proxy, BrowserProxy):
        proxy = {
            "server": proxy.server,
            "bypass": list(proxy.bypass),
            "credentials_secret_id": proxy.credentials_secret_id,
        }
    if not isinstance(proxy, Mapping):
        raise ValueError(  # noqa: TRY004 — one exception type for one class of mistake
            f"{what} must be a mapping {{server, bypass, credentials_secret_id}}, "
            f"not {type(proxy).__name__}"
        )
    unknown = sorted(str(k) for k in proxy if k not in _BROWSER_PROXY_KEYS)
    if unknown:
        raise ValueError(f"{what} has unknown keys {unknown}")
    server = canonical(proxy.get("server"), f"{what}.server")
    if not server.strip():
        raise ValueError(f"{what}.server must not be empty")
    body: dict[str, Any] = {"server": server}
    bypass = proxy.get("bypass")
    if bypass is not None:
        # A bare string is a sequence of characters, and would go out as one
        # bypass entry per letter.
        if isinstance(bypass, (str, bytes, Mapping)) or not isinstance(bypass, (list, tuple)):
            raise ValueError(f"{what}.bypass must be a list of hosts, not {type(bypass).__name__}")
        entries = []
        for i, entry in enumerate(bypass):
            text = canonical(entry, f"{what}.bypass[{i}]")
            if not text.strip():
                raise ValueError(f"{what}.bypass[{i}] must not be empty")
            entries.append(text)
        body["bypass"] = entries
    creds = proxy.get("credentials_secret_id")
    if creds is not None:
        creds = canonical(creds, f"{what}.credentials_secret_id")
        if not _SECRET_ID.fullmatch(creds):
            raise ValueError(
                f"{what}.credentials_secret_id must be a secret's id: "
                "csec- and sixteen hex characters"
            )
        body["credentials_secret_id"] = creds
    return body


def browser_proxy_update_body(proxy: object) -> dict[str, Any]:
    """The PATCH that replaces a computer's browser proxy, or removes it.

    ``None`` is sent, not omitted, for :func:`idle_suspend_body`'s reason: an
    explicit null is how the setting is removed. The platform requires this to
    be the only field in the request, which is why it has a method of its own.
    """
    return {"browser_proxy": None if proxy is None else browser_proxy_body(proxy)}


_EGRESS_PROXY_KEYS = frozenset({"server", "credentials_secret_id"})


def egress_proxy_body(proxy: object, what: str = "egress_proxy") -> dict[str, Any]:
    """An egress proxy as the wire takes it, checked for shape only.

    A :class:`~mandala_computer.EgressProxyArgs` mapping, or a
    :class:`~mandala_computer.EgressProxy` read off another computer. The rules
    on the server are the platform's, as :func:`browser_proxy_body` leaves
    them. A key this does not know is refused rather than dropped: ``bypass``
    copied over from a browser proxy has no meaning here, and saying so is
    better than a proxy that silently covers the hosts its caller meant to
    leave out.
    """
    from ._models import EgressProxy  # a cycle at import time; not at call time

    if isinstance(proxy, EgressProxy):
        proxy = {"server": proxy.server, "credentials_secret_id": proxy.credentials_secret_id}
    if not isinstance(proxy, Mapping):
        raise ValueError(  # noqa: TRY004 — one exception type for one class of mistake
            f"{what} must be a mapping {{server, credentials_secret_id}}, "
            f"not {type(proxy).__name__}"
        )
    if "bypass" in proxy:
        raise ValueError(
            f"{what} has no bypass list: every connection the computer opens goes through the proxy"
        )
    unknown = sorted(str(k) for k in proxy if k not in _EGRESS_PROXY_KEYS)
    if unknown:
        raise ValueError(f"{what} has unknown keys {unknown}")
    server = canonical(proxy.get("server"), f"{what}.server")
    if not server.strip():
        raise ValueError(f"{what}.server must not be empty")
    body: dict[str, Any] = {"server": server}
    creds = proxy.get("credentials_secret_id")
    if creds is not None:
        creds = canonical(creds, f"{what}.credentials_secret_id")
        if not _SECRET_ID.fullmatch(creds):
            raise ValueError(
                f"{what}.credentials_secret_id must be a secret's id: "
                "csec- and sixteen hex characters"
            )
        body["credentials_secret_id"] = creds
    return body


def egress_proxy_update_body(proxy: object) -> dict[str, Any]:
    """The PATCH that replaces a computer's egress proxy, or removes it.

    ``None`` is sent, not omitted: an explicit null is how the setting is
    removed. The platform requires this to be the only field in the request,
    which is why it has a method of its own.
    """
    return {"egress_proxy": None if proxy is None else egress_proxy_body(proxy)}


def snapshot_clone_body(
    name: str | None, memory: bool | None = None, inherit_secrets: bool = False
) -> dict[str, Any]:
    """A snapshot clone's body (platform OPL-4964).

    The name as :func:`name_body` has it, and the two memory options only when
    set, so an ordinary clone sends exactly what it always did. ``memory`` is
    ``None`` for the platform's default (resume a memory snapshot's session);
    ``inherit_secrets`` is sent only when true, since false is the default.
    """
    body = name_body(name)
    if memory is not None:
        body["memory"] = flag(memory, "memory")
    if flag(inherit_secrets, "inherit_secrets"):
        body["inherit_secrets"] = True
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


#: Foreground wait ceiling, mirrored from the platform's own bound and checked
#: by ``scripts/check_surface.py``.
MAX_EXEC_TIMEOUT_SECONDS = 600


def exec_body(
    command: str,
    timeout: int,
    desktop: bool = False,
    *,
    background: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    retain_output: object = False,
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
        message = (
            "timeout must be positive whole seconds from 1 to "
            f"{MAX_EXEC_TIMEOUT_SECONDS} for a foreground exec"
        )
        seconds = (
            whole(timeout, "timeout", exc=ValueError, message=message)
            if isinstance(timeout, int)
            else real(timeout, "timeout", message=message)
        )
        if not 0 < seconds <= MAX_EXEC_TIMEOUT_SECONDS or int(seconds) != seconds:
            raise ValueError(message)
        body["timeout_s"] = int(seconds)
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
    retention = retain_output_option(retain_output)
    if retention is not False:
        if background:
            raise ValueError("retain_output is only available for synchronous exec")
        body["retain_output"] = retention
    return body


#: The most environment entries an exec may carry, and the longest one entry
#: may be. Both are the platform's own bounds, and a value past either is a
#: request the guest agent would refuse after the round trip.
MAX_ENV_ENTRIES = 64
MAX_ENV_ENTRY_BYTES = 4096


def _env_object(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of ``env`` whose names and values the guest can actually use.

    JSON will carry a number, a null, an empty name. The guest agent will not:
    it turns this into a ``KEY=value`` list, so a non-string becomes a JSON type
    the agent never asked for, an empty name or a ``=`` inside one splits the
    entry at the wrong place, and a NUL is dropped rather than refused. Both
    produce a command that runs with an environment nobody asked for and reports
    success. Mirrored from the platform's own rule, for the same reason
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


#: The browsers :func:`open_url_command` looks for, in the order it prefers them.
#: The images do not agree on one: the Debian-based ones ship ``firefox-esr``
#: (and Chromium), and the Omarchy one ships Chromium and no Firefox at all.
OPEN_URL_BROWSERS = ("firefox-esr", "firefox", "chromium")

#: What the guest prints, and exits 127 with, when none of them is installed.
OPEN_URL_NO_BROWSER = "no browser (firefox-esr, firefox or chromium) on this image"


def open_url_command(url: str) -> str:
    """Build the shell command that puts ``url`` on the guest's screen.

    The browser is chosen from what the image has installed rather than asked
    of ``xdg-open`` or one of the other portable wrappers: ``firefox-esr``,
    then ``firefox``, then ``chromium``, the first one ``command -v`` finds.
    One name cannot be hard-coded, because the images do not share one — the
    Omarchy image has Chromium and no Firefox, and a fixed ``firefox`` there
    opened nothing while the call reported success (OPL-3705). Keeping the
    choice here still means a change of image is a change in one place rather
    than in every caller's prompt.

    The lookup runs in the foreground, so an image with none of the three exits
    127 with :data:`OPEN_URL_NO_BROWSER` on stderr, which the caller can see.
    Only the launch is detached, because a browser does not exit on its own: in
    the foreground the call would block until the timeout killed it and come
    back as a failure, having opened the window anyway. The cost of detaching
    is that a browser which starts and then dies is not reported.
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
    lookup = " || ".join(f"command -v {name}" for name in OPEN_URL_BROWSERS)
    return (
        f"b=$({lookup}) || {{ echo {shlex.quote(OPEN_URL_NO_BROWSER)} >&2; exit 127; }}; "
        f'nohup "$b" {shlex.quote(url)} >/dev/null 2>&1 &'
    )


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
        for key, value in (("x", x), ("y", y)):
            point = _coordinate(value, key, exc=ValueError)
            if not WINDOW_POSITION_MIN <= point <= WINDOW_GEOMETRY_MAX:
                raise ValueError(
                    f"{key} must be from {WINDOW_POSITION_MIN} to {WINDOW_GEOMETRY_MAX}, "
                    f"not {point}"
                )
            body[key] = point
    if width is not None and height is not None:
        for key, value in (("width", width), ("height", height)):
            size = whole(value, key, exc=ValueError)
            if not 1 <= size <= WINDOW_GEOMETRY_MAX:
                raise ValueError(f"{key} must be from 1 to {WINDOW_GEOMETRY_MAX}, not {size}")
            body[key] = size
    return body


#: The range a window's position and size take (OPL-5974). X11 coordinates are
#: signed 16-bit, so the platform refuses a move outside -32768 to 32767 and a
#: size outside 1 to 32767; refused here first, as the TypeScript SDK does.
WINDOW_POSITION_MIN = -32768
WINDOW_GEOMETRY_MAX = 32767


#: The most text ``PUT /computers/{id}/clipboard`` carries INTO a guest, in bytes.
#:
#: Mirrored rather than left to the server, so a request that can only fail is
#: not made, and kept in step by ``scripts/check_surface.py`` like
#: :data:`MAX_STEPS`. The platform states this one on the host side rather than
#: in the API reference, which the checker refused to read at first; the
#: docstring here said "NOT machine-checked" instead, which is an admission
#: rather than a check, so the checker learned to read it and the sentence
#: became true.
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

    Empty text clears the selection — the only way to say it, and the platform
    reads a present ``text`` field holding an empty string as exactly that.

    A NUL is refused, and it is the one worth explaining: the platform confirms
    a write by reading the selection back through a command substitution, and a
    shell truncates that at the first NUL — so the write would land, the
    read-back would disagree, and the answer would be a 409 inviting a retry at
    something that had already worked.

    The cap is counted in UTF-8 BYTES rather than characters. An emoji is four of
    them, so a ``len(text)`` check would pass four times the legal payload to an
    ``execve`` that answers E2BIG.
    """
    # `canonical` rather than an isinstance check, for the reason it documents:
    # every check below reads `text`, and the request then encodes the ORIGINAL
    # object again. A `str` subclass can answer differently the second time — an
    # override that reports a clear here and a full buffer to the serialiser
    # walks past every refusal below, and now sends a write where the checks saw
    # nothing. It also makes this a ValueError like every other refusal in this
    # file, rather than the one TypeError among them.
    text = canonical(text, "clipboard text")
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


def check_schedule_args(
    *, enabled: bool, hour: int | None, minute: int | None, tz: str | None
) -> None:
    """:func:`schedule_body`'s checks on the parts a caller gave, for
    ``set_schedule`` to make BEFORE it reads the current window for the rest:
    a bad value is refused without any request, as it always was."""
    schedule_body(
        enabled=enabled,
        hour=4 if hour is None else hour,
        minute=0 if minute is None else minute,
        tz="UTC" if tz is None else tz,
    )


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


def _modifier_text(modifiers: tuple[str, ...]) -> str | None:
    """The held keys as the ``text`` a click, drag or scroll sends, or None.

    A single string is refused rather than joined. ``"+".join("shift")`` is
    ``"s+h+i+f+t"``, which holds five letter keys instead of Shift; a single
    letter is a valid key name, so nothing downstream reports it. The keyword
    forms (``drag(..., modifiers=...)``, ``scroll(..., modifiers=...)``) are
    where a bare string is easy to write.
    """
    if isinstance(modifiers, str):
        raise ValueError(  # noqa: TRY004
            "modifiers must be a sequence of key names, e.g. "
            f'("shift",), not the string {modifiers!r}'
        )
    return "+".join(modifiers) if modifiers else None


#: The most presses one click action makes (``count``), as the platform bounds it.
MAX_CLICK_COUNT = 10


def click_body(
    action: str,
    x: int | None,
    y: int | None,
    modifiers: tuple[str, ...],
    count: int | None = None,
) -> dict[str, Any]:
    """A click, optionally at a point and optionally with keys held down.

    No coordinate means "where the pointer already is", which is a real and
    different request from clicking (0, 0) — so the keys are omitted rather than
    sent as zeros.

    ``count`` repeats a ``left_click``, ``right_click`` or ``middle_click``, 1 to
    :data:`MAX_CLICK_COUNT` times, and is refused here where the platform would
    refuse it: on the two clicks named by their count, and out of range.
    """
    text = _modifier_text(modifiers)
    _whole_point(x, y)
    body: dict[str, Any] = {"action": action}
    if x is not None and y is not None:
        body["x"] = x
        body["y"] = y
    if text is not None:
        body["text"] = text
    if count is not None:
        if action not in ("left_click", "right_click", "middle_click"):
            raise ValueError(f"{action} takes no count; it is a fixed number of clicks by name")
        # bool is an int; True is not a count.
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= MAX_CLICK_COUNT
        ):
            raise ValueError(
                f"count must be a whole number from 1 to {MAX_CLICK_COUNT}, not {count!r}"
            )
        body["count"] = count
    return body


def input_params(context: bool) -> dict[str, str] | None:
    """``context=1`` to have an input answer carry the desktop's windows after it."""
    return {"context": "1"} if flag(context, "context") else None


#: The most actions one OpenAI input batch carries.
INPUT_BATCH_MAX = 32
#: The fields each OpenAI action reads beside ``type``: the platform's own table.
_OPENAI_FIELDS: dict[str, tuple[str, ...]] = {
    "click": ("x", "y", "button", "keys"),
    "double_click": ("x", "y", "keys"),
    "move": ("x", "y", "keys"),
    "drag": ("path", "keys"),
    "scroll": ("x", "y", "scroll_x", "scroll_y", "keys"),
    "keypress": ("keys",),
    "type": ("text",),
    "wait": (),
    "screenshot": (),
}
_OPENAI_BUTTONS = ("left", "right", "wheel", "back", "forward")


def _openai_action(item: object, at: str) -> Mapping[str, Any]:
    """One action as a mapping: as given, or an ``openai`` package action
    object's own fields.

    The ``openai`` package hands a ``computer_call``'s actions over as model
    objects, not dicts, and they are taken as they are: ``model_dump`` with
    ``exclude_none``, because an unset optional field (``keys`` on a click) is
    ``None`` there and absent on the wire. What that leaves is checked exactly
    as a dict is, unknown fields included.
    """
    if isinstance(item, Mapping):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        fields = dump(exclude_none=True)
        if isinstance(fields, Mapping):
            return fields
    raise ValueError(f"{at} must be an object, not {type(item).__name__}")


def input_batch_body(actions: object) -> list[dict[str, Any]]:
    """The body of an OpenAI batch: the actions as a JSON array, each checked
    before anything is sent.

    Checked for SHAPE — a known ``type``, the fields that type needs with the
    right types, and no field it does not read — rather than for the
    platform's budgets (typed characters, wheel notches, drag points and waits
    summed over the batch), which it refuses itself before anything runs. A
    field the type does not read is refused rather than dropped, as the
    platform refuses it, so no part of a model's action silently disappears on
    the way. ``keys: None`` is OpenAI's "no keys" and is left off the wire. The
    TypeScript SDK's ``inputBatchBody`` draws the same lines.
    """
    if isinstance(actions, (str, bytes, Mapping)) or not isinstance(actions, Sequence):
        raise ValueError(  # noqa: TRY004 — one exception type for one class of mistake
            f"actions must be a list of OpenAI actions, not {type(actions).__name__}"
        )
    if not 1 <= len(actions) <= INPUT_BATCH_MAX:
        raise ValueError(f"a batch holds 1 to {INPUT_BATCH_MAX} actions, not {len(actions)}")
    body: list[dict[str, Any]] = []
    for i, item in enumerate(actions):
        at = f"actions[{i}]"
        action = _openai_action(item, at)
        kind = action.get("type")
        reads = _OPENAI_FIELDS.get(kind) if isinstance(kind, str) else None
        if reads is None:
            raise ValueError(f"{at}.type must be one of {', '.join(_OPENAI_FIELDS)}, not {kind!r}")
        for key in action:
            if key != "type" and key not in reads:
                raise ValueError(f"{at}: OpenAI {kind} does not take {key!r}")
        out: dict[str, Any] = {"type": kind}
        if "x" in reads:
            out["x"] = real(action.get("x"), f"{at}.x")
            out["y"] = real(action.get("y"), f"{at}.y")
        if kind == "click":
            button = action.get("button")
            if button not in _OPENAI_BUTTONS:
                raise ValueError(f"{at}.button must be one of {', '.join(_OPENAI_BUTTONS)}")
            out["button"] = button
        if kind == "drag":
            path = action.get("path")
            if isinstance(path, (str, bytes)) or not isinstance(path, Sequence) or len(path) < 2:
                raise ValueError(f"{at}.path must be at least two {{x, y}} points")
            points = []
            for j, point in enumerate(path):
                if not isinstance(point, Mapping) or set(point) != {"x", "y"}:
                    raise ValueError(f"{at}.path[{j}] must be an {{x, y}} point")
                points.append(
                    {
                        "x": real(point["x"], f"{at}.path[{j}].x"),
                        "y": real(point["y"], f"{at}.path[{j}].y"),
                    }
                )
            out["path"] = points
        if kind == "scroll":
            out["scroll_x"] = real(action.get("scroll_x"), f"{at}.scroll_x")
            out["scroll_y"] = real(action.get("scroll_y"), f"{at}.scroll_y")
        if kind == "type":
            text = action.get("text")
            if not isinstance(text, str) or not text:
                raise ValueError(f"{at}.text must be a nonempty string")
            out["text"] = canonical(text, f"{at}.text")
        if "keys" in reads:
            keys = action.get("keys")
            if kind == "keypress" or keys is not None:
                if (
                    isinstance(keys, (str, bytes))
                    or not isinstance(keys, Sequence)
                    or not all(isinstance(k, str) for k in keys)
                ):
                    raise ValueError(f"{at}.keys must be a list of key names")
                if kind == "keypress" and not keys:
                    raise ValueError(f"{at}.keys must name at least one key")
                out["keys"] = [canonical(k, f"{at}.keys") for k in keys]
        body.append(out)
    return body


def drag_body(
    from_x: int | None,
    from_y: int | None,
    to_x: int,
    to_y: int,
    modifiers: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A press, a move, and a release — one gesture, not two clicks, optionally
    with keys held down for all of it (as a click and a scroll send them).

    ``start_coordinate`` is omitted when the caller did not give one, which asks
    the platform to drag from wherever the pointer is. It refuses that if nothing
    has moved the pointer yet, rather than guessing at an origin.

    Half an origin is refused here rather than dropped. ``drag(90, 80,
    from_x=10)`` reads as a caller who meant to name a starting point, and
    silently ignoring the half they gave produces a drag that succeeds while
    selecting a different region — the worst shape a mistake can take, because
    nothing reports it.
    """
    text = _modifier_text(modifiers)
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
    if text is not None:
        body["text"] = text
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

#: The most wheel clicks one scroll takes, and the longest ``wait`` or held key,
#: in seconds. The platform's own ceilings, refused here before a request.
SCROLL_MAX_AMOUNT = 50
INPUT_MAX_SECONDS = 30


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
    text = _modifier_text(modifiers)
    direction = canonical(direction, "direction")
    if direction not in _SCROLL_DIRECTIONS:
        raise ValueError(f"direction must be one of {_SCROLL_DIRECTIONS}")
    amount = whole(amount, "amount", exc=ValueError, message="amount must be positive")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if amount > SCROLL_MAX_AMOUNT:
        raise ValueError(
            f"the platform caps a scroll at {SCROLL_MAX_AMOUNT} clicks; "
            "call scroll() again for more"
        )
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
    if text is not None:
        body["text"] = text
    return body


def type_body(text: str) -> dict[str, Any]:
    return {"action": "type", "text": canonical(text, "text")}


#: The most characters ``type`` takes in one request, and the most UTF-8
#: bytes ``paste`` does.
TYPE_MAX_CHARS = 400
PASTE_MAX_BYTES = 8192


def paste_body(text: str, shift: object) -> dict[str, Any]:
    """``{"action": "paste"}``: write the desktop clipboard, then press Ctrl+V.

    ``shift`` presses Ctrl+Shift+V instead, which terminals want. Sent as
    ``keys``, the chord as separate keys, like every other chord this SDK sends;
    without it the platform's default, Ctrl+V, is left to apply.
    """
    value = canonical(text, "text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("text must be valid Unicode") from None
    if not 1 <= size <= PASTE_MAX_BYTES:
        raise ValueError(f"paste text must be 1 to {PASTE_MAX_BYTES} bytes of UTF-8, not {size}")
    if "\x00" in value:
        raise ValueError("paste text must not contain NUL")
    body: dict[str, Any] = {"action": "paste", "text": value}
    if flag(shift, "shift"):
        body["keys"] = ["ctrl", "shift", "v"]
    return body


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
    if seconds > INPUT_MAX_SECONDS:
        raise ValueError(
            f"the platform caps a held key at {INPUT_MAX_SECONDS} seconds; "
            "call hold_key() again for longer"
        )
    return {
        "action": "hold_key",
        "keys": [canonical(key, "key") for key in keys],
        "duration": seconds,
    }


def wait_body(seconds: float) -> dict[str, Any]:
    seconds = _positive_seconds(seconds)
    if seconds > INPUT_MAX_SECONDS:
        raise ValueError(
            f"the platform caps a wait at {INPUT_MAX_SECONDS} seconds; call wait() again for longer"
        )
    return {"action": "wait", "duration": seconds}


def cursor_body() -> dict[str, Any]:
    return {"action": "cursor_position"}


def screenshot_params(
    width: int | None,
    fresh: bool = False,
    *,
    region: Sequence[int] | None = None,
    scale: float | None = None,
    format: str | None = None,
    quality: int | None = None,
    capture: str | None = None,
) -> dict[str, Any] | None:
    """``w`` downscales, ``fresh`` skips the cache; the rest shape the picture.

    A bare screenshot may be served from a frame up to 1.5 seconds old, which is
    right for a thumbnail and wrong for a loop: a model shown the frame from
    before its own click concludes the click missed and clicks again, and the
    second one lands on whatever the first one opened. ``fresh`` is therefore
    not an optimisation to reach for when a screenshot looks stale — it is what
    every screenshot feeding a decision wants, and the cost of it is one capture.

    Sent as ``1`` rather than ``true``: the platform documents the parameter as
    that single value and matches on it.

    ``region``, ``scale``, ``format`` and ``quality`` are :func:`screenshot_shape`'s,
    and absent from the query when not given, so every call that predates them
    builds the URL it always did. So is ``capture``, :func:`screenshot_capture`'s.
    """
    params: dict[str, Any] = {}
    if width is not None:
        width = whole(width, "width", exc=ValueError, message="width must be positive")
        if width <= 0:
            raise ValueError("width must be positive")
        params["w"] = width
    fresh = flag(fresh, "fresh")
    if fresh:
        params["fresh"] = 1
    if capture is not None:
        params["capture"] = screenshot_capture(capture, fresh)
    params.update(
        screenshot_shape(width, region=region, scale=scale, format=format, quality=quality)
    )
    return params or None


#: What a capture's name looks like: the platform names each capture with
#: sixteen lowercase hex digits, in a screenshot's ``X-GC-Capture`` header.
CAPTURE_NAME = re.compile(r"[0-9a-f]{16}")


def screenshot_capture(capture: object, fresh: bool) -> str:
    """``capture``, checked: the name of a capture an earlier screenshot carried.

    The platform answers from that capture and no other, so a crop worked out
    on one answer is cut from the same pixels by the next. A name it no longer
    holds is its ``409`` with ``reason`` ``"stale_capture"``; a malformed one
    would be its ``400``, and is refused here instead, since the shape is all
    there is to check.

    Not beside ``fresh``, which asks for a capture taken after the request: a
    named capture is one already taken, and the platform refuses the pair
    rather than picking one.
    """
    if not isinstance(capture, str) or not CAPTURE_NAME.fullmatch(capture):
        raise ValueError(
            f"capture must be the name in a screenshot's X-GC-Capture header, not {capture!r}"
        )
    if fresh:
        raise ValueError(
            "give fresh or capture, not both: capture answers from a screenshot already "
            "taken, and fresh asks for a new one"
        )
    return capture


#: A capture's size as the platform sends it, ``WIDTHxHEIGHT``.
CAPTURE_SIZE = re.compile(r"([0-9]{1,6})x([0-9]{1,6})")


def capture_size(value: str | None) -> tuple[int, int] | None:
    """A capture's ``X-GC-Capture-Size``, or ``None`` for one that is absent or
    is not ``WIDTHxHEIGHT`` with both above zero. Strict, because the size is
    what a crop of that capture is worked out in: a size read wrongly is a crop
    of the wrong pixels."""
    match = CAPTURE_SIZE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    return (width, height) if width > 0 and height > 0 else None


def capture_name(value: str | None) -> str | None:
    """A capture's ``X-GC-Capture``, or ``None`` for one that is absent or not a
    name the platform gives."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if CAPTURE_NAME.fullmatch(value) else None


#: The encodings a screenshot can be asked for. ``jpg`` is the platform's other
#: spelling of ``jpeg``.
SCREENSHOT_FORMATS = ("png", "jpeg", "jpg")


def screenshot_shape(
    width: int | None,
    *,
    region: Sequence[int] | None = None,
    scale: float | None = None,
    format: str | None = None,
    quality: int | None = None,
) -> dict[str, Any]:
    """The crop, scale and encoding a screenshot can be shaped into.

    Applied by the platform in that order to the same capture a bare call would
    return. Only what is cheap and cannot be wrong is checked here: the shape of
    each value, a scale beside a width, and a quality on a picture that will be
    a PNG. Whether a region fits the screen is the platform's to say, since it
    is the one that knows the screen, and it says so with the size in the
    message.

    The answer is a PNG when ``format`` says ``png``, or when it says nothing
    and no width was given; the platform refuses a quality on one with a 400,
    and so does this, before anything is sent.
    """
    params: dict[str, Any] = {}
    if format is not None:
        if not isinstance(format, str) or format not in SCREENSHOT_FORMATS:
            raise ValueError(
                f"format must be one of {', '.join(SCREENSHOT_FORMATS)}, not {format!r}"
            )
        params["format"] = format
    if scale is not None:
        # The same question as a width, answered twice. The platform refuses the
        # pair rather than picking one, and so does this.
        if width is not None:
            raise ValueError("give a width or a scale, not both: each sets the size of the picture")
        factor = real(
            scale,
            "scale",
            message=f"scale must be a number greater than 0 and at most 1, not {scale!r}",
        )
        if not 0 < factor <= 1:
            raise ValueError(
                f"scale must be a number greater than 0 and at most 1, not {scale!r}; "
                "a screenshot is only ever made smaller"
            )
        params["scale"] = factor
    if region is not None:
        params["region"] = screenshot_region(region)
    if quality is not None:
        q = whole(
            quality,
            "quality",
            exc=ValueError,
            message=f"quality must be a whole number from 1 to 100, not {quality!r}",
        )
        if not 1 <= q <= 100:
            raise ValueError(f"quality must be a whole number from 1 to 100, not {quality!r}")
        if format == "png" or (format is None and width is None):
            raise ValueError(
                "quality applies to a JPEG only; pass format='jpeg' with it"
                + (" in place of png" if format == "png" else "")
            )
        params["quality"] = q
    return params


def screenshot_region(region: Sequence[int]) -> str:
    """``(x, y, width, height)`` as the wire spells it, ``x,y,width,height``.

    In the screen pixels :attr:`resolution` reports, before any scaling. Whole
    numbers, and the size at least 1: the platform reads each with an integer
    parser, and a fraction — the natural result of dividing a coordinate by a
    scale — would be a 400 that could have been named here.
    """
    if isinstance(region, (str, bytes)) or not isinstance(region, Sequence) or len(region) != 4:
        raise ValueError(f"region must be (x, y, width, height), not {region!r}")
    parts = []
    for name, value, least in zip(("x", "y", "width", "height"), region, (0, 0, 1, 1)):
        message = f"region {name} must be a whole number of at least {least}, not {value!r}"
        n = whole(value, f"region {name}", exc=ValueError, message=message)
        if n < least:
            raise ValueError(message)
        parts.append(str(n))
    return ",".join(parts)


#: The platform's ceiling on ``max_steps``, mirrored.
#:
#: Kept in step by ``scripts/check_surface.py`` — a mirror nobody compares is a
#: comment, and one that drifts refuses a run the platform would have taken.
#:
#: Capped rather than obeyed for the reason the platform gives: every step drives
#: the desktop and most of them cost a model call and a screenshot on the
#: caller's own key, so a ``max_steps`` of ten thousand is a request to spend
#: their money for an hour on a task that has plainly gone wrong.
#:
#: MOST, not every, and the distinction is the platform's rather than ours: a
#: step is one ACTION, ``steps`` is incremented per tool call, and one model
#: reply may ask for several. A paused turn is resubmitted and costs tokens and
#: no step; a ``bash`` call and a cursor read take no screenshot. So this bounds
#: the work and not the bill, and the README says so where a caller sizing a run
#: will read it.
MAX_STEPS = 100
#: Background runs accept more actions; foreground runs retain their cap.
MAX_BACKGROUND_STEPS = 500
#: The range a funded run's ``max_cost_usd`` must fall in, in dollars. The
#: platform's default, when it is left out, is $2.
MIN_MAX_COST_USD = 0.25
MAX_MAX_COST_USD = 20


#: An agent thread's id: ``thr_`` and twenty-four lower-case hex characters, as
#: the platform mints them and the only shape it will find.
THREAD_ID = re.compile(r"thr_[0-9a-f]{24}")
#: The roles a turn of a caller's own ``messages`` may have.
AGENT_MESSAGE_ROLES = ("user", "assistant")


def agent_thread_id(value: object) -> str:
    """A ``thread_id`` to continue, checked against the platform's own shape.

    Refused here rather than sent, because a run that names a thread the
    platform cannot find is answered only after it has been admitted, and its
    rate cost is spent by then. The likeliest way to get one wrong is to hand
    over a background run's own ``id`` (``run_…``) where its ``thread_id`` was
    meant, and that deserves a sentence rather than a 404.
    """
    text = canonical(value, "thread_id")
    if not THREAD_ID.fullmatch(text):
        raise ValueError(
            "thread_id must be thr_ followed by 24 lowercase hex characters, the "
            f"thread_id of an earlier run, not {text!r}"
        )
    return text


def agent_messages(messages: object) -> list[dict[str, str]]:
    """A caller's own conversation so far, as the platform reads it.

    A list of ``{"role": "user" | "assistant", "content": str}``, oldest first —
    exactly what the platform accepts, and anything else is the 400 it would
    answer, refused here instead. Only ``role`` and ``content`` are sent: they
    are what the platform reads, and a copy built from them is a copy that
    cannot change after it was checked.

    A ``str`` is refused rather than iterated, for the reason :func:`_ids`
    gives; so is anything else that is not a list or a tuple, since a
    conversation is an order of turns.
    """
    if not isinstance(messages, (list, tuple)):
        raise ValueError(  # noqa: TRY004 — one exception type for one class of mistake
            "messages must be a list of {'role': 'user' | 'assistant', 'content': str}, "
            f"not {type(messages).__name__}"
        )
    out: list[dict[str, str]] = []
    for i, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ValueError(  # noqa: TRY004
                f"messages[{i}] must be a {{'role', 'content'}} mapping, "
                f"not {type(message).__name__}"
            )
        role = message.get("role")
        if not isinstance(role, str) or str.__str__(role) not in AGENT_MESSAGE_ROLES:
            raise ValueError(f"messages[{i}] role must be 'user' or 'assistant', not {role!r}")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError(  # noqa: TRY004
                f"messages[{i}] content must be a string, not {type(content).__name__}"
            )
        out.append({"role": str.__str__(role), "content": str.__str__(content)})
    return out


def agent_body(
    prompt: str,
    *,
    stream: bool | None,
    system: str | None = None,
    max_steps: int | None = None,
    model: str | None = None,
    provider: Literal["anthropic", "openai"] | None = None,
    max_cost_usd: float | None = None,
    thread_id: str | None = None,
    store: bool | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """One agent run's request.

    ``stream`` is ``True`` or ``False`` for ``POST computers/:id/agent``, and
    ``None`` for a background run, whose route refuses the field outright.

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

    ``max_cost_usd`` is a funded run's spending cap, checked against the range
    the platform accepts (:data:`MIN_MAX_COST_USD` to :data:`MAX_MAX_COST_USD`
    dollars) for the same reason. It is sent whenever it is given: a run on the
    caller's own key ignores it, which is the platform's rule to apply.

    The conversation is checked on the platform's rules too: ``thread_id`` has
    the shape :func:`agent_thread_id` checks, ``store`` is a real ``bool``,
    ``messages`` is what :func:`agent_messages` accepts, ``store=False`` cannot
    continue a thread, and a non-empty ``messages`` cannot be sent with one — a
    thread already holds its conversation. Each is sent only when given.
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
        step_limit = MAX_BACKGROUND_STEPS if stream is None else MAX_STEPS
        if max_steps > step_limit:
            raise ValueError(f"max_steps may not exceed {step_limit}")
    if provider is not None and provider not in ("anthropic", "openai"):
        raise ValueError("provider must be anthropic or openai")
    if max_cost_usd is not None:
        max_cost_usd = real(max_cost_usd, "max_cost_usd")
        if not MIN_MAX_COST_USD <= max_cost_usd <= MAX_MAX_COST_USD:
            raise ValueError(
                f"max_cost_usd must be between {MIN_MAX_COST_USD} and {MAX_MAX_COST_USD} dollars, "
                f"not {max_cost_usd}"
            )
    history = None if messages is None else agent_messages(messages)
    if thread_id is not None:
        thread_id = agent_thread_id(thread_id)
    if store is not None:
        store = flag(store, "store")
    if store is False and thread_id is not None:
        raise ValueError("store=False cannot continue a thread; leave out thread_id or store")
    if history and thread_id is not None:
        raise ValueError(
            "send messages or thread_id, not both: a thread already holds its conversation"
        )
    body: dict[str, Any] = {"prompt": prompt}
    if stream is not None:
        body["stream"] = flag(stream, "stream")
    for key, value in (
        ("system", system),
        ("max_steps", max_steps),
        ("model", model),
        ("provider", provider),
        ("max_cost_usd", max_cost_usd),
        ("thread_id", thread_id),
        ("store", store),
        ("messages", history),
    ):
        if value is not None:
            body[key] = value
    return body


# --- agent threads and background runs --------------------------------------

#: The account's agent threads: list, read, delete.
AGENT_THREADS = "agent/threads"
#: The account's background agent runs: list, read, cancel.
AGENT_RUNS = "agent/runs"
#: The most one page of threads or runs holds; the platform refuses more.
AGENT_PAGE_MAX = 100
#: Every status a background run can be in, the filter
#: ``GET agent/runs?status=`` accepts.
AGENT_RUN_STATUSES = ("running", "completed", "failed", "cancelled", "interrupted")


def agent_thread(thread_id: str) -> str:
    return f"agent/threads/{seg(thread_id)}"


def agent_run(run_id: str) -> str:
    return f"agent/runs/{seg(run_id)}"


def agent_run_cancel(run_id: str) -> str:
    return f"agent/runs/{seg(run_id)}/cancel"


def computer_agent_runs(computer_id: str) -> str:
    return f"computers/{seg(computer_id)}/agent/runs"


def agent_page_params(
    computer_id: object, limit: object, before: object, status: object = None
) -> dict[str, str] | None:
    """``GET agent/threads`` and ``GET agent/runs``: a computer, a status, a
    page size and a cursor, each only when given.

    Checked the way the platform checks them, since it answers anything else
    with 400. An empty ``computer_id`` is refused rather than dropped, for the
    reason :func:`operations_params` gives: dropped, it would list the whole
    account to a caller who believes they asked about one computer. ``before``
    is the id of the last row of the page before; ``status`` is one of
    :data:`AGENT_RUN_STATUSES`.
    """
    params: dict[str, str] = {}
    if computer_id is not None:
        cid = canonical(computer_id, "computer_id")
        if not cid or cid != cid.strip():
            raise ValueError("computer_id must be a computer id, with no spaces around it")
        params["computer_id"] = cid
    if status is not None:
        word = canonical(status, "status")
        if word not in AGENT_RUN_STATUSES:
            raise ValueError(f"status must be one of {', '.join(AGENT_RUN_STATUSES)}, not {word!r}")
        params["status"] = word
    if limit is not None:
        number = whole(limit, "limit", exc=ValueError)
        if not 1 <= number <= AGENT_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {AGENT_PAGE_MAX}, not {number}")
        params["limit"] = str(number)
    if before is not None:
        cursor = canonical(before, "before")
        if not cursor or cursor != cursor.strip():
            raise ValueError("before must be the id of the last row of the page before")
        params["before"] = cursor
    return params or None


def start_params(resume_only: bool) -> dict[str, Any] | None:
    """Only resume a saved session; never cold-boot when the flag is set."""
    return {"resume_only": "true"} if flag(resume_only, "resume_only") else None


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
#: the platform's own number by ``scripts/check_surface.py``, so the copy
#: cannot drift unnoticed.
WEBHOOK_DESCRIPTION_MAX = 200
#: The ceiling on a subscription's ``url``, mirrored and drift-checked the same
#: way. Counted as the platform counts it, in UTF-16 code units, so a character
#: outside the Basic Multilingual Plane counts twice.
WEBHOOK_URL_MAX = 2048
#: The ceiling on ``computers``, mirrored from the platform's own the same way.
WEBHOOK_COMPUTERS_MAX = 64
#: The replay window, mirrored from the platform's own number. Lives in
#: ``_webhooks`` as the verifier's default and is named here so the drift check,
#: which reads every mirrored number off this module, sees it.
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
    units = len(text.encode("utf-16-le", "surrogatepass")) // 2
    if units > WEBHOOK_URL_MAX:
        raise ValueError(f"url must be at most {WEBHOOK_URL_MAX} characters, not {units}")
    return text


def _description(value: object) -> str:
    """A webhook description, at most 200 characters as the platform counts them.

    UTF-16 code units, as :func:`_webhook_url` counts: the platform measures a
    JavaScript string, where a character outside the Basic Multilingual Plane
    (an emoji) is two, so 101 of them pass a code-point count and are refused
    with a 400 (OPL-6080).
    """
    text = canonical(value, "description")
    units = len(text.encode("utf-16-le", "surrogatepass")) // 2
    if units > WEBHOOK_DESCRIPTION_MAX:
        raise ValueError(
            f"description must be at most {WEBHOOK_DESCRIPTION_MAX} characters, not {units}"
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


def ssh_key(key_id: str) -> str:
    return f"ssh-keys/{seg(key_id)}"


#: What :func:`api_key` says when handed an API key itself. It never contains
#: the value: the whole point is that the key goes nowhere.
RAW_KEY_AS_ID = "that is an API key, not a key id; run api-keys list to find its id (key-...)"

#: The exact shape of an API key, found anywhere in a value: a key saved with a
#: byte-order mark, copied with a zero-width space, quoted or labelled
#: (``Bearer com_...``, ``MANDALA_API_KEY=com_...``) is still the key. A key id
#: (``key-`` and hex) can never contain it.
_RAW_KEY_ANYWHERE = re.compile(r"com_[0-9a-f]{48}", re.IGNORECASE)


def not_a_raw_key(key_id: object) -> str:
    """*key_id*, refused if it is an API key rather than a key's id.

    A key id is ``key-`` and hex; an API key is ``com_`` and hex, and nothing
    that starts ``com_`` can be an id. Revoking the key you hold by pasting it
    would otherwise put the live credential into the request path, where every
    access log on the way records it in plain text, and the platform would
    answer 404 and leave the key valid. Refused before any request, with a
    message that does not repeat the value. Surrounding whitespace and letter
    case are ignored for the check alone, so a pasted key with a stray space
    or in capitals is caught too, and a full key anywhere in the value (behind
    an invisible character, in quotes, after ``Bearer``) is refused as well.
    """
    value = canonical(key_id, "id")
    if value.strip().lower().startswith("com_") or _RAW_KEY_ANYWHERE.search(value):
        raise ValueError(RAW_KEY_AS_ID)
    return value


def api_key(key_id: str) -> str:
    return f"api-keys/{seg(not_a_raw_key(key_id))}"


def operation(operation_id: str) -> str:
    return f"operations/{seg(operation_id)}"


def workspace(workspace_id: str) -> str:
    return f"workspaces/{seg(workspace_id)}"


def workspace_members(workspace_id: str) -> str:
    return f"workspaces/{seg(workspace_id)}/members"


def workspace_move_body(workspace_id: str | None) -> dict[str, str | None]:
    """An explicit destination; only ``None`` unassigns a computer."""
    if workspace_id is not None:
        workspace_id = canonical(workspace_id, "workspace_id")
        if not re.fullmatch(r"wsp-[0-9a-f]{12}", workspace_id):
            raise ValueError(
                "workspace_id must be wsp- followed by 12 lowercase hex digits, or None"
            )
    return {"workspace_id": workspace_id}


def workspace_name_body(name: object) -> dict[str, Any]:
    """``POST workspaces`` and ``PATCH workspaces/:id``: ``{"name": …}`` and
    nothing else.

    The platform trims the name and judges it (1 to 40 characters, no control
    characters, unique within the account); a name that is empty once trimmed
    is refused here, before a request is spent on a certain 400.
    """
    n = canonical(name, "name")
    if not n.strip():
        raise ValueError("name must not be empty")
    return {"name": n}


#: The header every lifecycle call carries so that sending it again cannot do
#: it twice (platform OPL-5127). The platform records a call that carries one
#: before carrying it out, and for 24 hours answers the same key with the same
#: request from that record instead of doing the call again: ``409`` with
#: ``code: "idempotency_in_progress"`` while it runs, the original answer once
#: it has finished. A ``5xx`` that names an ``operation_id`` is different: its
#: outcome is unknown, and from then on the same key answers ``409`` with
#: ``code: "idempotency_outcome_unknown"`` and never the result, so read the
#: computer or its operation instead. A ``5xx`` that names none may have been
#: refused before the call was sent anywhere, which releases the key, and the
#: same call under it is carried out; so resending under the same key is safe
#: after any ``5xx``. Keys are kept per credential scope. A different request
#: under the same key is a ``422``.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

_IDEMPOTENCY_KEY = re.compile(r"[\x21-\x7e]{1,255}")


def idempotency_key(key: object, what: str = "idempotency_key") -> str:
    """A caller's ``Idempotency-Key``, checked against the platform's rule: 1
    to 255 characters, each printable ASCII other than a space."""
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError(
            f"{what} must be 1 to 255 characters, each printable ASCII other than a space"
        )
    return str.__str__(key)


def idempotency_headers(key: object = None) -> dict[str, str]:
    """The ``Idempotency-Key`` header for ONE logical lifecycle call: the
    caller's key, checked here rather than refused by the platform, or a fresh
    random one.

    Called once per call, before its first attempt, and passed in the call's
    headers — so whatever re-sends that call re-sends the same key, and a
    different call (the start a launch makes after its create) gets its own.
    """
    return {IDEMPOTENCY_KEY_HEADER: uuid.uuid4().hex if key is None else idempotency_key(key)}


def credits_params(before: object, limit: object) -> dict[str, str] | None:
    """``GET credits``: an older-entries cursor and a page size, each only when given.

    Both are checked the way the platform checks them, since it refuses any
    other value with 400: ``before`` is a whole number from 1 to
    :data:`CREDIT_CURSOR_MAX` (a page's ``next_before``), and ``limit`` from 1
    to :data:`CREDIT_PAGE_MAX`.
    """
    params: dict[str, str] = {}
    if before is not None:
        cursor = whole(before, "before", exc=ValueError)
        if not 1 <= cursor <= CREDIT_CURSOR_MAX:
            raise ValueError(
                f"before must be the next_before of the page before, a whole number from 1 "
                f"to {CREDIT_CURSOR_MAX}, not {cursor}"
            )
        params["before"] = str(cursor)
    if limit is not None:
        number = whole(limit, "limit", exc=ValueError)
        if not 1 <= number <= CREDIT_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {CREDIT_PAGE_MAX}, not {number}")
        params["limit"] = str(number)
    return params or None


def operations_params(
    computer_id: object, limit: object, cursor: object, idempotency_key_: object = None
) -> dict[str, str] | None:
    """``GET operations``: a computer, a key, a page size and a cursor, each only when given.

    An empty ``computer_id`` or ``cursor`` is refused rather than sent: the
    platform answers 400 for either, and an empty ``computer_id`` dropped
    instead would list the whole account's operations to a caller who believes
    they asked about one computer.
    """
    params: dict[str, str] = {}
    if computer_id is not None:
        cid = canonical(computer_id, "computer_id")
        if not cid or cid != cid.strip():
            raise ValueError("computer_id must be a computer id, with no spaces around it")
        params["computer_id"] = cid
    if idempotency_key_ is not None:
        params["idempotency_key"] = idempotency_key(idempotency_key_)
    if limit is not None:
        number = whole(limit, "limit", exc=ValueError)
        if not 1 <= number <= OPERATIONS_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {OPERATIONS_PAGE_MAX}, not {number}")
        params["limit"] = str(number)
    if cursor is not None:
        c = canonical(cursor, "cursor")
        if not c:
            raise ValueError("cursor must be the next_cursor of the page before, not empty")
        params["cursor"] = c
    return params or None


def api_key_body(
    name: object,
    workspace_id: object,
    *,
    expires_at: object = None,
    expires_in_hours: object = None,
    computer_ids: object = None,
) -> dict[str, Any]:
    """``POST api-keys``: a label, a workspace, an expiry and a computer list,
    each only when given.

    Never ``manage_keys``: the platform refuses ``true`` from every API key
    (403) and grants the permission only from a dashboard session, so there is
    nothing for this SDK to ask for. A workspace of ``""`` or with spaces around
    it is refused here — the platform keeps "absent" (the caller's own scope)
    and a workspace id apart, and would answer it as no such workspace.
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = canonical(name, "name")
    if workspace_id is not None:
        ws = canonical(workspace_id, "workspace_id")
        if not ws.strip() or ws != ws.strip():
            raise ValueError(
                "workspace_id must be a workspace id, with no spaces around it; "
                "omit it for a key in your own scope"
            )
        body["workspace_id"] = ws
    body.update(end_time_fields(expires_at, expires_in_hours, "expires_at", "expires_in_hours"))
    if computer_ids is not None:
        body["computer_ids"] = _computer_id_list(computer_ids)
    return body


def _computer_id_list(value: object) -> list[str]:
    """The computers a key is confined to: a real list of ids, never empty.

    A bare string is a sequence of characters and would go out as one id per
    letter; an empty list would confine the key to nothing, which the platform
    refuses. How many may be named is the platform's rule.
    """
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple)):
        raise ValueError(  # noqa: TRY004
            f"computer_ids must be a list of computer ids, not {type(value).__name__}"
        )
    if not value:
        raise ValueError("computer_ids must name at least one computer; omit it for any computer")
    ids = []
    for i, entry in enumerate(value):
        cid = canonical(entry, f"computer_ids[{i}]")
        if not cid.strip() or cid != cid.strip():
            raise ValueError(f"computer_ids[{i}] must be a computer id, with no spaces around it")
        ids.append(cid)
    return ids


def computer_ssh(computer_id: str) -> str:
    """A computer's SSH setting: read with GET, switch with PUT."""
    return computer_action(computer_id, "ssh")


def ssh_key_body(public_key: object, name: object) -> dict[str, Any]:
    """``POST ssh-keys``: one ``.pub`` line, and a label only when one was given.

    Surrounding whitespace is dropped, so the contents of a ``.pub`` file can be
    passed as read, trailing newline and all. Anything else about the line is
    the platform's to judge, and a refusal names the rule it broke.
    """
    line = canonical(public_key, "public_key").strip()
    if not line:
        raise ValueError("public_key must not be empty")
    body: dict[str, Any] = {"public_key": line}
    if name is not None:
        body["name"] = canonical(name, "name")
    return body


def ssh_access_body(enabled: object) -> dict[str, Any]:
    """``PUT computers/:id/ssh``. A real bool: this one opens a computer to logins."""
    return {"enabled": flag(enabled, "enabled")}


# --- public URLs ------------------------------------------------------------


def computer_public_urls(computer_id: str) -> str:
    """A computer's public URLs: list with GET, make one with POST."""
    return computer_action(computer_id, "public-urls")


def computer_public_url(computer_id: str, url_id: str) -> str:
    """One of a computer's public URLs: change it with PATCH, remove it with DELETE."""
    return f"{computer_public_urls(computer_id)}/{seg(url_id)}"


#: The two ways a public URL may be opened. ``public``: anyone with the link.
#: ``private``: an appropriately scoped account API key or dashboard browser sign-in.
PUBLIC_URL_ACCESS = ("public", "private")
#: The computer's own SSH port, which a public URL may not serve.
PUBLIC_URL_RESERVED_PORT = 22


def _public_url_port(value: object) -> int:
    port = whole(
        value,
        "port",
        exc=ValueError,
        message=f"port must be a whole number from 1 to 65535, not {value!r}",
    )
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be a whole number from 1 to 65535, not {port}")
    if port == PUBLIC_URL_RESERVED_PORT:
        raise ValueError("port 22 belongs to the computer's SSH server and cannot be served")
    return port


def _public_url_access(value: object) -> str:
    access = canonical(value, "access")
    if access not in PUBLIC_URL_ACCESS:
        raise ValueError(f'access must be "public" or "private", not {access!r}')
    return access


def public_url_body(
    *,
    port: object = _OMITTED,
    label: object = _OMITTED,
    access: object = _OMITTED,
    create: bool,
) -> dict[str, Any]:
    """The create or update payload, with only the fields the caller named.

    A create needs ``port`` and may name a ``label`` and an ``access``; without
    an ``access`` the platform chooses public on paid plans, private on trials.
    An update may change ``port`` and ``access`` but never the label, which IS
    the URL, and must
    change something: an update naming nothing is a 400 upstream, refused here
    first for :func:`webhook_body`'s reason.

    The label is passed as given, past being a string: which labels are taken,
    reserved or misleading is the platform's rule, and its 400 names the one a
    label broke. ``None`` for any field means "not given", so a caller can pass
    an optional through without a branch of its own.
    """
    body: dict[str, Any] = {}
    if port is not _OMITTED and port is not None:
        body["port"] = _public_url_port(port)
    elif create:
        raise ValueError("port is required")
    if label is not _OMITTED and label is not None:
        if not create:
            raise ValueError("a public URL's label cannot change: delete it and make another")
        text = canonical(label, "label")
        if not text.strip():
            raise ValueError("label must not be empty; omit it to have one picked")
        body["label"] = text
    if access is not _OMITTED and access is not None:
        body["access"] = _public_url_access(access)
    if not body:
        raise ValueError("an update must name a port, an access, or both")
    return body


# --- credential gateway -----------------------------------------------------


def computer_credential_gateway(computer_id: str) -> str:
    """A computer's credential gateway bindings: list with GET, make one with POST."""
    return computer_action(computer_id, "credential-gateway")


def computer_credential_gateway_binding(computer_id: str, binding_id: str) -> str:
    """One of a computer's credential gateway bindings: revoke it with DELETE."""
    return f"{computer_credential_gateway(computer_id)}/{seg(binding_id)}"


#: An HTTP method a credential gateway binding may allow.
CredentialGatewayMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
#: The HTTP methods a credential gateway binding may allow.
CREDENTIAL_GATEWAY_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
#: The longest origin the platform accepts, in characters.
CREDENTIAL_GATEWAY_ORIGIN_MAX = 256
#: The longest path prefix the platform accepts, in characters.
CREDENTIAL_GATEWAY_PATH_MAX = 4096


def _gateway_origin(value: object) -> str:
    """An exact public ``https://host`` origin, as far as it is knowable here.

    No path, query, fragment, userinfo or port, and a dotted name rather than
    an IP address: the platform refuses each, and its 400 does not say which
    part was wrong. Which names resolve publicly is still the platform's check.
    Sent as given; the platform keeps the origin it normalises it to.
    """
    origin = canonical(value, "origin")
    message = (
        "origin must be an exact HTTPS origin such as https://api.example.com: "
        f"no path, query, fragment, user, password or port (got {origin!r})"
    )
    if len(origin) > CREDENTIAL_GATEWAY_ORIGIN_MAX or any(
        ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in origin
    ):
        raise ValueError(message)
    try:
        parts = urllib.parse.urlsplit(origin)
        port = parts.port
    except ValueError:
        raise ValueError(message) from None
    host = parts.hostname or ""
    if (
        parts.scheme.lower() != "https"
        or "@" in parts.netloc
        or port is not None
        or parts.netloc.endswith(":")
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or not host
        or "[" in parts.netloc
        or "." not in host
    ):
        raise ValueError(message)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return origin
    raise ValueError(f"origin must name a host, not an IP address (got {origin!r})")


def _gateway_path_prefix(value: object) -> str:
    """A canonical absolute path prefix: what the platform would accept.

    It starts with ``/`` and not ``//``, has no query, fragment, backslash,
    whitespace, control or non-ASCII character, no empty segment and no ``.``
    or ``..`` segment. Percent-encoded separators and dots are refused by the
    platform as well; that is its 400 to give.
    """
    path = canonical(value, "path_prefix")
    message = (
        "path_prefix must be a canonical absolute path such as /repos/example: "
        f"starting with /, without //, ?, #, \\, spaces, or . and .. segments (got {path!r})"
    )
    if (
        not path.startswith("/")
        or len(path) > CREDENTIAL_GATEWAY_PATH_MAX
        or "//" in path
        or any(ch in path for ch in "?#\\")
        or any(ord(ch) <= 0x20 or ord(ch) >= 0x7F for ch in path)
        or any(part in (".", "..") for part in path.split("/"))
    ):
        raise ValueError(message)
    return path


def _gateway_methods(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(  # noqa: TRY004
            f'methods must be a list of HTTP methods, such as ["GET"] (got {type(value).__name__})'
        )
    methods = [canonical(m, "methods") for m in value]
    if not 1 <= len(methods) <= len(CREDENTIAL_GATEWAY_METHODS):
        raise ValueError(
            f"methods must name 1 to {len(CREDENTIAL_GATEWAY_METHODS)} HTTP methods "
            f"(got {len(methods)})"
        )
    for method in methods:
        if method not in CREDENTIAL_GATEWAY_METHODS:
            raise ValueError(
                f"methods may use {', '.join(CREDENTIAL_GATEWAY_METHODS)}, in capitals "
                f"(got {method!r})"
            )
    if len(set(methods)) != len(methods):
        raise ValueError("methods must not repeat a method")
    return methods


def credential_gateway_body(
    *, secret_id: object, origin: object, path_prefix: object, methods: object
) -> dict[str, Any]:
    """``POST computers/:id/credential-gateway``: every field is required.

    Checked here for what is knowable without the platform: the secret id is a
    non-empty string, the origin an exact HTTPS origin, the path prefix a
    canonical absolute path and the methods one to six distinct ones from
    :data:`CREDENTIAL_GATEWAY_METHODS`. Whether the secret exists in the
    computer's scope, and whether the host's gateway is ready, are the
    platform's to answer.
    """
    secret = canonical(secret_id, "secret_id")
    if not secret.strip():
        raise ValueError("secret_id must not be empty")
    return {
        "secret_id": secret,
        "origin": _gateway_origin(origin),
        "path_prefix": _gateway_path_prefix(path_prefix),
        "methods": _gateway_methods(methods),
    }


# --- secret bindings --------------------------------------------------------


def computer_secrets(computer_id: str) -> str:
    """A computer's secret bindings: read with GET, replace whole with PUT."""
    return computer_action(computer_id, "secrets")


#: At most this many secrets per computer, and this many of them as files. The
#: platform's own bounds, refused here so the mistake is named before the round
#: trip rather than as a 400 after it.
SECRET_BINDINGS_MAX = 32
SECRET_FILES_MAX = 8
#: A variable's name and a file's, as the platform takes them; matched whole
#: (``fullmatch``), on the way out and again on an answer read back.
SECRET_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
SECRET_FILE = re.compile(r"[a-z][a-z0-9_-]{0,47}")
_SECRET_KEYS = frozenset({"secret_id", "env", "file", "revision_id"})


def _secret_ref(value: object, what: str) -> str:
    """An id as the platform stores it: nonempty, with no surrounding whitespace.

    Refused rather than stripped: an id the caller padded is one it did not
    copy from a read, and trimming it would decide which secret it meant.
    """
    text = _encodable(canonical(value, what), what)
    if not text or text != text.strip():
        raise ValueError(f"{what} must be a nonempty id with no surrounding whitespace")
    return text


def _encodable(text: str, what: str) -> str:
    """``text``, if it can go on the wire as UTF-8 at all.

    A lone surrogate is a ``str`` that no encoder will write. Refused here, by
    name and without the text, because the alternative is the HTTP client
    failing to encode the WHOLE body — and on a secret's create or replace that
    body holds the value, which the encoder's exception then carries in its
    ``repr``.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        pass
    else:
        return text
    raise ValueError(f"{what} must be valid Unicode text")


def secret_bindings_body(
    bindings: object, what: str = "secrets", *, revisions: bool = True
) -> list[dict[str, str]]:
    """A binding list as the wire takes it, checked for what the platform would refuse.

    Each binding names one secret by id and publishes it under exactly one of
    ``env`` (an environment variable in the desktop session) and ``file``
    (``/run/mandala-secrets/user/files/<file>``). No secret, variable name or
    file name twice; a variable and a file may share a spelling, since they are
    two namespaces. A key this does not know is refused rather than dropped: a
    misspelt ``revision_id`` left out would silently record the latest instead.

    A bare mapping or string in the list position is refused rather than
    wrapped, for the reason :func:`_ids` gives.

    ``revisions=False`` is a create's list: a new computer always binds each
    secret's latest revision, and the platform refuses a ``revision_id`` there,
    so one is refused here before anything is sent (OPL-6080). It is for a
    rebind with ``set_secrets``.
    """
    if isinstance(bindings, (str, bytes, Mapping)) or not isinstance(bindings, (list, tuple)):
        raise ValueError(  # noqa: TRY004 — one exception type for one class of mistake
            f"{what} must be a list of {{secret_id, env}} or {{secret_id, file}}, "
            f"not {type(bindings).__name__}"
        )
    if len(bindings) > SECRET_BINDINGS_MAX:
        raise ValueError(f"{what}: at most {SECRET_BINDINGS_MAX} secrets per computer")
    ids: set[str] = set()
    envs: set[str] = set()
    files: set[str] = set()
    out: list[dict[str, str]] = []
    for i, binding in enumerate(bindings):
        at = f"{what}[{i}]"
        if not isinstance(binding, Mapping):
            raise ValueError(  # noqa: TRY004
                f"{at} must be a mapping, not {type(binding).__name__}"
            )
        unknown = sorted(str(k) for k in binding if k not in _SECRET_KEYS)
        if unknown:
            raise ValueError(f"{at} has unknown keys {unknown}")
        secret_id = _secret_ref(binding.get("secret_id"), f"{at}.secret_id")
        if secret_id in ids:
            raise ValueError(f"{at}: {secret_id} is bound twice")
        ids.add(secret_id)
        env, file = binding.get("env"), binding.get("file")
        if (env is None) == (file is None):
            raise ValueError(f"{at} must name exactly one of env and file")
        wire = {"secret_id": secret_id}
        if env is not None:
            name = canonical(env, f"{at}.env")
            if not SECRET_ENV.fullmatch(name):
                raise ValueError(
                    f"{at}.env must be letters, digits and underscores, "
                    "not starting with a digit, at most 64 characters"
                )
            if name in envs:
                raise ValueError(f"{at}.env {name} is bound twice")
            envs.add(name)
            wire["env"] = name
        else:
            name = canonical(file, f"{at}.file")
            if not SECRET_FILE.fullmatch(name):
                raise ValueError(
                    f"{at}.file must be lowercase letters, digits, - and _, "
                    "starting with a letter, at most 48 characters"
                )
            if name in files:
                raise ValueError(f"{at}.file {name} is bound twice")
            files.add(name)
            wire["file"] = name
        revision_id = binding.get("revision_id")
        if revision_id is not None and not revisions:
            raise ValueError(
                f"{at}.revision_id is for a rebind (set_secrets); a create binds each "
                "secret's latest revision, so omit it"
            )
        if revision_id is not None:
            wire["revision_id"] = _secret_ref(revision_id, f"{at}.revision_id")
        out.append(wire)
    if len(files) > SECRET_FILES_MAX:
        raise ValueError(f"{what}: at most {SECRET_FILES_MAX} secrets as files")
    return out


def secrets_body(bindings: object, version: object = None) -> dict[str, Any]:
    """``PUT computers/:id/secrets``: the whole list, and the version it was read at.

    ``version`` is sent only when given, and must be the whole number a read
    answered.
    """
    body: dict[str, Any] = {"secrets": secret_bindings_body(bindings)}
    if version is not None:
        message = f"version must be the whole number a read answered, not {version!r}"
        number = whole(version, "version", exc=ValueError, message=message)
        if number < 0:
            raise ValueError(message)
        body["version"] = number
    return body


# --- the account's secret store ---------------------------------------------

#: The account's secret store (OPL-4984): names, ids and revisions — never a
#: value on the way out.
SECRETS = "secrets"
#: The platform's bounds on a name and a value, refused here so the mistake is
#: named before the round trip rather than as a 400 after it. ``GET secrets``
#: reports the live numbers in ``limits``.
SECRET_NAME_MAX = 60
SECRET_VALUE_MAX_BYTES = 4096


def secret(secret_id: str) -> str:
    return f"secrets/{seg(_encodable(canonical(secret_id, 'id'), 'id'))}"


#: The one thing said about a secret-bearing request that could not be encoded.
#: Fixed, because the failure it replaces carries the body — value included.
SECRET_UNENCODABLE = "the secret request could not be encoded; its value is not shown"


def sealed(call: Callable[[], T]) -> T:
    """Run a secret-bearing request, and never let its value out in an error.

    EXHAUSTIVE on purpose. Every exception but control flow (``Exception`` and
    not ``BaseException``, so ``KeyboardInterrupt``, ``SystemExit``,
    ``GeneratorExit`` and ``asyncio.CancelledError`` pass untouched) is replaced
    by a sanitized one — see :func:`_sanitized` — raised OUTSIDE the ``except``
    block, so neither ``__cause__`` nor ``__context__`` reaches the request.

    The ways a value got out before this: an encoder failing on the body; the
    SDK's transport errors, chained to an httpx exception that keeps the
    request; and an ``httpx.HTTPStatusError`` from a caller-supplied client
    whose response hook calls ``raise_for_status()`` — raised before this SDK
    maps anything, and holding the request and the response.
    """
    safe: Exception
    try:
        return call()
    except Exception as err:  # noqa: BLE001 — exhaustive by design; see above
        safe = _total(err)
    raise safe


async def asealed(call: Callable[[], Awaitable[T]]) -> T:
    """:func:`sealed`, awaited."""
    safe: Exception
    try:
        return await call()
    except Exception as err:  # noqa: BLE001 — exhaustive by design; see sealed
        safe = _total(err)
    raise safe


#: What a secret write raises when even sanitizing its failure failed.
SECRET_WRITE_FAILED = (
    "a secret write failed, and its details are withheld because they may hold the value"
)


def _total(err: Exception) -> Exception:
    """:func:`_sanitized`, which cannot fail.

    Sanitizing is code, and code can raise — a response rebuilt from a body
    already decoded once was decoded again, and the error that raised was
    chained to the original, request and all. So a failure here is caught and
    answered with a fixed exception built from nothing but the status and the
    method, read defensively. This returns rather than raises: the caller
    raises the result outside every ``except`` block, so no path chains to the
    original.
    """
    try:
        return _sanitized(err)
    except Exception:  # noqa: BLE001, S110 — the fallback below is the answer
        pass
    status: int | None = None
    method: str | None = None
    try:
        if isinstance(err, httpx.HTTPStatusError):
            status = int(err.response.status_code)
            method = str(err.request.method)
        elif isinstance(err, MandalaError):
            found = getattr(err, "status", None)
            status = found if type(found) is int else None
            said = getattr(err, "method", None)
            method = said if type(said) is str else None
    except Exception:  # noqa: BLE001, S110 — status and method are best-effort
        pass
    if status is not None:
        return APIError(SECRET_WRITE_FAILED, status=status, method=method)
    return MandalaError(SECRET_WRITE_FAILED)


def _sanitized(err: Exception) -> Exception:
    """An exception that says what ``err`` said, and holds nothing of the request.

    * An SDK error: a copy of it — same class, ``args`` and fields, so
      ``except`` clauses and :func:`is_transient` answer as before — minus any
      httpx object or exception among its attributes.
    * ``httpx.HTTPStatusError``: it cannot be built without a request and a
      response, so it becomes the SDK error its status maps to, as if the SDK
      had read the response itself, with the method kept.
    * An encoding or serialization failure: a fixed ``ValueError``.
    * Anything else: the same type built from a fixed message, if it can be and
      the result holds nothing it should not; otherwise a
      :class:`~mandala_computer.MandalaError` naming the original class.
    """
    if isinstance(err, MandalaError):
        return _scrubbed(err)
    if isinstance(err, httpx.HTTPStatusError):
        return _from_status(err)
    if isinstance(err, (UnicodeError, TypeError, ValueError)):
        return ValueError(SECRET_UNENCODABLE)
    name = type(err).__name__
    message = (
        f"{name} during a secret write; its details are withheld because they may hold the value"
    )
    try:
        rebuilt = type(err)(message)
    except Exception:  # noqa: BLE001 — any failure to rebuild falls back below
        rebuilt = None
    if rebuilt is not None and _clean(rebuilt):
        return rebuilt
    return MandalaError(message)


def _clean(err: BaseException) -> bool:
    """Whether a freshly built exception holds no request, response or chain."""
    if err.__cause__ is not None or err.__context__ is not None:
        return False
    for name in ("request", "response"):
        try:
            if getattr(err, name, None) is not None:
                return False
        except RuntimeError:  # httpx: "the .request property has not been set"
            pass
    return not any(
        isinstance(v, (httpx.Request, httpx.Response, BaseException)) for v in vars(err).values()
    )


def _from_status(err: httpx.HTTPStatusError) -> MandalaError:
    """The SDK's own error for a status a caller's response hook raised on.

    Built from a new response carrying only the status, the headers and the
    body the platform sent — no request — through the same mapping every
    other failure goes through.

    ``response.content`` is already decoded, so the headers that describe the
    wire encoding are left off: a ``Content-Encoding: gzip`` beside decoded
    bytes makes httpx decode them a second time, and fail.
    """
    from ._client import _BaseTransport  # a cycle at import time; not at call time

    response = err.response
    method = err.request.method
    try:
        content = response.content
    except httpx.ResponseNotRead:
        content = b""
    # The raw byte pairs, untouched: decoding a value to text and encoding it
    # back refuses a header that is not ASCII, and that sent every such refusal
    # to the fixed fallback — a 429 lost its class and Retry-After.
    headers = [
        (key, value)
        for key, value in response.headers.raw
        if key.lower() not in _WIRE_ENCODING_HEADERS
    ]
    mapped = _BaseTransport._error(
        httpx.Response(response.status_code, headers=headers, content=content)
    )
    mapped.method = method
    return _scrubbed(mapped)


#: Headers about how the body was carried, which no longer describe decoded bytes.
_WIRE_ENCODING_HEADERS = frozenset({b"content-encoding", b"content-length", b"transfer-encoding"})


def _scrubbed(err: MandalaError) -> MandalaError:
    """A copy of ``err`` with nothing that could hold a request body.

    Same class, same ``args``, same attributes — minus any httpx request or
    response — and, being a new object, no ``__cause__``, ``__context__`` or
    traceback from the exchange that failed.
    """
    copy = type(err).__new__(type(err))
    copy.args = err.args
    for key, value in vars(err).items():
        if not isinstance(value, (httpx.Request, httpx.Response, BaseException)):
            setattr(copy, key, value)
    return copy


def _workspace(workspace_id: object) -> str | None:
    if workspace_id is None:
        return None
    text = _encodable(canonical(workspace_id, "workspace_id"), "workspace_id")
    if not text or text != text.strip():
        raise ValueError("workspace_id must be a nonempty id with no surrounding whitespace")
    return text


def secret_scope_params(workspace_id: object) -> dict[str, str] | None:
    """``workspace_id`` on a read: absent for the account-wide scope."""
    workspace = _workspace(workspace_id)
    return None if workspace is None else {"workspace_id": workspace}


#: What the platform trims off a secret's name before it stores or looks one
#: up: JavaScript's ``String.prototype.trim`` set, which is neither
#: ``str.strip()``'s (that one also strips U+001C-U+001F and keeps U+FEFF) nor
#: ASCII whitespace alone.
_JS_TRIM = (
    "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006"
    "\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
)


def secret_name(name: object) -> str:
    """A secret's name as the platform keeps it: trimmed, 1 to 60 code points,
    no control characters (C0, DEL or C1).

    Normalized the way the platform normalizes it, so a name sent with padding
    is the name it was stored under — and a later lookup of the same padded
    name finds it.
    """
    text = _encodable(canonical(name, "name"), "name").strip(_JS_TRIM)
    if not text or len(text) > SECRET_NAME_MAX:
        raise ValueError(f"name must be 1 to {SECRET_NAME_MAX} characters once trimmed")
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in text):
        raise ValueError("name must not contain control characters")
    return text


def secret_value(value: object) -> str:
    """A value as the store takes it: 1 to 4096 bytes of UTF-8, as text.

    ``bytes`` are accepted when they are UTF-8, and refused when they are not,
    rather than decoded with replacement characters the caller never wrote.
    """
    # Every refusal is raised OUTSIDE the ``except`` that noticed it: a codec
    # error carries the text it failed on, and ``from None`` only hides the
    # context from a traceback — ``__context__`` would still hold the value.
    text: str | None = None
    if isinstance(value, (bytes, bytearray)):
        try:
            text = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            pass
    else:
        text = canonical(value, "value")
    size: int | None = None
    if text is not None:
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            pass
    if text is None or size is None:
        raise ValueError("value must be UTF-8 text")
    if not 1 <= size <= SECRET_VALUE_MAX_BYTES:
        raise ValueError(f"value must be 1 to {SECRET_VALUE_MAX_BYTES} bytes of UTF-8, not {size}")
    return text


def secret_revision(revision_id: object) -> str:
    """The ``revision_id`` a read answered: required on a replace and a delete."""
    if revision_id is None:
        raise ValueError("revision_id is required: read the secret and send the one it answered")
    return _secret_ref(revision_id, "revision_id")


def secret_create_body(name: object, value: object, workspace_id: object) -> dict[str, Any]:
    """``POST secrets``: a name and a value, and a workspace only when one was named."""
    body: dict[str, Any] = {"name": secret_name(name), "value": secret_value(value)}
    workspace = _workspace(workspace_id)
    if workspace is not None:
        body["workspace_id"] = workspace
    return body


def secret_replace_body(value: object, revision_id: object, workspace_id: object) -> dict[str, Any]:
    """``PUT secrets/:id``: the new value and the revision it replaces."""
    body: dict[str, Any] = {
        "value": secret_value(value),
        "revision_id": secret_revision(revision_id),
    }
    workspace = _workspace(workspace_id)
    if workspace is not None:
        body["workspace_id"] = workspace
    return body


def secret_delete_params(revision_id: object, workspace_id: object) -> dict[str, str]:
    """``DELETE secrets/:id``: ``revision_id`` is required, a workspace optional."""
    params = {"revision_id": secret_revision(revision_id)}
    workspace = _workspace(workspace_id)
    if workspace is not None:
        params["workspace_id"] = workspace
    return params


def file_uploads(computer_id: str) -> str:
    return f"{files(computer_id)}/uploads"


def file_upload_params(path: str, upload_id: str, no_wake: bool = False) -> dict[str, str]:
    params = upload_params(path, True, no_wake)
    path = params["path"]
    if (
        not path.startswith("/")
        or path == "/"
        or any(part in ("", ".", "..") for part in path.split("/")[1:])
    ):
        raise ValueError("Upload path must be a normalized absolute Linux file path")
    if not isinstance(upload_id, str) or re.fullmatch(r"[a-f0-9]{32}", upload_id) is None:
        raise ValueError("upload_id must be 32 lowercase hexadecimal characters")
    return {**params, "upload_id": str.__str__(upload_id)}


def file_chunk_params(
    path: str, upload_id: str, offset: int, size: int, data: bytes, overwrite: bool, no_wake: bool
) -> dict[str, str]:
    params = file_upload_params(path, upload_id, no_wake)
    if (
        type(size) is not int
        or type(offset) is not int
        or not 0 <= offset <= size <= 2147483648
        or not isinstance(data, bytes)
        or len(data) > 8 * 1024 * 1024
        or len(data) > size - offset
        or (not data and offset != size)
        or type(overwrite) is not bool
    ):
        raise ValueError("Invalid upload chunk, offset or size (8 MiB per chunk, 2 GiB per file)")
    return {**params, "offset": str(offset), "size": str(size), "overwrite": str(overwrite).lower()}
