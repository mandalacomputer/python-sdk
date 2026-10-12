"""Private CDP implementation shared by the sync and async Anthropic drivers."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import math
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

import anyio
from anthropic.tools import ToolError
from websockets.asyncio.client import connect

from ._browser_connection import BrowserSessionLease, BrowserSessionPolicy
from ._exceptions import APIError

# Every event is routed as it arrives:
#
# - Bookkeeping that awaits nothing is done on arrival, in arrival order: which
#   frames and sessions are a tab's (attach, detach, destroy), target info,
#   frame notes, console and network logs, and which frame owns a download. So
#   a reply that follows a detach finds the frame gone, and a note that follows
#   a removal is not undone by it.
# - What then needs Chromium's replies runs as a task. Target setup and dialogs
#   start at once and never wait behind caller code. Paused requests and
#   downloads wait on caller code (the URL policy, the download decision), so
#   they run in lanes: a tab's own top-level document requests in one, so that
#   a navigation never waits behind the page's subresources, everything else
#   in another. A lane runs up to its concurrency at once; the rest wait their
#   turn in arrival order, trimmed to the fields their handler reads. Chromium
#   holds a paused request for as long as it takes, so waiting costs nothing.
#
# Only a page flooding the session faster than any policy could answer reaches
# a limit, and it ends the session rather than growing without bound.
_POLICY_CONCURRENCY = 256
_NAVIGATION_CONCURRENCY = 32
_EVENT_QUEUE_LIMIT = 8192
_EVENT_QUEUE_BYTES = 32 * 1024 * 1024
_TARGET_EVENT_LIMIT = 1024
_OWNERSHIP_LIMIT = 8192
_OWNERSHIP_LOOKUPS = 128
_OWNERSHIP_TARGETS = 128
_OWNERSHIP_DEADLINE = 5.0
_OWNERSHIP_ERROR = (
    "Browser request ownership could not be established. "
    "The session ended; create a new toolset to continue."
)
# A caller's URL policy runs on threads of the session's own, never the default
# executor that renewal and the sync computer's calls use. Navigation checks
# have threads of their own, which held subresource checks cannot occupy.
# A document back from the back/forward cache brings its frames back without
# reporting them; until its frame tree is read again, a download from a frame
# not yet tracked waits at most this long, and at most this many frames
# reported beneath a frame not yet tracked wait for it.
_RESTORE_WAIT = 5.0
_RESTORE_ORPHANS = 256
_POLICY_THREADS = 32
_NAVIGATION_THREADS = 4
#: How long one URL policy check may take, waiting for a thread included.
_POLICY_DEADLINE = 5.0
_NAVIGATION_CHECK: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mandala_navigation_check", default=False
)
# Set around a command whose reply must be applied as it arrives, in order with
# the events read before and after it, rather than when its caller resumes.
_ON_REPLY: contextvars.ContextVar[Callable[[Any], None] | None] = contextvars.ContextVar(
    "mandala_on_reply", default=None
)
_FLOODED = (
    "The page produced browser events faster than the session could handle them. "
    "The session ended; create a new toolset to continue."
)
_TARGET_EVENTS = frozenset(
    {
        "Target.attachedToTarget",
        "Target.detachedFromTarget",
        "Target.targetDestroyed",
        "Target.targetInfoChanged",
        "Target.targetCreated",
    }
)
_NOTED_EVENTS = frozenset(
    {
        "Page.frameAttached",
        "Page.frameDetached",
        "Page.frameNavigated",
        "Runtime.consoleAPICalled",
        "Network.responseReceived",
    }
)
_LANE_EVENTS = frozenset(
    {"Fetch.requestPaused", "Browser.downloadWillBegin", "Browser.downloadProgress"}
)
_AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
_INTERCEPT = {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
# Browser-root Fetch interception is installed before any owned page runs.
# It covers frame requests even when Chromium does not pause a sandboxed
# srcdoc target before its first script. Frame sessions still provide DOM,
# runtime and download bookkeeping, but never install a second interceptor.
# Dedicated workers remain unsupported. In every frame of an owned tab that
# the driver initializes before it runs, Worker is a constructor that refuses.
# Chromium cannot close a held dedicated worker without closing its page, and
# releases it when that page goes. A frame that starts
# running without waiting for the driver (a sandboxed srcdoc frame) runs its
# first scripts before its session can register this, so it can keep the
# original constructor for later. The browser-root interceptor still checks
# its HTTP(S) requests and its workers', including before frame attachment.
_NO_WORKERS = """(() => {
  const refuse = function Worker() {
    throw new DOMException(
      "Dedicated workers are disabled in this browser session.",
      "SecurityError"
    );
  };
  Object.defineProperty(globalThis, "Worker", {
    value: refuse, writable: false, configurable: false, enumerable: false
  });
})();"""
_NO_WORKERS_INIT = (
    "Page.addScriptToEvaluateOnNewDocument",
    {"source": _NO_WORKERS, "runImmediately": True},
)
# A popup is refused, but must first be released: auto-attach holds it before
# its first line, and one opened by script shares its opener's renderer, so a
# held popup holds its opener too (a click that opens one never returns). Before
# release it is fenced, so that it loads nothing: it is offline (see
# BrowserCDP._offline), no frame in it can start a worker (Page enabled first,
# so that the script reaches the frames made in it after release, not only its
# current document), and anything it starts is held. Then it is closed.
# Offline rather than intercepted: Chromium lets an intercepted request go on
# when its session detaches, which closing the popup does, while an offline
# request fails at once in the network stack. Blocked URLs do not stop a
# navigation.
_POPUP_FENCE: tuple[tuple[str, dict[str, Any]], ...] = (
    ("Page.enable", {}),
    ("Network.enable", {}),
    # Offline is below the HTTP cache: a cached document would still load.
    ("Network.setCacheDisabled", {"cacheDisabled": True}),
    ("Network.setBypassServiceWorker", {"bypass": True}),
    _NO_WORKERS_INIT,
    # And its documents run no script of their own: a document that needs no
    # network at all (a blob: one) would otherwise load and run.
    ("Emulation.setScriptExecutionDisabled", {"value": True}),
    ("Target.setAutoAttach", _AUTO_ATTACH),
)
_OFFLINE = {"offline": True, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1}
_FRAME_INIT: tuple[tuple[str, dict[str, Any]], ...] = (
    ("Page.enable", {}),
    _NO_WORKERS_INIT,
    ("Network.enable", {}),
    ("Network.setBypassServiceWorker", {"bypass": True}),
    ("Runtime.enable", {}),
    ("Target.setAutoAttach", _AUTO_ATTACH),
)
# Renewal keeps retrying until this long before the lease deadline, so that the
# last attempt can still arrive while the lease is active.
_RENEW_MARGIN = 2.0
_RENEW_ATTEMPT = 10.0
_RENEW_BACKOFF = 0.5
_RENEW_BACKOFF_MAX = 8.0


def _renewable(error: BaseException) -> bool:
    """Whether a failed renewal attempt is worth another try before the deadline."""
    # A refusal is an answer about this session: 401/403 is the credential, 404
    # the connection, and 409 a session that is no longer active (expired,
    # revoked, disconnected). None of them changes by asking again, whatever
    # reason the body gives.
    if isinstance(error, APIError):
        return error.status in (408, 429) or error.status >= 500
    # Renewal changes nothing but the deadline, so a lost answer is safe to ask
    # again; so is an attempt that hit its own timeout.
    return isinstance(error, (ConnectionError, TimeoutError, asyncio.TimeoutError))


def _retrieve(future: asyncio.Future[Any]) -> None:
    """Mark an abandoned attempt's outcome as seen; the caller has moved on."""
    if not future.cancelled():
        future.exception()


def _checked(value: Any, limit: int) -> str | None:
    """A field a check reads, cut to one past the check's own limit.

    So an over-long value stays over-long and is refused as before, and
    anything that is not a string becomes ``None``, which every check refuses.
    """
    return value[: limit + 1] if isinstance(value, str) else None


def _shown(value: Any, limit: int) -> str:
    """A field only displayed, already cut to ``limit`` where it is shown."""
    return value[:limit] if isinstance(value, str) else ""


def _trimmed(message: dict[str, Any]) -> dict[str, Any]:
    """A queued event, cut down to the fields its handler reads.

    Trimming never turns a refusal into an acceptance: see :func:`_checked`.
    """
    method, p = message.get("method"), message.get("params") or {}
    if method == "Fetch.requestPaused":
        request = p.get("request") or {}
        params: dict[str, Any] = {
            "requestId": p.get("requestId"),
            "request": {"url": _checked(request.get("url"), 8192)},
            "frameId": _checked(p.get("frameId"), 128),
            "resourceType": _shown(p.get("resourceType"), 32),
        }
    elif method == "Page.javascriptDialogOpening":
        params = {"type": _shown(p.get("type"), 32), "message": _shown(p.get("message"), 1000)}
    else:
        params = {
            key: p[key]
            for key in ("guid", "frameId", "receivedBytes", "totalBytes", "state")
            if key in p
        }
        if "url" in p:
            params["url"] = _shown(p["url"], 4096)
        if "suggestedFilename" in p:
            params["suggestedFilename"] = _checked(p["suggestedFilename"], 4096)
    trimmed = {"method": method, "params": params}
    if "sessionId" in message:
        trimmed["sessionId"] = message["sessionId"]
    return trimmed


def _target_info(message: dict[str, Any]) -> dict[str, Any]:
    """A lifecycle event with its target's URL and title cut to what is used.

    The URL is checked again before a reload, so it keeps one character past
    the URL limit and an over-long one is still refused.
    """
    p = message.get("params") or {}
    info = p.get("targetInfo")
    if not isinstance(info, dict):
        return message
    info = {
        **info,
        "url": _shown(info.get("url"), 8193),
        "title": _shown(info.get("title"), 1000),
    }
    return {**message, "params": {**p, "targetInfo": info}}


