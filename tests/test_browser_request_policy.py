"""Browser-root policy composition and disposal against real Chromium."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections import Counter
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from tests.test_browser_toolset import chrome, owned_backend, paused, remote  # noqa: F401
from websockets.asyncio.client import connect

from mandala_computer import BrowserSessionPolicy
from mandala_computer._browser_cdp import BrowserCDP, BrowserError


@pytest.fixture
def policy_site() -> Any:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<body>policy test</body>")

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class RawBrowser:
    """A foreign controller without an SDK URL policy or autoattachment."""

    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.counter = 0
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.reader = asyncio.create_task(self.read())

    async def read(self) -> None:
        async for raw in self.socket:
            message = json.loads(raw)
            future = self.pending.get(message.get("id"))
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(AssertionError(message["error"]))
                else:
                    future.set_result(message.get("result", {}))

    async def send(self, method: str, params: Any = None, session: str | None = None) -> Any:
        self.counter += 1
        ident = self.counter
        self.pending[ident] = future = asyncio.get_running_loop().create_future()
        message = {"id": ident, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        try:
            await self.socket.send(json.dumps(message))
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(ident, None)

    async def page(self, *, incognito: bool = False) -> str:
        params: dict[str, Any] = {"url": "about:blank"}
        if incognito:
            params["browserContextId"] = (
                await self.send("Target.createBrowserContext", {"disposeOnDetach": True})
            )["browserContextId"]
        target = (await self.send("Target.createTarget", params))["targetId"]
        return (await self.send("Target.attachToTarget", {"targetId": target, "flatten": True}))[
            "sessionId"
        ]

    async def evaluate(self, session: str, source: str) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {"expression": source, "awaitPromise": True, "returnByValue": True},
            session,
        )
        assert "exceptionDetails" not in result, result
        return result.get("result", {}).get("value")


@asynccontextmanager
async def raw_browser(url: str) -> Any:
    async with connect(url, proxy=None) as socket:
        peer = RawBrowser(socket)
        try:
            yield peer
        finally:
            await socket.close()
            await peer.reader


async def until(condition: Any) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("browser condition did not complete")


def backend(url: str, policy: Any) -> BrowserCDP:
    computer, _ = remote(url, True)
    return BrowserCDP(
        computer.create_browser_connection, computer.revoke_browser_connection, policy
    )


async def navigate(driver: BrowserCDP, url: str) -> str:
    await driver.start()
    assert driver.active is not None
    tab = driver.active
    await driver.send("Page.navigate", {"url": url}, driver.sessions[tab])
    for _ in range(200):
        if await driver.evaluate(tab, "document.readyState") == "complete":
            return tab
        await asyncio.sleep(0.02)
    raise AssertionError("navigation did not complete")


@pytest.mark.asyncio
async def test_root_policy_composes_with_foreign_contexts_and_another_toolset(
    chrome: str,  # noqa: F811
    policy_site: Any,
) -> None:
    base, hits = policy_site
    seen: dict[str, list[tuple[str | None, str]]] = {"a": [], "b": []}

    def policy(name: str) -> Any:
        async def check(tab: str | None, url: str) -> None:
            seen[name].append((tab, url))
            if f"deny-{name}" in url:
                raise BrowserError("refused")

        return check

    first, second = backend(chrome, policy("a")), backend(chrome, policy("b"))
    async with raw_browser(chrome) as foreign:
        ordinary, incognito = await foreign.page(), await foreign.page(incognito=True)
        try:
            a = await navigate(first, base + "/a")
            b = await navigate(second, base + "/b")
            # Denied only by each computer's own policy. The other controller
            # must not see that URL in its callback, even with both interceptors.
            for driver, tab, own, other in [(first, a, "a", "b"), (second, b, "b", "a")]:
                await driver.evaluate(
                    tab,
                    f"Promise.all([fetch('{base}/deny-{own}').catch(()=>null),"
                    f"fetch('{base}/deny-{other}-allowed-by-{own}')]).then(()=>true)",
                )
                # Ordinary same-process and cross-process frames both inherit
                # this tab's callback context, exactly once per HTTP request.
                for host, kind in [("127.0.0.1", "same"), ("localhost", "cross")]:
                    url = base.replace("127.0.0.1", host) + f"/{own}-{kind}"
                    await driver.evaluate(
                        tab,
                        "new Promise(resolve=>{const f=document.createElement('iframe');"
                        f"f.src={json.dumps(url)};f.onload=()=>resolve(true);"
                        "document.body.appendChild(f)})",
                    )
                    assert seen[own].count((tab, url)) == 1
            for index, session in enumerate([ordinary, incognito]):
                await foreign.send("Page.navigate", {"url": base + f"/foreign-{index}"}, session)
                for _ in range(200):
                    if await foreign.evaluate(session, "document.readyState") == "complete":
                        break
                    await asyncio.sleep(0.02)
                # A same-process srcdoc frame has no Target metadata; resolving
                # it requires the temporary read-only foreign frame-tree probe.
                frame_source = f"<script>fetch('{base}/foreign-frame-{index}')</script>"
                worker_source = f"fetch('{base}/foreign-worker-{index}')"
                await foreign.evaluate(
                    session,
                    "const f=document.createElement('iframe');"
                    f"f.srcdoc={json.dumps(frame_source)};document.body.appendChild(f);"
                    f"new Worker(URL.createObjectURL(new Blob([{json.dumps(worker_source)}],"
                    "{type:'text/javascript'})));true",
                )
            await until(lambda: all(f"/foreign-worker-{i}" in hits for i in range(2)))
            await until(lambda: all(f"/foreign-frame-{i}" in hits for i in range(2)))
            assert "/deny-a" not in hits and "/deny-b" not in hits
            assert "/deny-b-allowed-by-a" in hits and "/deny-a-allowed-by-b" in hits
            for name, tab in [("a", a), ("b", b)]:
                assert all(owner == tab for owner, _ in seen[name])
                assert not [url for _, url in seen[name] if "/foreign-" in url]
                counts = Counter(url for _, url in seen[name])
                assert max(counts.values()) == 1
            assert not first.failed and not second.failed
            await until(lambda: not first.probe_sessions and not second.probe_sessions)
        finally:
            await first.close()
            await second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["close", "expired", "renewal-failed"])
async def test_confirmed_context_disposal_never_releases_a_held_owned_request(
    chrome: str,  # noqa: F811
    policy_site: Any,
    ending: str,
) -> None:
    base, hits = policy_site
    checked, release = asyncio.Event(), asyncio.Event()

    async def policy(_tab: str | None, url: str) -> None:
        if "/held" in url:
            checked.set()
            await release.wait()

    driver = backend(chrome, policy)
    async with raw_browser(chrome) as foreign:
        session = await foreign.page()
        try:
            tab = await navigate(driver, base + "/owner")
            await driver.evaluate(tab, f"void fetch('{base}/held').catch(()=>null)")
            await asyncio.wait_for(checked.wait(), 3)
            if ending == "close":
                await driver.close()
            else:
                from tests.test_browser_lease import short_lease

                async def refuse_renewal(_ident: str, _timeout: float) -> Any:
                    raise BrowserError("renewal refused")

                driver.session_policy = BrowserSessionPolicy(60, 120, ending == "renewal-failed")
                driver.renew = refuse_renewal
                driver._accept_lease(short_lease(), 0)
                await driver._maintain_lease()
            release.set()
            await asyncio.sleep(0.1)
            await foreign.send("Page.navigate", {"url": base + "/still-foreign"}, session)
            await until(lambda: "/still-foreign" in hits)
            assert "/held" not in hits
            assert driver.failed
        finally:
            release.set()
            await driver.close()


@pytest.mark.asyncio
async def test_abrupt_cdp_loss_ends_the_sdk_and_removes_its_context(
    chrome: str,  # noqa: F811
    policy_site: Any,
) -> None:
    """Cleanup remains supported; pending HTTP no-delivery does not.

    MANDALA_TEST_STRICT_CDP_LOSS=1 keeps the original failing Linux no-delivery
    assertion available as an opt-in characterization of OPL-5952.
    A passing characterization on one build is not an enforcement guarantee.
    """
    base, hits = policy_site
    checked, release = asyncio.Event(), asyncio.Event()

    async def policy(_tab: str | None, url: str) -> None:
        if "/held" in url:
            checked.set()
            await release.wait()

    driver = backend(chrome, policy)
    commands: list[str] = []
    plain = driver.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        commands.append(method)
        return await plain(method, params, session)

    driver.send = send
    async with raw_browser(chrome) as foreign:
        session = await foreign.page()
        try:
            tab = await navigate(driver, base + "/owner")
            owned_context = driver.context
            assert owned_context is not None
            await driver.evaluate(tab, f"void fetch('{base}/held').catch(()=>null)")
            await asyncio.wait_for(checked.wait(), 3)
            driver.ws.transport.abort()
            await asyncio.wait_for(driver.reader, 3)
            after_loss = len(commands)
            assert driver.failed and driver.session_status()["state"] == "ended"
            with pytest.raises(BrowserError, match="connection ended"):
                await driver.start()

            # A late user callback cannot send an SDK continuation. Chromium
            # itself can resume the pending request when its interceptor goes,
            # before disposeOnDetach finishes removing this browser context.
            release.set()
            await until(lambda: not driver.policy_lane.running)
            assert "Fetch.continueRequest" not in commands[after_loss:]
            for _ in range(200):
                contexts = (await foreign.send("Target.getBrowserContexts"))["browserContextIds"]
                if owned_context not in contexts:
                    break
                await asyncio.sleep(0.02)
            assert owned_context not in contexts
            await foreign.send("Page.navigate", {"url": base + "/still-foreign"}, session)
            await until(lambda: "/still-foreign" in hits)
            if os.environ.get("MANDALA_TEST_STRICT_CDP_LOSS") == "1":
                assert "/held" not in hits, (
                    "OPL-5952 CDP-loss limitation: Chromium delivered pending HTTP before "
                    "owned-context removal; SDK cleanup is not network enforcement."
                )
        finally:
            release.set()
            await driver.close()


@pytest.mark.asyncio
async def test_a_detached_frame_cannot_revive_an_old_policy_decision() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def policy(_tab: str | None, _url: str) -> None:
        entered.set()
        await release.wait()

    driver, sent = owned_backend(policy)
    driver.request_owners["child"] = "tab"
    driver.request_parents["child"] = "tab"
    request = asyncio.create_task(driver._event(paused("held", "child", "https://ok.test/")))
    await entered.wait()
    driver._note("Page.frameDetached", {"frameId": "child", "reason": "remove"}, "page")
    driver._note("Page.frameNavigated", {"frame": {"id": "child", "parentId": "tab"}}, "page")
    release.set()
    await request
    assert sent == [
        ("Fetch.failRequest", {"requestId": "held", "errorReason": "BlockedByClient"}, None)
    ]


@pytest.mark.asyncio
async def test_an_error_document_commit_does_not_cancel_the_next_navigation_policy() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def policy(_tab: str | None, _url: str) -> None:
        entered.set()
        await release.wait()

    driver, sent = owned_backend(policy)
    request = asyncio.create_task(driver._event(paused("next", "tab", "https://ok.test/next")))
    await entered.wait()
    # The previous refused navigation's error document can commit while this
    # next navigation is waiting for its policy. The main frame never detached.
    driver._note("Page.frameNavigated", {"frame": {"id": "tab"}}, "page")
    release.set()
    await request
    assert sent == [("Fetch.continueRequest", {"requestId": "next"}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("removed_after_query", [False, True])
async def test_bfcache_restores_request_ownership_without_reviving_a_later_detach(
    removed_after_query: bool,
) -> None:
    driver, sent = owned_backend()
    # File support is deliberately absent. Request ownership restoration must
    # not depend on opting into uploads/downloads.
    assert driver.files is None
    driver.request_owners["child"] = "tab"
    driver.request_parents["child"] = "tab"
    driver._note("Page.frameNavigated", {"frame": {"id": "tab"}}, "page")
    assert "child" in driver.dead_frames
    asked, answer = asyncio.Event(), asyncio.Event()
    plain = driver.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getFrameTree":
            asked.set()
            await answer.wait()
            return {
                "frameTree": {"frame": {"id": "tab"}, "childFrames": [{"frame": {"id": "child"}}]}
            }
        return await plain(method, params, session)

    driver.send = send
    driver._note(
        "Page.frameNavigated", {"frame": {"id": "tab"}, "type": "BackForwardCacheRestore"}, "page"
    )
    await asked.wait()
    request = asyncio.create_task(driver._event(paused("restored", "child", "https://ok.test/")))
    await asyncio.sleep(0)
    assert sent == []
    if removed_after_query:
        driver._note("Page.frameDetached", {"frameId": "child", "reason": "remove"}, "page")
    answer.set()
    await request
    assert [method for method, _, _ in sent] == [
        "Fetch.failRequest" if removed_after_query else "Fetch.continueRequest"
    ]
    assert ("child" in driver.dead_frames) == removed_after_query


def test_worker_context_is_learned_from_its_owned_parent_not_missing_metadata() -> None:
    driver, _ = owned_backend()
    driver._target_owner({"type": "worker", "targetId": "owned-worker"}, "tab")
    assert driver.request_owners["owned-worker"] == "tab"
    driver._target_owner({"type": "worker", "targetId": "unknown-worker"})
    assert "unknown-worker" not in driver.request_owners
    with pytest.raises(BrowserError):
        driver._target_owner(
            {"type": "worker", "targetId": "conflict", "browserContextId": "foreign"}, "tab"
        )


@pytest.mark.asyncio
async def test_foreign_probe_attachment_before_command_reply_is_read_only_and_cleaned_up() -> None:
    driver, _ = owned_backend()
    sent: list[tuple[str, Any, Any]] = []

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        sent.append((method, params, session))
        if method == "Target.attachToTarget":
            driver._dispatch(
                {
                    "method": "Target.attachedToTarget",
                    "params": {
                        "sessionId": "probe",
                        "targetInfo": {
                            "targetId": "foreign",
                            "type": "page",
                            "browserContextId": "other",
                        },
                    },
                }
            )
            return {"sessionId": "probe"}
        if method == "Page.getFrameTree":
            return {
                "frameTree": {"frame": {"id": "foreign"}, "childFrames": [{"frame": {"id": "kid"}}]}
            }
        return {}

    driver.send = send
    await driver._foreign_tree("foreign")
    await asyncio.sleep(0)
    assert driver.request_owners["kid"] is None
    assert [method for method, _, _ in sent] == [
        "Target.attachToTarget",
        "Page.getFrameTree",
        "Target.detachFromTarget",
    ]
    assert driver.probe_sessions == {} and driver.probe_targets == set()


@pytest.mark.asyncio
async def test_cancelled_foreign_probe_detaches_its_temporary_session() -> None:
    driver, _ = owned_backend()
    entered = asyncio.Event()
    detached: list[str] = []

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Target.attachToTarget":
            return {"sessionId": "probe"}
        if method == "Page.getFrameTree":
            entered.set()
            await asyncio.Event().wait()
        if method == "Target.detachFromTarget":
            detached.append(params["sessionId"])
        return {}

    driver.send = send
    task = asyncio.create_task(driver._foreign_tree("foreign"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert detached == ["probe"]
    assert driver.probe_sessions == {} and driver.probe_targets == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lookup", "provenance", "queue"])
async def test_uncertain_ownership_or_bounds_dispose_before_releasing_traffic(
    chrome: str,  # noqa: F811
    policy_site: Any,
    monkeypatch: Any,
    failure: str,
) -> None:
    from mandala_computer import _browser_cdp

    base, hits = policy_site
    entered, release = asyncio.Event(), asyncio.Event()

    async def policy(_tab: str | None, url: str) -> None:
        if "/held" in url:
            entered.set()
            await release.wait()

    driver = backend(chrome, policy)
    async with raw_browser(chrome) as foreign:
        session = await foreign.page()
        try:
            tab = await navigate(driver, base + "/owner")
            await driver.evaluate(tab, f"void fetch('{base}/held').catch(()=>null)")
            await asyncio.wait_for(entered.wait(), 3)
            if failure == "lookup":
                # A stale unobserved frame has neither a live Target nor a
                # frame-tree entry. Missing metadata is never foreign proof.
                await driver._event(paused("missing", "unobserved-gone-frame", base + "/unknown"))
            elif failure == "provenance":
                monkeypatch.setattr(_browser_cdp, "_OWNERSHIP_LIMIT", len(driver.request_owners))
                # Exercise the reader's event-failure disposal path.
                try:
                    await driver.send("Target.createTarget", {"url": "about:blank"})
                except BrowserError:
                    pass  # Disposal may finish before the create reply lands.
                await until(lambda: driver.failed)
            else:
                monkeypatch.setattr(_browser_cdp, "_EVENT_QUEUE_LIMIT", 0)
                driver.policy_lane.limit = 1
                try:
                    await driver.evaluate(tab, f"void fetch('{base}/held-again').catch(()=>null)")
                except BrowserError:
                    pass  # Disposal may finish before Runtime's reply lands.
                await until(lambda: driver.failed)
            release.set()
            await foreign.send("Page.navigate", {"url": base + "/foreign-after-failure"}, session)
            await until(lambda: "/foreign-after-failure" in hits)
            assert not [path for path in hits if path.startswith("/held")]
            assert driver.failed
        finally:
            release.set()
            await driver.close()
