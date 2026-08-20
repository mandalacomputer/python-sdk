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
    "GatewayTimeoutError",
    "MandalaError",
    "NotFoundError",
    "OriginResponseError",
    "OriginUnreachableError",
    "PermissionDeniedError",
    "PlanLimitError",
    "RateLimitError",
    "TimeoutError",
    "UnavailableError",
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


class GatewayTimeoutError(APIError):
    """A proxy in front of the platform gave up before the platform answered (504, 524).

    Not the platform refusing anything. The request reached it and nothing was
    cancelled — what ended was one hop's willingness to hold the connection open
    with no response crossing it, so any work the request had already started
    carries on without it.

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
    never arrived, so nothing was started. A 520 means it **did** arrive — the
    platform received it and then returned an empty, unknown or oversized
    response, so it may have been carried out in full, in part, or not at all,
    and the answer was lost rather than never produced.

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
    """521-523, 525, 526 — a proxy in front of the platform could not reach it.

    The rest of what an edge generates on its own, and the opposite event to
    :class:`GatewayTimeoutError` despite sitting beside it in the numbering. A
    524 means the request arrived and its work carries on; these mean it never
    arrived, so nothing was started and there is nothing left running to account
    for. A caller branching on the class to decide whether to expect a busy
    guest agent gets opposite answers, correctly — which is why they are two
    types rather than more statuses on one.

    521-523 are an origin that is down or unreachable, which is what a platform
    restart looks like from outside and does clear. 525 and 526 are a TLS
    handshake the edge and the platform cannot agree on — an expired
    certificate, a mismatched name — which fails identically on every retry.
    ``str(e)`` says which of the two this is, because the right response differs:
    wait, or report it.

    :meth:`~mandala_computer.Computer.wait_for_guest` waits one of these out, as
    it waits out every error not named in ``_FATAL_WHILE_WAITING``.
    :meth:`~mandala_computer.Computer.wait_until_built` and
    :meth:`~mandala_computer.Computer.wait_until_running` do **not** — they read
    the computer's state without a retry around it, so one 522 mid-poll ends the
    wait. That is unchanged by this class existing, and whether the three of them
    should agree is part of a wider question about how the clients decide
    transience, being settled separately rather than here.
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

    Every one of these clears itself without anybody doing anything, so the
    answer is to wait and try again rather than to change the request. It means
    something is in flight that this operation cannot run alongside:

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
    """


class UnavailableError(APIError):
    """Something between the request and a hypervisor could not be reached (503).

    Universal on this surface rather than particular to listings, because every
    route on it ends at a hypervisor. Four things raise it:

    - **A listing would have been short.** ``GET /computers`` and ``GET
      /snapshots`` fan out across every hypervisor holding something of yours,
      so one that cannot be reached makes the answer incomplete, and the
      platform fails closed rather than answering a short 200. A short list is
      not a smaller truth — it reads exactly like the missing rows were deleted,
      and the obvious next thing a script does with a computer that has
      disappeared is tidy up after it. Pass ``allow_partial=True`` to take the
      short answer knowingly, which returns a
      :class:`~mandala_computer.Listing` whose
      :attr:`~mandala_computer.Listing.is_complete` is False.
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