class _Lane:
    """Events waiting on caller code, run up to ``limit`` at once, in order."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.running: set[asyncio.Task[None]] = set()
        self.waiting: deque[tuple[dict[str, Any], int]] = deque()


class BrowserError(Exception):
    """Only driver-authored messages may be exposed to a model."""


class _RequestGone(BrowserError):
    """Chromium discarded a paused request before its policy check completed."""


class _SessionGone(BrowserError):
    """A frame's session detached while a command on it was in flight."""


class _DirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc  # Never forward the capability through an HTTP redirect.


def number(value: Any, name: str, maximum: int, *, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise BrowserError(f"{name} must be a finite number")
    if value < 0 or value > maximum or (integer and int(value) != value):
        raise BrowserError(
            f"{name} must be between 0 and {maximum}" + (" and an integer" if integer else "")
        )
    return value


class BrowserCDP:
    """One capability and context, behind Anthropic's serialized call pipeline.

    A failed transport is never replayed.
    """

    def __init__(
        self,
        create: Callable[..., Any],
        revoke: Callable[..., Any],
        policy: Any,
        *,
        renew: Callable[..., Any] | None = None,
        session_policy: BrowserSessionPolicy | None = None,
    ) -> None:
        self.create, self.revoke, self.policy = create, revoke, policy
        self.files: Any = None
        self._starting: asyncio.Task[None] | None = None
        self.renew = renew
        self.session_policy = session_policy
        self.lease: BrowserSessionLease | None = None
        self.lease_deadline = 0.0
        self.lease_task: asyncio.Task[None] | None = None
        self.terminal_reason: str | None = None
        self.ws: Any = None
        self.reader: asyncio.Task[None] | None = None
        self.tasks: set[asyncio.Task[Any]] = set()
        self.policy_lane = _Lane(_POLICY_CONCURRENCY)
        self.navigation_lane = _Lane(_NAVIGATION_CONCURRENCY)
        self.queued = 0
        self.queued_bytes = 0
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.pending_sessions: dict[int, str] = {}
        self.reply_hooks: dict[int, Callable[[Any], None]] = {}
        # Immutable provenance, separate from the live file/frame maps. None
        # means positively foreign, never merely absent. Retain detached frame
        # owners so their pending requests cannot become foreign after removal.
        self.request_owners: dict[str, str | None] = {}
        self.request_parents: dict[str, str] = {}
        self.dead_frames: set[str] = set()
        self.request_epochs: dict[str, int] = {}
        self.owner_lookups: dict[str, asyncio.Task[None]] = {}
        self.owner_survey: asyncio.Task[None] | None = None
        # A temporary foreign attachment can be reported before attach's reply.
        self.probe_targets: set[str] = set()
        self.probe_sessions: dict[str, str] = {}
        self.stopping = False
        self.ending: asyncio.Task[None] | None = None
        self.intercepting = False
        # Child session -> (owning tab, frame target, parent session): the
        # cross-site frames of a tab, at any depth.
        self.child_frames: dict[str, tuple[str, str, str]] = {}
        # Session -> target, of pages attached in the context that are not (yet)
        # tabs: popups being refused, and a new tab until it is adopted.
        # Recorded as the attach arrives and dropped as its detach or
        # destruction does, so a refusal never acts for a page already gone.
        self.refused: dict[str, str] = {}
        # Download ownership, as the file session keeps it, is decided here:
        # tracked frame -> the session that last reported it (a session that
        # goes takes the frames only it could see), and -> its parent frame.
        self.frame_source: dict[str, str] = {}
        self.frame_parent: dict[str, str] = {}
        # For each frame tree on its way: the frames events have reported since.
        self.snapshots: list[set[str]] = []
        # Cache restores whose frame tree is on its way, and the frames reported
        # meanwhile beneath a frame not tracked yet: frame -> (tab, session, parent).
        self.restoring: set[asyncio.Task[None]] = set()
        self.orphans: dict[str, tuple[str, str, str]] = {}
        # Download GUID -> the task handling its latest event. A download's
        # events are handled in arrival order, one at a time: its progress must
        # not overtake its beginning while that waits on a restore.
        self.download_events: dict[str, asyncio.Task[Any]] = {}
        # Frame -> downloads waiting on a restore to learn whether it is the
        # tab's. Settled as soon as it is: a later removal does not unsettle it.
        self.awaiting_frames: dict[str, list[asyncio.Future[bool]]] = {}
        self._pools: dict[bool, tuple[ThreadPoolExecutor, asyncio.Semaphore]] = {}
        self.creating: asyncio.Future[str] | None = None
        self.counter = 0
        self.grant: Any = None
        self.context: str | None = None
        self.closed = False
        self.cleanup: asyncio.Task[None] | None = None
        self.buttons: dict[str, int] = {}
        self.failed = False
        self.tabs: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, str] = {}
        self.active: str | None = None
        self.ready: dict[str, asyncio.Event] = {}
        self.refs: dict[str, dict[str, int]] = {}
        self.ref_counter = 0
        self.console: dict[str, deque[str]] = {}
        self.network: dict[str, deque[str]] = {}
        self.changes: deque[dict[str, Any]] = deque(maxlen=100)

    async def _invoke(self, fn: Callable[..., Any], *args: Any) -> Any:
        result = fn(*args)
        return await result if inspect.isawaitable(result) else result

    async def start(self) -> None:
        if self.closed or self.failed or self.stopping:
            raise BrowserError(
                self.terminal_reason
                or "Browser connection ended. Create a new toolset for a fresh session."
            )
        # Staging is a public harness API outside the inherited tool-call queue.
        if self._starting is None:
            self._starting = asyncio.create_task(self._start())
        await asyncio.shield(self._starting)

    async def _start(self) -> None:
        if self.closed or self.failed:
            raise BrowserError(
                self.terminal_reason
                or "Browser connection ended. Create a new toolset for a fresh session."
            )
        if self.ws is not None:
            return
        try:
            requested = time.monotonic()
            self.grant = await self._invoke(self.create)
            if self.closed:
                raise BrowserError("Browser connection ended.")
            if self.session_policy is not None:
                self._accept_lease(self.grant.lease, time.monotonic() - requested)
            self.ws = await _DirectConnect(
                self.grant.url,
                additional_headers={"Authorization": f"Bearer {self.grant.token}"},
                open_timeout=15,
                close_timeout=2,
                max_size=8 * 1024 * 1024,
            )
            if self.closed:
                raise BrowserError("Browser connection ended.")
            self.reader = asyncio.create_task(self._read())
            self.context = (
                await self.send("Target.createBrowserContext", {"disposeOnDetach": True})
            )["browserContextId"]
            await self.send(
                "Browser.setDownloadBehavior",
                {"behavior": "deny", "browserContextId": self.context},
            )
            if self.files is not None:
                await self.files.setup(self.context)
            await self.send("Target.setDiscoverTargets", {"discover": True})
            await self.send("Fetch.enable", _INTERCEPT)
            self.intercepting = True
            await self.send("Target.setAutoAttach", _AUTO_ATTACH)
            await self.new_tab()
            if self.lease is not None:
                self.lease_task = asyncio.create_task(self._maintain_lease())
        except BaseException:
            await self.close()
            raise

    def _accept_lease(self, lease: BrowserSessionLease | None, elapsed: float) -> None:
        if lease is None:
            raise BrowserError(
                "The server did not provide the requested renewable browser session."
            )
        previous = self.lease
        if previous is not None and (
            lease.id != previous.id
            or lease.attach_expires_at != previous.attach_expires_at
            or lease.absolute_expires_at != previous.absolute_expires_at
            or lease.lease_seconds != previous.lease_seconds
            or lease.server_time < previous.server_time
            or lease.lease_expires_at < previous.lease_expires_at
        ):
            raise BrowserError("Browser renewal returned an inconsistent session lease.")
        # Server time avoids dependence on the SDK host's wall clock. Subtract
        # the whole request duration so network latency never extends a lease.
        remaining = (lease.lease_expires_at - lease.server_time).total_seconds() - elapsed
        if remaining <= 0:
            raise BrowserError("Browser session lease expired before its response arrived.")
        self.lease = lease
        self.lease_deadline = time.monotonic() + remaining

    def session_status(self) -> dict[str, Any]:
        lease, deadline = self.lease, self.lease_deadline
        ended = self.failed or self.closed or self.stopping
        return {
            "state": "ended" if ended else "active" if self.ws is not None else "not_started",
            "lease_expires_at": lease.lease_expires_at if lease else None,
            "absolute_expires_at": lease.absolute_expires_at if lease else None,
            "remaining_seconds": max(0.0, deadline - time.monotonic())
            if lease and not ended
            else 0.0,
            "terminal_error": self.terminal_reason,
            "file_cleanup_failed": bool(self.files and self.files.cleanup_failed),
        }

    async def _maintain_lease(self) -> None:
        try:
            while not self.closed and not self.failed:
                assert self.lease is not None and self.session_policy is not None
                remaining = self.lease_deadline - time.monotonic()
                can_renew = (
                    self.session_policy.auto_renew
                    and self.renew is not None
                    and self.lease.lease_expires_at < self.lease.absolute_expires_at
                )
                await asyncio.sleep(
                    max(0.0, remaining - min(60.0, remaining / 3))
                    if can_renew
                    else max(0.0, remaining)
                )
                if self.closed or self.failed:
                    return
                if not can_renew or time.monotonic() >= self.lease_deadline:
                    self.terminal_reason = "Browser session lease expired or its absolute limit was reached. Create a new toolset to continue."
                    break
                await self._renew_lease()
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - never expose credentials or account error bodies
            if self.closed or self.failed:
                return
            self.terminal_reason = "Browser session renewal failed. The session ended; create a new toolset to continue."
        await self._terminate(self.terminal_reason or "Browser session ended.")

    async def _renew_lease(self) -> None:
        """Renew once, retrying passing failures until shortly before the deadline.

        One failed attempt is not a failed renewal. A connection failure, a
        timeout, a 408, a 429 or a 5xx is tried again with backoff, honouring
        ``Retry-After``; a refusal (any other status, including every 409)
        ends the session at once, as does running out of time.
        """
        assert self.renew is not None
        attempt = 0
        while True:
            requested = time.monotonic()
            # The first attempt is scheduled well inside the lease; a retry must
            # also leave the margin for its own answer to arrive in time.
            budget = self.lease_deadline - requested - (_RENEW_MARGIN if attempt else 0.0)
            if budget <= 0:
                raise BrowserError("Browser session renewal ran out of time.")
            limit = min(_RENEW_ATTEMPT, budget)
            # The limit also bounds the request itself. A sync computer renews
            # on a thread that nothing can cancel, so an attempt that outlives
            # its limit is waited out, never run beside another.
            call = asyncio.ensure_future(self._invoke(self.renew, self.grant.id, limit))
            call.add_done_callback(_retrieve)
            try:
                try:
                    lease = await asyncio.wait_for(asyncio.shield(call), limit)
                except asyncio.TimeoutError:
                    left = self.lease_deadline - time.monotonic() - _RENEW_MARGIN
                    await asyncio.wait({call}, timeout=max(0.0, left))
                    if not call.done():
                        call.cancel()  # Out of time: the session ends with it.
                        raise
                    lease = call.result()
            except asyncio.CancelledError:
                call.cancel()
                raise
            except Exception as error:
                if self.closed or self.failed or not _renewable(error):
                    raise
                wait = min(_RENEW_BACKOFF * 2**attempt, _RENEW_BACKOFF_MAX)
                hinted = error.retry_after if isinstance(error, APIError) else None
                if hinted is not None:
                    wait = max(wait, hinted)
                if time.monotonic() + wait >= self.lease_deadline - _RENEW_MARGIN:
                    raise
                await asyncio.sleep(wait)
                if self.closed or self.failed:
                    return
                attempt += 1
                continue
            if self.closed or self.failed:
                return
            self._accept_lease(lease, time.monotonic() - requested)
            return

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session: str | None = None
    ) -> Any:
        if self.ws is None or self.failed:
            raise BrowserError(
                self.terminal_reason
                or "Browser connection ended. Create a new toolset for a fresh session."
            )
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        message: dict[str, Any] = {"id": ident, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
            self.pending_sessions[ident] = session
        hook = _ON_REPLY.get()
        if hook is not None:
            self.reply_hooks[ident] = hook
        try:
            await self.ws.send(json.dumps(message))
            return await asyncio.wait_for(future, 15)
        finally:
            self.pending.pop(ident, None)
            self.pending_sessions.pop(ident, None)
            self.reply_hooks.pop(ident, None)

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if "id" in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            error = message["error"]
                            kind = (
                                _RequestGone
                                if error.get("code") == -32602
                                and error.get("message") == "Invalid InterceptionId."
                                else BrowserError
                            )
                            future.set_exception(
                                kind("Chromium could not complete the browser action.")
                            )
                        else:
                            result = message.get("result", {})
                            hook = self.reply_hooks.pop(message["id"], None)
                            if hook is not None:
                                try:
                                    hook(result)
                                except Exception:  # noqa: BLE001, S110 - its caller fails closed
                                    pass
                            future.set_result(result)
                else:
                    try:
                        self._dispatch(message)
                    except Exception:  # noqa: BLE001 - malformed browser events fail closed
                        # Keep reading replies while disposal runs. Awaiting it
                        # here would deadlock its command on this same reader.
                        self.stopping = True
                        task = asyncio.create_task(
                            self._terminate(self.terminal_reason or _OWNERSHIP_ERROR)
                        )
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
        except Exception:  # noqa: BLE001, S110 - fail closed without exposing browser-supplied errors
            pass
        finally:
            self.failed = True
            for lane in (self.policy_lane, self.navigation_lane):
                lane.waiting.clear()
            self.queued = self.queued_bytes = 0
            if self.lease_task is not None and self.lease_task is not asyncio.current_task():
                self.lease_task.cancel()
            for ready in self.ready.values():
                ready.set()
            for mapping in (
                self.tabs,
                self.sessions,
                self.child_frames,
                self.ready,
                self.refs,
                self.console,
                self.network,
                self.buttons,
            ):
                mapping.clear()
            self.active = None
            self.changes.clear()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        BrowserError(
                            self.terminal_reason or "Browser connection ended or its lease expired."
                        )
                    )
            if self.ws is not None:
                await self.ws.close()
            if self.files is not None:
                await self.files.close()

    def _flooded(self) -> BrowserError:
        self.terminal_reason = _FLOODED
        return BrowserError(_FLOODED)

    def _dispatch(self, message: dict[str, Any]) -> None:
        """Route one event as it arrives; see the note on _POLICY_CONCURRENCY."""
        if self.stopping:
            return  # Replies still drain; no new policy work during disposal.
        method = message.get("method")
        if method in _TARGET_EVENTS:
            message = _target_info(message)
            self._settle(message)
            if method in ("Target.attachedToTarget", "Target.targetDestroyed"):
                child = (message.get("params") or {}).get("sessionId")
                if method != "Target.attachedToTarget" or child not in self.probe_sessions:
                    self._launch(message)
        elif method in _NOTED_EVENTS:
            self._settle(message)
        elif method == "Page.javascriptDialogOpening":
            self._launch(_trimmed(message))
        elif method in _LANE_EVENTS:
            p = message.get("params") or {}
            trimmed = _trimmed(message)
            if method == "Browser.downloadWillBegin" and self.files is not None:
                # Whose download it is is decided as it arrives, in order with
                # the frame notes: a frame that goes while it waits its turn
                # must not orphan it. While a cache restore is reading its
                # frames back, a frame not tracked yet waits for it, briefly.
                frame = trimmed["params"].get("frameId")
                if frame in self.files.frames:
                    trimmed["params"]["tracked"] = True
                elif self.restoring and isinstance(frame, str):
                    # Waiting from now, before any later event is noted: its
                    # frame may be tracked, and removed, in this same read.
                    owned: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
                    self.awaiting_frames.setdefault(frame, []).append(owned)
                    decision = asyncio.create_task(self._decide(frame, owned))
                    self.tasks.add(decision)
                    decision.add_done_callback(self.tasks.discard)
                    trimmed["decision"] = decision
                else:
                    trimmed["params"]["tracked"] = False
            session = message.get("sessionId")
            frame_id = p.get("frameId")
            tab = (
                self.request_owners.get(frame_id)
                if session is None and isinstance(frame_id, str)
                else None
            )
            own_document = (
                tab is not None and p.get("frameId") == tab and p.get("resourceType") == "Document"
            )
            if own_document:
                trimmed["navigation"] = True
            self._enqueue(self.navigation_lane if own_document else self.policy_lane, trimmed)
        # Anything else is an event this driver does not use.

    def _launch(self, message: dict[str, Any]) -> None:
        """A task that waits only on Chromium, started at once."""
        if (
            len(self.tasks) - len(self.policy_lane.running) - len(self.navigation_lane.running)
            >= _TARGET_EVENT_LIMIT
        ):
            raise self._flooded()
        task = asyncio.create_task(self._after(message))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _enqueue(self, lane: _Lane, message: dict[str, Any]) -> None:
        # Arrival order is kept: nothing overtakes an event already waiting.
        if not lane.waiting and len(lane.running) < lane.limit:
            self._run_in(lane, message)
            return
        size = len(json.dumps(message, default=lambda _: None))
        if self.queued >= _EVENT_QUEUE_LIMIT or self.queued_bytes + size > _EVENT_QUEUE_BYTES:
            raise self._flooded()
        lane.waiting.append((message, size))
        self.queued += 1
        self.queued_bytes += size

    def _run_in(self, lane: _Lane, message: dict[str, Any]) -> None:
        task = asyncio.create_task(self._after(message))
        self.tasks.add(task)
        lane.running.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self.tasks.discard(done)
            lane.running.discard(done)
            while (
                not self.failed
                and not self.closed
                and lane.waiting
                and len(lane.running) < lane.limit
            ):
                waiting, size = lane.waiting.popleft()
                self.queued -= 1
                self.queued_bytes -= size
                self._run_in(lane, waiting)

        task.add_done_callback(finished)

    def _note(self, method: str, p: dict[str, Any], session: str | None) -> None:
        """Bookkeeping that needs no reply from Chromium: frames and the logs."""
        tab = self._owner(session)
        if not tab:
            return
        try:
            if method == "Page.frameAttached":
                self._remember_owner(p["frameId"], tab, p.get("parentFrameId"))
            elif method == "Page.frameDetached" and p.get("reason") != "swap":
                self._retire_frames(p["frameId"])
            elif method == "Page.frameNavigated":
                frame = p["frame"]
                # A navigation can finish (including an error document) while
                # the next navigation's policy callback is pending. The root
                # frame itself still exists; only its old children retire.
                # Actual detach/revival keeps its separate epoch below.
                self._retire_frames(frame["id"], include=False)
                self._remember_owner(frame["id"], tab, frame.get("parentId"))
                self.dead_frames.discard(frame["id"])
            if method.startswith("Page.frame"):
                frame = p.get("frameId") or (p.get("frame") or {}).get("id")
                if isinstance(frame, str):
                    for touched in self.snapshots:
                        touched.add(frame)  # Newer than any frame tree on its way.
            if self.files is not None and method == "Page.frameAttached":
                self._track(p["frameId"], tab, session, p.get("parentFrameId"))
            if (
                self.files is not None
                and method == "Page.frameDetached"
                and p.get("reason") != "swap"  # It lives on in another process.
            ):
                self._prune_below(p["frameId"])
                self._untrack(p["frameId"])
            if method == "Page.frameNavigated":
                if session not in self.child_frames:
                    # The tab's own document changed. A cross-site frame's
                    # navigation leaves the tab's element refs as they were.
                    self.refs.pop(tab, None)
                if self.files is not None:
                    frame = p["frame"]
                    # A new document: the old one's frames are gone, and
                    # Chromium need not report each of them (a cross-site frame
                    # keeps its session across sites, but not its children).
                    self._prune_below(frame["id"])
                    self._track(frame["id"], tab, session, frame.get("parentId"))
                    # A document back from the back/forward cache brings its
                    # frames back, and Chromium reports none of them.
                if p.get("type") == "BackForwardCacheRestore" and session is not None:
                    self._restore(session, tab)
            elif method == "Runtime.consoleAPICalled":
                text = " ".join(
                    str(a.get("value", a.get("description", "")))[:1000]
                    for a in p.get("args", [])[:20]
                )
                self.console.setdefault(tab, deque(maxlen=100)).append(text[:2000])
            elif method == "Network.responseReceived":
                response = p["response"]
                self.network.setdefault(tab, deque(maxlen=100)).append(
                    f"{response['status']} {str(response['url'])[:2000]}"
                )
        except (KeyError, TypeError, AttributeError):
            # A malformed note is dropped. An untracked frame's downloads are
            # canceled, so dropping one fails closed.
            if method == "Page.frameNavigated" and session not in self.child_frames:
                self.refs.pop(tab, None)

    def _track(self, frame: Any, tab: str, session: str | None, parent: Any = None) -> bool:
        """Make a frame's downloads its tab's, beneath a tracked frame only.

        So every tracked frame stays reachable from the frames above it when
        they change documents. A frame reported while a cache restore is
        reading its parent back waits for it, a bounded number of them.
        """
        files = self.files
        assert files is not None
        if not isinstance(frame, str) or session is None:
            return False
        if isinstance(parent, str) and parent not in files.frames:
            if self.restoring and (frame in self.orphans or len(self.orphans) < _RESTORE_ORPHANS):
                self.orphans[frame] = (tab, session, parent)
            return False
        if not files.frame(frame, tab):
            return False
        self.frame_source[frame] = session
        if isinstance(parent, str):
            self.frame_parent[frame] = parent
        for owned in self.awaiting_frames.pop(frame, []):
            if not owned.done():
                owned.set_result(True)
        return True

    def _track_tree(
        self,
        tree: dict[str, Any],
        tab: str,
        session: str,
        skip: set[str],
        parent: str | None = None,
    ) -> None:
        # A frame an event has spoken for since the tree was asked for is newer
        # than the tree, and so is everything below it.
        frame = (tree.get("frame") or {}).get("id")
        if frame in skip or not self._track(frame, tab, session, parent):
            return
        for child in tree.get("childFrames") or []:
            self._track_tree(child, tab, session, skip, frame)

    def _untrack(self, frame: str) -> None:
        if self.files is not None:
            self.files.frames.pop(frame, None)
        self.frame_source.pop(frame, None)
        self.frame_parent.pop(frame, None)
        self.orphans.pop(frame, None)

    def _prune_below(self, frame: str) -> None:
        """Forget every tracked or waiting frame below ``frame``."""
        below = {frame}
        grew = True
        while grew:
            grew = False
            pairs = [*self.frame_parent.items()]
            pairs += [(orphan, held[2]) for orphan, held in self.orphans.items()]
            for child, parent in pairs:
                if parent in below and child not in below:
                    below.add(child)
                    grew = True
        below.discard(frame)
        for child in below:
            self._untrack(child)

    def _forget_frames(self, session: str) -> None:
        """Forget the frames only ``session`` reported last: it has gone."""
        for frame, source in list(self.frame_source.items()):
            if source == session:
                self._untrack(frame)
        for frame, held in list(self.orphans.items()):
            if held[1] == session:
                del self.orphans[frame]

    async def _snapshot(self, session: str, tab: str, *, adopt: bool = False) -> None:
        """Track the frames a session holds now.

        The reply is a snapshot: a frame event that arrived while it was on its
        way is newer and stands, and a session that went meanwhile has nothing
        left to track.
        """
        touched: set[str] = set()
        self.snapshots.append(touched)
        applied = False

        def forget() -> None:
            # By identity: two snapshots' sets can be equal.
            self.snapshots = [other for other in self.snapshots if other is not touched]

        def apply(reply: Any) -> None:
            # Applied as the reply is read, before any event read after it.
            nonlocal applied
            if applied:
                return
            applied = True
            try:
                if self._owner(session) == tab:
                    self._remember_tree(reply["frameTree"], tab, skip=touched, revive=True)
                    if self.files is not None:
                        self._track_tree(reply["frameTree"], tab, session, touched)
                if adopt and self.files is not None:
                    # Frames reported meanwhile beneath ones this tree tracked
                    # are tracked now too, before any later event is read.
                    self._adopt()
            finally:
                forget()

        hook = _ON_REPLY.set(apply)
        try:
            reply = await self.send("Page.getFrameTree", session=session)
        finally:
            _ON_REPLY.reset(hook)
            if not applied:
                forget()
        apply(reply)  # When no reader applied it as it arrived.

    def _adopt(self) -> None:
        """Track waiting frames whose parent is tracked now, and theirs in turn."""
        assert self.files is not None
        grew = True
        while grew:
            grew = False
            for frame, (tab, session, parent) in list(self.orphans.items()):
                if parent in self.files.frames:
                    del self.orphans[frame]
                    if self._owner(session) == tab and self._track(frame, tab, session, parent):
                        grew = True

    def _restore(self, session: str, tab: str) -> None:
        """Read a session's frames again after a back/forward cache restore."""

        async def restore() -> None:
            try:
                await self._snapshot(session, tab, adopt=True)
            except Exception:  # noqa: BLE001, S110 - unread frames stay untracked: fail closed
                pass

        task = asyncio.create_task(restore())
        self.restoring.add(task)
        self.tasks.add(task)

        def done(finished: asyncio.Task[None]) -> None:
            self.restoring.discard(finished)
            self.tasks.discard(finished)
            if not self.restoring:
                self.orphans.clear()

        task.add_done_callback(done)

    async def _decide(self, frame: str, owned: asyncio.Future[bool]) -> bool:
        """Whether a download's frame is the tab's, once pending restores land.

        ``owned`` is settled the moment its frame is tracked, however the other
        restores go; otherwise this answers when none is left pending, or after
        _RESTORE_WAIT.
        """
        loop = asyncio.get_running_loop()
        try:
            if self.files is None:
                return False
            deadline = loop.time() + _RESTORE_WAIT
            while self.restoring and not owned.done():
                left = deadline - loop.time()
                if left <= 0:
                    break
                pending: set[asyncio.Future[Any]] = {owned, *self.restoring}
                await asyncio.wait(pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
            return (owned.done() and not owned.cancelled()) or frame in self.files.frames
        finally:
            if not owned.done():
                owned.cancel()
            waiting = self.awaiting_frames.get(frame)
            if waiting is not None:
                waiting[:] = [other for other in waiting if other is not owned]
                if not waiting:
                    del self.awaiting_frames[frame]

    def _remember_owner(self, frame: Any, tab: str | None, parent: Any = None) -> None:
        if not isinstance(frame, str) or not frame or len(frame) > 128:
            raise BrowserError(_OWNERSHIP_ERROR)
        if frame in self.request_owners:
            if self.request_owners[frame] != tab:
                raise BrowserError(_OWNERSHIP_ERROR)
        elif len(self.request_owners) >= _OWNERSHIP_LIMIT:
            raise BrowserError(_OWNERSHIP_ERROR)
        else:
            self.request_owners[frame] = tab
        if isinstance(parent, str) and parent != frame:
            self.request_parents[frame] = parent

    def _target_owner(self, info: Any, parent: str | None = None) -> None:
        """Learn only positive browser metadata, never ownership from a URL."""
        if not isinstance(info, dict) or not isinstance(info.get("type"), str):
            raise BrowserError(_OWNERSHIP_ERROR)
        target, context = info.get("targetId"), info.get("browserContextId")
        if context is not None and not isinstance(context, str):
            raise BrowserError(_OWNERSHIP_ERROR)
        if self.context is None:
            return
        if parent is not None:
            if context is not None and context != self.context:
                raise BrowserError(_OWNERSHIP_ERROR)
            self._remember_owner(target, parent)
        elif context is not None and context != self.context:
            self._remember_owner(target, None)
        elif context is None and info["type"] in ("page", "iframe"):
            # Page/frame targets without a browserContextId belong to the
            # default profile. Worker metadata may omit it even when owned.
            self._remember_owner(target, None)
        elif context == self.context and info["type"] == "page":
            self._remember_owner(target, target)
        else:
            ancestor = info.get("parentFrameId") or info.get("parentId")
            if ancestor in self.request_owners and self.request_owners[ancestor] is not None:
                self._remember_owner(target, self.request_owners[ancestor], ancestor)

    def _remember_tree(
        self,
        tree: Any,
        tab: str | None,
        parent: str | None = None,
        *,
        skip: set[str] | None = None,
        revive: bool = False,
    ) -> None:
        if not isinstance(tree, dict) or not isinstance(tree.get("frame"), dict):
            raise BrowserError(_OWNERSHIP_ERROR)
        frame = tree["frame"].get("id")
        if skip is not None and frame in skip:
            return  # An event since the query supersedes this entire subtree.
        self._remember_owner(frame, tab, parent)
        if revive:
            self.dead_frames.discard(frame)
        children = tree.get("childFrames", [])
        if not isinstance(children, list):
            raise BrowserError(_OWNERSHIP_ERROR)
        for child in children:
            self._remember_tree(child, tab, frame, skip=skip, revive=revive)

    def _retire_frames(self, frame: str, *, include: bool = True) -> None:
        below = {frame}
        while True:
            extra = {child for child, parent in self.request_parents.items() if parent in below}
            if extra <= below:
                break
            below.update(extra)
        if not include:
            below.discard(frame)
        for retired in below:
            if retired in self.request_owners:
                self.dead_frames.add(retired)
                self.request_epochs[retired] = self.request_epochs.get(retired, 0) + 1

    async def _foreign_tree(self, target: str) -> None:
        """Read another page's tree without installing policy or runtime state."""
        self.probe_targets.add(target)
        try:
            result = await self.send("Target.attachToTarget", {"targetId": target, "flatten": True})
            session = result["sessionId"]
            if not isinstance(session, str):
                raise BrowserError(_OWNERSHIP_ERROR)
            self.probe_sessions[session] = target
            reply = await self.send("Page.getFrameTree", session=session)
            tree = reply["frameTree"]
            if tree.get("frame", {}).get("id") != target:
                raise BrowserError(_OWNERSHIP_ERROR)
            self._remember_tree(tree, None)
        finally:
            self.probe_targets.discard(target)
            # Include an autoattachment that arrived while the probe was in
            # progress, as well as attachToTarget's explicit session.
            sessions = [s for s, t in self.probe_sessions.items() if t == target]
            for session in sessions:
                try:
                    await self.send("Target.detachFromTarget", {"sessionId": session})
                except BrowserError:
                    pass  # A disappeared target/session has nothing to detach.
                finally:
                    self.probe_sessions.pop(session, None)

    async def _survey_owners(self) -> None:
        response = await self.send("Target.getTargets")
        targets = response.get("targetInfos")
        if not isinstance(targets, list) or len(targets) > _OWNERSHIP_TARGETS:
            raise BrowserError(_OWNERSHIP_ERROR)
        for info in targets:
            self._target_owner(info)
        # A single coalesced survey bounds temporary attachments. No URL policy
        # callback is awaited here, so a held request cannot block discovery.
        for info in targets:
            if self.stopping:
                return
            if info.get("type") != "page":
                continue
            target = info["targetId"]
            try:
                if info.get("browserContextId") == self.context:
                    session = self.sessions.get(target)
                    if session is not None:
                        reply = await self.send("Page.getFrameTree", session=session)
                        self._remember_tree(reply["frameTree"], target)
                else:
                    await self._foreign_tree(target)
            except (BrowserError, KeyError, TypeError):
                # A page may have disappeared. That proves nothing about an
                # unresolved request; its lookup still fails closed below.
                continue

    async def _lookup_owner(self, frame: str) -> None:
        try:
            info = (await self.send("Target.getTargetInfo", {"targetId": frame}))["targetInfo"]
            if info.get("targetId") != frame:
                raise BrowserError(_OWNERSHIP_ERROR)
            self._target_owner(info)
        except (BrowserError, KeyError, TypeError):
            pass  # Same-process frames have no Target; inspect their trees.
        if frame in self.request_owners or self.stopping:
            return
        # A survey already in flight may predate this frame. At most one fresh
        # survey follows it; both run within this lookup's overall deadline.
        previous = self.owner_survey
        for _ in range(2 if previous is not None and not previous.done() else 1):
            if self.owner_survey is None or self.owner_survey.done():
                self.owner_survey = asyncio.create_task(self._survey_owners())
                self.tasks.add(self.owner_survey)
                self.owner_survey.add_done_callback(self.tasks.discard)
                self.owner_survey.add_done_callback(_retrieve)
            await asyncio.shield(self.owner_survey)
            if frame in self.request_owners or self.stopping:
                return
        raise BrowserError(_OWNERSHIP_ERROR)

    async def _request_owner(self, frame: Any) -> str | None:
        if not isinstance(frame, str) or not frame or len(frame) > 128:
            raise BrowserError(_OWNERSHIP_ERROR)
        if frame not in self.request_owners:
            lookup = self.owner_lookups.get(frame)
            if lookup is None:
                if len(self.owner_lookups) >= _OWNERSHIP_LOOKUPS:
                    raise BrowserError(_OWNERSHIP_ERROR)
                lookup = asyncio.create_task(self._lookup_owner(frame))
                self.owner_lookups[frame] = lookup
                self.tasks.add(lookup)
                lookup.add_done_callback(self.tasks.discard)
                lookup.add_done_callback(_retrieve)
                lookup.add_done_callback(lambda done: self.owner_lookups.pop(frame, None))
            await asyncio.wait_for(asyncio.shield(lookup), _OWNERSHIP_DEADLINE)
        if frame not in self.request_owners:
            raise BrowserError(_OWNERSHIP_ERROR)
        return self.request_owners[frame]

    def _owner(self, session: str | None) -> str | None:
        """The owned tab a session belongs to: the tab's own, or a frame inside it."""
        if session is None:
            return None
        frame = self.child_frames.get(session)
        if frame is not None:
            return frame[0] if frame[0] in self.sessions else None
        return next((t for t, s in self.sessions.items() if s == session), None)

    def _drop_frame(self, session: str | None) -> None:
        """Forget a frame's session and every frame session attached beneath it.

        Chromium need not report the detach of a session whose parent has gone,
        nor answer a command on a session that has gone: the subtree goes, and
        its pending commands are abandoned at once.
        """
        gone = [session] if session in self.child_frames else []
        for current in gone:
            gone.extend(c for c, (_, _, parent) in self.child_frames.items() if parent == current)
        for current in gone:
            self._retire_frames(self.child_frames[current][1])
            del self.child_frames[current]
            # Its parent's notes cannot be relied on to say it went, nor its own
            # to say the frames inside it went: the frames only it reported go
            # with it. One its parent has reported since lives on.
            self._forget_frames(current)
        self._abandon(gone)

    def _abandon(self, sessions: list[str]) -> None:
        # Chromium need not answer a command on a session that has gone.
        for ident, owner in list(self.pending_sessions.items()):
            future = self.pending.get(ident)
            if owner in sessions and future is not None and not future.done():
                future.set_exception(_SessionGone("Browser session detached."))

    def _settle(self, message: dict[str, Any]) -> None:
        """The part of an event that awaits nothing, done as it arrives."""
        method, p, session = (
            message.get("method"),
            message.get("params") or {},
            message.get("sessionId"),
        )
        if method in _NOTED_EVENTS:
            self._note(method, p, session)
        elif method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = p.get("targetInfo") or {}
            self._target_owner(info)
            if info.get("targetId") in self.tabs:
                self.tabs[info["targetId"]] = info
        elif method == "Target.detachedFromTarget":
            if isinstance(p.get("sessionId"), str):
                self.probe_sessions.pop(p["sessionId"], None)
            self._drop_frame(p.get("sessionId"))
            if p.get("sessionId") in self.refused:
                del self.refused[p["sessionId"]]
                self._abandon([p["sessionId"]])
        elif method == "Target.targetDestroyed":
            if isinstance(p.get("targetId"), str):
                self._retire_frames(p["targetId"])
            for child, (_, frame, _) in list(self.child_frames.items()):
                if frame == p.get("targetId"):
                    self._drop_frame(child)
            gone = [child for child, target in self.refused.items() if target == p.get("targetId")]
            for child in gone:
                del self.refused[child]
            self._abandon(gone)
        elif method == "Target.attachedToTarget":
            info, child = p["targetInfo"], p["sessionId"]
            tab = self._owner(session)
            self._target_owner(info, tab)
            if tab is not None:
                ancestor = self.child_frames[session][1] if session in self.child_frames else tab
                self._remember_owner(info["targetId"], tab, ancestor)
            if session is None and info["targetId"] in self.probe_targets:
                self.probe_sessions[child] = info["targetId"]
                return
            if (
                session is None
                and info.get("type") == "page"
                and info.get("browserContextId") == self.context
            ):
                self.refused[child] = info["targetId"]
            if tab is not None and session is not None and info.get("type") == "iframe":
                # A cross-site frame of an owned tab, at any depth. Closing it
                # would close its tab; it is set up and run instead.
                self.child_frames[child] = (tab, info["targetId"], session)
                if self.files is not None:
                    # Its root frame has its target's ID. Its downloads are its
                    # tab's, even after its parent reports it swapped out.
                    self._track(info["targetId"], tab, child)

    async def _setup_frame(self, child: str) -> None:
        tab = self.child_frames[child][0]
        try:
            for name, args in _FRAME_INIT:
                await self.send(name, args, child)
                if child not in self.child_frames:
                    return  # Detached mid-setup: nothing left to set up.
            await self._snapshot(child, tab)
            if child not in self.child_frames:
                return
            await self.send("Runtime.runIfWaitingForDebugger", session=child)
        except Exception:  # noqa: BLE001 - never expose browser-supplied errors
            # Usually the frame went away first. A paused frame is never
            # released after incomplete setup. An already-running frame is
            # still behind the browser-root interceptor; retiring its tracked
            # session makes its later requests fail closed.
            self._drop_frame(child)

    async def _event(self, message: dict[str, Any]) -> None:
        """One event, whole: its bookkeeping, then whatever it awaits."""
        if message.get("method") in _TARGET_EVENTS:
            message = _target_info(message)
        self._settle(message)
        await self._after(message)

    async def _after(self, message: dict[str, Any]) -> None:
        """The part of an event that waits on Chromium or caller code."""
        method, p, session = (
            message.get("method"),
            message.get("params", {}),
            message.get("sessionId"),
        )
        try:
            if self.files is not None and method in (
                "Browser.downloadWillBegin",
                "Browser.downloadProgress",
            ):
                guid = p.get("guid")
                current = asyncio.current_task()
                before = self.download_events.get(guid) if isinstance(guid, str) else None
                if isinstance(guid, str) and current is not None:
                    self.download_events[guid] = current
                try:
                    if before is not None:
                        await asyncio.gather(before, return_exceptions=True)
                    decision = message.get("decision")
                    if decision is not None:
                        p["tracked"] = await decision
                    await self.files.event(method, p)
                finally:
                    if isinstance(guid, str) and self.download_events.get(guid) is current:
                        del self.download_events[guid]
                return
            if method == "Target.attachedToTarget":
                info, child = p["targetInfo"], p["sessionId"]
                if child in self.probe_sessions:
                    return
                if child in self.child_frames:
                    await self._setup_frame(child)
                    return
                parent = self._owner(session)
                if info.get("type") == "iframe" and parent is not None:
                    return  # Detached before its setup began.
                if info.get("browserContextId") != self.context and parent is None:
                    await self.send("Runtime.runIfWaitingForDebugger", session=child)
                    await self.send("Target.detachFromTarget", {"sessionId": child})
                    return
                if parent is not None and info.get("type") == "worker":
                    # No document of an owned tab can make one (_NO_WORKERS). One
                    # that attaches anyway is held where auto-attach paused it,
                    # before its first line: Chromium cannot close it.
                    return
                # Popups and other targets cannot outlive the request policy.
                # This driver runs only explicitly-created top-level tabs and the
                # frames inside them.
                target = info["targetId"]
                if target not in self.ready and self.creating is not None:
                    await asyncio.shield(self.creating)
                if info["type"] != "page" or target not in self.ready:
                    await self._refuse(child, info)
                    return
                if child not in self.refused:
                    return  # Gone before it could be set up as a tab.
                del self.refused[child]
                self.sessions[target] = child
                self.tabs[target] = info
                session = child  # Initialization can race with this target's destruction.
                initializers: list[tuple[str, dict[str, Any]]] = [
                    ("Page.enable", {}),
                    _NO_WORKERS_INIT,
                    ("Runtime.enable", {}),
                    ("DOM.enable", {}),
                    ("Accessibility.enable", {}),
                    ("Network.enable", {}),
                    ("Network.setBypassServiceWorker", {"bypass": True}),
                    (
                        "Emulation.setDeviceMetricsOverride",
                        {"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": False},
                    ),
                    ("Target.setAutoAttach", _AUTO_ATTACH),
                ]
                for name, args in initializers:
                    await self.send(name, args, child)
                await self._snapshot(child, target)
                await self.send("Runtime.runIfWaitingForDebugger", session=child)
                if ready := self.ready.get(target):
                    ready.set()
            elif method == "Target.targetDestroyed":
                target = p["targetId"]
                if target not in self.ready and self.creating is not None:
                    await asyncio.shield(self.creating)
                self.drop_tab(target)
            elif method == "Fetch.requestPaused":
                if session is not None:
                    raise BrowserError(_OWNERSHIP_ERROR)
                frame = p.get("frameId")
                try:
                    tab = await self._request_owner(frame)
                    if tab is not None and frame in self.dead_frames and self.restoring:
                        # BFCache restores descendants without attach events.
                        # Wait for the already-running bounded snapshot before
                        # deciding whether a previously retired frame is live.
                        await asyncio.wait_for(
                            asyncio.shield(asyncio.gather(*self.restoring)),
                            _OWNERSHIP_DEADLINE,
                        )
                except Exception:  # noqa: BLE001 - uncertainty never permits a request
                    await self._terminate(_OWNERSHIP_ERROR)
                    return
                if self.stopping or self.closed or self.failed:
                    return
                epoch = self.request_epochs.get(frame, 0)
                allowed = True
                if tab is not None:
                    if tab not in self.sessions or frame in self.dead_frames:
                        allowed = False
                    else:
                        try:
                            await self.check_url(
                                p["request"]["url"],
                                tab,
                                navigation=message.get("navigation") is True,
                            )
                        except Exception:  # noqa: BLE001 - withhold callback errors
                            allowed = False
                    allowed = (
                        allowed
                        and tab in self.sessions
                        and frame not in self.dead_frames
                        and self.request_epochs.get(frame, 0) == epoch
                    )
                if self.stopping or self.closed or self.failed:
                    return
                if not allowed:
                    self.changes.append({"type": "navigation_refused"})
                await self.send(
                    "Fetch.continueRequest" if allowed else "Fetch.failRequest",
                    {
                        "requestId": p["requestId"],
                        **({} if allowed else {"errorReason": "BlockedByClient"}),
                    },
                    session,
                )
            elif method == "Page.javascriptDialogOpening":
                if session in self.refused:
                    # A refused popup's: dismissed, so it holds up nothing, and
                    # not reported as the tab's.
                    await self.send("Page.handleJavaScriptDialog", {"accept": False}, session)
                    return
                if self._owner(session) is None:
                    return
                self.changes.append(
                    {
                        "type": "dialog_dismissed",
                        "kind": p["type"],
                        "message": p.get("message", "")[:1000],
                    }
                )
                try:
                    await self.send("Page.handleJavaScriptDialog", {"accept": False}, session)
                except BrowserError:
                    if session not in self.child_frames:
                        raise
                    # Its tab may have answered it first; a frame's dialog
                    # left open blocks only that frame.
        except Exception as error:  # noqa: BLE001 - fail closed without exposing browser-supplied errors
            if method == "Fetch.requestPaused" and isinstance(error, _RequestGone):
                return  # Page-side cancellation invalidates the paused request ID.
            if isinstance(error, _SessionGone):
                return  # Its frame went away mid-command.
            if session is not None and self._owner(session) is None:
                return  # An in-flight command raced with target destruction.
            # A failed policy installation must never leave an unguarded page.
            await self._terminate(self.terminal_reason or "Browser request policy failed.")

    async def _refuse(self, child: str, info: dict[str, Any]) -> None:
        """Close a target this driver does not run; a popup is fenced and released first."""
        target = info["targetId"]
        popup = info.get("type") == "page"
        failed = False
        if popup:
            if child not in self.refused:
                return  # It went first: nothing is left to refuse.
            try:
                for name, args in _POPUP_FENCE:
                    if child not in self.refused:
                        return  # It went first: nothing is left to refuse.
                    await self.send(name, args, child)
                    if name == "Network.setBypassServiceWorker":
                        await self._offline(child)
                if child not in self.refused:
                    return
                await self.send("Runtime.runIfWaitingForDebugger", {}, child)
            except BrowserError:
                if child not in self.refused:
                    return
                failed = True  # Not released: closing it is all that is left.
        try:
            closed = await self.send("Target.closeTarget", {"targetId": target})
        except BrowserError:
            if popup and child not in self.refused:
                return  # It closed itself once released.
            raise
        if failed or (not closed.get("success") and (not popup or child in self.refused)):
            raise BrowserError("Unsupported browser target could not be closed.")

    async def _offline(self, session: str) -> None:
        """Every request from a refused page fails at once in the network stack.

        Per-rule emulation leaves navigator.onLine alone, which matters because
        a popup in its opener's process shares that state with the opener. It
        is offline per rule where Chromium has it (153 on), and for the whole
        target where it has only the earlier form of the command (145). A
        Chromium without the command at all gets the older one, which takes the
        opener's renderer offline too while the popup lives.
        """
        try:
            await self.send(
                "Network.emulateNetworkConditionsByRule",
                {"offline": True, "matchedNetworkConditions": [{"urlPattern": "", **_OFFLINE}]},
                session,
            )
        except _SessionGone:
            raise
        except BrowserError:
            await self.send("Network.emulateNetworkConditions", _OFFLINE, session)

    def drop_tab(self, target: str) -> None:
        self._retire_frames(target)
        if self.files is not None:
            for frame, tab in list(self.files.frames.items()):
                if tab == target:
                    self._untrack(frame)
        for frame, held in list(self.orphans.items()):
            if held[0] == target:
                del self.orphans[frame]
        for child, (tab, _, _) in list(self.child_frames.items()):
            if tab == target:
                self._drop_frame(child)
        if ready := self.ready.get(target):
            ready.set()
        for mapping in (
            self.tabs,
            self.sessions,
            self.ready,
            self.refs,
            self.console,
            self.network,
            self.buttons,
        ):
            mapping.pop(target, None)
        if self.active == target:
            self.active = next(iter(self.tabs), None)

    async def call_policy(self, callback: Callable[..., Any], *args: Any) -> Any:
        """Run a caller's URL policy on this session's own threads; see _POLICY_THREADS.

        A check waits for a thread rather than queueing work on one, and holds
        it until the callback really returns: a check that gave up cannot let
        another in beside a callback still running, and nothing queues behind
        blocked callbacks.
        """
        navigation = _NAVIGATION_CHECK.get()
        if navigation not in self._pools:
            size = _NAVIGATION_THREADS if navigation else _POLICY_THREADS
            self._pools[navigation] = (
                ThreadPoolExecutor(
                    max_workers=size,
                    thread_name_prefix="mandala-browser-"
                    + ("navigation" if navigation else "policy"),
                ),
                asyncio.Semaphore(size),
            )
        pool, threads = self._pools[navigation]
        await threads.acquire()
        loop = asyncio.get_running_loop()

        def free() -> None:
            try:
                loop.call_soon_threadsafe(threads.release)
            except RuntimeError:
                pass  # The session's loop has closed.

        def run() -> Any:
            try:
                return context.run(callback, *args)
            finally:
                free()

        context = contextvars.copy_context()
        try:
            work = pool.submit(run)
        except BaseException:
            threads.release()
            raise
        # Canceled before a thread took it, it never runs, and frees its thread here.
        work.add_done_callback(lambda done: free() if done.cancelled() else None)
        returned = await asyncio.wrap_future(work)
        return await returned if inspect.isawaitable(returned) else returned

    async def check_url(self, url: str, tab: str | None, *, navigation: bool = False) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise BrowserError(
                "Only HTTP and HTTPS URLs without embedded credentials are supported."
            )
        if len(url) > 8192:
            raise BrowserError("URL exceeds 8192 characters.")
        if self.policy:
            check = _NAVIGATION_CHECK.set(navigation)
            try:
                await asyncio.wait_for(self._invoke(self.policy, tab, url), _POLICY_DEADLINE)
            except ToolError as error:
                raise BrowserError(str(error)) from None
            except Exception:  # noqa: BLE001 - withhold arbitrary callback error text
                raise BrowserError(
                    "Navigation was refused by the URL policy or its deadline."
                ) from None
            finally:
                _NAVIGATION_CHECK.reset(check)
        return url

    async def new_tab(self) -> dict[str, Any]:
        if len(self.tabs) >= 10:
            raise BrowserError("At most ten browser tabs may be open.")
        # Create paused without auto-attachment, then explicitly attach and
        # install policy before allowing any navigation. The first document is
        # inert about:blank; no caller-supplied URL enters createTarget.
        self.creating = asyncio.get_running_loop().create_future()
        try:
            result = await self.send(
                "Target.createTarget", {"url": "about:blank", "browserContextId": self.context}
            )
            if self.failed or self.closed:
                raise BrowserError("Browser connection ended.")
            target = result["targetId"]
            ready = asyncio.Event()
            self.ready[target] = ready
            self.creating.set_result(target)
        except BaseException:
            self.creating.cancel()
            raise
        finally:
            self.creating = None
        await asyncio.wait_for(ready.wait(), 15)
        if self.failed or self.closed:
            raise BrowserError("Browser connection ended.")
        if target not in self.tabs:
            raise BrowserError("Browser tab closed during initialization.")
        self.active = target
        return self.tab_state(target)

    def tab_state(self, target: str) -> dict[str, Any]:
        tab = self.tabs[target]
        return {
            "tab_id": target,
            "url": tab.get("url", "")[:4096],
            "title": tab.get("title", "")[:1000],
            "active": target == self.active,
        }

    def state(self) -> dict[str, Any]:
        if self.active not in self.tabs:
            self.active = next(iter(self.tabs), None)
        result = {
            "tabs": [self.tab_state(t) for t in self.tabs],
            "state_changes": list(self.changes),
        }
        self.changes.clear()
        return result

    async def evaluate(self, tab: str, expression: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": 5000,
            },
            self.sessions[tab],
        )
        if "exceptionDetails" in result:
            raise BrowserError("JavaScript could not complete. Its exception text was withheld.")
        return result.get("result", {}).get("value")

    async def perform(self, name: str, data: dict[str, Any]) -> Any:
        await self.start()
        if name == "new_tab":
            return await self.new_tab()
        if name == "list_tabs":
            return [self.tab_state(t) for t in self.tabs]
        tab = data.get("tab_id") or self.active
        if tab not in self.tabs:
            raise BrowserError("Tab is missing or closed. List the tabs and choose an open tab.")
        assert tab is not None
        session = self.sessions[tab]
        if name == "switch_tab":
            await self.send("Target.activateTarget", {"targetId": tab})
            self.active = tab
            return self.tab_state(tab)
        if name == "close_tab":
            await self.send("Target.closeTarget", {"targetId": tab})
            self.drop_tab(tab)
            return None
        if name == "navigate":
            url = data["url"]
            if url in ("back", "forward"):
                history = await self.send("Page.getNavigationHistory", session=session)
                index = history["currentIndex"] + (-1 if url == "back" else 1)
                if not 0 <= index < len(history["entries"]):
                    raise BrowserError("No history entry in that direction.")
                entry = history["entries"][index]
                await self.check_url(entry["url"], tab, navigation=True)
                await self.send("Page.navigateToHistoryEntry", {"entryId": entry["id"]}, session)
            elif url == "reload":
                await self.check_url(self.tabs[tab].get("url", ""), tab, navigation=True)
                await self.send("Page.reload", session=session)
            else:
                if ":" not in url.split("/")[0]:
                    url = "https://" + url
                await self.check_url(url, tab, navigation=True)
                result = await self.send("Page.navigate", {"url": url}, session)
                if "errorText" in result:
                    raise BrowserError("Navigation failed or was refused by the URL policy.")
            self.refs.pop(tab, None)
            for _ in range(100):
                try:
                    if await self.evaluate(tab, "document.readyState") in (
                        "interactive",
                        "complete",
                    ):
                        break
                except BrowserError:
                    if self.failed or self.closed:
                        raise
                await asyncio.sleep(0.1)
            info = await self.evaluate(tab, "({url:location.href,title:document.title})")
            return info
        if name in ("screenshot", "zoom"):
            args: dict[str, Any] = {"format": "png", "captureBeyondViewport": False}
            if name == "zoom":
                region = data["region"]
                if len(region) != 4:
                    raise BrowserError("region must contain x1, y1, x2, y2")
                x1, y1, x2, y2 = region
                for value, label, limit in [
                    (x1, "x1", 1279),
                    (x2, "x2", 1280),
                    (y1, "y1", 719),
                    (y2, "y2", 720),
                ]:
                    number(value, label, limit, integer=True)
                if x2 <= x1 or y2 <= y1:
                    raise BrowserError("region must have positive width and height")
                viewport = (await self.send("Page.getLayoutMetrics", session=session))[
                    "cssVisualViewport"
                ]
                args["clip"] = {
                    "x": x1 + viewport["pageX"],
                    "y": y1 + viewport["pageY"],
                    "width": x2 - x1,
                    "height": y2 - y1,
                    "scale": 1,
                }
            image = await self.send("Page.captureScreenshot", args, session)
            return {"data": image["data"], "media_type": "image/png"}
        if name in ("read_page", "find"):
            depth = data.get("depth")
            nodes = (
                await self.send(
                    "Accessibility.getFullAXTree",
                    {
                        "depth": int(
                            number(20 if depth is None else depth, "depth", 50, integer=True)
                        )
                    },
                    session,
                )
            )["nodes"]
            root = data.get("ref")
            selected: set[str] | None = None
            if root:
                backend = self.refs.get(tab, {}).get(root)
                found = next((n for n in nodes if n.get("backendDOMNodeId") == backend), None)
                if found is None:
                    raise BrowserError("Unknown or stale element reference. Read the page again.")
                selected = {found["nodeId"]}
                for node in nodes:
                    if node["nodeId"] in selected:
                        selected.update(node.get("childIds", []))
            refs: dict[str, int] = {}
            lines = []
            query = data.get("query", "").casefold()
            if len(query) > 1000:
                raise BrowserError("query exceeds 1000 characters")
            interactive = {
                "button",
                "link",
                "textbox",
                "checkbox",
                "radio",
                "combobox",
                "slider",
                "spinbutton",
                "menuitem",
                "tab",
                "option",
                "switch",
            }
            for node in nodes:
                role = node.get("role", {}).get("value", "")
                label = str(node.get("name", {}).get("value", ""))[:1000]
                value = str(node.get("value", {}).get("value", ""))[:1000]
                if node.get("ignored") or (selected is not None and node["nodeId"] not in selected):
                    continue
                if data.get("filter") == "interactive" and role not in interactive:
                    continue
                if query and query not in (role + " " + label + " " + value).casefold():
                    continue
                prefix = ""
                if node.get("backendDOMNodeId"):
                    self.ref_counter += 1
                    ref = f"e{self.ref_counter}"
                    refs[ref] = node["backendDOMNodeId"]
                    prefix = f"[{ref}] "
                lines.append(f"{prefix}{role} {label} {value}".strip())
                if len(lines) >= 500:
                    break
            self.refs[tab] = refs
            return "\n".join(lines)[:24000] or "No matching accessible elements."
        if name == "get_page_text":
            return await self.evaluate(tab, "(document.body?.innerText || '').slice(0,24000)")
        if name in ("read_console", "read_network"):
            source = self.console if name == "read_console" else self.network
            return "\n".join(source.pop(tab, []))[-24000:] or "No entries recorded."
        if name == "javascript_exec":
            text = data["text"]
            if len(text) > 16000:
                raise BrowserError("JavaScript exceeds 16000 characters")
            # Only this explicit, confirmation-gated action evaluates model text.
            return await self.evaluate(
                tab,
                "(async()=>{const r=await (0,eval)("
                + json.dumps(text)
                + "); return String(typeof r==='string'?r:JSON.stringify(r)).slice(0,24000)})()",
            )
        if name == "wait":
            await asyncio.sleep(number(data["duration"], "duration", 30))
            return None
        if name == "type":
            text = data["text"]
            if len(text) > 16000:
                raise BrowserError("text exceeds 16000 characters")
            await self.send("Input.insertText", {"text": text}, session)
            return None
        if name == "form_input":
            if isinstance(data["value"], str) and len(data["value"]) > 16000:
                raise BrowserError("value exceeds 16000 characters")
            node = self.refs.get(tab, {}).get(data["target"]["ref"])
            if node is None:
                raise BrowserError("Unknown or stale element reference. Read the page again.")
            obj = (await self.send("DOM.resolveNode", {"backendNodeId": node}, session))["object"][
                "objectId"
            ]
            try:
                result = await self.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": obj,
                        "functionDeclaration": "function(v){if(!this.isConnected)throw Error(); if(this instanceof HTMLInputElement && this.type==='file')throw Error(); if(this instanceof HTMLInputElement && ['checkbox','radio'].includes(this.type)){if(typeof v!=='boolean')throw Error(); if(this.checked!==v)this.click();}else if(this instanceof HTMLInputElement || this instanceof HTMLTextAreaElement || this instanceof HTMLSelectElement){if(this instanceof HTMLSelectElement){const options=Array.from(this.options);const option=options.find(o=>o.value===String(v))||options.find(o=>o.label===String(v));if(!option||option.disabled||option.parentElement instanceof HTMLOptGroupElement&&option.parentElement.disabled)throw Error();v=option.value;}const p=this instanceof HTMLInputElement?HTMLInputElement.prototype:this instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:HTMLSelectElement.prototype; Object.getOwnPropertyDescriptor(p,'value').set.call(this,String(v));this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));}else throw Error();}",
                        "arguments": [{"value": data["value"]}],
                        "returnByValue": True,
                    },
                    session,
                )
                if "exceptionDetails" in result:
                    raise BrowserError(
                        "Reference is not a supported form field or its value is invalid."
                    )
            finally:
                try:
                    await self.send("Runtime.releaseObject", {"objectId": obj}, session)
                except (BrowserError, asyncio.TimeoutError):
                    # Detach/navigation may have already released the object, and
                    # a slow release must not end the session or hide the result.
                    pass
            return None
        if name in ("key", "hold_key"):
            pieces = data["text"].split()
            if not 1 <= len(pieces) <= (100 if name == "key" else 1):
                raise BrowserError("Use up to 100 key chords, or one chord for hold_key.")
            sequence = [key_chord(piece) for piece in pieces]
            repeat = int(
                number(
                    data.get("repeat") if data.get("repeat") is not None else 1,
                    "repeat",
                    100,
                    integer=True,
                )
            )
            if repeat < 1:
                raise BrowserError("repeat must be at least one")
            duration = number(data.get("duration", 0), "duration", 10)
            for keys in sequence * repeat:
                pressed = []
                try:
                    modifiers = 0
                    for key, code in keys:
                        modifiers |= {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}.get(key, 0)
                        if modifiers & 8 and len(key) == 1:
                            key = dict(zip("1234567890", "!@#$%^&*()", strict=True)).get(
                                key, key.upper()
                            )
                        character = "\r" if key == "Enter" else key if len(key) == 1 else ""
                        await self.send(
                            "Input.dispatchKeyEvent",
                            {
                                "type": "keyDown",
                                "key": key,
                                "windowsVirtualKeyCode": code,
                                "modifiers": modifiers,
                                **({"text": character} if character and not modifiers & 7 else {}),
                            },
                            session,
                        )
                        pressed.append((key, code))
                    if duration:
                        await asyncio.sleep(duration)
                finally:
                    for key, code in reversed(pressed):
                        modifiers &= ~{"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}.get(key, 0)
                        await self.send(
                            "Input.dispatchKeyEvent",
                            {
                                "type": "keyUp",
                                "key": key,
                                "windowsVirtualKeyCode": code,
                                "modifiers": modifiers,
                            },
                            session,
                        )
            return None
        point = await self.point(tab, data["target"])
        x, y = point
        if name == "scroll_to":
            return None  # Resolving the reference above also scrolls it into view.
        if name == "scroll":
            amount = (
                number(
                    data.get("scroll_amount") if data.get("scroll_amount") is not None else 3,
                    "scroll_amount",
                    50,
                    integer=True,
                )
                * 100
            )
            direction = data.get("scroll_direction")
            if direction not in ("up", "down", "left", "right"):
                raise BrowserError("scroll_direction must be up, down, left or right")
            dx, dy = {
                "up": (0, -amount),
                "down": (0, amount),
                "left": (-amount, 0),
                "right": (amount, 0),
            }[direction]
            await self.send(
                "Input.dispatchMouseEvent",
                {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy},
                session,
            )
            return None
        if name in ("hover", "mouse_move"):
            await self.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": x,
                    "y": y,
                    "buttons": self.buttons.get(tab, 0),
                    "button": "left" if self.buttons.get(tab) else "none",
                },
                session,
            )
            return None
        if name == "left_click_drag":
            fx, fy = await self.point(tab, data["from"])
            await self.send(
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": fx, "y": fy, "button": "left", "clickCount": 1},
                session,
            )
            try:
                await self.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": x, "y": y, "button": "left", "buttons": 1},
                    session,
                )
            finally:
                await self.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
                    session,
                )
            return None
        button = (
            "right" if name == "right_click" else "middle" if name == "middle_click" else "left"
        )
        count = 3 if name == "triple_click" else 2 if name == "double_click" else 1
        modifiers = 0
        if data.get("modifiers"):
            for key, _ in key_chord(data["modifiers"]):
                if key not in ("Alt", "Control", "Meta", "Shift"):
                    raise BrowserError("Click modifiers must be Alt, Control, Meta or Shift")
                modifiers |= {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}[key]
        for n in range(1, count + 1):
            args = {"x": x, "y": y, "button": button, "clickCount": n, "modifiers": modifiers}
            if name != "left_mouse_up":
                await self.send(
                    "Input.dispatchMouseEvent", {**args, "type": "mousePressed"}, session
                )
            if name == "left_mouse_down":
                self.buttons[tab] = 1
            if name != "left_mouse_down":
                self.buttons.pop(tab, None)
                await self.send(
                    "Input.dispatchMouseEvent", {**args, "type": "mouseReleased"}, session
                )
        return None

    async def point(self, tab: str, target: dict[str, Any]) -> tuple[float, float]:
        if target.get("type") == "coordinate":
            return (
                number(target.get("x"), "x", 1279, integer=True),
                number(target.get("y"), "y", 719, integer=True),
            )
        node = self.refs.get(tab, {}).get(target.get("ref", ""))
        if node is None:
            raise BrowserError("Unknown or stale element reference. Read the page again.")
        try:
            await self.send(
                "DOM.scrollIntoViewIfNeeded", {"backendNodeId": node}, self.sessions[tab]
            )
            quads = (
                await self.send("DOM.getContentQuads", {"backendNodeId": node}, self.sessions[tab])
            )["quads"]
            q = quads[0]
            return (
                number(sum(q[::2]) / 4, "element x", 1279),
                number(sum(q[1::2]) / 4, "element y", 719),
            )
        except (KeyError, IndexError):
            raise BrowserError("Element is no longer visible. Read the page again.") from None

    async def close(self) -> None:
        # AnyIO uses level cancellation: every await in the caller's cancelled
        # scope can raise again. Cleanup must run shielded, including revocation.
        with anyio.CancelScope(shield=True):
            self.closed = True
            if (
                self._starting is not None
                and self._starting is not asyncio.current_task()
                and not self._starting.done()
            ):
                try:
                    await asyncio.shield(self._starting)
                except Exception:  # noqa: BLE001, S110 - failed startup still needs teardown
                    pass
            if (
                self.cleanup is not None
                and self.cleanup.done()
                and self.files
                and self.files.created
            ):
                await self.files.close()
            if self.cleanup is None:
                self.closed = True
                self.cleanup = asyncio.create_task(self._close())
            try:
                await asyncio.shield(self.cleanup)
            except Exception:
                self.cleanup = None  # Explicit close may retry a failed revocation.
                raise

    async def _terminate(self, reason: str) -> None:
        self.terminal_reason = reason
        self.stopping = True
        if self.ending is None:
            self.ending = asyncio.create_task(self._end_socket())
        await asyncio.shield(self.ending)

    async def _end_socket(self) -> None:
        """Remove our context before removing a browser-wide interceptor.

        No policy/ownership queue is awaited. If disposal cannot be confirmed,
        disconnect with disposeOnDetach still set; never explicitly disable
        interception while the owned context may still be present.
        """
        if self.ws is None:
            self.failed = True
            return
        disposed = self.context is None
        if self.context is not None and not self.failed:
            try:
                await self.send("Target.disposeBrowserContext", {"browserContextId": self.context})
                disposed = True
                self.context = None
            except Exception:  # noqa: BLE001, S110 - disconnect remains the fail-closed fallback
                pass
        if disposed and self.intercepting and not self.failed:
            try:
                await self.send("Fetch.disable")
            except Exception:  # noqa: BLE001, S110 - disposal already removed the owned context
                pass
            self.intercepting = False
        self.failed = True
        await self.ws.close()

    async def _close(self) -> None:
        if self.lease_task is not None and self.lease_task is not asyncio.current_task():
            self.lease_task.cancel()
            await asyncio.gather(self.lease_task, return_exceptions=True)
        try:
            self.stopping = True
            if self.ending is None:
                self.ending = asyncio.create_task(self._end_socket())
            await asyncio.shield(self.ending)
            if self.reader and self.reader is not asyncio.current_task():
                await self.reader
            if self.files is not None:
                await self.files.close()
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            for pool, _ in self._pools.values():
                # A callback still running cannot be stopped; its answer is unused.
                pool.shutdown(wait=False, cancel_futures=True)
            self.tabs.clear()
            if self.grant:
                await asyncio.wait_for(self._invoke(self.revoke, self.grant.id), 10)
                self.grant = None


