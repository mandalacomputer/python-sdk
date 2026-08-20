"""HTTP transports for the Mandala Computer API.

The sync and async transports differ only in where the awaits go. Everything
that decides *meaning* — key resolution, URL building, and which exception a
status maps to — lives on the shared base, so the two can never disagree about
what a 402 is or where a request goes.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

import httpx

from ._exceptions import (
    APIError,
    AuthenticationError,
    ConflictError,
    MandalaError,
    NotFoundError,
    PermissionDeniedError,
    PlanLimitError,
    RateLimitError,
    TimeoutError,
    UnavailableError,
)

DEFAULT_BASE_URL = "https://app.mandala.computer/api/v1"

#: How long the transport waits, when nothing asks for longer.
#:
#: Enough for every route that answers at the speed of the control plane. The
#: two that do not — a foreground ``exec`` and the file transfers — say so per
#: request; see :meth:`_BaseTransport._budget`.
DEFAULT_TIMEOUT = 60.0

#: Added to a guest-side deadline to get the client's.
#:
#: The platform holds an ``exec`` open for ``timeout_s + 5s`` before giving up
#: on the hypervisor, so a client waiting exactly ``timeout_s`` would abandon
#: the request in the window where the answer is still coming. This is that 5s
#: plus room for the round trip.
DEADLINE_SLACK = 15.0

#: Read and write budget for the file routes.
#:
#: A transfer is capped at 64 MiB and runs against a much longer platform-side
#: deadline than an ordinary call, so :data:`DEFAULT_TIMEOUT` would abandon a
#: large one the platform is still willing to finish.
FILE_TIMEOUT = 300.0

_STATUS_ERRORS = {
    401: AuthenticationError,
    402: PlanLimitError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    429: RateLimitError,
    # A fan-out listing that would have been short, without allow_partial. Its
    # own class rather than a bare APIError because it is the one 5xx on this
    # surface that is not a fault: nothing is broken from the caller's side, the
    # platform is declining to hand over a list it knows is incomplete.
    503: UnavailableError,
}

#: How many rows a listing is short by. Present means short; the number can be
#: 0, which means "short, by an amount the placement cache cannot state" rather
#: than "not short". So the presence is the signal and the count is detail —
#: which is why :attr:`Listing.incomplete` is ``None``-or-a-number rather than a
#: count that would read as complete at zero.
INCOMPLETE_HEADER = "X-GC-Incomplete"


def _incomplete(resp: httpx.Response) -> int | None:
    """How many rows this listing is short by, or ``None`` when it is complete.

    A malformed or negative count becomes ``0`` rather than ``None``. The header
    being there at all is the platform saying the list is short, and letting an
    unparseable number promote that to "complete" would turn a warning into its
    opposite — which is the one wrong answer this whole mechanism exists to
    prevent.
    """
    raw = resp.headers.get(INCOMPLETE_HEADER)
    if raw is None:
        return None
    try:
        n = int(raw)
    except ValueError:
        return 0
    return max(n, 0)


class _BaseTransport:
    """Auth, URL, and error rules — everything about a request except the IO."""

    def __init__(self, api_key: str | None, base_url: str | None) -> None:
        key = api_key or os.environ.get("MANDALA_API_KEY")
        if not key:
            raise MandalaError(
                "No API key. Pass api_key=... or set MANDALA_API_KEY "
                "(create one at Settings -> API keys)."
            )
        self.base_url = (base_url or os.environ.get("MANDALA_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _budget(current: httpx.Timeout, seconds: float | None) -> httpx.Timeout:
        """This request's timeout, when the call needs longer than the default.

        Only ever widens, and returns ``current`` unchanged when it does not.
        A caller who handed us a patient client of their own keeps it, and a
        call asking for less than the client already allows changes nothing —
        the point is to stop the transport giving up on a deadline the platform
        is still honouring, not to impose a new one.

        ``connect`` and ``pool`` are left alone: neither has anything to do with
        how long the *guest* takes, and shortening the wait for a connection is
        not what a long-running command asked for. A ``None`` on either half of
        the budget already means "wait indefinitely", which is longer than
        anything we would ask for.
        """
        if seconds is None or current.read is None or current.write is None:
            return current
        if current.read >= seconds and current.write >= seconds:
            return current
        return httpx.Timeout(
            connect=current.connect,
            read=max(current.read, seconds),
            write=max(current.write, seconds),
            pool=current.pool,
        )

    @staticmethod
    def _parse(resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    @staticmethod
    def _error(resp: httpx.Response) -> APIError:
        body: Any = None
        message = f"HTTP {resp.status_code}"
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("error"):
                message = str(body["error"])
        except ValueError:
            text = resp.text.strip()
            if text:
                message = text[:500]
        cls = _STATUS_ERRORS.get(resp.status_code, APIError)
        if cls is RateLimitError:
            return RateLimitError(
                message,
                status=resp.status_code,
                body=body,
                retry_after=_retry_after(resp),
            )
        return cls(message, status=resp.status_code, body=body)


def _timed_out(method: str, path: str, exc: httpx.TimeoutException) -> TimeoutError:
    """The SDK's own error for a request the transport stopped waiting on.

    ``httpx.TimeoutException`` is not a :class:`MandalaError`, so letting it out
    raw means ``except MandalaError`` — the one handler the README tells callers
    to write — misses the failure entirely.

    The distinction in the message matters: nothing has been cancelled. The
    command is still running in the guest and the file is still being written;
    what was lost is this request's view of the outcome.
    """
    return TimeoutError(
        f"{method} {path} did not answer within the client's timeout "
        f"({type(exc).__name__}). This is the SDK giving up, not the platform "
        "refusing — the work may still be running."
    )


def _retry_after(resp: httpx.Response) -> float | None:
    """``Retry-After`` in seconds, or ``None`` if it was not usable.

    Only the delta-seconds form is read. The HTTP-date form is legal and this
    surface does not send it, and guessing at a date against a clock that may
    disagree with the server's is worse than saying nothing.

    ``nan`` and ``inf`` parse as floats but are not delays: this value is handed
    to ``time.sleep``, where an infinity blocks forever and a ``nan`` raises. A
    negative is a delay that has already passed, which is ``0``. So anything
    that is not a finite number becomes ``None`` — the header was there, and it
    was not usable, which is exactly what this returns ``None`` to say.
    """
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds):
        return None
    return max(seconds, 0.0)


class Transport(_BaseTransport):
    """Blocking transport."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(api_key, base_url)
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """One request. ``timeout`` widens this call's budget; see :meth:`_budget`."""
        try:
            resp = self._http.request(
                method,
                self._url(path),
                json=json,
                params=params,
                content=content,
                headers=self._headers,
                timeout=self._budget(self._http.timeout, timeout),
            )
        except httpx.TimeoutException as exc:
            raise _timed_out(method, path, exc) from exc
        if resp.is_success:
            return resp
        raise self._error(resp)

    def json(self, method: str, path: str, **kw: Any) -> Any:
        return self._parse(self.request(method, path, **kw))

    def listing(self, path: str, **kw: Any) -> tuple[Any, int | None]:
        """A collection read, and whether the platform had to answer it short.

        Separate from :meth:`json` because the news is in a header, and a header
        nothing reads is not a warning. See :data:`INCOMPLETE_HEADER`.
        """
        resp = self.request("GET", path, **kw)
        return self._parse(resp), _incomplete(resp)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()


class AsyncTransport(_BaseTransport):
    """Non-blocking transport."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(api_key, base_url)
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=timeout)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """One request. ``timeout`` widens this call's budget; see :meth:`_budget`."""
        try:
            resp = await self._http.request(
                method,
                self._url(path),
                json=json,
                params=params,
                content=content,
                headers=self._headers,
                timeout=self._budget(self._http.timeout, timeout),
            )
        except httpx.TimeoutException as exc:
            raise _timed_out(method, path, exc) from exc
        if resp.is_success:
            return resp
        raise self._error(resp)

    async def json(self, method: str, path: str, **kw: Any) -> Any:
        return self._parse(await self.request(method, path, **kw))

    async def listing(self, path: str, **kw: Any) -> tuple[Any, int | None]:
        """A collection read, and whether the platform had to answer it short.

        Separate from :meth:`json` because the news is in a header, and a header
        nothing reads is not a warning. See :data:`INCOMPLETE_HEADER`.
        """
        resp = await self.request("GET", path, **kw)
        return self._parse(resp), _incomplete(resp)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()
