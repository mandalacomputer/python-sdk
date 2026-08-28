"""Exceptions raised by the SDK."""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._agent import AgentFailed

__all__ = [
    "APIError",
    "AuthenticationError",
    "ConflictError",
    "ConnectionError",
    "ConnectionInterruptedError",
    "FileTooLargeError",
    "GatewayTimeoutError",
    "MandalaError",
    "NotFoundError",
    "OriginResponseError",
    "OriginTLSError",
    "OriginUnreachableError",
    "PermissionDeniedError",
    "PlanLimitError",
    "RangeNotSatisfiableError",
    "RateLimitError",
    "TimeoutError",
    "UnavailableError",
    "is_transient",
]


class MandalaError(Exception):
    """Base class for every error this SDK raises."""

    #: The failed agent run behind this error, or ``None`` — which is every
    #: error that did not come out of one.
    #:
    #: Set by :meth:`~mandala_computer.Computer.agent`, which has to raise: the
    #: platform reports a mid-run failure as an event rather than a status, and
    #: the class that status deserves is not one that can carry a run. Without
    #: it, collecting the stream would be the one way of running the agent that
    #: throws away what the run had already spent and already done — the
    #: :attr:`~mandala_computer.AgentFailed.usage` billed to your own model key,
    #: and the :attr:`~mandala_computer.AgentFailed.steps` that are still on the
    #: desktop. :meth:`~mandala_computer.Computer.agent_stream` hands the same
    #: record over as an event and never needs this.
    agent: AgentFailed | None = None


