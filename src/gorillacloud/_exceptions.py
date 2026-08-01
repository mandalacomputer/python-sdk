"""Exceptions raised by the SDK."""

from __future__ import annotations

__all__ = [
    "APIError",
    "AuthenticationError",
    "ConflictError",
    "GorillaCloudError",
    "NotFoundError",
    "PermissionDeniedError",
    "PlanLimitError",
    "TimeoutError",
]


class GorillaCloudError(Exception):
    """Base class for every error this SDK raises."""


class APIError(GorillaCloudError):
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

    Distinct from :class:`PlanLimitError`, which will not clear on its own, and
    from a plain :class:`APIError`, which usually will not either.
    """


class TimeoutError(GorillaCloudError):
    """A wait helper gave up before the computer reached the expected state."""