def key_chord(text: str) -> list[tuple[str, int]]:
    aliases = {
        "ctrl": "Control",
        "control": "Control",
        "alt": "Alt",
        "shift": "Shift",
        "super": "Meta",
        "meta": "Meta",
        "cmd": "Meta",
        "return": "Enter",
        "enter": "Enter",
        "esc": "Escape",
        "escape": "Escape",
        "space": " ",
        "tab": "Tab",
        "backspace": "Backspace",
        "delete": "Delete",
        "up": "ArrowUp",
        "down": "ArrowDown",
        "left": "ArrowLeft",
        "right": "ArrowRight",
        "home": "Home",
        "end": "End",
        "pageup": "PageUp",
        "pagedown": "PageDown",
    }
    codes = {
        "Control": 17,
        "Alt": 18,
        "Shift": 16,
        "Meta": 91,
        "Enter": 13,
        "Escape": 27,
        " ": 32,
        "Tab": 9,
        "Backspace": 8,
        "Delete": 46,
        "ArrowUp": 38,
        "ArrowDown": 40,
        "ArrowLeft": 37,
        "ArrowRight": 39,
        "Home": 36,
        "End": 35,
        "PageUp": 33,
        "PageDown": 34,
    }
    pieces = text.split("+")
    if not 1 <= len(pieces) <= 5:
        raise BrowserError("Use one key or a chord of at most five keys, separated by +")
    result = []
    for piece in pieces:
        key = aliases.get(piece.lower(), piece)
        code = codes.get(key)
        if code is None and len(key) == 1 and key.isascii() and key.isalnum():
            code = ord(key.upper())
        if code is None:
            raise BrowserError("Unsupported browser key. Use a named key or a letter/digit.")
        result.append((key, code))
    return result
