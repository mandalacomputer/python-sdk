"""HTTP transports for the Mandala Computer API.

The sync and async transports differ only in where the awaits go. Everything
that decides *meaning* — key resolution, URL building, and which exception a
status maps to — lives on the shared base, so the two can never disagree about
what a 402 is or where a request goes.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import AsyncGenerator, Generator, Mapping
from typing import Any

import httpx

from ._exceptions import (
    APIError,
    AuthenticationError,
    ConflictError,
    ConnectionError,
    ConnectionInterruptedError,
    FileTooLargeError,
    GatewayTimeoutError,
    MandalaError,
    MoveRequiredError,
    NotFoundError,
    OriginResponseError,
    OriginTLSError,
    OriginUnreachableError,
    PermissionDeniedError,
    PlanLimitError,
    RangeNotSatisfiableError,
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

#: The most one file request moves, mirroring the platform's ``guestFileMax``.
#:
#: A limit on the *request*, not on the file. The bytes cross the guest agent's
#: one connection in chunks and a transfer holds it for as long as it takes, so
#: the ceiling falls on what a single exchange carries. An upload meets it as a
#: limit on the file, because ``PUT`` takes no range; a download meets it as a
#: limit on the window, which is what makes a file of any size reachable — see
#: :meth:`~mandala_computer.Computer.read_file_part`.
FILE_SIZE_LIMIT = 64 * 1024 * 1024

#: How much of a file :meth:`~mandala_computer.Computer.download_file` asks for
#: at a time.
#:
#: Well under :data:`FILE_SIZE_LIMIT`, which is the most it *could* ask for. The
#: ceiling is the point at which one request stops being served, not the point
#: at which it stops being a good idea: a part is held whole in memory on both
#: sides, and it is also the unit a failure costs you, since a part that dies
#: mid-transfer is re-fetched from its start. Eight of these is one ceiling's
#: worth of round trips, which is a cheap price for both.
FILE_PART_SIZE = 8 * 1024 * 1024

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
    # Both file statuses, and both of them about the same ceiling seen from
    # different sides — see FILE_SIZE_LIMIT. Their own classes rather than a
    # bare APIError because each one has a specific next move attached: a 413
    # says ask for part of it, and a 416 hands over the length to ask against.
    413: FileTooLargeError,
    416: RangeNotSatisfiableError,
    429: RateLimitError,
    # A fan-out listing that would have been short, without allow_partial. Its
    # own class rather than a bare APIError because it is the one 5xx on this
    # surface that is not a fault: nothing is broken from the caller's side, the
    # platform is declining to hand over a list it knows is incomplete.
    503: UnavailableError,
    # Neither of these is the platform's answer. Both are a hop in front of it
    # saying it waited long enough — 504 from an ordinary proxy, 524 from
    # Cloudflare, which is what serves app.mandala.computer. Same class because
    # they are the same event to a caller, and because which one arrives depends
    # on which hop gave up first rather than on anything they did.
    504: GatewayTimeoutError,
    524: GatewayTimeoutError,
    # The rest of what an edge answers on its own, and the opposite event to the
    # pair above despite the neighbouring numbers: those mean the platform was
    # reached and did not answer in time, these mean it was never reached.
    #
    # 520 is NOT one of them, which is the trap in this range. It means the
    # platform WAS reached and answered unreadably, so it belongs with neither
    # group and gets a class of its own — see OriginResponseError for what a
    # wrong answer here costs.
    520: OriginResponseError,
    521: OriginUnreachableError,
    522: OriginUnreachableError,
    523: OriginUnreachableError,
    # Their own class, not more entries on the one above: an unreachable origin
    # is a passing outage and these are a deployment somebody has to fix, so a
    # caller asking "should I try again" needs opposite answers.
    525: OriginTLSError,
    526: OriginTLSError,
}

#: What a caller is told when a proxy abandoned their request and named nothing.
#:
#: Used only where the response carried no message of the platform's own — an
#: empty body, or an intermediary's HTML error page. Where the platform *did*
#: name the failure, its own words are better than these and are kept; see
#: :meth:`_error`.
#:
#: Measured against ``app.mandala.computer`` on 2026-08-20: Cloudflare
#: content-negotiates its error page, and every request from this client asks
#: for JSON, so the body arrived **empty** and ``str(e)`` read ``HTTP 524`` — a
#: caller with nowhere to go, which is where the ceiling below cost real
#: debugging time. Stated as a measurement rather than a rule, because it is a
#: property of an edge this SDK does not own: a proxy that answers 5xx with a
#: structured body instead (RFC 9457 names its fields ``title`` and ``detail``,
#: not ``error``) still lands here, which is safe — a proxy's account of the
#: platform is not the platform's, and ``e.body`` keeps it either way.
#:
#: Worded for any route, because any of them can meet the ceiling. The exec
#: sentence is hedged for the same reason — most callers arrive here from one,
#: but a listing or a screenshot can too, and telling the caller of a GET that
#: their command is still running would be a confident falsehood.
GATEWAY_TIMEOUT_MESSAGE = (
    "a proxy in front of the platform gave up waiting for it to answer. Nothing "
    "was cancelled: the platform never saw this deadline, so any work the "
    "request had already started carries on. Most often that is a foreground "
    "exec(), which ends this way after about two minutes however long a timeout "
    "it was given — the ceiling belongs to the proxy, not to the platform or to "
    "this client, so raising the timeout cannot buy time from it and start_exec() "
    "is the way to run something slower. After one of those, the next call on "
    "that computer may report the guest agent as busy with the command that "
    "outlived the request"
)

#: What a caller is told when a proxy could not reach the platform at all.
#:
#: No "did the platform name it" guard on any of the three below, unlike the
#: gateway pair, and the asymmetry is the point: on these the edge never got an
#: answer out of the platform, so a body cannot carry its account of what
#: happened. There is nothing to defer to.
ORIGIN_UNREACHABLE_MESSAGE = (
    "a proxy in front of the platform could not reach it. Almost always that "
    "means the request was never sent, so nothing was started and there is no "
    "work on the other side of this to account for — unlike a gateway timeout. "
    "Almost, rather than never, because a connection can also time out after it "
    "was established, and bytes already on the wire are not unsent because the "
    "answer never came back: retry a read freely, and look before retrying "
    "something that creates. Usually this is the platform restarting or a short "
    "outage, which clears on its own; if it persists the platform is down, and "
    "waiting is the only thing that helps"
)

#: What a caller is told when the platform was reached and the exchange broke.
ORIGIN_RESPONSE_MESSAGE = (
    "the platform received the request and the exchange then broke on the way "
    "back — an empty or unreadable response, a connection dropped before the "
    "headers, an origin that stopped part-way. Unlike an unreachable origin, the "
    "request did arrive, so it may have been carried out in full, in part, or "
    "not at all. Retrying a read costs nothing; before retrying anything that "
    "creates something, look at whether the first attempt took effect"
)

#: What a caller is told when the edge and the platform cannot agree on TLS.
#:
#: The one edge failure with no "wait and see" in it, which is why it is raised
#: rather than waited out — see ``_FATAL_WHILE_WAITING``.
ORIGIN_TLS_MESSAGE = (
    "a proxy in front of the platform could not complete a TLS handshake with "
    "it, so the request was never sent. This is a misconfigured deployment "
    "rather than a passing outage — an expired or mismatched certificate fails "
    "the same way on every retry, so report it rather than waiting it out"
)

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


#: ``Content-Range: bytes 0-1048575/2147483648`` — which bytes came back, and
#: how many the file has. Only on a 206.
_CONTENT_RANGE = re.compile(r"\s*bytes\s+(\d+)\s*-\s*(\d+)\s*/\s*(\d+)\s*", re.IGNORECASE)

#: The three single-range requests :func:`~mandala_computer._api.files_range`
#: emits: a bounded window, everything from one position, or a suffix.
_REQUESTED_RANGE = re.compile(r"\s*bytes\s*=\s*(?:(\d+)\s*-\s*(\d*)|-\s*(\d+))\s*", re.IGNORECASE)

#: ``Content-Range: bytes */2147483648`` — the same header on a 416, where there
#: is no window to name and the length is the entire point of the answer.
_UNSATISFIED_RANGE = re.compile(r"\s*bytes\s+\*\s*/\s*(\d+)\s*", re.IGNORECASE)


def _refused_size(resp: httpx.Response) -> int | None:
    """The file's length off a 416, or ``None`` if the refusal did not say.

    ``None`` rather than a guess, and a guess is what any default would be: the
    caller is about to ask again, and a zero standing in for "the header was
    dropped" would tell them the file is empty.
    """
    m = _UNSATISFIED_RANGE.fullmatch(resp.headers.get("content-range", ""))
    return int(m.group(1)) if m else None


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
    def _phase_ceiling(timeout: httpx.Timeout, err: BaseException) -> float | None:
        """The client's own limit for the PHASE that actually timed out.

        What a poll's deadline cap is measured against: a cap wider than this
        never tightened anything, so a timeout under it belongs to the platform
        rather than to the wait. Per phase, because httpx has four and they
        differ — comparing every timeout against ``read`` alone got both
        directions wrong (adversarial review, OPL-3835). With ``connect=4.8``
        and ``read=60``, a five-second budget tightens neither, yet a genuine
        connect stall at 4.8s was excused as ours; with ``connect=60`` and
        ``read=1``, a two-second budget DOES tighten connect, and its own cap
        firing was blamed on the platform.

        Read off ``__cause__``, which is the httpx exception ``_timed_out``
        raises from, so no exception type or signature has to change.
        """
        # WALKED, not read once. A custom transport or hook that wraps a
        # phase-specific timeout in the base `TimeoutException` hid the phase one
        # level down, and the wait then reported the fleet unreachable rather
        # than recognising its own tightened cap (adversarial review,
        # OPL-3835). Bounded, because a hand-built chain can loop.
        cause: BaseException | None = err
        for _ in range(10):
            cause = cause.__cause__ if cause is not None else None
            if cause is None or isinstance(
                cause,
                (
                    httpx.ConnectTimeout,
                    httpx.ReadTimeout,
                    httpx.WriteTimeout,
                    httpx.PoolTimeout,
                ),
            ):
                break
        if isinstance(cause, httpx.ConnectTimeout):
            value = timeout.connect
        elif isinstance(cause, httpx.WriteTimeout):
            value = timeout.write
        elif isinstance(cause, httpx.PoolTimeout):
            value = timeout.pool
        elif isinstance(cause, httpx.ReadTimeout):
            value = timeout.read
        else:
            # Not a phase we can name — an httpx.TimeoutException with no
            # subtype, or an error raised without a cause. UNKNOWN, and the
            # caller treats unknown as "cannot claim it".
            return None
        # A named phase the client puts no limit on is INFINITE, not unknown,
        # and collapsing the two into None was a regression (/code-review,
        # OPL-3835): against a caller-supplied client with no timeout at all —
        # the case `_cap_budget`'s own docstring exists for — the wait's cap is
        # then the ONLY thing that can have fired, and reading it as unknown
        # made a wait blame its own deadline on the fleet.
        return math.inf if value is None else float(value)

    @staticmethod
    def _cap_budget(current: httpx.Timeout, seconds: float | None) -> httpx.Timeout:
        """Tighten one request's timeouts to the time its caller has left.

        An UPPER BOUND ON EACH OPERATION, and deliberately not a wall-clock
        deadline for the request — httpx's four settings cannot express one, and
        a commit on this branch got that wrong in both directions before it was
        caught (second adversarial review, OPL-3835). ``read`` is an INACTIVITY
        timeout: httpcore reads it once and then applies it to every chunk in
        ``_receive_response_body``, so it restarts on each one and a slow but
        steady response can outlast any value set here. Dividing the budget
        across the four to make them sum to it was arithmetic about a total that
        does not exist, and it broke the callers it was meant to help — a
        legitimate three-second refresh with eight seconds left began failing at
        two, and `Computer.wait_until_built` and `wait_until_running` do not
        catch a timeout on that refresh.

        What it is still for is the case that motivated it: a `wait(timeout=1)`
        inheriting the client's own sixty-second read, or a caller-supplied
        client with no timeout at all. The loop's deadline check is what bounds
        the wait; this only keeps a single request from sitting far past it.

        THE OVERSHOOT IS BOUNDED BUT NOT SMALL: the four phases are sequential,
        so a request can spend up to four times what was left, and a wait can
        return or raise that far past the deadline it documents
        (/code-review, OPL-3835). Named rather than fixed, because the fix is
        not arithmetic on these four numbers — that is what the reverted commit
        tried — but a deadline enforced around the whole request, which is a
        change to every caller and its own piece of work. Four times a shrinking
        remainder is a bad hour; four copies of a full sixty-second read, which
        is what the uncapped version gave, is a worse one.
        """
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
    def _binary_body(
        method: str,
        path: str,
        resp: httpx.Response,
        content_types: tuple[str, ...],
    ) -> bytes:
        """Read bytes only from a response of the promised wire type.

        Older platform versions omitted ``Content-Type``, so absence remains
        compatible. An explicit JSON or HTML type is not a file or screenshot,
        however, and returning its bytes would disguise a proxy/login response
        as successful binary data.
        """
        content_type = resp.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type and not any(
            content_type == expected or expected.endswith("/") and content_type.startswith(expected)
            for expected in content_types
        ):
            expected = " or ".join(content_types)
            raise MandalaError(
                f"{method} {path} answered {content_type}, not binary content ({expected})"
            )
        return resp.content

    @staticmethod
    def _served_range(
        method: str,
        path: str,
        resp: httpx.Response,
        data: bytes,
        requested_range: str,
    ) -> tuple[int, int | None, bool]:
        """Where in the file these bytes are — ``(offset, total, partial)``.

        The status is what says whether a window came back, not the presence of
        the header: a 200 to a request that carried a ``Range`` is the platform
        *ignoring* it, which it does for a file whose length the guest cannot
        report. There are no positions to name in one of those and no total to
        promise, so the answer is the whole file at offset zero.

        A 206 whose ``Content-Range`` is missing or unreadable is refused rather
        than assumed. The header is the only thing that says which bytes these
        are, and every mistake available without it is silent: a window taken for
        the start of the file writes the middle of a download over its beginning
        and reports success. A proxy that drops the header on the way back is the
        way this happens — see ``passThrough`` in the platform's ``lib/hvproxy``,
        where it is forwarded by name.

        An empty window is refused with it, and that one is not fussiness. The
        length check below passes an ``A-B`` whose ``B`` is before its ``A`` when
        the body is empty, and a window of no bytes ends exactly where it began —
        so :meth:`~mandala_computer.Computer.download_file` would ask for it
        again, receive it again, and never stop. Requiring at least one byte here
        is what makes the paging loop's advance a property of the parse rather
        than something every caller has to check for itself.
        """
        if resp.status_code != 206:
            return 0, None, False
        raw = resp.headers.get("content-range", "")
        m = _CONTENT_RANGE.fullmatch(raw)
        if m is None:
            raise MandalaError(
                f"{method} {path} answered 206 Partial Content with no readable "
                f"Content-Range ({raw or 'header absent'}). Which bytes those are is "
                "not knowable without it, so they are refused rather than guessed at."
            )
        first, last, total = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if last < first:
            raise MandalaError(
                f"{method} {path} answered 206 with Content-Range {raw.strip()}, whose "
                "window ends before it starts. A window holds at least one byte — a "
                "range naming none is a 416 — so there is nothing here to believe."
            )
        if last >= total:
            raise MandalaError(
                f"{method} {path} answered 206 with Content-Range {raw.strip()}, whose "
                f"{first}-{last} window does not fit inside a {total}-byte file. The "
                "window and total cannot both be true."
            )
        if last - first + 1 != len(data):
            raise MandalaError(
                f"{method} {path} sent {len(data)} bytes for the window "
                f"{first}-{last} its Content-Range names. Something between here and "
                "the platform altered the body, and the two cannot both be true."
            )
        asked = _REQUESTED_RANGE.fullmatch(requested_range)
        if asked is None:
            raise MandalaError(
                f"{method} {path} answered a partial request whose Range "
                f"{requested_range!r} cannot be checked"
            )
        if asked.group(3) is not None:
            lower = max(total - int(asked.group(3)), 0)
            upper: int | None = total - 1
        else:
            lower = int(asked.group(1))
            upper = int(asked.group(2)) if asked.group(2) else None
        if first < lower or upper is not None and last > upper:
            raise MandalaError(
                f"{method} {path} served the wrong bytes at the wrong place: "
                f"Content-Range {raw.strip()} falls outside the requested "
                f"Range {requested_range}."
            )
        return first, total, True

    @staticmethod
    def _error(resp: httpx.Response) -> APIError:
        body: Any = None
        message = f"HTTP {resp.status_code}"
        # Whether the PLATFORM named this failure, as opposed to the message
        # being our fallback or some intermediary's error page. Only the
        # structured form counts: a JSON body with an ``error`` in it is this
        # surface answering, and nothing else on this path is.
        named = False
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("error"):
                message = str(body["error"])
                named = True
        except ValueError:
            text = resp.text.strip()
            if text:
                message = text[:500]
                # Kept on the exception even where the wording below replaces
                # it. An edge's error page is the wrong thing to show a caller
                # and the right thing to still have when one asks support about
                # it — a Cloudflare Ray ID lives in that HTML and nowhere else,
                # and substituting the message used to drop it on the floor.
                body = message
        cls = _STATUS_ERRORS.get(resp.status_code, APIError)

        def substituted(said: str) -> str:
            """Our wording, carrying the status it stands in for.

            The number matters on a message we wrote in a way it does not on one
            the platform wrote. Four classes cover eight statuses between them
            and three of those share a sentence, so a log line holding only
            ``str(e)`` could no longer tell 521 from 523 — which it could before
            this SDK started explaining them.
            """
            return f"{said} (HTTP {resp.status_code})"

        # All four set `message` rather than returning, so the status above is
        # appended in one place and a fifth class cannot forget it.
        if cls is OriginResponseError and not named:
            # Guarded, where the two below are not, and the difference is which
            # of them the platform could have spoken through. A 520 is its own
            # answer arriving mangled, so a body that parsed as this surface's
            # JSON plausibly IS its account. On 521-526 it provably cannot be.
            message = substituted(ORIGIN_RESPONSE_MESSAGE)
        elif cls is OriginTLSError:
            message = substituted(ORIGIN_TLS_MESSAGE)
        elif cls is OriginUnreachableError:
            message = substituted(ORIGIN_UNREACHABLE_MESSAGE)
        elif cls is GatewayTimeoutError and not named:
            # The substitution is worth making twice over and worth NOT making a
            # third time. An empty body leaves "HTTP 524", which says nothing; an
            # HTML body leaves 500 characters of a proxy's boilerplate, which is
            # worse. But a 504 can also come from a hop that speaks this
            # surface's JSON — "upstream unavailable before dispatch" is a more
            # specific true thing than anything written here, and replacing it
            # would be this client overwriting the platform with a guess.
            message = substituted(GATEWAY_TIMEOUT_MESSAGE)
        if cls is ConflictError:
            # The 409 that is an offer, told apart by its body — see
            # MoveRequiredError. Never given a substituted message: the
            # platform's sentence here is the whole account of what will not fit
            # and what moving costs, written for whoever has to agree to it.
            offer = _move_offer(body)
            if offer is not None:
                return MoveRequiredError(
                    message,
                    status=resp.status_code,
                    body=body,
                    move_possible=offer,
                )
        if cls is RateLimitError:
            return RateLimitError(
                message,
                status=resp.status_code,
                body=body,
                retry_after=_retry_after(resp),
            )
        if cls is RangeNotSatisfiableError:
            # The one refusal on this surface that answers the question it is
            # refusing. Parsed here rather than left on the response, because the
            # response is gone by the time a caller has the exception.
            return RangeNotSatisfiableError(
                message,
                status=resp.status_code,
                body=body,
                size=_refused_size(resp),
            )
        return cls(message, status=resp.status_code, body=body)


def _move_offer(body: object) -> bool | None:
    """``move.possible`` off a refusal that carries one, or ``None``.

    Shape-checked rather than trusted, because it decides both what a caller is
    told to do next and whether retrying is worth anything: a body whose ``move``
    is a string, or a dict with no boolean ``possible``, has to read as "not that
    refusal" rather than as a move that is impossible. Absent and malformed get
    the same answer, and it is the conservative one — an ordinary
    :class:`~mandala_computer.ConflictError`, which is what this was before.
    """
    if not isinstance(body, dict):
        return None
    move = body.get("move")
    if not isinstance(move, dict):
        return None
    possible = move.get("possible")
    if move.get("required") is not True or not isinstance(possible, bool):
        return None
    return possible


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
    cls = _STATUS_ERRORS.get(status, APIError)
    if cls in (OriginUnreachableError, OriginResponseError, OriginTLSError):
        # For the reason below, one step further: this status arrived ON a
        # response, so the claim that the request never reached the platform is
        # not merely unproven here, it is contradicted.
        cls = APIError
    if cls is GatewayTimeoutError:
        # Not here, and the reason is the arrival itself. This status did not
        # come off a response — it came out of an event on a stream the platform
        # opened, answered 200, and successfully delivered. That is proof no
        # proxy abandoned anything, which is the one thing
        # :class:`~mandala_computer.GatewayTimeoutError` asserts. A downstream
        # 504 the platform is REPORTING and an edge that stopped waiting are
        # different events, and a caller branching on the class to decide
        # whether its work survived would be answered wrongly here.
        cls = APIError
    return cls(message, status=status)


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


#: httpx exceptions that can only be raised before a byte of the request goes out.
#:
#: The allow-list ``_request_failed`` fails closed against, and httpx is what
#: makes it short and honest: these classes name the connect phase outright,
#: where the two TypeScript clients have to read undici's cause chain to find
#: it. ``ConnectError`` covers DNS, a refused socket and a TLS handshake;
#: ``ProxyError`` and ``UnsupportedProtocol`` both fail before a request is
#: built.
#:
#: Everything else httpx raises describes a request already on the wire —
#: ``ReadError``, ``WriteError``, ``RemoteProtocolError`` — or is something this
#: SDK has no rule for, and both take the cautious answer.
#:
#: The two timeouts are here and are unreachable through this function today,
#: which is deliberate rather than an oversight. Every ``httpx.TimeoutException``
#: is caught one clause earlier and becomes :class:`TimeoutError`, which carries
#: the same pair of answers :class:`ConnectionInterruptedError` does — fatal to
#: :func:`is_transient`, polled through — so a timeout was never part of this
#: hole. They are named so that this tuple states the phase correctly on its own
#: terms, and so that reordering those clauses cannot quietly turn a connect
#: timeout into a possible dispatch.
_NEVER_DISPATCHED = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)


def _request_failed(method: str, path: str, exc: httpx.RequestError) -> ConnectionError:
    """The SDK's error for a request that failed before an HTTP response arrived.

    A :class:`ConnectionError` rather than a bare :class:`MandalaError` since
    OPL-3724. The base class was catchable by the SDK-wide handler and by
    nothing more specific, and the retry predicates could not name it at all —
    which is why this SDK had no public :func:`is_transient` while the other two
    both listed their equivalent class in theirs.

    **Two classes since OPL-3855**, because ``httpx.RequestError`` is two
    different outcomes wearing one name. ``ConnectError`` means nothing was
    dispatched and even a ``create`` may be replayed; ``ReadError`` and
    ``RemoteProtocolError`` happen with the request already at the platform, so
    it may have acted and the answer is what was lost.

    **Fails closed.** Only the classes above are read as connect-phase, and
    everything else — anything unrecognised, anything httpx adds later — is
    treated as possibly dispatched. The two wrong answers do not cost the same:
    calling a connect failure a possible dispatch costs one retry a caller
    could have made blind, and calling a lost response a connect failure costs a
    second billable computer.
    """
    if isinstance(exc, _NEVER_DISPATCHED):
        return ConnectionError(f"{method} {path} could not complete ({type(exc).__name__}): {exc}")
    return ConnectionInterruptedError(
        f"{method} {path} failed after the request was sent "
        f"({type(exc).__name__}): {exc}. It may have been received, so treat "
        "anything it would have changed as unknown rather than undone."
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

    def phase_ceiling(self, err: BaseException) -> float | None:
        """See :meth:`_BaseTransport._phase_ceiling`."""
        return self._phase_ceiling(self._http.timeout, err)

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
    ) -> Generator[SSEEvent, None, None]:
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

    def binary(
        self,
        method: str,
        path: str,
        *,
        accept: str,
        content_types: tuple[str, ...],
        **kw: Any,
    ) -> bytes:
        """A successful raw body with an explicit binary ``Accept`` type."""
        resp = self.request(method, path, headers={"Accept": accept}, **kw)
        return self._binary_body(method, path, resp, content_types)

    def binary_part(
        self,
        method: str,
        path: str,
        *,
        accept: str,
        content_types: tuple[str, ...],
        headers: Mapping[str, str],
        **kw: Any,
    ) -> tuple[bytes, int, int | None, bool]:
        """:meth:`binary`, for a request that asked for a window of the body.

        Returns the bytes and where they sit — ``(data, offset, total, partial)``,
        which is :class:`~mandala_computer.FilePart`'s four fields in its own
        order. Kept as a tuple so this layer stays about the wire; what the
        numbers *mean* is written down once, on the record the caller gets.
        """
        resp = self.request(method, path, headers={**headers, "Accept": accept}, **kw)
        data = self._binary_body(method, path, resp, content_types)
        offset, total, partial = self._served_range(method, path, resp, data, headers["Range"])
        return data, offset, total, partial

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

    def phase_ceiling(self, err: BaseException) -> float | None:
        """See :meth:`_BaseTransport._phase_ceiling`."""
        return self._phase_ceiling(self._http.timeout, err)

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
    ) -> AsyncGenerator[SSEEvent, None]:
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

    async def binary(
        self,
        method: str,
        path: str,
        *,
        accept: str,
        content_types: tuple[str, ...],
        **kw: Any,
    ) -> bytes:
        """A successful raw body with an explicit binary ``Accept`` type."""
        resp = await self.request(method, path, headers={"Accept": accept}, **kw)
        return self._binary_body(method, path, resp, content_types)

    async def binary_part(
        self,
        method: str,
        path: str,
        *,
        accept: str,
        content_types: tuple[str, ...],
        headers: Mapping[str, str],
        **kw: Any,
    ) -> tuple[bytes, int, int | None, bool]:
        """:meth:`binary`, for a request that asked for a window of the body.

        Returns the bytes and where they sit — ``(data, offset, total, partial)``,
        which is :class:`~mandala_computer.FilePart`'s four fields in its own
        order. Kept as a tuple so this layer stays about the wire; what the
        numbers *mean* is written down once, on the record the caller gets.
        """
        resp = await self.request(method, path, headers={**headers, "Accept": accept}, **kw)
        data = self._binary_body(method, path, resp, content_types)
        offset, total, partial = self._served_range(method, path, resp, data, headers["Range"])
        return data, offset, total, partial

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
