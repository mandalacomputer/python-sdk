"""HTTP transports for the Mandala Computer API.

The sync and async transports differ only in where the awaits go. Everything
that decides *meaning* — key resolution, URL building, and which exception a
status maps to — lives on the shared base, so the two can never disagree about
what a 402 is or where a request goes.
"""

from __future__ import annotations

import math
import os
from collections.abc import AsyncIterator, Iterator, Mapping
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
from ._sse import SSEDecoder, SSEEvent

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

#: A request with no deadline at all, for the non-streaming agent loop.
#:
#: Not a very large number: a run is minutes of clicking with no upper bound
#: anybody can name, and any finite guess would end every run longer than the
#: guess at exactly the same place. ``agent_once`` holds one request open for
#: the whole run and answers only at the end, so nothing crosses the connection
#: to time a shorter budget against — the ordinary deadline is not a safety net
#: there, it is a cut-off. Abandoning the call is what stops one early.
#:
#: The streaming form does have something to time against, which is why it uses
#: :data:`STREAM_IDLE_TIMEOUT` instead.
NO_DEADLINE = math.inf

#: How long a stream may say nothing before the transport gives up on it.
#:
#: An httpx ``read`` budget is per-chunk idle time, not total run duration, so
#: this bounds the silence rather than the run: a stream that keeps arriving
#: runs as long as it likes. That distinction is what makes a finite number
#: right here and wrong for :data:`NO_DEADLINE` — the platform sends
#: ``: keepalive`` every 10s precisely so a quiet run still ticks, and six
#: missed heartbeats is a connection that is gone rather than one that is busy.
#:
#: Without it, a connection dropped without a FIN — a NAT rebind, a load
#: balancer reaping an idle socket, a laptop suspended mid-run — leaves the
#: caller blocked forever on a stream nothing will ever arrive on, which the
#: generator cannot be closed out of from inside the call that is waiting.
STREAM_IDLE_TIMEOUT = 60.0