class APIError(MandalaError):
    """The API returned an unsuccessful response."""

    def __init__(self, message: str, *, status: int, body: object = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class AuthenticationError(APIError):
    """The API key is missing, malformed, or revoked (401)."""


class PermissionDeniedError(APIError):
    """Authenticated, but not allowed — e.g. a suspended or unverified account (403)."""


class NotFoundError(APIError):
    """No such resource (404).

    Tenant scoping is enforced server-side, so another tenant's resource is
    reported as missing rather than forbidden — existence is not leaked.
    """


class PlanLimitError(APIError):
    """The account's plan does not cover this request (402).

    Raised for computer-count caps, per-computer size ceilings, account-wide RAM
    and storage pools, and OS entitlements. ``str(e)`` carries the API's
    explanation of which limit was hit.
    """


class FileTooLargeError(APIError):
    """More than one request carries, in either direction (413).

    Named for the case it was written for and now raised by three routes, only
    two of which move a file. :meth:`~mandala_computer.Computer.clipboard` is
    the third: a selection past 128 KiB is refused rather than truncated, and
    **the paging remedy below does not apply to it** — there is no ``Range`` on
    a clipboard, so the text is either under the cap or out of reach. The name
    stays because renaming a public exception breaks every caller catching it;
    read the message, which names the limit that actually applied.

    The file case, which is the rest of this docstring:

    A guest transfer larger than the 64 MiB one request moves.

    The ceiling is on what a single *request* moves, not on the file. The bytes
    cross the guest agent's one connection in chunks and a transfer holds it for
    as long as it takes, so the limit has to fall somewhere — and asking for a
    whole file bigger than that is what earns this.

    Which makes it a signpost rather than a dead end, on the read side.
    :meth:`~mandala_computer.Computer.read_file_part` applies the same ceiling to
    the *window* you asked for, so the file behind it may be any size, and
    :meth:`~mandala_computer.Computer.download_file` is that loop already
    written. The platform's own refusal says so too — it carries
    ``Accept-Ranges: bytes`` and names the header, because this is the one answer
    a caller gets precisely when they have a file too big to fetch and no reason
    yet to think there is another way to ask.

    An upload raises it too, and there it really is a limit on the file: ``PUT``
    takes no range. :meth:`~mandala_computer.Computer.write_file` refuses an
    oversized body before the request is made, so from that direction this
    arrives only if the platform's ceiling is lower than the one mirrored here.
    """


class RangeNotSatisfiableError(APIError):
    """The window asked for named no byte the file has (416).

    :attr:`size` is the file's real length, taken from the ``Content-Range`` the
    refusal carries. That header is the point of this status: the caller asked
    about a file whose length they did not know, and the number is what lets
    them ask again instead of guessing.

    ``size`` is legitimately ``0``. An empty file has no byte at any position, so
    *every* range against one is refused — which is why
    :meth:`~mandala_computer.Computer.download_file` reads a zero at offset zero
    as an empty file rather than as a failure. It is ``None`` only when the
    header was absent or unreadable, which is a hop in front of the platform
    having dropped it rather than the platform declining to say.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        body: object = None,
        size: int | None = None,
    ) -> None:
        super().__init__(message, status=status, body=body)
        #: The file's length in bytes, or ``None`` if the refusal did not carry it.
        self.size = size


class GatewayTimeoutError(APIError):
    """A proxy in front of the platform gave up before the platform answered (504, 524).

    Not the platform refusing anything. What ended was one hop's willingness to
    hold the connection open with no response crossing it, so anything the
    request had already set going carries on without it.

    Usually that means the platform received the request and is still working on
    it, which is what a 524 is: the edge connected, sent it, and gave up waiting
    for the answer. It is a strong default rather than a guarantee. A 504 can be
    raised by a hop that never reached the platform at all, and a 524 can end an
    upload whose body had not finished arriving — so before retrying something
    that *creates* rather than reads, check whether the first attempt took
    effect.

    The ceiling this reports is not the SDK's and not ``timeout``\'s: it belongs
    to whatever sits between the caller and the platform, and it is reached at
    the same place however long the call asked to wait. Against
    ``app.mandala.computer`` that is about two minutes, so a foreground
    :meth:`~mandala_computer.Computer.exec` of a slower command always ends
    here. :meth:`~mandala_computer.Computer.start_exec` is the way to run one:
    it answers as soon as the command has started and is polled afterwards, so
    no request is ever held open for the length of the work.

    Where the abandoned request was an ``exec``, the command keeps running, which
    is why the next call on that computer often raises :class:`ConflictError` —
    the guest agent is still busy with it. That is the earlier command, not a
    second failure. A read that met the ceiling started nothing and leaves
    nothing behind.

    ``str(e)`` is the platform's own message whenever it sent one. A gateway
    status usually arrives from an intermediary with an empty or HTML body, and
    that is what the SDK's own wording is for; a hop that answers in this
    surface's JSON has said something more specific than the SDK could, and it
    is kept.
    """


class OriginResponseError(APIError):
    """520 — the platform answered a proxy with something it could not read.

    Sits between the other two edge failures and must not be filed with either,
    because the question a caller is really asking is whether their work
    happened, and this is the one status whose honest answer is "unknown".

    A 524 means the request arrived and is still being worked on. 521-523 mean it
    almost certainly never arrived. A 520 means it **did** arrive and the
    exchange then broke on the way back: an empty or unreadable response, a
    connection dropped before headers, an origin that crashed part-way. So the
    work may have been carried out in full, in part, or not at all, and no
    answer may ever have been produced to lose.

    Which makes a blind retry the thing to be careful about. Re-sending a read
    costs nothing; re-sending a create can leave two computers where one was
    meant, both of them billable, on the strength of a failure that said the
    first one never happened. Look before retrying anything that makes something.

    This one was filed with :class:`OriginUnreachableError` at first, on the
    reading that the whole 52x range is the edge failing to reach the platform.
    It is not, and the message that came with it — "the request never arrived, so
    nothing was started" — was exactly the kind of confident falsehood the rest
    of this work exists to remove, pointed the other way.
    """


class OriginUnreachableError(APIError):
    """521-523 — a proxy in front of the platform could not reach it.

        One of four classes for an edge failing rather than the platform refusing,
        and they are four because a caller asking *did my work happen* needs four
        answers. :class:`GatewayTimeoutError` is a hop that stopped waiting, usually
        on a request the platform has. :class:`OriginResponseError` is 520, where the
        platform was reached and the exchange broke coming back. :class:`OriginTLSError`
        is a certificate that will never agree. This one is an origin that is down or
        unreachable — what a platform restart looks like from outside, and it clears.

        Almost always the request was never sent, so nothing was started and there is
        nothing left running to account for. *Almost*, rather than never: a 522 is a
        connection that timed out, and the edge can give up after one was
        established, so bytes already on the wire are not unsent because no
        acknowledgement came back. Retry a read freely; look before retrying
        something that creates.

    Every ``wait_*`` helper waits one of these out, which is a change: only
        :meth:`~mandala_computer.Computer.wait_for_guest` used to.
        :meth:`~mandala_computer.Computer.wait_until_built`,
        :meth:`~mandala_computer.Computer.wait_until_running` and
        :meth:`~mandala_computer.Computer.wait_for_move` read the control plane with
        no retry around it at all, so one of these mid-poll ended the wait and
        reported a machine that was coming up as one that never did (OPL-3724).
        :class:`OriginTLSError` is the sibling that is still never waited out, which
        is the whole reason it stopped sharing this class.

        Not in :func:`is_transient`, though, and that is the other half of the same
        change: *almost* never sent is not never, so an application replaying a
        create through one of these can end up paying for two computers.
    """


class OriginTLSError(APIError):
    """525, 526 — a proxy and the platform could not agree on TLS.

    Split from :class:`OriginUnreachableError`, which it used to share, because
    the two need opposite answers to "should I try again". An unreachable origin
    is a passing outage; an expired or mismatched certificate fails identically
    on every retry, and is a deployment somebody has to go and fix.

    Being its own class is what lets the ``wait_*`` helpers act on that. Sharing
    one meant the fatal set could not name it, so
    :meth:`~mandala_computer.Computer.wait_for_guest` retried a certificate
    failure for its full 180 seconds and then reported "the guest did not
    respond" — losing the cause, the class and the three minutes, while this
    error's own message said to report it rather than wait it out.
    ``_is_transient_for_poll`` names it now, on behalf of all four waits.
    """


class RateLimitError(APIError):
    """Too many requests, too fast (429).

    Every route on this surface is metered, including the ones that go on to
    answer 404 — the meter runs before the allowlist, the role gate and the
    forward, so a burst of anything counts against the same budget. The budget
    is generous (a plan's ``apiRatePerMin``, in the low thousands even at the
    bottom of the range), which is why hitting this usually means a loop with no
    sleep in it rather than real load.

    Its own class rather than a bare :class:`APIError` because it is the one
    refusal on this surface that says exactly how long to wait:
    :attr:`retry_after` carries the ``Retry-After`` header in seconds. Sleeping
    that long and repeating the request is the whole remedy.

    Which is what the ``wait_*`` helpers do with it, and why this left the fatal
    set in OPL-3724. It was named there for a real reason — a rate limit clears
    only on the server's cadence, and a poll loop substituting its own faster
    one makes a short limit into a longer one — but honouring the header is the
    remedy for that, and failing a wait the caller asked for is not.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        body: object = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status=status, body=body)
        #: Seconds to wait before retrying, from ``Retry-After``.
        #:
        #: ``None`` where there was no usable header, which on an ordinary
        #: response should not happen — every 429 on this surface carries one.
        #: The exception is a 429 the agent loop reported from inside a stream:
        #: the response there was a 200 and the refusal is an event in its body,
        #: so there is no header to read and nothing to guess from. That is the
        #: one place to expect this to be ``None`` and back off on your own.
        self.retry_after = retry_after


