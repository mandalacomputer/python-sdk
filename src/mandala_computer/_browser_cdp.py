"""Private CDP implementation shared by the sync and async Anthropic drivers."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import anyio
from anthropic.tools import ToolError
from websockets.asyncio.client import connect

from ._browser_connection import BrowserSessionLease, BrowserSessionPolicy


class BrowserError(Exception):
    """Only driver-authored messages may be exposed to a model."""


class _RequestGone(BrowserError):
    """Chromium discarded a paused request before its policy check completed."""


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
        self.renew = renew
        self.session_policy = session_policy
        self.lease: BrowserSessionLease | None = None
        self.lease_deadline = 0.0
        self.lease_task: asyncio.Task[None] | None = None
        self.terminal_reason: str | None = None
        self.ws: Any = None
        self.reader: asyncio.Task[None] | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.pending: dict[int, asyncio.Future[Any]] = {}
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
            if self.session_policy is not None:
                self._accept_lease(self.grant.lease, time.monotonic() - requested)
            self.ws = await _DirectConnect(
                self.grant.url,
                additional_headers={"Authorization": f"Bearer {self.grant.token}"},
                open_timeout=15,
                close_timeout=2,
                max_size=8 * 1024 * 1024,
            )
            self.reader = asyncio.create_task(self._read())
            self.context = (
                await self.send("Target.createBrowserContext", {"disposeOnDetach": True})
            )["browserContextId"]
            await self.send(
                "Browser.setDownloadBehavior",
                {"behavior": "deny", "browserContextId": self.context},
            )
            await self.send("Target.setDiscoverTargets", {"discover": True})
            await self.send(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
            )
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
        ended = self.failed or self.closed
        return {
            "state": "ended" if ended else "active" if self.ws is not None else "not_started",
            "lease_expires_at": lease.lease_expires_at if lease else None,
            "absolute_expires_at": lease.absolute_expires_at if lease else None,
            "remaining_seconds": max(0.0, deadline - time.monotonic())
            if lease and not ended
            else 0.0,
            "terminal_error": self.terminal_reason,
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
                assert self.renew is not None
                requested = time.monotonic()
                lease = await asyncio.wait_for(
                    self._invoke(self.renew, self.grant.id),
                    min(10.0, self.lease_deadline - requested),
                )
                self._accept_lease(lease, time.monotonic() - requested)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - never expose credentials or account error bodies
            self.terminal_reason = "Browser session renewal failed. The session ended; create a new toolset to continue."
        self.failed = True
        if self.ws is not None:
            await self.ws.close()  # Version 2 disconnect revokes the grant on the server.

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
        try:
            await self.ws.send(json.dumps(message))
            return await asyncio.wait_for(future, 15)
        finally:
            self.pending.pop(ident, None)

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
                            future.set_result(message.get("result", {}))
                else:
                    if len(self.tasks) >= 256:
                        raise BrowserError("Browser exceeded the pending event limit.")
                    task = asyncio.create_task(self._event(message))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
        except Exception:  # noqa: BLE001, S110 - fail closed without exposing browser-supplied errors
            pass
        finally:
            self.failed = True
            if self.lease_task is not None and self.lease_task is not asyncio.current_task():
                self.lease_task.cancel()
            for ready in self.ready.values():
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

    async def _event(self, message: dict[str, Any]) -> None:
        method, p, session = (
            message.get("method"),
            message.get("params", {}),
            message.get("sessionId"),
        )
        try:
            if method == "Target.attachedToTarget":
                info, child = p["targetInfo"], p["sessionId"]
                if (
                    info.get("browserContextId") != self.context
                    and session not in self.sessions.values()
                ):
                    await self.send("Runtime.runIfWaitingForDebugger", session=child)
                    await self.send("Target.detachFromTarget", {"sessionId": child})
                    return
                # Workers and popups cannot outlive the request policy. This
                # first driver permits only explicitly-created top-level tabs.
                target = info["targetId"]
                if target not in self.ready and self.creating is not None:
                    await asyncio.shield(self.creating)
                if info["type"] != "page" or target not in self.ready:
                    closed = await self.send("Target.closeTarget", {"targetId": target})
                    if not closed.get("success"):
                        raise BrowserError("Unsupported browser target could not be closed.")
                    return
                self.sessions[target] = child
                self.tabs[target] = info
                session = child  # Initialization can race with this target's destruction.
                initializers: list[tuple[str, dict[str, Any]]] = [
                    ("Page.enable", {}),
                    ("Runtime.enable", {}),
                    ("DOM.enable", {}),
                    ("Accessibility.enable", {}),
                    ("Network.enable", {}),
                    ("Network.setBypassServiceWorker", {"bypass": True}),
                    (
                        "Emulation.setDeviceMetricsOverride",
                        {"width": 1280, "height": 720, "deviceScaleFactor": 1, "mobile": False},
                    ),
                    (
                        "Fetch.enable",
                        {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                    ),
                    (
                        "Target.setAutoAttach",
                        {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                    ),
                ]
                for name, args in initializers:
                    await self.send(name, args, child)
                await self.send("Runtime.runIfWaitingForDebugger", session=child)
                if ready := self.ready.get(target):
                    ready.set()
            elif method == "Target.targetInfoChanged":
                info = p["targetInfo"]
                if info["targetId"] in self.tabs:
                    self.tabs[info["targetId"]] = info
            elif method == "Target.targetDestroyed":
                target = p["targetId"]
                if target not in self.ready and self.creating is not None:
                    await asyncio.shield(self.creating)
                self.drop_tab(target)
            elif method == "Fetch.requestPaused":
                tab = next((t for t, s in self.sessions.items() if s == session), None)
                if tab is None:
                    return  # The target has already detached; its requests die with it.
                allowed = True
                try:
                    await self.check_url(p["request"]["url"], tab)
                except Exception:  # noqa: BLE001 - fail closed without exposing callback errors
                    allowed = False
                if self.sessions.get(tab) != session:
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
                self.changes.append(
                    {
                        "type": "dialog_dismissed",
                        "kind": p["type"],
                        "message": p.get("message", "")[:1000],
                    }
                )
                await self.send("Page.handleJavaScriptDialog", {"accept": False}, session)
            else:
                tab = next((t for t, s in self.sessions.items() if s == session), None)
                if tab:
                    if method == "Page.frameNavigated":
                        self.refs.pop(tab, None)
                    elif method == "Runtime.consoleAPICalled":
                        text = " ".join(
                            str(a.get("value", a.get("description", "")))[:1000]
                            for a in p.get("args", [])[:20]
                        )
                        self.console.setdefault(tab, deque(maxlen=100)).append(text[:2000])
                    elif method == "Network.responseReceived":
                        response = p["response"]
                        self.network.setdefault(tab, deque(maxlen=100)).append(
                            f"{response['status']} {response['url'][:2000]}"
                        )
        except Exception as error:  # noqa: BLE001 - fail closed without exposing browser-supplied errors
            if method == "Fetch.requestPaused" and isinstance(error, _RequestGone):
                return  # Page-side cancellation invalidates the paused request ID.
            if session is not None and session not in self.sessions.values():
                return  # An in-flight command raced with target destruction.
            # A failed policy installation must never leave an unguarded page.
            self.failed = True
            if self.ws is not None:
                await self.ws.close()

    def drop_tab(self, target: str) -> None:
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

    async def check_url(self, url: str, tab: str | None) -> str:
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
            try:
                await asyncio.wait_for(self._invoke(self.policy, tab, url), 5)
            except ToolError as error:
                raise BrowserError(str(error)) from None
            except Exception:  # noqa: BLE001 - withhold arbitrary callback error text
                raise BrowserError(
                    "Navigation was refused by the URL policy or its deadline."
                ) from None
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
                await self.check_url(entry["url"], tab)
                await self.send("Page.navigateToHistoryEntry", {"entryId": entry["id"]}, session)
            elif url == "reload":
                await self.check_url(self.tabs[tab].get("url", ""), tab)
                await self.send("Page.reload", session=session)
            else:
                if ":" not in url.split("/")[0]:
                    url = "https://" + url
                await self.check_url(url, tab)
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
            nodes = (
                await self.send(
                    "Accessibility.getFullAXTree",
                    {"depth": int(number(data.get("depth") or 20, "depth", 50, integer=True))},
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
                except BrowserError:
                    pass  # Detach/navigation may have already released the object.
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
            if self.cleanup is None:
                self.closed = True
                self.cleanup = asyncio.create_task(self._close())
            try:
                await asyncio.shield(self.cleanup)
            except Exception:
                self.cleanup = None  # Explicit close may retry a failed revocation.
                raise

    async def _close(self) -> None:
        if self.lease_task is not None and self.lease_task is not asyncio.current_task():
            self.lease_task.cancel()
            await asyncio.gather(self.lease_task, return_exceptions=True)
        try:
            if self.ws is not None:
                if self.context and not self.failed:
                    try:
                        await self.send(
                            "Target.disposeBrowserContext", {"browserContextId": self.context}
                        )
                    except Exception:  # noqa: BLE001, S110 - fail closed without exposing browser-supplied errors
                        pass
                await self.ws.close()
            if self.reader and self.reader is not asyncio.current_task():
                await self.reader
        finally:
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
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