#: The header carrying the caller's own model key. Never stored by the platform,
#: never metered, and never held on this client either — see
#: :meth:`Computer.agent`.
MODEL_KEY_HEADER = "X-Model-Key"

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
        if seconds == NO_DEADLINE:
            # The maximal widening, and the one this cannot express as a number:
            # httpx spells "wait indefinitely" as None rather than as infinity,
            # which it would reject.
            return httpx.Timeout(connect=current.connect, read=None, write=None, pool=current.pool)
        if seconds is None:
            return current

        def widened(value: float | None) -> float | None:
            return None if value is None else max(value, seconds)

        read = widened(current.read)
        write = widened(current.write)
        if read == current.read and write == current.write:
            return current
        return httpx.Timeout(
            connect=current.connect,
            read=read,
            write=write,
            pool=current.pool,
        )

    @staticmethod
    def _cap_budget(current: httpx.Timeout, seconds: float | None) -> httpx.Timeout:
        """Cap every phase of one request to the time its caller has left."""
        if seconds is None:
            return current
        cap = max(seconds, 0.001)

        def capped(value: float | None) -> float:
            return cap if value is None else min(value, cap)

        return httpx.Timeout(
            connect=capped(current.connect),
            read=capped(current.read),
            write=capped(current.write),
            pool=capped(current.pool),
        )

    @staticmethod
    def _parse(resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _sent(self, headers: Mapping[str, str] | None) -> dict[str, str]:
        """This request's headers — the client's, plus whatever the call adds.

        The call's win on a clash, which is what makes the stream able to ask
        for ``text/event-stream`` over the client-wide ``application/json``.
        """
        return {**self._headers, **(headers or {})}

    @staticmethod
    def _is_event_stream(resp: httpx.Response) -> bool:
        """Whether this is a stream to frame, read off the content type alone.

        The captive-portal case, on the one route with no JSON parse to catch
        it: a proxy that answered instead of the platform sends an HTML page,
        which contains no ``data:`` lines, so it frames to a stream of no events
        and surfaces as "the run ended without a result" — a sentence about the
        platform, describing something that never reached it.

        Deliberately does not touch the body. A streaming response has not read
        one yet, and reading it is the caller's to do — and to await, on the
        half of this file where that is a different verb.
        """
        return "text/event-stream" in resp.headers.get("content-type", "").lower()

    @staticmethod
    def _not_a_stream(method: str, path: str, resp: httpx.Response) -> MandalaError:
        """The complaint, once the body behind it has been read."""
        content_type = resp.headers.get("content-type") or "no content type"
        return MandalaError(
            f"{method} {path} answered {content_type}, not an event stream: "
            f"{resp.text.strip()[:200]}"
        )

    @staticmethod
    def _not_an_object(method: str, path: str, resp: httpx.Response) -> MandalaError:
        """The complaint for a 200 that carried no object to read a result out of."""
        content_type = resp.headers.get("content-type") or "no content type"
        return MandalaError(
            f"{method} {path} answered {content_type}, not a JSON object: {resp.text.strip()[:200]}"
        )

    @staticmethod
    def _not_an_array(method: str, path: str, resp: httpx.Response) -> MandalaError:
        """The complaint for a collection route that didn't return object rows."""
        content_type = resp.headers.get("content-type") or "no content type"
        return MandalaError(
            f"{method} {path} answered {content_type}, not a JSON array of objects: "
            f"{resp.text.strip()[:200]}"
        )

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


def error_for_status(status: int, message: str) -> APIError:
    """The exception a status deserves, for a failure that arrived in a stream.

    The agent loop reports its own failures as events rather than as a status —
    the response was a 200 and the run went wrong afterwards — so the mapping
    every other route gets for free has to be reached for by hand here. Without
    it the one failure that reaches a caller from inside a stream is the one
    their ``except AuthenticationError`` cannot catch.

    A 429 arriving this way has no ``Retry-After`` to carry, because the header
    belonged to a response that was a success. So this is the one path that
    builds a :class:`~mandala_computer.RateLimitError` whose
    :attr:`~mandala_computer.RateLimitError.retry_after` is ``None`` — see it
    there, rather than inventing a delay the platform did not name.
    """
    return _STATUS_ERRORS.get(status, APIError)(message, status=status)


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


def _request_failed(method: str, path: str, exc: httpx.RequestError) -> MandalaError:
    """The SDK's error for a request that failed before an HTTP response arrived."""
    return MandalaError(f"{method} {path} could not complete ({type(exc).__name__}): {exc}")


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
        timeout_cap: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One request, with optional widening and a final per-phase cap."""
        try:
            resp = self._http.request(
                method,
                self._url(path),
                json=json,
                params=params,
                content=content,
                headers=self._sent(headers),
                timeout=self._cap_budget(self._budget(self._http.timeout, timeout), timeout_cap),
            )
        except httpx.TimeoutException as exc:
            raise _timed_out(method, path, exc) from exc
        except httpx.RequestError as exc:
            raise _request_failed(method, path, exc) from exc
        if resp.is_success:
            return resp
        raise self._error(resp)

    def sse(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Iterator[SSEEvent]:
        """A route that answers with a stream of events rather than a result.

        Yielded rather than collected, so a caller can report progress while the
        run is going: an agent run is minutes of clicking, and something that
        says nothing until it is over cannot be told from a hang.

        No deadline on the run, but a bound on its silence — see
        :data:`STREAM_IDLE_TIMEOUT`. Closing the generator is what stops one
        early, and it closes the response with it.
        """
        sent = {**(headers or {}), "Accept": "text/event-stream"}
        try:
            with self._http.stream(
                method,
                self._url(path),
                json=json,
                headers=self._sent(sent),
                timeout=self._budget(self._http.timeout, STREAM_IDLE_TIMEOUT),
            ) as resp:
                if not resp.is_success:
                    # Read first: the body of a streamed response is not there
                    # until it is asked for, and the error message is in it.
                    resp.read()
                    raise self._error(resp)
                if not self._is_event_stream(resp):
                    resp.read()
                    raise self._not_a_stream(method, path, resp)
                decoder = SSEDecoder()
                for chunk in resp.iter_bytes():
                    yield from decoder.feed(chunk)
                tail = decoder.flush()
                if tail is not None:
                    yield tail
        except httpx.TimeoutException as exc:
            raise _timed_out(method, path, exc) from exc
        except httpx.RequestError as exc:
            raise _request_failed(method, path, exc) from exc

    def json(self, method: str, path: str, **kw: Any) -> Any:
        return self._parse(self.request(method, path, **kw))

    def json_object(self, method: str, path: str, **kw: Any) -> Mapping[str, Any]:
        """:meth:`json`, for a route whose answer is only useful as an object.

        The captive-portal case that :meth:`_is_event_stream` catches on the
        streaming half of a route, for the half that has no frames to notice it
        by: a proxy that answered instead of the platform sends an HTML page,
        which parses to ``None``, and the tolerant ``from_api`` constructors
        turn ``None`` into a well-formed record of nothing having happened. That
        is a sentence about the platform, describing something that never
        reached it — the same failure, and the same complaint, either way.
        """
        resp = self.request(method, path, **kw)
        data = self._parse(resp)
        if not isinstance(data, Mapping):
            raise self._not_an_object(method, path, resp)
        return data

    def json_array(self, method: str, path: str, **kw: Any) -> list[Mapping[str, Any]]:
        """A JSON route whose successful answer must be an array of objects."""
        resp = self.request(method, path, **kw)
        data = self._parse(resp)
        if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
            raise self._not_an_array(method, path, resp)
        return data

    def listing(self, path: str, **kw: Any) -> tuple[list[Mapping[str, Any]], int | None]:
        """A collection read, and whether the platform had to answer it short.

        Separate from :meth:`json` because the news is in a header, and a header
        nothing reads is not a warning. See :data:`INCOMPLETE_HEADER`.
        """
        resp = self.request("GET", path, **kw)
        data = self._parse(resp)
        if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
            raise self._not_an_array("GET", path, resp)
        return data, _incomplete(resp)

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
        timeout_cap: float | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """One request, with optional widening and a final per-phase cap."""
        try:
            resp = await self._http.request(
                method,
                self._url(path),
                json=json,
                params=params,
                content=content,
                headers=self._sent(headers),
                timeout=self._cap_budget(self._budget(self._http.timeout, timeout), timeout_cap),
            )
        except httpx.TimeoutException as exc:
            raise _timed_out(method, path, exc) from exc
        except httpx.RequestError as exc:
            raise _request_failed(method, path, exc) from exc
        if resp.is_success:
            return resp
        raise self._error(resp)

    async def sse(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[SSEEvent]:
        """A route that answers with a stream of events rather than a result.

        Yielded rather than collected, so a caller can report progress while the
        run is going: an agent run is minutes of clicking, and something that
        says nothing until it is over cannot be told from a hang.

        No deadline on the run, but a bound on its silence — see
        :data:`STREAM_IDLE_TIMEOUT`. Closing the generator is what stops one
        early, and it closes the response with it.
        """
        sent = {**(headers or {}), "Accept": "text/event-stream"}
        try:
            async with self._http.stream(
                method,
                self._url(path),
                json=json,
                headers=self._sent(sent),
                timeout=self._budget(self._http.timeout, STREAM_IDLE_TIMEOUT),
            ) as resp:
                if not resp.is_success:
                    await resp.aread()
                    raise self._error(resp)
                if not self._is_event_stream(resp):
                    await resp.aread()
                    raise self._not_a_stream(method, path, resp)
                decoder = SSEDecoder()
                async for chunk in resp.aiter_bytes():
                    for event in decoder.feed(chunk):
                        yield event
                tail = decoder.flush()
                if tail is not None:
                    yield tail
        except httpx.TimeoutException as exc:
            raise _timed_out(method, path, exc) from exc
        except httpx.RequestError as exc:
            raise _request_failed(method, path, exc) from exc

    async def json(self, method: str, path: str, **kw: Any) -> Any:
        return self._parse(await self.request(method, path, **kw))

    async def json_object(self, method: str, path: str, **kw: Any) -> Mapping[str, Any]:
        """:meth:`json`, for a route whose answer is only useful as an object.

        See :meth:`Transport.json_object`.
        """
        resp = await self.request(method, path, **kw)
        data = self._parse(resp)
        if not isinstance(data, Mapping):
            raise self._not_an_object(method, path, resp)
        return data

    async def json_array(self, method: str, path: str, **kw: Any) -> list[Mapping[str, Any]]:
        """A JSON route whose successful answer must be an array of objects."""
        resp = await self.request(method, path, **kw)
        data = self._parse(resp)
        if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
            raise self._not_an_array(method, path, resp)
        return data

    async def listing(self, path: str, **kw: Any) -> tuple[list[Mapping[str, Any]], int | None]:
        """A collection read, and whether the platform had to answer it short.

        Separate from :meth:`json` because the news is in a header, and a header
        nothing reads is not a warning. See :data:`INCOMPLETE_HEADER`.
        """
        resp = await self.request("GET", path, **kw)
        data = self._parse(resp)
        if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
            raise self._not_an_array("GET", path, resp)
        return data, _incomplete(resp)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()