class ConflictError(APIError):
    """The request was fine; the moment was not (409).

    Nearly every one of these clears itself without anybody doing anything, so
    the answer is to wait and try again rather than to change the request. It
    means something is in flight that this operation cannot run alongside:

    - the computer's disk is still being copied from a snapshot or another
      computer (see :meth:`Computer.wait_until_built`)
    - a snapshot of it is being taken, or one is already being taken
    - it is already being deleted
    - a snapshot being deleted is one another is chaining onto
    - a purge was confirmed against a set of snapshots that has since changed
    - the guest agent has not answered yet, in the first seconds of a start —
      so retrying is the remedy, and giving up here abandons a machine that was
      about to answer
    - another operation is holding that computer's guest agent
    - a restart was asked of a computer with a suspended session, or a suspend
      of one that is not running
    - something is driving the guest at the moment a suspend was asked for, or
      a suspend has committed to the computer a request just arrived for — the
      retry resumes it, which is what the caller wanted

    Distinct from :class:`PlanLimitError`, which will not clear on its own, and
    from a plain :class:`APIError`, which usually will not either. A guest agent
    that stays silent past its boot window stops being a conflict and becomes a
    502 :class:`APIError`, so a retry loop on this exception terminates rather
    than being told "still booting" forever.

    NEARLY every one, and the exception is :class:`MoveRequiredError`. Whether a
    409 clears is a property of the body rather than of the status: a refusal
    that clears describes a passing state, and one that does not describes a
    decision about the request. This docstring said "every one" of them, which
    made a resize past what a host can run something to retry forever.
    """


