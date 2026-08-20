"""Exceptions raised by the SDK."""

from __future__ import annotations

__all__ = [
    "APIError",
    "AuthenticationError",
    "ConflictError",
    "MandalaError",
    "NotFoundError",
    "PermissionDeniedError",
    "PlanLimitError",
    "RateLimitError",
    "TimeoutError",
    "UnavailableError",
]


class MandalaError(Exception):
    """Base class for every error this SDK raises."""


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
        #: Seconds to wait before retrying, from ``Retry-After``. ``None`` only
        #: if the header was missing or unparseable, which should not happen.
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


class TimeoutError(MandalaError):
    """The SDK stopped waiting.

    Two things raise it: a wait helper that gave up before the computer reached
    the expected state, and a request that outran the transport's budget for it.

    Either way nothing has been cancelled — a command goes on running in the
    guest after the request carrying it is abandoned. What was lost is this
    call's view of the outcome, which is why a command slower than its request
    wants :meth:`~mandala_computer.Computer.start_exec` rather than a longer
    deadline.
    """
