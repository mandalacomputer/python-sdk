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
    """A listing would have been short, and nothing said you would accept one (503).

    ``GET /computers`` and ``GET /snapshots`` fan out across every hypervisor
    holding something of yours, so one that cannot be reached makes the answer
    incomplete. The platform fails closed about that rather than answering a
    short 200: a short list is not a smaller truth — it reads exactly like the
    missing rows were deleted, and the obvious next thing a script does with a
    computer that has disappeared is tidy up after it.

    So this is the platform declining to let that happen silently. Retry — these
    clear on their own — or pass ``allow_partial=True`` to take the short answer
    knowingly, which returns a :class:`~mandala_computer.Listing` whose
    :attr:`~mandala_computer.Listing.is_complete` is False.
    """


class TimeoutError(MandalaError):
    """A wait helper gave up before the computer reached the expected state."""