class MoveRequiredError(ConflictError):
    """This resize needs the computer moved to another host first (409).

    Growing ``ram_mb`` past what the computer's current host can run is refused
    with an OFFER rather than an ending: the platform puts a ``move`` object on
    the body, because another host in the same region may be able to run that
    size and :meth:`~mandala_computer.Computer.relocate` is how you agree to go
    there.

    :attr:`move_possible` is the whole branch, read off the body here so that no
    caller has to: ``move.required`` is true either way, and it is the second
    field that says whether there is anything to do.

    - ``True`` — somewhere in the region can run it.
      :meth:`~mandala_computer.Computer.relocate` with the same sizing arguments
      moves the computer and applies the size on arrival. It copies the disk to
      different hardware, which is why it is a separate call rather than
      something a resize does to you quietly, and the computer has to be
      stopped.
    - ``False`` — nothing in the region can run that size at all. There is
      nowhere to move to; ask for less.

    NOT worth retrying either way, which is what earns it a class: it is a
    :class:`ConflictError` by status and the opposite of one by nature. The host
    cannot run that size and will not grow, so the same request answers the same
    way for as long as the computer is where it is.

    A subclass rather than a sibling, so that ``except ConflictError`` written
    before this existed still catches it. What changes is only what a caller
    should do about it.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        body: object = None,
        move_possible: bool,
    ) -> None:
        super().__init__(message, status=status, body=body)
        #: Whether a host in this region could run the size that was asked for.
        self.move_possible = move_possible


class UnavailableError(APIError):
    """Something between the request and a hypervisor could not be reached (503).

    Universal on this surface rather than particular to listings, because every
    route on it ends at a hypervisor. Four things raise it:

    - **A listing would have been short.** ``GET /computers``, ``GET
      /snapshots`` and ``GET /builds`` fan out across every hypervisor holding
      something of yours, so one that cannot be reached makes the answer
      incomplete, and the platform fails closed rather than answering a short
      200. A short list is not a smaller truth — it reads exactly like the
      missing rows were deleted, and the obvious next thing a script does with a
      computer that has disappeared is tidy up after it. Pass
      ``allow_partial=True`` to take the short answer knowingly, which returns a
      :class:`~mandala_computer.Listing` whose
      :attr:`~mandala_computer.Listing.is_complete` is False. Three listings and
      not two since OPL-3840: the builds one always answered this way and
      documented no way out, which made it strictly less available than the
      other two.
    - **The host holding one named computer is unreachable.** Every route that
      names a computer answers this rather than a 404, on the same reasoning:
      the computer has not gone anywhere. So ``start()``, ``exec()``,
      ``screenshot()`` and the rest can all raise it, and none of them takes an
      ``allow_partial``, because there is no partial version of them.
    - **A create or a resize could not check the plan**, because the fan-out
      that counts what you already hold came back short.
    - **The host has no room for another guest**, which clears when something
      on it stops.

    None of these is a fault on the caller's side, which is why this is its own
    class rather than a bare :class:`APIError`: the answer is to wait and try
    again rather than to change the request. Unlike :class:`ConflictError`,
    which is about something in flight on your own resources, this is about
    something we have to clear.
    """


class ConnectionError(MandalaError, builtins.ConnectionError):
    """The request never left. Nothing was dispatched, so anything may be replayed.

    A DNS failure, a refused connection, a TLS handshake that did not finish.
    Safe to retry without changing the request, which is what
    :func:`is_transient` says about it.

    **Narrower than it used to be**, and the narrowing is the point. This class
    once wrapped every ``httpx.RequestError``, which includes ``ReadError`` and
    ``RemoteProtocolError`` — failures that happen *after* the request reached
    the platform. Those wear the opposite outcome: the platform may well have
    acted, and the answer is what was lost. They now get
    :class:`ConnectionInterruptedError`, which is a subclass, so
    ``except ConnectionError`` still catches both.

    Its own class for :class:`TimeoutError`'s reason: ``httpx.RequestError`` is
    not a :class:`MandalaError`, so this used to leave here as a bare
    ``MandalaError`` — catchable by the SDK-wide handler and by nothing more
    specific. It is also both a :class:`MandalaError` and Python's built-in
    :class:`ConnectionError`, so either handler catches it.

    Named to match, and to be the same thing as, ``ConnectionError`` in
    mandala-computer-typescript and ``ConnectivityError`` in
    mandala-computer-mcp. All three now name it in the same retry predicate
    (OPL-3724), and until this class existed this SDK could not.
    """


class ConnectionInterruptedError(ConnectionError):
    """The request was dispatched and the answer was lost. Outcome unknown.

    A socket that resets while the response body is being read, a protocol
    error on the way back, a write that failed with part of the request already
    out. The shared property is the one that matters: the platform may have
    received the request and acted on it, and nothing in the error says whether
    it did.

    So this is **fatal** to :func:`is_transient` and transparent to
    ``_is_transient_for_poll``, and the split is the same one OPL-3724 made for
    502 and 504. Its reasoning applies here unchanged, and this case had escaped
    it only because it wears a class whose name says the request never left.
    ``computers.create()`` reaches the platform, the platform builds the
    computer, the socket dies mid-response: a caller asking
    :func:`is_transient` used to be told yes, replayed the create, and paid for
    two computers (OPL-3855).

    A **subclass** rather than a sibling, which is what keeps this from breaking
    anyone. ``except ConnectionError`` still catches it, and so does
    ``except builtins.ConnectionError``, so existing handlers and
    ``_is_transient_for_poll``'s floor need no change; only the one predicate
    that promises a blind replay had to learn the difference. It is the same
    shape :class:`MoveRequiredError` has under :class:`ConflictError`, for the
    same reason: a case that is genuinely a kind of its parent and genuinely
    answers one question the other way.

    httpx is what makes the split possible here, and it is the friendliest of
    the three transports for it — ``ConnectError`` and ``ConnectTimeout`` name
    the connect phase outright, where the TypeScript clients have to read
    undici's cause chain. See ``_request_failed`` in ``_client.py``.

    The poll predicate still rides it out, and that is not an oversight. The
    ``wait_*`` helpers replay reads — a computer read, an ``exit 0`` probe — and
    a read whose outcome was lost can simply be read again. Only a caller who
    might be replaying a **write** needs the distinction, which is exactly the
    caller :func:`is_transient` is exported for.
    """


class TimeoutError(MandalaError, builtins.TimeoutError):
    """The SDK stopped waiting.

    Two things raise it: a wait helper that gave up before the computer reached
    the expected state, and a request that outran the transport's budget for it.

    It is both a :class:`MandalaError` and Python's built-in
    :class:`TimeoutError`, so either the SDK-wide handler or an ordinary timeout
    handler catches it.

    Either way nothing has been cancelled — a command goes on running in the
    guest after the request carrying it is abandoned. What was lost is this
    call's view of the outcome, which is why a command slower than its request
    wants :meth:`~mandala_computer.Computer.start_exec` rather than a longer
    deadline.
    """


def is_transient(err: BaseException) -> bool:
    """Whether an error is worth trying again without changing the request.

    The **public** answer, and the one this SDK did not have. Its caller is
    application code wrapping an arbitrary call in ``if is_transient(err):``
    — possibly a ``create`` — so it names only failures that both clear on
    their own *and* are safe to replay blind:

    * :class:`ConflictError` — something in flight this cannot run alongside,
      minus :class:`MoveRequiredError`, which is a decision rather than a moment
    * :class:`RateLimitError` — a cadence, and the response usually says how long
    * :class:`UnavailableError` — a hypervisor briefly out of reach
    * :class:`ConnectionError` — the request never left

    That last line is now literally true, and it was not always. The class used
    to cover every ``httpx.RequestError``, a lost response body included, so
    this predicate told a caller replaying a create that the request had never
    completed when in fact it had been received and the *answer* was what went
    missing. :class:`ConnectionInterruptedError` carries that case now and is
    excluded below — the same decision as the paragraph after this one, applied
    to the one class it had missed (OPL-3855).

    Answered by TYPE, with no status numbers, and identical in all three clients
    as of OPL-3724. Before it, one question had three answers: this SDK named
    the *fatal* exceptions in ``_FATAL_WHILE_WAITING`` and retried the rest,
    mandala-computer-mcp matched classes plus a list of status numbers, and
    mandala-computer-typescript matched classes alone. Three mechanisms is how
    they drifted — and the numbers are what let it happen quietly, because a
    status can be added to a list without anyone saying which answer changed.

    Deliberately absent: 502, 504, 520-523, and :class:`TimeoutError`. Every one
    of them means the outcome is **unknown**, and "worth trying again" is not
    "the call definitely did not happen". Replaying a create through one is how
    one computer becomes two. The wait helpers still ride all of them out —
    they ask :func:`_is_transient_for_poll`, which can afford to, because it
    only ever replays a read.
    """
    if isinstance(err, MoveRequiredError):
        return False
    # A lost RESPONSE is not a request that never left, and only one of the two
    # is safe to replay blind. Same shape as the line above and the same reason:
    # a subclass of a branch below that would otherwise say yes (OPL-3855).
    if isinstance(err, ConnectionInterruptedError):
        return False
    return isinstance(err, (ConflictError, RateLimitError, UnavailableError, ConnectionError))


def _is_transient_for_poll(err: BaseException) -> bool:
    """The same words, a different question: whether a poll is worth making again.

    Asked only by the ``wait_*`` helpers, and deliberately private. They replay
    a computer read, a moves listing, a build read or an ``exit 0`` guest probe
    — idempotent, every one, and every one under a deadline the caller set.
    That pair of properties is a fact about what those calls *do* rather than
    about the error, which is why it cannot be published as the same ``True``.

    A deny-list, where :func:`is_transient` is an allow-list, and this SDK had
    the polarity right before either of the others: ``_FATAL_WHILE_WAITING``
    named what to give up on and waited out the rest. What it lacked was the
    second predicate beside it, so the same generosity could not be published,
    and three of the four ``wait_*`` helpers had no transience handling at all.

    The polarity follows from who pays for a wrong answer. Retrying something
    unretryable costs one poll interval and at worst the deadline the caller
    chose. *Not* retrying something that would have cleared costs a wait that
    reports a machine as unreachable while it was coming up — and under an
    allow-list every status the edge invents next lands there, silently, until
    somebody adds a class.

    The line is REQUEST versus MOMENT. A failure describing the request answers
    the same way forever and is fatal here; a failure describing the moment is
    what a poll exists to outlast. Fatal, therefore:

    * anything that is not a failed request. Only :class:`APIError`,
      :class:`ConnectionError` and :class:`TimeoutError` describe an exchange
      with the platform that did not work. A ``ValueError`` from a bug in this
      file is not a hypervisor being slow, and riding one out spends the
      caller's deadline before reporting the wrong cause; a bare
      :class:`MandalaError` is a verdict this SDK reached about a poll that
      *succeeded*, and polling through a verdict is a loop with a deadline on
      it. :class:`TimeoutError` is in the set because in this SDK it is also the
      transport's own give-up, which the next poll may well survive — the one
      place the three clients differ in spelling rather than in meaning, since
      an equivalent failure is a ``ConnectionError`` in the TypeScript SDK.
    * :class:`MoveRequiredError` — a decision about the size that was asked for.
    * :class:`OriginTLSError` (525, 526) — a certificate the edge and the
      platform cannot agree on fails identically on every retry, so waiting one
      out spends the whole deadline to report the wrong cause.
    * 524 — reached only by holding a request open past the edge's ceiling, so
      an identical retry reproduces it at the same place. It shares
      :class:`GatewayTimeoutError` with 504, which *is* worth another poll, and
      that is the one status still matched by NUMBER: a type cannot separate two
      statuses that share it.
    * anything below 500 that is not named. A 4xx is a request the platform
      refused on its merits — a bad body, a revoked key, a plan limit, a deleted
      id, an offset past the end of a file — and repeating it unchanged cannot
      change the answer. Three are named because they describe the moment
      instead: 409 (something in flight), 429 (a cadence) and 408, which RFC 9110
      defines as a request the client may repeat unchanged and which the edge in
      front of this surface does emit.

      A 3xx goes with the 4xx, which is why the rule is ``>= 500`` rather than
      "not a 4xx": httpx is left on its default of not following redirects and
      every non-2xx is an error here, so a base URL missing its trailing path
      answered 301 and got polled until the deadline — half an hour ending in a
      timeout that named nothing about the redirect (adversarial review,
      OPL-3835, on the build wait this rule came from).

    Everything at 5xx polls through, 502 and 520-523 included: they mean the
    outcome is unknown, and a read whose outcome is unknown can simply be read
    again. 5xx has an UPPER bound as well as a lower one, and it is not
    decoration: httpx accepts any three-digit status, so a broken or hostile
    origin can answer 700 — which ``>= 500`` alone called a passing moment and
    polled until the caller's deadline (Codex adversarial review, OPL-3724).

    What the floor costs, said plainly: ``_not_an_object`` raises a bare
    :class:`MandalaError` for a proxy answering HTML where the platform's JSON
    should be, and that used to be waited out. It is now fatal to a poll, in all
    three clients. That is the better answer anyway — "answered text/html, not a
    JSON object" names the cause on the first poll, where riding it out spends
    the deadline to report a timeout that names nothing.

    :class:`RateLimitError` is the one entry that moved *out* of the old fatal
    set. It was there for a real reason — a rate limit clears only on the
    server's cadence, and a helper replacing that with its own faster poll makes
    a short limit into a longer one — but the fix for that is to honour
    ``retry_after``, which the poll loops now do, rather than to fail the wait.
    """
    if not isinstance(err, (APIError, ConnectionError, TimeoutError)):
        return False
    if isinstance(err, (MoveRequiredError, OriginTLSError)):
        return False
    if isinstance(err, APIError):
        if err.status == 524:
            return False
        if err.status in (408, 409, 429):
            return True
        return 500 <= err.status < 600
    return True
