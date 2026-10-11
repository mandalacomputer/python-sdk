"""Browser toolsets through Anthropic's real pipeline and, when available, Chromium."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import selectors
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import anyio
import pytest
import respx
from anthropic.tools import ToolError, ToolsetConfigError
from anthropic.types.beta import BetaToolUseBlock

from mandala_computer import AsyncClient, BrowserConnection, Client, MandalaError
from mandala_computer.anthropic import AsyncMandalaBrowserToolset, MandalaBrowserToolset

BASE = "https://api.test/api/v1"
IDENT = "a" * 32
TOKEN = "bcdp_" + "b" * 64
PAYLOAD = {
    "id": IDENT,
    "url": f"wss://api.test/api/v1/computers/vm-1/browser-connections/{IDENT}/cdp",
    "token": TOKEN,
    "expires_at": "2026-10-09T01:00:00Z",
}


def use(name: str, **data: Any) -> BetaToolUseBlock:
    return BetaToolUseBlock(
        type="tool_use", id="toolu_browser", name=name, toolset_name="browser", input=data
    )


def text(result: Any) -> str:
    return "\n".join(b["text"] for b in result["content"] if b["type"] == "text")


def tabs(result: Any) -> list[Any]:
    return next(b["tabs"] for b in result["content"] if b["type"] == "browser_state")


@respx.mock
@pytest.mark.parametrize("asynchronous", [False, True])
def test_connection_lifecycle_and_redaction(asynchronous: bool) -> None:
    respx.get(f"{BASE}/computers/vm-1").respond(200, json={"id": "vm-1", "status": "running"})
    create = respx.post(f"{BASE}/computers/vm-1/browser-connections").respond(201, json=PAYLOAD)
    revoke = respx.delete(f"{BASE}/computers/vm-1/browser-connections/{IDENT}").respond(
        200, json={"ok": True}
    )
    if asynchronous:

        async def run() -> BrowserConnection:
            async with AsyncClient("com_test", base_url=BASE) as client:
                c = await client.computers.get("vm-1")
                grant = await c.create_browser_connection()
                await c.revoke_browser_connection(grant.id)
                return grant

        grant = asyncio.run(run())
    else:
        with Client("com_test", base_url=BASE) as client:
            c = client.computers.get("vm-1")
            grant = c.create_browser_connection()
            c.revoke_browser_connection(grant.id)
    assert grant.token == TOKEN and TOKEN not in repr(grant)
    assert grant.expires_at.tzinfo is not None
    assert json.loads(create.calls[0].request.content) == {}
    assert revoke.called


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "wss://evil.test/cdp"),
        ("url", PAYLOAD["url"] + "?token=secret"),
        ("token", "not-a-token"),
        ("id", "../escape"),
        ("expires_at", "2026-10-09T01:00:00"),
        ("expires_at", {}),
    ],
)
def test_invalid_grant_never_echoes_secret(field: str, value: Any) -> None:
    with pytest.raises(MandalaError) as error:
        BrowserConnection.from_api(
            {**PAYLOAD, field: value}, BASE, "computers/vm-1/browser-connections"
        )
    assert TOKEN not in str(error.value) and "evil" not in str(error.value)


def test_disabled_upload_and_confirm_required_for_javascript() -> None:
    computer = SimpleNamespace(
        create_browser_connection=lambda: None, revoke_browser_connection=lambda _: None
    )
    with MandalaBrowserToolset(computer) as browser:
        assert browser.to_dict()["configs"]["file_upload"]["enabled"] is False
        result = browser.tool_result(use("javascript_exec", text="1+1"))
        assert result.get("is_error")
        assert browser._backend.state()["tabs"] == []
    with pytest.raises(ToolsetConfigError):
        MandalaBrowserToolset(computer, configs={"javascript_exec": {"enabled": True}})


@pytest.fixture
def chrome(tmp_path: Any) -> Any:
    executable = os.environ.get("MANDALA_TEST_CHROMIUM")
    if not executable:
        pytest.skip("set MANDALA_TEST_CHROMIUM for the real-browser integration tests")
    process = subprocess.Popen(
        [
            executable,
            "--headless",
            "--no-sandbox",
            "--no-first-run",
            # The guest's Chromium isolates sites; say so rather than rely on
            # this build's default, so cross-site frames are their own targets.
            "--site-per-process",
            "--remote-debugging-port=0",
            f"--user-data-dir={tmp_path}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    try:
        assert process.stderr
        selector = selectors.DefaultSelector()
        selector.register(process.stderr, selectors.EVENT_READ)
        while selector.select(timeout=10):
            line = process.stderr.readline().decode()
            if "DevTools listening on " in line:
                yield line.strip().split()[-1]
                return
        pytest.fail("Chromium did not expose its CDP endpoint")
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def website() -> Any:
    hits: list[str] = []
    html = b"""<title>Driver test</title><label>Name <input id="name"></label><label>Agree <input id="agree" type="checkbox"></label><button onclick="document.getElementById('result').textContent=document.getElementById('name').value">Apply</button><p id="result">pending</p><button onclick="alert('hello')">Dialog</button><a href="/download" download>Download</a>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/blocked")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            if self.path == "/download":
                self.send_header("Content-Disposition", 'attachment; filename="private.txt"')
            self.end_headers()
            self.wfile.write(html)

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


def remote(chrome: str, asynchronous: bool) -> tuple[Any, list[str]]:
    revoked: list[str] = []
    grant = BrowserConnection(IDENT, chrome, TOKEN, datetime.now(timezone.utc))

    async def create() -> BrowserConnection:
        return grant

    async def revoke(ident: str) -> None:
        revoked.append(ident)

    return SimpleNamespace(
        create_browser_connection=create if asynchronous else lambda: grant,
        revoke_browser_connection=revoke if asynchronous else revoked.append,
    ), revoked


def ref(result: Any, description: str) -> dict[str, str]:
    line = next(line for line in text(result).splitlines() if description in line)
    return {"type": "ref", "ref": re.search(r"\[(e\d+)\]", line).group(1)}


def assert_success(result: Any) -> Any:
    assert not result.get("is_error"), text(result)
    return result


def test_sync_real_browser(chrome: str, website: Any) -> None:
    computer, revoked = remote(chrome, False)
    base, hits = website
    seen: list[str] = []

    def policy(_context: Any, url: str) -> None:
        seen.append(url)
        if url.endswith("/blocked"):
            raise ToolError("not permitted")

    with MandalaBrowserToolset(computer, url_policy=policy) as browser:
        nav = assert_success(browser.tool_result(use("navigate", url=base)))
        tab = tabs(nav)[0]["tab_id"]
        page = assert_success(browser.tool_result(use("read_page", filter="interactive")))
        name = ref(page, "textbox Name")
        assert_success(browser.tool_result(use("form_input", target=name, value="Mandala")))
        assert_success(browser.tool_result(use("left_click", target=ref(page, "button Apply"))))
        assert "Mandala" in text(assert_success(browser.tool_result(use("get_page_text"))))
        shot = assert_success(browser.tool_result(use("screenshot")))
        image = next(b for b in shot["content"] if b["type"] == "image")
        png = base64.b64decode(image["source"]["data"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(png[16:20], "big") == 1280
        assert browser.tool_result(
            use("left_click", target={"type": "coordinate", "x": 1280, "y": 0})
        ).get("is_error")
        assert_success(browser.tool_result(use("new_tab")))
        assert len(tabs(assert_success(browser.tool_result(use("switch_tab", tab_id=tab))))) == 2
        refused = browser.tool_result(use("navigate", url=base + "/redirect"))
        assert refused.get("is_error") and "/blocked" not in hits
        assert base + "/blocked" in seen
        # A refused redirect may retain the old document. Successful navigation
        # deterministically invalidates references from that document.
        assert_success(browser.tool_result(use("navigate", url=base + "/after-redirect")))
        assert browser.tool_result(use("left_click", target=name)).get("is_error")
    assert revoked == [IDENT]
    assert TOKEN not in str(refused)


@pytest.mark.asyncio
async def test_async_real_browser(chrome: str, website: Any) -> None:
    computer, revoked = remote(chrome, True)
    base, _ = website
    confirms: list[str] = []

    async def confirm(context: Any) -> bool:
        confirms.append(context.member)
        return True

    async with AsyncMandalaBrowserToolset(
        computer,
        configs={
            "javascript_exec": {"enabled": True},
            "read_console": {"enabled": True},
            "read_network": {"enabled": True},
        },
        confirm=confirm,
    ) as browser:
        assert_success(await browser.tool_result(use("navigate", url=base)))
        answer = assert_success(await browser.tool_result(use("javascript_exec", text="6 * 7")))
        assert "42" in text(answer) and "javascript_exec" in confirms
        assert_success(await browser.tool_result(use("new_tab")))
        state = tabs(await browser.tool_result(use("list_tabs")))
        assert len(state) == 2 and sum(t["active"] for t in state) == 1
        assert_success(await browser.tool_result(use("close_tab", tab_id=state[0]["tab_id"])))
        assert len(tabs(await browser.tool_result(use("list_tabs")))) == 1
    assert revoked == [IDENT]


@pytest.mark.asyncio
async def test_anyio_cancellation_disposes_and_revokes() -> None:
    revoked: list[str] = []

    class Socket:
        closed = False

        async def close(self) -> None:
            await asyncio.sleep(0)
            self.closed = True

    async def revoke(ident: str) -> None:
        await asyncio.sleep(0)
        revoked.append(ident)

    browser = AsyncMandalaBrowserToolset(
        SimpleNamespace(create_browser_connection=lambda: None, revoke_browser_connection=revoke)
    )
    backend = browser._backend
    backend.grant = SimpleNamespace(id=IDENT)
    backend.ws = socket = Socket()
    backend.context = "context"
    backend.tabs = {"t": {}}
    backend.sessions = {"t": "s"}
    backend.active = "t"
    calls: list[str] = []

    async def send(method: str, *_: Any, **__: Any) -> Any:
        calls.append(method)
        await asyncio.sleep(0)
        return {}

    backend.send = send
    with anyio.move_on_after(0.01) as scope:
        await browser.tool_result(use("wait", duration=5))
    assert scope.cancel_called and socket.closed
    assert revoked == [IDENT] and "Target.disposeBrowserContext" in calls
    await browser.close()
    assert revoked == [IDENT]


@pytest.mark.asyncio
async def test_review_regressions_real_browser(chrome: str, website: Any) -> None:
    computer, revoked = remote(chrome, True)
    base, _ = website
    reject_reload = False
    pending, release = asyncio.Event(), asyncio.Event()

    async def policy(_ctx: Any, url: str) -> None:
        if url.endswith("/slow"):
            pending.set()
            await release.wait()
        if reject_reload or url.endswith("/blocked"):
            raise ToolError("Allowed origin only.")

    async with AsyncMandalaBrowserToolset(
        computer,
        configs={
            "javascript_exec": {"enabled": True},
            "read_console": {"enabled": True},
            "read_network": {"enabled": True},
        },
        confirm=lambda _: True,
        url_policy=policy,
    ) as browser:

        async def call(name: str, **data: Any) -> Any:
            return assert_success(await browser.tool_result(use(name, **data)))

        async def js(expression: str) -> str:
            return text(await call("javascript_exec", text=expression))

        first = tabs(await call("navigate", url=base))[0]["tab_id"]
        await js(
            "document.body.innerHTML += '<label>Choice <select id=choice><option value=one>First</option><option value=two>Second</option></select></label>'; document.addEventListener('mousemove', e => window.buttons=e.buttons); document.getElementById('name').focus()"
        )
        await call("key", text="a")
        assert await js("document.getElementById('name').value") == "a"
        await call("key", text="b c Backspace")
        assert await js("document.getElementById('name').value") == "ab"
        await js(
            "document.body.insertAdjacentHTML('beforeend', '<textarea id=area></textarea><form id=form><input id=field><button>Submit</button></form>');document.getElementById('area').focus();window.keys=[];document.getElementById('area').addEventListener('keyup',e=>keys.push([e.key,e.shiftKey]));window.submitted=0;document.getElementById('form').onsubmit=e=>{e.preventDefault();submitted++}"
        )
        await call("key", text="Shift+1 Shift+a Enter")
        assert await js("JSON.stringify(document.getElementById('area').value)") == '"!A\\n"'
        assert await js("JSON.stringify(keys.slice(0,2))") == '[["!",true],["Shift",false]]'
        await js("document.getElementById('field').focus()")
        await call("key", text="Enter")
        assert await js("window.submitted") == "1"
        point = {"type": "coordinate", "x": 20, "y": 20}
        await call("left_mouse_down", target=point)
        await call("mouse_move", target={**point, "x": 50})
        assert await js("window.buttons") == "1"
        await call("left_mouse_up", target=point)
        page = await call("read_page", filter="interactive")
        choice = ref(page, "combobox Choice")
        await call("form_input", target=choice, value="Second")
        assert await js("document.getElementById('choice').value") == "two"
        assert (await browser.tool_result(use("form_input", target=choice, value="missing"))).get(
            "is_error"
        )
        assert await js("document.getElementById('choice').value") == "two"
        name = ref(page, "textbox Name")
        assert (await browser.tool_result(use("form_input", target=name, value="x" * 16001))).get(
            "is_error"
        )
        assert await js("document.getElementById('name').value") == "ab"
        for member, data in [
            ("scroll", {"target": point, "scroll_direction": "diagonal"}),
            ("zoom", {}),
            ("type", {}),
            ("left_click", {}),
        ]:
            assert (await browser.tool_result(use(member, **data))).get("is_error")
        await call("get_page_text")
        await js("console.log('unique-console-message')")
        assert "unique-console-message" in text(await call("read_console"))
        assert "unique-console-message" not in text(await call("read_console"))
        await js("fetch('/logged').then(r=>r.text())")
        assert "/logged" in text(await call("read_network"))
        assert "/logged" not in text(await call("read_network"))
        await js(
            "window.fetchControl=new AbortController();void fetch('/slow',{signal:fetchControl.signal}).catch(()=>{})"
        )
        await asyncio.wait_for(pending.wait(), 2)
        await js("fetchControl.abort()")
        release.set()
        await asyncio.sleep(0.1)
        await call("get_page_text")
        pending.clear()
        release.clear()
        reject_reload = True
        refused = await browser.tool_result(use("navigate", url="reload"))
        assert refused.get("is_error") and "Allowed origin only." in text(refused)
        reject_reload = False
        await call("form_input", target=name, value="still valid")
        second = next(t["tab_id"] for t in tabs(await call("new_tab")) if t["tab_id"] != first)
        await call("javascript_exec", tab_id=first, text="void fetch('/slow').catch(()=>{})")
        await asyncio.wait_for(pending.wait(), 2)
        await call("close_tab", tab_id=first)
        release.set()
        await asyncio.sleep(0.1)
        await call("get_page_text", tab_id=second)
        assert first not in browser._backend.ready and first not in browser._backend.sessions
        browser._backend.changes.append({"type": "navigation_refused"})
        await call("list_tabs")
        result = await call("get_page_text")
        assert "refused" in text(result).lower()
    assert revoked == [IDENT]


@pytest.mark.asyncio
async def test_zoom_uses_scrolled_viewport_origin() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = object()
    backend.tabs = {"t": {}}
    backend.sessions = {"t": "s"}
    backend.active = "t"
    clip: dict[str, Any] = {}

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"pageX": 10, "pageY": 800}}
        clip.update(params["clip"])
        return {"data": "image"}

    backend.send = send
    await backend.perform("zoom", {"region": [0, 20, 100, 120]})
    assert clip == {"x": 10, "y": 820, "width": 100, "height": 100, "scale": 1}


@pytest.mark.asyncio
async def test_navigation_fails_promptly_after_transport_loss() -> None:
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = object()
    backend.tabs = {"t": {}}
    backend.sessions = {"t": "s"}
    backend.active = "t"

    async def send(*_: Any, **__: Any) -> Any:
        return {}

    async def evaluate(*_: Any) -> Any:
        backend.failed = True
        raise BrowserError("Browser connection ended.")

    backend.send = send
    backend.evaluate = evaluate
    with pytest.raises(BrowserError, match="ended"):
        await asyncio.wait_for(backend.perform("navigate", {"url": "https://example.com"}), 1)


def test_sync_revocation_can_retry_without_retaining_worker(chrome: str, website: Any) -> None:
    computer, _ = remote(chrome, False)
    calls: list[str] = []

    def revoke(ident: str) -> None:
        calls.append(ident)
        if len(calls) == 1:
            raise RuntimeError("temporary revoke failure")

    computer.revoke_browser_connection = revoke
    browser = MandalaBrowserToolset(computer)
    assert_success(browser.tool_result(use("navigate", url=website[0])))
    with pytest.raises(ToolError, match="could not be revoked"):
        browser.close()
    assert browser._worker is None
    browser.close()
    browser.close()
    assert calls == [IDENT, IDENT]


@pytest.mark.asyncio
async def test_tab_reply_followed_by_eof_does_not_wait_for_readiness() -> None:
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError

    class Socket:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[str] = asyncio.Queue()
            self.delivered = False

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            if self.delivered:
                raise StopAsyncIteration
            self.delivered = True
            return await self.queue.get()

        async def send(self, raw: str) -> None:
            message = json.loads(raw)
            await self.queue.put(json.dumps({"id": message["id"], "result": {"targetId": "new"}}))

        async def close(self) -> None:
            pass

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = Socket()
    backend.reader = asyncio.create_task(backend._read())
    with pytest.raises(BrowserError, match="ended"):
        await asyncio.wait_for(backend.new_tab(), 1)
    await backend.reader
    assert not backend.ready and not backend.tabs


@pytest.mark.asyncio
async def test_target_destroyed_before_create_reply_wakes_waiter() -> None:
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError

    class Socket:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[str | None] = asyncio.Queue()

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            raw = await self.queue.get()
            if raw is None:
                raise StopAsyncIteration
            return raw

        async def send(self, raw: str) -> None:
            message = json.loads(raw)
            await self.queue.put(
                json.dumps({"method": "Target.targetDestroyed", "params": {"targetId": "new"}})
            )
            await self.queue.put(json.dumps({"id": message["id"], "result": {"targetId": "new"}}))

        async def close(self) -> None:
            await self.queue.put(None)

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = socket = Socket()
    backend.tabs = {"other": {}}
    backend.sessions = {"other": "existing"}
    backend.active = "other"
    backend.reader = asyncio.create_task(backend._read())
    try:
        with pytest.raises(BrowserError, match="closed during initialization"):
            await asyncio.wait_for(backend.new_tab(), 1)
        assert not backend.failed and not backend.ready
        assert backend.active == "other" and list(backend.tabs) == ["other"]
    finally:
        await socket.close()
        await backend.reader


@pytest.mark.asyncio
async def test_tab_destroyed_during_initialization_preserves_other_tabs() -> None:
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = object()
    backend.context = "context"
    backend.tabs = {"other": {}}
    backend.sessions = {"other": "existing"}
    backend.active = "other"
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Target.createTarget":
            return {"targetId": "new"}
        if method == "Page.enable":
            entered.set()
            await release.wait()
            raise BrowserError("Target closed")
        assert session == "existing"
        return {"result": {"value": "Remaining page"}}

    backend.send = send
    creation = asyncio.create_task(backend.new_tab())
    await asyncio.sleep(0)
    initialization = asyncio.create_task(
        backend._event(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": "new-session",
                    "targetInfo": {
                        "targetId": "new",
                        "type": "page",
                        "browserContextId": "context",
                    },
                },
            }
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    await backend._event({"method": "Target.targetDestroyed", "params": {"targetId": "new"}})
    release.set()
    await initialization
    with pytest.raises(BrowserError, match="closed during initialization"):
        await asyncio.wait_for(creation, 1)
    assert not backend.failed and not backend.ready
    assert backend.active == "other"
    assert await backend.perform("get_page_text", {}) == "Remaining page"


@pytest.mark.asyncio
async def test_accessibility_rows_without_dom_nodes_do_not_advertise_refs() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = object()
    backend.tabs = {"t": {}}
    backend.sessions = {"t": "s"}
    backend.active = "t"

    async def send(*_: Any, **__: Any) -> Any:
        return {
            "nodes": [
                {"role": {"value": "paragraph"}, "name": {"value": "Virtual text"}},
                {
                    "role": {"value": "button"},
                    "name": {"value": "Real button"},
                    "backendDOMNodeId": 42,
                },
            ]
        }

    backend.send = send
    page = await backend.perform("read_page", {})
    assert page.splitlines() == ["paragraph Virtual text", "[e1] button Real button"]
    assert backend.refs["t"] == {"e1": 42}


@pytest.mark.asyncio
async def test_async_driver_sync_url_policy_does_not_block_event_loop() -> None:
    entered, release = threading.Event(), threading.Event()
    completed: list[bool] = []

    def policy(_context: Any, _url: str) -> None:
        entered.set()
        completed.append(release.wait(2))
        raise ToolError("Expected test refusal")

    async with AsyncMandalaBrowserToolset(
        SimpleNamespace(
            create_browser_connection=lambda: None, revoke_browser_connection=lambda _: None
        ),
        url_policy=policy,
    ) as browser:
        call = asyncio.create_task(browser.tool_result(use("navigate", url="https://example.com")))
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
        release.set()
        result = await call
        assert result.get("is_error") and completed == [True]


@pytest.mark.asyncio
async def test_implicit_action_uses_remaining_tab_after_active_target_dies() -> None:
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = object()
    backend.tabs = {"active": {}, "other": {}}
    backend.sessions = {"active": "a", "other": "b"}
    backend.active = "active"
    used_sessions: list[str] = []

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        used_sessions.append(session)
        return {"result": {"value": "Remaining page"}}

    backend.send = send
    # An external target closure occurs between tool calls, without state().
    await backend._event({"method": "Target.targetDestroyed", "params": {"targetId": "active"}})
    assert await backend.perform("get_page_text", {}) == "Remaining page"
    assert used_sessions == ["b"]
    await backend._event({"method": "Target.targetDestroyed", "params": {"targetId": "other"}})
    with pytest.raises(BrowserError, match="Tab is missing"):
        await backend.perform("get_page_text", {})
    assert backend.active is None


# --- cross-site frames, workers and other targets ------------------------------


@pytest.fixture
def cross_site() -> Any:
    """One server, two sites: the page on 127.0.0.1 embeds a frame from localhost.

    Under site isolation the frame is its own CDP target, attached beneath the
    page, and it embeds a 127.0.0.1 frame of its own, which attaches beneath it.
    It also embeds a localhost frame, which runs in its own process. Each
    cross-site frame asks for a URL the policy refuses and reports, to the page,
    whether it got through. The page and the frame each try to start a
    dedicated worker, which would do the same if it ran, and report whether it
    was refused. The frame is a button at a known
    place, which reports a click. ``/churn``, served from localhost, adds and
    removes 127.0.0.1 frames faster than they can be set up, then says so.
    """
    hits: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append((self.headers.get("Host", "").split(":")[0], self.path))
            port = self.server.server_port
            probe = (
                "fetch('{0}-allowed').then(()=>fetch('{0}-blocked'))"
                ".then(()=>'fetched',()=>'refused')"
            )
            pages = {
                "/": f"""<title>Cross-site</title><p id=log>waiting</p><script>
const seen=new Set();const show=v=>{{seen.add(String(v));document.getElementById('log').textContent=[...seen].sort().join(' ')}};
addEventListener('message',e=>show(e.data));
try{{new Worker('/worker.js').onmessage=e=>show('worker:'+e.data);show('page-worker:made')}}catch(e){{show('page-worker:refused')}}
</script><iframe src="http://localhost:{port}/frame"
style="position:absolute;left:0;top:100px;width:600px;height:300px;border:0"></iframe>""",
                "/frame": f"""<body style="margin:0"><button style="width:600px;height:200px"
onclick="parent.postMessage('clicked','*')">inside</button><script>try{{new Worker('/worker.js');parent.postMessage('frame-worker:made','*')}}catch(e){{parent.postMessage('frame-worker:refused','*')}};{probe.format("/frame")}.then(r=>parent.postMessage('frame:'+r,'*'))</script>
<iframe src="http://127.0.0.1:{port}/nested" style="height:60px"></iframe>
<iframe src="http://localhost:{port}/inner" style="height:60px"></iframe></body>""",
                "/inner": "<p>inner</p>",
                "/churn": f"""<title>Churn</title><p id=log>churning</p><script>
let n=0;const t=setInterval(()=>{{const f=document.createElement('iframe');
f.src='http://127.0.0.1:{port}/lite?'+n;document.body.appendChild(f);
setTimeout(()=>f.remove(),n%10?n%7:500);if(++n===300){{clearInterval(t);
setTimeout(()=>{{const last=document.createElement('iframe');last.src='http://127.0.0.1:{port}/lite?last';last.onload=()=>document.getElementById('log').textContent='churned '+n;document.body.appendChild(last)}},300)}}}},3);
</script>""",
                "/lite": f"<script>{probe.format('/lite')}</script>",
                "/nested": f"<p>nested</p><script>{probe.format('/nested')}.then(r=>top.postMessage('nested:'+r,'*'))</script>",
            }
            if self.path == "/worker.js":
                body = (probe.format("/worker") + ".then(r=>postMessage(r))").encode()
                kind = "text/javascript"
            else:
                body = pages.get(self.path, "ok").encode()
                kind = "text/html"
            try:
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A churned frame went away mid-answer.

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


CROSS_SITE_DONE = "frame-worker:refused frame:refused nested:refused page-worker:refused"


def assert_no_workers(hits: list[tuple[str, str]]) -> None:
    # Neither worker started: its script was never even asked for.
    assert not [path for _, path in hits if path.startswith("/worker")]


def assert_cross_site_policy(port: int, hits: list[tuple[str, str]], seen: list[str]) -> None:
    # Each frame ran and its requests met the policy: allowed ones reached the
    # server, refused ones never did.
    for host, path in [
        ("localhost", "/frame"),
        ("localhost", "/frame-allowed"),
        ("127.0.0.1", "/nested"),
        ("127.0.0.1", "/nested-allowed"),
    ]:
        assert (host, path) in hits
    assert not [path for _, path in hits if path in ("/frame-blocked", "/nested-blocked")]
    for url in [
        f"http://localhost:{port}/frame-blocked",
        f"http://127.0.0.1:{port}/nested-blocked",
    ]:
        assert url in seen


def test_sync_cross_site_frame_runs_under_the_policy(chrome: str, cross_site: Any) -> None:
    computer, revoked = remote(chrome, False)
    port, hits = cross_site
    seen: list[str] = []

    def policy(_context: Any, url: str) -> None:
        seen.append(url)
        if url.endswith("-blocked"):
            raise ToolError("not permitted")

    with MandalaBrowserToolset(computer, url_policy=policy) as browser:
        nav = assert_success(browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}/")))
        assert len(tabs(nav)) == 1
        page = ""
        for _ in range(50):
            page = text(assert_success(browser.tool_result(use("get_page_text"))))
            if CROSS_SITE_DONE in page:
                break
            assert_success(browser.tool_result(use("wait", duration=0.1)))
        assert CROSS_SITE_DONE in page
        # Coordinate input reaches into the cross-site frame.
        point = {"type": "coordinate", "x": 300, "y": 200}
        assert_success(browser.tool_result(use("left_click", target=point)))
        for _ in range(50):
            page = text(assert_success(browser.tool_result(use("get_page_text"))))
            if "clicked" in page:
                break
            assert_success(browser.tool_result(use("wait", duration=0.1)))
        assert "clicked" in page
        # The tab the frame lives in is still there, and still answers.
        assert len(tabs(assert_success(browser.tool_result(use("list_tabs"))))) == 1
        assert_success(browser.tool_result(use("screenshot")))
        assert_no_workers(hits)
    assert revoked == [IDENT]
    assert_cross_site_policy(port, hits, seen)
    assert_no_workers(hits)  # Not at the page's exit either.


@pytest.mark.asyncio
async def test_async_cross_site_frame_runs_under_the_policy(chrome: str, cross_site: Any) -> None:
    computer, revoked = remote(chrome, True)
    port, hits = cross_site
    seen: list[str] = []

    async def policy(_context: Any, url: str) -> None:
        seen.append(url)
        if url.endswith("-blocked"):
            raise ToolError("not permitted")

    async with AsyncMandalaBrowserToolset(computer, url_policy=policy) as browser:
        browser._backend.files = tracking = TrackingFiles()
        nav = assert_success(
            await browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}/"))
        )
        assert len(tabs(nav)) == 1
        page = ""
        for _ in range(50):
            page = text(assert_success(await browser.tool_result(use("get_page_text"))))
            if CROSS_SITE_DONE in page:
                break
            await asyncio.sleep(0.1)
        assert CROSS_SITE_DONE in page
        assert len(tabs(assert_success(await browser.tool_result(use("list_tabs"))))) == 1
        assert_success(await browser.tool_result(use("screenshot")))
        assert_no_workers(hits)
        # Every frame is the tab's for downloads: the page, both cross-site
        # frames, and the same-process frame inside the localhost one, which
        # only that frame's own session reports.
        tab = tabs(nav)[0]["tab_id"]
        assert len(tracking.frames) == 4 and set(tracking.frames.values()) == {tab}
    assert revoked == [IDENT]
    assert_cross_site_policy(port, hits, seen)
    assert_no_workers(hits)  # Not at the page's exit either.


def owned_backend(policy: Any = None) -> tuple[Any, list[tuple[str, Any, Any]]]:
    """A backend with one ready tab, recording every command it sends."""
    from mandala_computer._browser_cdp import BrowserCDP

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = object()
    backend.context = "context"
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.active = "tab"
    sent: list[tuple[str, Any, Any]] = []

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        sent.append((method, params, session))
        if method == "Page.getFrameTree":
            # A tab's or a cross-site frame's own frame is its target.
            if session in backend.child_frames:
                return {"frameTree": {"frame": {"id": backend.child_frames[session][1]}}}
            tab = next(t for t, own in backend.sessions.items() if own == session)
            return {"frameTree": {"frame": {"id": tab}}}
        return {"success": True}

    backend.send = send
    strict_page_events(backend, sent)
    return backend, sent


def strict_page_events(backend: Any, sent: list[tuple[str, Any, Any]]) -> None:
    """Chromium sends no Page events to a session that never enabled Page.

    A test that notes one anyway would hide a missing Page.enable, so it
    fails here instead. The tab's own session ("page") is already set up.
    """
    note = backend._note

    def strict(method: str, p: Any, session: Any) -> None:
        enabled = {"page"} | {s for m, _, s in sent if m == "Page.enable"}
        if method.startswith("Page.") and session not in enabled:
            raise AssertionError(f"{method} from {session}, which never enabled Page")
        note(method, p, session)

    backend._note = strict


def attached(child: str, target: str, kind: str, parent: str | None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "method": "Target.attachedToTarget",
        "params": {
            "sessionId": child,
            "targetInfo": {"targetId": target, "type": kind, "browserContextId": "context"},
        },
    }
    if parent is not None:
        message["sessionId"] = parent
    return message


@pytest.mark.asyncio
async def test_frames_under_an_owned_tab_run_and_workers_are_held_never_closed() -> None:
    from mandala_computer._browser_cdp import _FRAME_INIT

    seen: list[tuple[Any, str]] = []

    async def policy(tab: Any, url: str) -> None:
        seen.append((tab, url))
        if url.endswith("/blocked"):
            raise ToolError("no")

    backend, sent = owned_backend(policy)
    await backend._event(attached("frame-s", "frame-t", "iframe", "page"))
    assert [(m, s) for m, _, s in sent] == [
        *[(name, "frame-s") for name, _ in _FRAME_INIT],
        ("Runtime.runIfWaitingForDebugger", "frame-s"),
    ]
    assert {"Page.enable", "Fetch.enable", "Target.setAutoAttach"} <= {m for m, _, _ in sent}
    # A frame inside that frame is the same tab's, however deep.
    sent.clear()
    await backend._event(attached("nested-s", "nested-t", "iframe", "frame-s"))
    assert ("Runtime.runIfWaitingForDebugger", None, "nested-s") in sent
    assert backend._owner("nested-s") == "tab"
    # A worker, in the tab or in one of its frames, is held where it paused:
    # not released (it could start workers beyond the policy), not detached
    # (which would release it) and not closed (Chromium refuses).
    sent.clear()
    await backend._event(attached("worker-s", "worker-t", "worker", "frame-s"))
    await backend._event(attached("worker2-s", "worker2-t", "worker", "page"))
    assert sent == [] and backend._owner("worker-s") is None
    # The frame's requests answer to the tab's policy, on the frame's session.
    sent.clear()
    for ident, url in [("1", "https://ok.test/"), ("2", "https://ok.test/blocked")]:
        await backend._event(
            {
                "method": "Fetch.requestPaused",
                "sessionId": "nested-s",
                "params": {"requestId": ident, "request": {"url": url}},
            }
        )
    assert seen == [("tab", "https://ok.test/"), ("tab", "https://ok.test/blocked")]
    assert [(m, p["requestId"], s) for m, p, s in sent] == [
        ("Fetch.continueRequest", "1", "nested-s"),
        ("Fetch.failRequest", "2", "nested-s"),
    ]
    assert not backend.failed
    # Closing the tab forgets its frames, and their requests die with them.
    backend.drop_tab("tab")
    assert not backend.child_frames and backend._owner("frame-s") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        attached("popup-s", "popup-t", "page", None),
        attached("sw-s", "sw-t", "service_worker", None),
        attached("frame-s", "frame-t", "iframe", None),
        attached("frame-s", "frame-t", "iframe", "someone-elses-session"),
        attached("shared-s", "shared-t", "shared_worker", "page"),
    ],
)
async def test_targets_outside_an_owned_tab_are_still_closed(message: dict[str, Any]) -> None:
    from mandala_computer._browser_cdp import _POPUP_FENCE

    backend, sent = owned_backend()
    await backend._event(message)
    info = message["params"]["targetInfo"]
    close = [("Target.closeTarget", {"targetId": info["targetId"]}, None)]
    if info["type"] == "page":
        # A popup is fenced (offline, after the service-worker bypass) and
        # released before it is closed: held, it would hold the renderer it
        # shares with the page that opened it.
        from mandala_computer._browser_cdp import _OFFLINE

        child = message["params"]["sessionId"]
        fence = [(name, args, child) for name, args in _POPUP_FENCE]
        offline = {"offline": True, "matchedNetworkConditions": [{"urlPattern": "", **_OFFLINE}]}
        fence.insert(3, ("Network.emulateNetworkConditionsByRule", offline, child))
        assert sent == [*fence, ("Runtime.runIfWaitingForDebugger", {}, child), *close]
        assert not [m for m, _, _ in sent if m.startswith("Fetch.")]
    else:
        assert sent == close
    assert not backend.child_frames and not backend.failed


@pytest.mark.asyncio
async def test_a_frame_that_cannot_take_the_policy_stays_paused_without_ending_the_tab() -> None:
    from mandala_computer._browser_cdp import BrowserError

    backend, sent = owned_backend()
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Fetch.enable" and session == "frame-s":
            raise BrowserError("Chromium could not complete the browser action.")
        return await plain(method, params, session)

    backend.send = send
    await backend._event(attached("frame-s", "frame-t", "iframe", "page"))
    assert ("Runtime.runIfWaitingForDebugger", None, "frame-s") not in sent
    assert not [m for m, _, _ in sent if m == "Target.closeTarget"]
    assert not backend.failed and "frame-s" not in backend.child_frames
    assert backend.tabs == {"tab": {}}


@pytest.mark.asyncio
async def test_a_detached_frame_fails_its_pending_command_promptly() -> None:
    from mandala_computer._browser_cdp import BrowserCDP, _SessionGone

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, Any]] = []

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = Socket()
    backend.context = "context"
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.child_frames = {"frame-s": ("tab", "frame-t", "page")}
    command = asyncio.create_task(backend.send("Runtime.enable", session="frame-s"))
    other = asyncio.create_task(backend.send("Runtime.enable", session="page"))
    await asyncio.sleep(0)
    await backend._event(
        {
            "method": "Target.detachedFromTarget",
            "sessionId": "page",
            "params": {"sessionId": "frame-s", "targetId": "frame-t"},
        }
    )
    with pytest.raises(_SessionGone):
        await asyncio.wait_for(command, 1)
    assert not other.done() and not backend.failed
    other.cancel()


@pytest.mark.asyncio
async def test_a_burst_of_paused_requests_waits_its_turn_instead_of_ending_the_session() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    release = asyncio.Event()
    checked: list[str] = []

    async def policy(_tab: Any, url: str) -> None:
        checked.append(url)
        await release.wait()

    class Socket:
        def __init__(self) -> None:
            self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
            self.answered: list[str] = []

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            raw = await self.inbox.get()
            if raw is None:
                raise StopAsyncIteration
            return raw

        async def send(self, raw: str) -> None:
            message = json.loads(raw)
            self.answered.append(message["params"]["requestId"])
            await self.inbox.put(json.dumps({"id": message["id"], "result": {}}))

        async def close(self) -> None:
            await self.inbox.put(None)

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = Socket()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.reader = asyncio.create_task(backend._read())
    total = 600
    for i in range(total):
        socket.inbox.put_nowait(
            json.dumps(
                {
                    "method": "Fetch.requestPaused",
                    "sessionId": "page",
                    "params": {"requestId": str(i), "request": {"url": f"https://a.test/{i}"}},
                }
            )
        )
    try:
        for _ in range(100):
            if len(checked) >= 256:
                break
            await asyncio.sleep(0.01)
        assert len(checked) == 256 and len(backend.tasks) == 256 and not backend.failed
        release.set()
        for _ in range(300):
            if len(socket.answered) == total:
                break
            await asyncio.sleep(0.01)
        # Every request was answered, in arrival order, with the session intact.
        assert socket.answered == [str(i) for i in range(total)] and not backend.failed
    finally:
        await socket.close()
        await backend.reader


@pytest.mark.asyncio
async def test_an_unbounded_event_flood_still_ends_the_session(monkeypatch: Any) -> None:
    from mandala_computer import _browser_cdp
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError

    monkeypatch.setattr(_browser_cdp, "_POLICY_CONCURRENCY", 2)
    monkeypatch.setattr(_browser_cdp, "_EVENT_QUEUE_LIMIT", 10)
    stuck = asyncio.Event()

    async def policy(_tab: Any, _url: str) -> None:
        await stuck.wait()

    class Socket:
        closed = False

        def __init__(self) -> None:
            self.events = [
                json.dumps(
                    {
                        "method": "Fetch.requestPaused",
                        "sessionId": "page",
                        "params": {"requestId": str(i), "request": {"url": "https://a.test/"}},
                    }
                )
                for i in range(20)
            ]

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            if not self.events:
                await asyncio.Event().wait()
            return self.events.pop(0)

        async def close(self) -> None:
            self.closed = True

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = Socket()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    await asyncio.wait_for(backend._read(), 1)
    assert backend.failed and socket.closed
    with pytest.raises(BrowserError, match="faster than the session"):
        await backend.start()
    for task in list(backend.tasks):
        task.cancel()


@pytest.mark.asyncio
async def test_read_page_depth_zero_is_sent_as_zero() -> None:
    backend, sent = owned_backend()
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        await plain(method, params, session)
        return {"nodes": []}

    backend.send = send
    await backend.perform("read_page", {"depth": 0})
    await backend.perform("read_page", {})
    assert [p["depth"] for m, p, _ in sent if m == "Accessibility.getFullAXTree"] == [0, 20]


@pytest.mark.asyncio
async def test_a_slow_form_field_release_does_not_end_the_session() -> None:
    backend, sent = owned_backend()
    backend.refs = {"tab": {"e1": 7}}
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        await plain(method, params, session)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "obj"}}
        if method == "Runtime.releaseObject":
            raise asyncio.TimeoutError()
        return {"result": {}}

    backend.send = send
    assert (
        await backend.perform("form_input", {"target": {"type": "ref", "ref": "e1"}, "value": "x"})
        is None
    )
    assert ("Runtime.releaseObject", {"objectId": "obj"}, "page") in sent
    assert not backend.failed


class ScriptedSocket:
    """A CDP socket the test feeds by hand; commands wait for a scripted reply."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> str:
        raw = await self.inbox.get()
        if raw is None:
            raise StopAsyncIteration
        return raw

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True
        await self.inbox.put(None)

    def push(self, message: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(message))


def paused(ident: str, session: str, url: str) -> dict[str, Any]:
    return {
        "method": "Fetch.requestPaused",
        "sessionId": session,
        "params": {"requestId": ident, "request": {"url": url}},
    }


async def settle(condition: Any) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached")


@pytest.mark.asyncio
async def test_a_frame_detached_while_the_queue_is_full_does_not_end_the_session() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    hold = asyncio.Event()

    async def policy(_tab: Any, url: str) -> None:
        if url.endswith("/slow"):
            await hold.wait()

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = ScriptedSocket()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.child_frames = {"frame-s": ("tab", "frame-t", "page")}
    backend.reader = asyncio.create_task(backend._read())
    try:
        # A request in the frame is allowed and its continue is in flight...
        socket.push(paused("f1", "frame-s", "https://ok.test/"))
        await settle(lambda: any(m.get("sessionId") == "frame-s" for m in socket.sent))
        command = next(m for m in socket.sent if m.get("sessionId") == "frame-s")
        # ...when the tab fills every slot with requests waiting on the policy.
        for i in range(300):
            socket.push(paused(str(i), "page", "https://ok.test/slow"))
        await settle(lambda: len(backend.tasks) >= 256 and backend.policy_lane.waiting)
        # The frame goes; Chromium says so, then refuses the in-flight command.
        socket.push(
            {
                "method": "Target.detachedFromTarget",
                "sessionId": "page",
                "params": {"sessionId": "frame-s", "targetId": "frame-t"},
            }
        )
        socket.push(
            {"id": command["id"], "error": {"code": -32001, "message": "Session not found."}}
        )
        await asyncio.sleep(0.05)
        assert not backend.failed and not socket.closed
        assert "frame-s" not in backend.child_frames
    finally:
        hold.set()
        await socket.close()
        await backend.reader


@pytest.mark.asyncio
async def test_large_events_are_noted_or_trimmed_rather_than_queued_whole(
    monkeypatch: Any,
) -> None:
    from mandala_computer import _browser_cdp
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError

    hold = asyncio.Event()

    async def policy(_tab: Any, _url: str) -> None:
        await hold.wait()

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = ScriptedSocket()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.reader = asyncio.create_task(backend._read())
    huge = "x" * (2 * 1024 * 1024)
    try:
        for i in range(256):
            socket.push(paused(str(i), "page", "https://ok.test/"))
        await settle(lambda: len(backend.policy_lane.running) == 256)
        # Console output is noted in place, whatever its size, and never queued.
        for _ in range(20):
            socket.push(
                {
                    "method": "Runtime.consoleAPICalled",
                    "sessionId": "page",
                    "params": {"args": [{"value": huge}]},
                }
            )
        # A queued request keeps only what its check reads: an over-long URL
        # stays over-long, so it is still refused.
        socket.push(paused("long", "page", "https://ok.test/" + huge))
        await settle(lambda: backend.policy_lane.waiting)
        assert len(backend.console["tab"]) == 20
        assert all(len(line) <= 2000 for line in backend.console["tab"])
        queued, size = backend.policy_lane.waiting[0]
        assert len(queued["params"]["request"]["url"]) == 8193 and size < 10_000
        assert backend.queued_bytes == size
        # The byte budget still ends a flood of queued events.
        monkeypatch.setattr(_browser_cdp, "_EVENT_QUEUE_BYTES", size * 3)
        for i in range(5):
            socket.push(paused(f"more{i}", "page", "https://ok.test/" + huge))
        await settle(lambda: backend.failed)
        with pytest.raises(BrowserError, match="faster than the session"):
            await backend.start()
    finally:
        hold.set()
        await socket.close()
        await backend.reader
        for task in list(backend.tasks):
            task.cancel()
        await asyncio.gather(*backend.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_unused_events_are_dropped_and_target_events_skip_the_queue() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    hold = asyncio.Event()

    async def policy(_tab: Any, _url: str) -> None:
        await hold.wait()

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = ScriptedSocket()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.child_frames = {"frame-s": ("tab", "frame-t", "page")}
    backend.reader = asyncio.create_task(backend._read())
    try:
        for i in range(300):
            socket.push(paused(str(i), "page", "https://ok.test/"))
        socket.push({"method": "Network.dataReceived", "sessionId": "page", "params": {}})
        socket.push({"method": "Target.targetDestroyed", "params": {"targetId": "frame-t"}})
        await settle(lambda: "frame-s" not in backend.child_frames)
        assert len(backend.policy_lane.waiting) == 300 - 256
        assert all(m["method"] == "Fetch.requestPaused" for m, _ in backend.policy_lane.waiting)
    finally:
        hold.set()
        await socket.close()
        await backend.reader


@pytest.mark.parametrize(
    "message,field,reader",
    [
        (
            {
                "method": "Browser.downloadWillBegin",
                "params": {"guid": "g", "suggestedFilename": "report.txt" + "a" * 5000},
            },
            "suggestedFilename",
            "safe_filename",
        ),
        (
            {"method": "Browser.downloadWillBegin", "params": {"suggestedFilename": 12345}},
            "suggestedFilename",
            "safe_filename",
        ),
        (paused("1", "page", "https://ok.test/" + "a" * 9000), "url", "check"),
        (paused("1", "page", None), "url", "check"),
    ],
)
@pytest.mark.asyncio
async def test_trimming_keeps_every_refusal(message: Any, field: str, reader: str) -> None:
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError, _trimmed
    from mandala_computer._browser_files import safe_filename

    params = _trimmed(message)["params"]
    if reader == "safe_filename":
        with pytest.raises(ValueError):
            safe_filename(message["params"][field])
        with pytest.raises(ValueError):
            safe_filename(params[field])
    else:
        backend = BrowserCDP(lambda: None, lambda _: None, None)
        with pytest.raises((BrowserError, TypeError)):
            await backend.check_url(params["request"][field], "tab")


@pytest.mark.asyncio
async def test_a_frame_leaving_never_overtakes_its_own_download() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    class Files:
        def __init__(self) -> None:
            self.frames = {"frame": "tab"}
            self.tracked: list[bool] = []

        def frame(self, ident: str, tab: str) -> bool:
            self.frames[ident] = tab
            return True

        async def event(self, method: str, params: dict[str, Any]) -> None:
            self.tracked.append(params.get("tracked"))

        async def close(self) -> None:
            pass

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = socket = ScriptedSocket()
    backend.files = files = Files()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.reader = asyncio.create_task(backend._read())
    try:
        # Both arrive in one read, before the download's handler can run.
        socket.push(
            {
                "method": "Browser.downloadWillBegin",
                "params": {"guid": "g", "frameId": "frame", "url": "https://ok.test/f"},
            }
        )
        socket.push(
            {"method": "Page.frameDetached", "sessionId": "page", "params": {"frameId": "frame"}}
        )
        await settle(lambda: files.tracked and "frame" not in files.frames)
        assert files.tracked == [True]
    finally:
        await socket.close()
        await backend.reader


@pytest.mark.asyncio
async def test_frames_churning_faster_than_their_setup_leave_the_session_intact(
    chrome: str, cross_site: Any
) -> None:
    computer, revoked = remote(chrome, True)
    port, hits = cross_site
    seen: list[str] = []

    async def policy(_context: Any, url: str) -> None:
        seen.append(url)
        if url.endswith("-blocked"):
            raise ToolError("not permitted")

    async with AsyncMandalaBrowserToolset(computer, url_policy=policy) as browser:
        backend = browser._backend
        frames: list[str] = []
        settle_event = backend._settle

        def counting(message: dict[str, Any]) -> None:
            info = (message.get("params") or {}).get("targetInfo") or {}
            if message.get("method") == "Target.attachedToTarget" and info.get("type") == "iframe":
                frames.append(info.get("targetId"))
            settle_event(message)

        backend._settle = counting
        assert_success(
            await browser.tool_result(use("navigate", url=f"http://localhost:{port}/churn"))
        )
        page = ""
        for _ in range(100):
            page = text(assert_success(await browser.tool_result(use("get_page_text"))))
            if "churned" in page:
                break
            await asyncio.sleep(0.1)
        assert "churned 300" in page
        assert_success(await browser.tool_result(use("screenshot")))
        assert len(tabs(assert_success(await browser.tool_result(use("list_tabs"))))) == 1
        assert not browser._backend.failed
    assert revoked == [IDENT]
    # Cross-site frames came and went, as their own targets.
    assert frames
    assert not [path for _, path in hits if path.endswith("-blocked")]


@pytest.mark.asyncio
async def test_a_detach_takes_the_whole_subtree_and_its_pending_commands() -> None:
    from mandala_computer._browser_cdp import BrowserCDP, _SessionGone

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = socket = ScriptedSocket()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    # page > a > b > c, and page > other.
    backend.child_frames = {
        "a": ("tab", "a-t", "page"),
        "b": ("tab", "b-t", "a"),
        "c": ("tab", "c-t", "b"),
        "other": ("tab", "other-t", "page"),
    }
    waiting = [asyncio.create_task(backend.send("Runtime.enable", session=s)) for s in "bc"]
    kept = asyncio.create_task(backend.send("Runtime.enable", session="other"))
    await asyncio.sleep(0)
    # Chromium reports only the top of the subtree.
    await backend._event(
        {
            "method": "Target.detachedFromTarget",
            "sessionId": "page",
            "params": {"sessionId": "a", "targetId": "a-t"},
        }
    )
    assert set(backend.child_frames) == {"other"}
    for task in waiting:
        with pytest.raises(_SessionGone):
            await asyncio.wait_for(task, 1)
    assert not kept.done() and not backend.failed and not socket.closed
    kept.cancel()


@pytest.mark.asyncio
async def test_a_frame_detached_mid_setup_is_not_set_up_further() -> None:
    from mandala_computer._browser_cdp import _FRAME_INIT

    backend, sent = owned_backend()
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        result = await plain(method, params, session)
        if method == _FRAME_INIT[0][0] and session == "frame-s":
            backend._drop_frame("frame-s")  # Its detach arrives meanwhile.
        return result

    backend.send = send
    await backend._event(attached("frame-s", "frame-t", "iframe", "page"))
    assert [(m, s) for m, _, s in sent] == [(_FRAME_INIT[0][0], "frame-s")]
    assert not backend.failed


@pytest.mark.asyncio
async def test_a_cross_site_frame_keeps_its_downloads_through_a_process_swap() -> None:
    backend, sent = owned_backend()
    backend.files = files = FrameFiles()
    # The parent reports the frame swapped out; the frame's own target arrives.
    files.frames["frame-t"] = "tab"
    backend._note("Page.frameDetached", {"frameId": "frame-t", "reason": "swap"}, session="page")
    assert files.frames == {"frame-t": "tab"}
    await backend._event(attached("frame-s", "frame-t", "iframe", "page"))
    assert files.frames == {"frame-t": "tab"}
    # Its own session reports its frames, and the ones it already has are
    # tracked, before it is released.
    calls = [m for m, _, session in sent if session == "frame-s"]
    assert calls.index("Page.enable") < calls.index("Runtime.runIfWaitingForDebugger")
    assert calls.index("Page.getFrameTree") < calls.index("Runtime.runIfWaitingForDebugger")
    backend._note("Page.frameAttached", {"frameId": "same-process"}, session="frame-s")
    assert files.frames == {"frame-t": "tab", "same-process": "tab"}
    files.frames.pop("same-process")
    # A frame really removed is forgotten.
    backend._note("Page.frameDetached", {"frameId": "frame-t", "reason": "remove"}, session="page")
    assert files.frames == {}
    files.frames.clear()
    await backend._event(attached("other-s", "other-t", "iframe", "page"))
    assert files.frames == {"other-t": "tab"}


@pytest.mark.asyncio
async def test_a_download_belongs_to_its_frame_as_it_arrives() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    hold = asyncio.Event()

    async def policy(_tab: Any, _url: str) -> None:
        await hold.wait()

    class Files:
        def __init__(self) -> None:
            self.frames = {"frame": "tab"}
            self.seen: list[Any] = []

        def frame(self, ident: str, tab: str) -> bool:
            self.frames[ident] = tab
            return True

        async def event(self, method: str, params: dict[str, Any]) -> None:
            self.seen.append(params.get("tracked"))

        async def close(self) -> None:
            pass

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = ScriptedSocket()
    backend.files = files = Files()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.ready = {"tab": asyncio.Event()}
    backend.reader = asyncio.create_task(backend._read())
    try:
        for i in range(256):
            socket.push(paused(str(i), "page", "https://ok.test/"))
        await settle(lambda: len(backend.policy_lane.running) == 256)
        # The download waits its turn; its tab goes at once.
        socket.push(
            {
                "method": "Browser.downloadWillBegin",
                "params": {"guid": "g", "frameId": "frame", "url": "https://ok.test/f"},
            }
        )
        socket.push({"method": "Target.targetDestroyed", "params": {"targetId": "tab"}})
        await settle(lambda: "tab" not in backend.tabs)
        assert "frame" not in files.frames
        hold.set()
        await settle(lambda: files.seen)
        assert files.seen == [True]
    finally:
        hold.set()
        await socket.close()
        await backend.reader


class FrameFiles:
    """Download ownership as the file session keeps it, with its 256-frame cap."""

    def __init__(self) -> None:
        self.frames: dict[str, str] = {}

    def frame(self, ident: str, tab: str) -> bool:
        if ident not in self.frames and len(self.frames) >= 256:
            return False
        self.frames[ident] = tab
        return True

    def frame_tree(self, tree: dict[str, Any], tab: str) -> None:
        if self.frame(tree["frame"]["id"], tab):
            for child in tree.get("childFrames", []):
                self.frame_tree(child, tab)

    async def event(self, method: str, params: dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_detached_cross_site_frames_give_up_their_download_ownership() -> None:
    backend, _ = owned_backend()
    backend.files = files = FrameFiles()
    for i in range(300):
        await backend._event(attached(f"f{i}", f"f{i}-t", "iframe", "page"))
        await backend._event(attached(f"n{i}", f"n{i}-t", "iframe", f"f{i}"))
        # Chromium reports only the outer frame's detach.
        await backend._event(
            {
                "method": "Target.detachedFromTarget",
                "sessionId": "page",
                "params": {"sessionId": f"f{i}", "targetId": f"f{i}-t"},
            }
        )
    assert files.frames == {} and backend.child_frames == {}
    await backend._event(attached("fresh", "fresh-t", "iframe", "page"))
    assert files.frames == {"fresh-t": "tab"}


@pytest.mark.asyncio
async def test_frames_removed_while_the_queue_is_full_are_forgotten_at_once() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    hold = asyncio.Event()

    async def policy(_tab: Any, _url: str) -> None:
        await hold.wait()

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = ScriptedSocket()
    backend.files = files = FrameFiles()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.reader = asyncio.create_task(backend._read())
    try:
        for i in range(256):
            socket.push(paused(str(i), "page", "https://ok.test/"))
        await settle(lambda: len(backend.policy_lane.running) == 256)
        for i in range(300):
            for method in ("Page.frameAttached", "Page.frameDetached"):
                socket.push({"method": method, "sessionId": "page", "params": {"frameId": f"x{i}"}})
        socket.push(
            {"method": "Page.frameAttached", "sessionId": "page", "params": {"frameId": "live"}}
        )
        await settle(lambda: "live" in files.frames)
        assert files.frames == {"live": "tab"}
    finally:
        hold.set()
        await socket.close()
        await backend.reader


class TrackingFiles(FrameFiles):
    """A file session reduced to its frame tracking, for a real browser."""

    created = False
    cleanup_failed = False

    async def setup(self, context: str) -> None:
        pass


@pytest.mark.asyncio
async def test_a_frame_noted_after_a_detach_stays_tracked() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = socket = ScriptedSocket()
    backend.files = files = FrameFiles()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.child_frames = {"frame-s": ("tab", "frame", "page")}
    files.frames = {"frame": "tab"}
    seen: list[Any] = []

    async def event(method: str, params: dict[str, Any]) -> None:
        seen.append(params.get("tracked"))

    files.event = event  # type: ignore[method-assign]
    backend.reader = asyncio.create_task(backend._read())
    try:
        # The frame returns to its parent's process: its target detaches and
        # the parent notes it navigated, in one read; then it downloads.
        socket.push(
            {
                "method": "Target.detachedFromTarget",
                "sessionId": "page",
                "params": {"sessionId": "frame-s", "targetId": "frame"},
            }
        )
        socket.push(
            {
                "method": "Page.frameNavigated",
                "sessionId": "page",
                "params": {"frame": {"id": "frame"}},
            }
        )
        socket.push(
            {
                "method": "Browser.downloadWillBegin",
                "params": {"guid": "g", "frameId": "frame", "url": "https://ok.test/f"},
            }
        )
        await settle(lambda: seen)
        assert files.frames == {"frame": "tab"} and seen == [True]
    finally:
        await socket.close()
        await backend.reader


@pytest.mark.asyncio
async def test_a_navigation_and_a_dialog_never_wait_behind_held_subresources() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    hold = asyncio.Event()
    checked: list[str] = []

    async def policy(_tab: Any, url: str) -> None:
        checked.append(url)
        if url.endswith(".png"):
            await hold.wait()

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = ScriptedSocket()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.reader = asyncio.create_task(backend._read())
    try:
        for i in range(400):
            socket.push(paused(str(i), "page", f"https://ok.test/{i}.png"))
        await settle(lambda: len(backend.policy_lane.waiting) == 400 - 256)
        document = paused("doc", "page", "https://ok.test/next")
        document["params"].update(frameId="tab", resourceType="Document")
        socket.push(document)
        socket.push(
            {
                "method": "Page.javascriptDialogOpening",
                "sessionId": "page",
                "params": {"type": "alert", "message": "hi"},
            }
        )
        await settle(
            lambda: (
                {"Fetch.continueRequest", "Page.handleJavaScriptDialog"}
                <= {m["method"] for m in socket.sent}
            )
        )
        answered = [m for m in socket.sent if m["method"] == "Fetch.continueRequest"]
        assert [m["params"]["requestId"] for m in answered] == ["doc"]
        assert len(backend.navigation_lane.running) == 1 and not backend.failed
    finally:
        hold.set()
        await socket.close()
        await backend.reader


@pytest.mark.asyncio
async def test_slow_sync_policies_for_subresources_hold_no_navigation_or_renewal() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    release = threading.Event()
    checked: list[str] = []

    def sync_policy(_context: Any, url: str) -> None:
        # A caller's synchronous policy: images take as long as they take.
        checked.append(url)
        if url.endswith(".png"):
            release.wait(10)

    async def policy(tab: Any, url: str) -> None:
        await backend.call_policy(sync_policy, tab, url)

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    backend.ws = socket = ScriptedSocket()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    backend.reader = asyncio.create_task(backend._read())
    try:
        for i in range(300):
            socket.push(paused(str(i), "page", f"https://ok.test/{i}.png"))
        await settle(lambda: len(checked) >= 32)
        document = paused("doc", "page", "https://ok.test/next")
        document["params"].update(frameId="tab", resourceType="Document")
        socket.push(document)
        await settle(lambda: any(m["method"] == "Fetch.continueRequest" for m in socket.sent))
        answered = [m for m in socket.sent if m["method"] == "Fetch.continueRequest"]
        assert [m["params"]["requestId"] for m in answered] == ["doc"]
        # Nor do they occupy the default executor that renewal runs on.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "renewed"), 1) == "renewed"
        # The model's own navigation check is a navigation too.
        assert await asyncio.wait_for(
            backend.check_url("https://ok.test/elsewhere", "tab", navigation=True), 1
        )
    finally:
        release.set()
        await socket.close()
        await backend.reader
        for task in list(backend.tasks):
            task.cancel()
        await asyncio.gather(*backend.tasks, return_exceptions=True)
        for pool, _ in backend._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.asyncio
async def test_frames_inside_a_detached_cross_site_frame_are_forgotten_with_it() -> None:
    backend, _ = owned_backend()
    backend.files = files = FrameFiles()
    for i in range(300):
        await backend._event(attached(f"f{i}", f"f{i}-t", "iframe", "page"))
        # Its own session reports a same-process frame inside it, and nothing
        # says that frame went when the cross-site frame did.
        backend._note(
            "Page.frameAttached",
            {"frameId": f"inner{i}", "parentFrameId": f"f{i}-t"},
            session=f"f{i}",
        )
        assert files.frames[f"inner{i}"] == "tab"
        await backend._event(
            {
                "method": "Target.detachedFromTarget",
                "sessionId": "page",
                "params": {"sessionId": f"f{i}", "targetId": f"f{i}-t"},
            }
        )
    assert files.frames == {} and backend.frame_source == {} and backend.frame_parent == {}
    # Frames the tab itself reports stay until the tab or a note removes them.
    backend._note("Page.frameAttached", {"frameId": "own"}, session="page")
    await backend._event(attached("late", "late-t", "iframe", "page"))
    assert files.frames == {"own": "tab", "late-t": "tab"}


@pytest.mark.parametrize("asynchronous", [False, True])
def test_toolset_url_policies_leave_navigations_and_renewal_their_threads(
    asynchronous: bool,
) -> None:
    release = threading.Event()
    started: list[str] = []

    def sync_policy(_context: Any, url: str) -> None:
        started.append(url)
        if url.endswith(".png"):
            release.wait(10)

    computer = SimpleNamespace(
        create_browser_connection=lambda: None, revoke_browser_connection=lambda _: None
    )
    toolset: Any = (AsyncMandalaBrowserToolset if asynchronous else MandalaBrowserToolset)(
        computer, url_policy=sync_policy
    )
    backend = toolset._backend

    async def scenario() -> float:
        images = [
            asyncio.create_task(backend.check_url(f"https://ok.test/{i}.png", "tab"))
            for i in range(64)
        ]
        await asyncio.sleep(0.2)
        began = time.monotonic()
        await backend.check_url("https://ok.test/next", "tab", navigation=True)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "renewed"), 1) == "renewed"
        took = time.monotonic() - began
        release.set()
        await asyncio.gather(*images, return_exceptions=True)
        for pool, _ in backend._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)
        return took

    try:
        took = asyncio.run(scenario()) if asynchronous else toolset._run(scenario())
    finally:
        release.set()
        if not asynchronous:
            with toolset._worker_lock:
                worker, toolset._worker = toolset._worker, None
            if worker is not None:
                worker.close()
    assert took < 1 and "https://ok.test/next" in started


def test_async_navigate_preflight_does_not_wait_behind_held_policy_threads() -> None:
    release = threading.Event()

    def sync_policy(_context: Any, url: str) -> None:
        if url.endswith(".png"):
            release.wait(10)

    async def create() -> Any:
        raise RuntimeError("no browser here")

    computer = SimpleNamespace(
        create_browser_connection=create, revoke_browser_connection=lambda _: None
    )

    async def scenario() -> tuple[float, Any]:
        browser = AsyncMandalaBrowserToolset(computer, url_policy=sync_policy)
        backend = browser._backend
        held = [
            asyncio.create_task(backend.check_url(f"https://ok.test/{i}.png", "tab"))
            for i in range(40)
        ]
        await asyncio.sleep(0.2)
        began = time.monotonic()
        # Anthropic's own policy check runs before the driver's; it must not
        # queue behind the held subresource checks.
        result = await asyncio.wait_for(
            browser.tool_result(use("navigate", url="https://ok.test/next")), 4
        )
        took = time.monotonic() - began
        release.set()
        await asyncio.gather(*held, return_exceptions=True)
        await browser.close()
        return took, result

    try:
        took, result = asyncio.run(scenario())
    finally:
        release.set()
    # It reached the driver (which has no browser here) instead of hanging.
    assert took < 2 and result.get("is_error")


@pytest.mark.asyncio
async def test_policy_checks_that_time_out_queue_nothing_behind_blocked_callbacks(
    monkeypatch: Any,
) -> None:
    from mandala_computer import _browser_cdp
    from mandala_computer._browser_cdp import BrowserCDP, BrowserError

    monkeypatch.setattr(_browser_cdp, "_POLICY_THREADS", 2)
    monkeypatch.setattr(_browser_cdp, "_POLICY_DEADLINE", 0.1)
    release = threading.Event()
    ran: list[str] = []

    def sync_policy(_tab: Any, url: str) -> None:
        ran.append(url)
        release.wait(10)

    async def policy(tab: Any, url: str) -> None:
        await backend.call_policy(sync_policy, tab, url)

    backend = BrowserCDP(lambda: None, lambda _: None, policy)
    try:
        for batch in range(3):
            results = await asyncio.gather(
                *(backend.check_url(f"https://ok.test/{batch}/{i}", "tab") for i in range(20)),
                return_exceptions=True,
            )
            assert all(isinstance(r, BrowserError) for r in results)
        pool, threads = backend._pools[False]
        # Only the two callbacks that got threads ever ran or waited to run.
        assert pool._work_queue.qsize() == 0 and len(ran) == 2
        release.set()
        for _ in range(100):
            if threads._value == 2:
                break
            await asyncio.sleep(0.01)
        assert threads._value == 2
        # With the threads free again, checks run as before.
        release.clear()
        release.set()
        await backend.check_url("https://ok.test/after", "tab")
        assert ran[-1] == "https://ok.test/after"
    finally:
        release.set()
        for pool, _ in backend._pools.values():
            pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.asyncio
async def test_a_cross_site_frame_navigating_keeps_the_tabs_refs() -> None:
    from mandala_computer._browser_cdp import BrowserError

    backend, sent = owned_backend()
    await backend._event(attached("frame-s", "frame-t", "iframe", "page"))
    assert ("Page.enable", None, "frame-s") in [(m, None, s) for m, _, s in sent]
    backend.refs = {"tab": {"e1": 7}}
    backend._note("Page.frameNavigated", {"frame": {"id": "frame-t"}}, session="frame-s")
    assert backend.refs == {"tab": {"e1": 7}}
    backend._note("Page.frameNavigated", {"frame": {"id": "tab"}}, session="page")
    assert backend.refs == {}
    # A frame's dialog that is already answered does not end the session.
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.handleJavaScriptDialog":
            raise BrowserError("Chromium could not complete the browser action.")
        return await plain(method, params, session)

    backend.send = send
    dialog = {"type": "alert", "message": "hi"}
    await backend._event(
        {"method": "Page.javascriptDialogOpening", "sessionId": "frame-s", "params": dialog}
    )
    assert not backend.failed
    # The tab's own dialog left unanswered still fails closed.
    closed: list[bool] = []

    async def close() -> None:
        closed.append(True)

    backend.ws = SimpleNamespace(close=close)
    await backend._event(
        {"method": "Page.javascriptDialogOpening", "sessionId": "page", "params": dialog}
    )
    assert backend.failed and closed


#: The ways a page can reach a Worker constructor in its own tab: its own
#: realm, about:blank, srcdoc, javascript: and document.write frames (srcdoc
#: before and after it loads), a same-origin child, a window taken from a frame
#: then removed, URL, blob: and data: scripts, and deleting or reassigning
#: Worker first. Each records "made" or "refused" in the title.
WORKER_REALMS = r"""
const out = [];
const blob = URL.createObjectURL(new Blob(["fetch('/ran-blob')"], {type: 'text/javascript'}));
function attempt(tag, make) {
  try { make(); out.push(tag + ':made'); } catch (e) { out.push(tag + ':refused'); }
  document.title = out.sort().join(' ');
}
attempt('main', () => { for (let i = 0; i < 10; i++) new Worker('/w.js?' + i); });
attempt('main-blob', () => { for (let i = 0; i < 30; i++) new Worker(blob); });
attempt('main-data', () => new Worker('data:text/javascript,fetch("/ran-data")'));
const blank = document.createElement('iframe'); document.body.appendChild(blank);
attempt('blank', () => new blank.contentWindow.Worker(blob));
const doc = document.createElement('iframe'); doc.srcdoc = '<p>s</p>'; document.body.appendChild(doc);
attempt('srcdoc-new', () => new doc.contentWindow.Worker(blob));
doc.onload = () => attempt('srcdoc', () => new doc.contentWindow.Worker(blob));
const child = document.createElement('iframe'); child.src = '/child'; document.body.appendChild(child);
child.onload = () => attempt('child', () => new child.contentWindow.Worker(blob));
const kept = document.createElement('iframe'); document.body.appendChild(kept);
const window2 = kept.contentWindow; kept.remove();
attempt('removed', () => new window2.Worker(blob));
const script = document.createElement('iframe'); script.src = "javascript:'<p>j</p>'";
document.body.appendChild(script);
script.onload = () => attempt('javascript', () => new script.contentWindow.Worker(blob));
const written = document.createElement('iframe'); document.body.appendChild(written);
written.contentDocument.open(); written.contentDocument.write('<p>w</p>');
written.contentDocument.close();
attempt('written', () => new written.contentWindow.Worker(blob));
attempt('deleted', () => { delete window.Worker; return new Worker(blob); });
attempt('reassigned', () => {
  try { window.Worker = function () {}; } catch (e) {}
  try { Object.defineProperty(window, 'Worker', {value: function () {}}); } catch (e) {}
  return new Worker(blob);
});
addEventListener('message', (e) => { out.push(String(e.data)); document.title = out.sort().join(' '); });
"""

#: Inside a cross-site frame, and a frame inside that.
WORKER_REALMS_ELSEWHERE = r"""
const blob = URL.createObjectURL(new Blob(["fetch('/ran-elsewhere')"]));
function report(tag, make) {
  try { make(); top.postMessage(tag + ':made', '*'); } catch (e) { top.postMessage(tag + ':refused', '*'); }
}
report('cross-site', () => new Worker(blob));
const inner = document.createElement('iframe'); document.body.appendChild(inner);
report('cross-site-inner', () => new inner.contentWindow.Worker(blob));
"""

WORKERS_REFUSED = " ".join(
    sorted(
        f"{realm}:refused"
        for realm in [
            "blank",
            "child",
            "cross-site",
            "cross-site-inner",
            "deleted",
            "javascript",
            "main",
            "main-blob",
            "main-data",
            "reassigned",
            "removed",
            "srcdoc",
            "srcdoc-new",
            "written",
        ]
    )
)


@pytest.fixture
def worker_realms() -> Any:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            port = self.server.server_port
            if self.path == "/":
                body = (
                    f"<title>w</title><body><p>w</p><script>{WORKER_REALMS}</script>"
                    f"<iframe src='http://localhost:{port}/elsewhere'></iframe></body>"
                )
            elif self.path == "/elsewhere":
                body = f"<body><p>x</p><script>{WORKER_REALMS_ELSEWHERE}</script></body>"
            elif self.path.startswith("/w.js"):
                body = "fetch('/ran-url')"  # An allowed script, which asks for what is not.
            else:
                body = "<p>page</p>"
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/javascript" if "/w.js" in self.path else "text/html"
            )
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("leave", ["navigate", "close"])
def test_sync_dedicated_workers_never_start_so_none_escapes_at_page_exit(
    chrome: str, worker_realms: Any, leave: str
) -> None:
    computer, revoked = remote(chrome, False)
    port, hits = worker_realms
    workers: list[str] = []

    def policy(_context: Any, url: str) -> None:
        if "/ran" in url:
            raise ToolError("not permitted")

    browser = MandalaBrowserToolset(computer, url_policy=policy)
    try:
        backend = browser._backend
        settle_event = backend._settle

        def counting(message: dict[str, Any]) -> None:
            info = (message.get("params") or {}).get("targetInfo") or {}
            if message.get("method") == "Target.attachedToTarget" and info.get("type") == "worker":
                workers.append(info.get("url"))
            settle_event(message)

        backend._settle = counting
        assert_success(browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}/")))
        title = ""
        for _ in range(50):
            title = str(browser._run(backend.evaluate(backend.active, "document.title")))
            if title.count(":") == WORKERS_REFUSED.count(":"):
                break
            assert_success(browser.tool_result(use("wait", duration=0.1)))
        if leave == "navigate":
            assert_success(
                browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}/away"))
            )
    finally:
        browser.close()
    time.sleep(1)  # Anything a worker released at the page's exit would send by now.
    # Nothing a worker would have sent reached the server, at the page's exit
    # included, because no worker was ever made.
    assert not [path for path in hits if path.startswith("/ran")]
    assert title == WORKERS_REFUSED and workers == [] and revoked == [IDENT]


@pytest.mark.asyncio
async def test_async_dedicated_workers_never_start(chrome: str, worker_realms: Any) -> None:
    computer, revoked = remote(chrome, True)
    port, hits = worker_realms

    async def policy(_context: Any, url: str) -> None:
        if "/ran" in url:
            raise ToolError("not permitted")

    async with AsyncMandalaBrowserToolset(computer, url_policy=policy) as browser:
        backend = browser._backend
        assert_success(await browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}/")))
        title = ""
        for _ in range(50):
            title = str(await backend.evaluate(backend.active, "document.title"))
            if title == WORKERS_REFUSED:
                break
            await asyncio.sleep(0.1)
        assert title == WORKERS_REFUSED
        assert_success(
            await browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}/away"))
        )
    await asyncio.sleep(1)
    assert revoked == [IDENT] and not [path for path in hits if path.startswith("/ran")]


@pytest.mark.asyncio
async def test_tabs_and_frames_refuse_workers_before_they_run() -> None:
    from mandala_computer._browser_cdp import _NO_WORKERS

    backend, sent = owned_backend()
    await backend._event(attached("frame-s", "frame-t", "iframe", "page"))
    backend.ready["tab2"] = asyncio.Event()
    await backend._event(attached("page2", "tab2", "page", None))
    for session in ("frame-s", "page2"):
        calls = [(m, p) for m, p, s in sent if s == session]
        names = [m for m, _ in calls]
        script = names.index("Page.addScriptToEvaluateOnNewDocument")
        assert names.index("Page.enable") < script < names.index("Runtime.runIfWaitingForDebugger")
        assert calls[script][1] == {"source": _NO_WORKERS, "runImmediately": True}


def tracking_backend() -> tuple[Any, list[tuple[str, Any, Any]], Any]:
    """A tab whose own frame ("tab") is tracked, as after its first document."""
    backend, sent = owned_backend()
    backend.files = files = FrameFiles()
    backend._note("Page.frameNavigated", {"frame": {"id": "tab"}}, "page")
    return backend, sent, files


def attach_frame(backend: Any, frame: str, parent: str, session: str = "page") -> None:
    backend._note("Page.frameAttached", {"frameId": frame, "parentFrameId": parent}, session)


@pytest.mark.asyncio
async def test_frames_are_tracked_only_beneath_tracked_frames() -> None:
    backend, _, files = tracking_backend()
    attach_frame(backend, "a", "tab")
    attach_frame(backend, "b", "a")
    attach_frame(backend, "lost", "never-reported")
    assert set(files.frames) == {"tab", "a", "b"}
    # A frame refused at the table's limit takes its children with it, even
    # once there is room again: nothing above them could ever prune them.
    for i in range(253):
        attach_frame(backend, f"filler{i}", "tab")
    attach_frame(backend, "refused", "tab")
    assert "refused" not in files.frames
    backend._note("Page.frameDetached", {"frameId": "filler0"}, "page")
    attach_frame(backend, "under-refused", "refused")
    assert "under-refused" not in files.frames


@pytest.mark.asyncio
async def test_a_new_document_or_a_removal_forgets_the_frames_below() -> None:
    backend, _, files = tracking_backend()
    attach_frame(backend, "a", "tab")
    attach_frame(backend, "b", "a")
    attach_frame(backend, "c", "b")
    backend._note("Page.frameNavigated", {"frame": {"id": "a", "parentId": "tab"}}, "page")
    assert set(files.frames) == {"tab", "a"}
    attach_frame(backend, "b2", "a")
    backend._note("Page.frameDetached", {"frameId": "a", "reason": "swap"}, "page")
    assert set(files.frames) == {"tab", "a", "b2"}
    backend._note("Page.frameDetached", {"frameId": "a", "reason": "remove"}, "page")
    assert set(files.frames) == {"tab"}
    # A cross-site frame keeps its session across sites, and Chromium reports no
    # detach for the frames of its old document.
    await backend._event(attached("x-s", "x", "iframe", "page"))
    attach_frame(backend, "x-child", "x", "x-s")
    attach_frame(backend, "x-grandchild", "x-child", "x-s")
    backend._note("Page.frameNavigated", {"frame": {"id": "x", "parentId": "tab"}}, "x-s")
    assert set(files.frames) == {"tab", "x"}


@pytest.mark.asyncio
async def test_a_frame_its_parent_reported_since_outlives_the_session_that_went() -> None:
    backend, _, files = tracking_backend()
    await backend._event(attached("x-s", "x", "iframe", "page"))
    attach_frame(backend, "x-child", "x", "x-s")
    # The frame comes back to its tab's process: the tab reports it now.
    backend._note("Page.frameNavigated", {"frame": {"id": "x", "parentId": "tab"}}, "page")
    backend._drop_frame("x-s")
    assert set(files.frames) == {"tab", "x"}
    assert backend.frame_source["x"] == "page"


@pytest.mark.asyncio
async def test_a_frame_tree_reply_never_undoes_a_newer_frame_event() -> None:
    backend, _, files = tracking_backend()
    asked, answer = asyncio.Event(), asyncio.Event()
    plain = backend.send
    tree = {
        "frame": {"id": "x"},
        "childFrames": [
            {"frame": {"id": "gone"}, "childFrames": [{"frame": {"id": "gone-child"}}]},
            {"frame": {"id": "kept"}},
        ],
    }

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getFrameTree" and session == "x-s":
            await plain(method, params, session)
            asked.set()
            await answer.wait()
            return {"frameTree": tree}
        return await plain(method, params, session)

    backend.send = send
    setup = asyncio.create_task(backend._event(attached("x-s", "x", "iframe", "page")))
    await asyncio.wait_for(asked.wait(), 1)
    # While the tree is on its way, "gone" is removed.
    backend._note("Page.frameDetached", {"frameId": "gone", "reason": "remove"}, "x-s")
    answer.set()
    await setup
    assert set(files.frames) == {"tab", "x", "kept"}
    # A session that goes while its tree is on its way tracks nothing.
    asked.clear()
    answer.clear()
    tree = {"frame": {"id": "y"}, "childFrames": [{"frame": {"id": "y-child"}}]}

    async def send_y(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getFrameTree" and session == "y-s":
            await plain(method, params, session)
            asked.set()
            await answer.wait()
            return {"frameTree": tree}
        return await plain(method, params, session)

    backend.send = send_y
    setup = asyncio.create_task(backend._event(attached("y-s", "y", "iframe", "page")))
    await asyncio.wait_for(asked.wait(), 1)
    backend._drop_frame("y-s")
    answer.set()
    await setup
    assert "y" not in files.frames and "y-child" not in files.frames


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["lands", "fails", "never"])
async def test_a_cache_restore_reads_its_frames_back_and_holds_downloads_briefly(
    outcome: str, monkeypatch: Any
) -> None:
    from mandala_computer import _browser_cdp

    monkeypatch.setattr(_browser_cdp, "_RESTORE_WAIT", 0.2)
    backend, _, files = tracking_backend()
    backend.ws = SimpleNamespace(close=lambda: asyncio.sleep(0))
    decided: list[Any] = []

    async def event(method: str, params: dict[str, Any]) -> None:
        decided.append(params.get("tracked"))

    files.event = event  # type: ignore[method-assign]
    asked, answer = asyncio.Event(), asyncio.Event()
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getFrameTree":
            await plain(method, params, session)
            asked.set()
            if outcome == "never":
                await asyncio.Event().wait()
            await answer.wait()
            if outcome == "fails":
                raise _browser_cdp.BrowserError("Chromium could not complete the browser action.")
            return {
                "frameTree": {
                    "frame": {"id": "tab"},
                    "childFrames": [{"frame": {"id": "restored"}}],
                }
            }
        return await plain(method, params, session)

    backend.send = send
    # The tab's document comes back from the cache, with frames nobody reports.
    backend._note(
        "Page.frameNavigated",
        {"frame": {"id": "tab"}, "type": "BackForwardCacheRestore"},
        "page",
    )
    await asyncio.wait_for(asked.wait(), 1)
    # Meanwhile a frame is reported beneath one not read back yet, and a
    # restored frame starts a download.
    attach_frame(backend, "kid", "restored")
    backend._dispatch(
        {
            "method": "Browser.downloadWillBegin",
            "params": {
                "guid": "00000000-0000-0000-0000-000000000001",
                "frameId": "restored",
                "url": "https://ok.test/f",
                "suggestedFilename": "f.txt",
            },
        }
    )
    await asyncio.sleep(0.05)
    assert decided == []  # Held until the tree lands, or briefly.
    answer.set()
    for _ in range(100):
        if decided:
            break
        await asyncio.sleep(0.01)
    if outcome == "lands":
        assert decided == [True] and {"restored", "kid"} <= set(files.frames)
    else:
        # Failed or late: nothing restored is tracked, and the download is
        # its frame's only if the frame was: fail closed.
        assert decided == [False] and "restored" not in files.frames
    if outcome != "never":
        await asyncio.sleep(0.01)
        assert backend.orphans == {} and not backend.restoring
    for task in list(backend.tasks):
        task.cancel()
    await asyncio.gather(*backend.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_overlapping_frame_trees_each_keep_their_own_record_of_newer_events() -> None:
    backend, _, files = tracking_backend()
    asked = {"x-s": asyncio.Event(), "y-s": asyncio.Event()}
    answer = {"x-s": asyncio.Event(), "y-s": asyncio.Event()}
    trees = {
        "x-s": {"frame": {"id": "x"}, "childFrames": [{"frame": {"id": "x-kid"}}]},
        "y-s": {"frame": {"id": "y"}},
    }
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getFrameTree" and session in trees:
            await plain(method, params, session)
            asked[session].set()
            await answer[session].wait()
            return {"frameTree": trees[session]}
        return await plain(method, params, session)

    backend.send = send
    first = asyncio.create_task(backend._event(attached("x-s", "x", "iframe", "page")))
    second = asyncio.create_task(backend._event(attached("y-s", "y", "iframe", "page")))
    await asyncio.wait_for(asyncio.gather(asked["x-s"].wait(), asked["y-s"].wait()), 1)
    # Both records are empty, so equal; the second tree lands first.
    answer["y-s"].set()
    await second
    backend._note("Page.frameDetached", {"frameId": "x-kid", "reason": "remove"}, "x-s")
    answer["x-s"].set()
    await first
    assert "x-kid" not in files.frames and {"x", "y"} <= set(files.frames)
    assert backend.snapshots == [] and not backend.failed


@pytest.mark.asyncio
async def test_a_downloads_progress_waits_for_its_beginning() -> None:
    backend, _, files = tracking_backend()
    order: list[tuple[str, Any]] = []

    async def event(method: str, params: dict[str, Any]) -> None:
        order.append((method, params.get("tracked")))

    files.event = event  # type: ignore[method-assign]
    asked, answer = asyncio.Event(), asyncio.Event()
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getFrameTree":
            await plain(method, params, session)
            asked.set()
            await answer.wait()
            return {"frameTree": {"frame": {"id": "tab"}, "childFrames": [{"frame": {"id": "r"}}]}}
        return await plain(method, params, session)

    backend.send = send
    backend._note(
        "Page.frameNavigated", {"frame": {"id": "tab"}, "type": "BackForwardCacheRestore"}, "page"
    )
    await asyncio.wait_for(asked.wait(), 1)
    guid = "00000000-0000-0000-0000-000000000002"
    backend._dispatch(
        {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": guid, "frameId": "r", "url": "https://ok.test/f"},
        }
    )
    for state in ("inProgress", "completed"):
        backend._dispatch(
            {
                "method": "Browser.downloadProgress",
                "params": {"guid": guid, "state": state, "receivedBytes": 1, "totalBytes": 1},
            }
        )
    await asyncio.sleep(0.05)
    assert order == []  # Nothing of the download before its beginning is decided.
    answer.set()
    for _ in range(100):
        if len(order) == 3:
            break
        await asyncio.sleep(0.01)
    assert [m for m, _ in order] == [
        "Browser.downloadWillBegin",
        "Browser.downloadProgress",
        "Browser.downloadProgress",
    ]
    assert order[0][1] is True and backend.download_events == {}


@pytest.mark.asyncio
async def test_a_download_owned_once_its_restore_lands_stays_owned() -> None:
    backend, _, files = tracking_backend()
    decided: list[Any] = []

    async def event(method: str, params: dict[str, Any]) -> None:
        decided.append(params.get("tracked"))

    files.event = event  # type: ignore[method-assign]
    answers = {"page": asyncio.Event(), "x-s": asyncio.Event()}
    trees = {
        "page": {"frame": {"id": "tab"}, "childFrames": [{"frame": {"id": "r"}}]},
        "x-s": {"frame": {"id": "x"}},
    }
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getFrameTree" and session in answers and backend.restoring:
            await plain(method, params, session)
            await answers[session].wait()
            return {"frameTree": trees[session]}
        return await plain(method, params, session)

    await backend._event(attached("x-s", "x", "iframe", "page"))
    backend.send = send
    # Two restores at once: the tab's, and a cross-site frame's.
    restore = {"type": "BackForwardCacheRestore"}
    backend._note("Page.frameNavigated", {"frame": {"id": "tab"}, **restore}, "page")
    backend._note(
        "Page.frameNavigated", {"frame": {"id": "x", "parentId": "tab"}, **restore}, "x-s"
    )
    await asyncio.sleep(0)
    backend._dispatch(
        {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": "00000000-0000-0000-0000-000000000003", "frameId": "r"},
        }
    )
    await asyncio.sleep(0.01)
    answers["page"].set()  # The tab's restore lands: "r" is the tab's...
    await asyncio.sleep(0.05)
    backend._note("Page.frameDetached", {"frameId": "r", "reason": "remove"}, "page")
    answers["x-s"].set()  # ...and stays its owner after "r" is removed.
    for _ in range(100):
        if decided:
            break
        await asyncio.sleep(0.01)
    assert decided == [True] and backend.awaiting_frames == {}


@pytest.mark.asyncio
async def test_a_download_owned_within_the_same_read_stays_owned() -> None:
    backend, _, files = tracking_backend()
    decided: list[Any] = []

    async def event(method: str, params: dict[str, Any]) -> None:
        decided.append(params.get("tracked"))

    files.event = event  # type: ignore[method-assign]
    answer = asyncio.Event()
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Page.getFrameTree":
            await plain(method, params, session)
            await answer.wait()
            return {"frameTree": {"frame": {"id": "tab"}}}
        return await plain(method, params, session)

    backend.send = send
    backend._note(
        "Page.frameNavigated", {"frame": {"id": "tab"}, "type": "BackForwardCacheRestore"}, "page"
    )
    await asyncio.sleep(0)
    # In one read, with no turn of the loop between them: a download begins in
    # a frame not tracked yet, the frame is noted, and it is removed.
    for message in [
        {
            "method": "Browser.downloadWillBegin",
            "params": {"guid": "00000000-0000-0000-0000-000000000004", "frameId": "r"},
        },
        {
            "method": "Page.frameNavigated",
            "sessionId": "page",
            "params": {"frame": {"id": "r", "parentId": "tab"}},
        },
        {
            "method": "Page.frameDetached",
            "sessionId": "page",
            "params": {"frameId": "r", "reason": "remove"},
        },
    ]:
        backend._dispatch(message)
    answer.set()
    for _ in range(100):
        if decided:
            break
        await asyncio.sleep(0.01)
    assert decided == [True] and backend.awaiting_frames == {}


@pytest.mark.asyncio
async def test_a_frame_tree_reply_counts_before_the_events_read_after_it() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = socket = ScriptedSocket()
    backend.files = files = FrameFiles()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    files.frames = {"tab": "tab"}
    decided: list[Any] = []

    async def event(method: str, params: dict[str, Any]) -> None:
        decided.append(params.get("tracked"))

    files.event = event  # type: ignore[method-assign]
    backend.reader = asyncio.create_task(backend._read())
    try:
        socket.push(
            {
                "method": "Page.frameNavigated",
                "sessionId": "page",
                "params": {"frame": {"id": "tab"}, "type": "BackForwardCacheRestore"},
            }
        )
        await settle(lambda: any(m["method"] == "Page.getFrameTree" for m in socket.sent))
        asked = next(m for m in socket.sent if m["method"] == "Page.getFrameTree")
        # In one read: a download from a restored frame, the frame tree that
        # says the frame is the tab's, and the frame's removal.
        socket.push(
            {
                "method": "Browser.downloadWillBegin",
                "params": {"guid": "00000000-0000-0000-0000-000000000005", "frameId": "r"},
            }
        )
        socket.push(
            {
                "id": asked["id"],
                "result": {
                    "frameTree": {"frame": {"id": "tab"}, "childFrames": [{"frame": {"id": "r"}}]}
                },
            }
        )
        socket.push(
            {
                "method": "Page.frameDetached",
                "sessionId": "page",
                "params": {"frameId": "r", "reason": "remove"},
            }
        )
        await settle(lambda: decided)
        assert decided == [True] and "r" not in files.frames and backend.snapshots == []
    finally:
        await socket.close()
        await backend.reader


@pytest.mark.asyncio
async def test_a_child_waiting_on_a_restored_parent_is_adopted_as_the_tree_is_read() -> None:
    from mandala_computer._browser_cdp import BrowserCDP

    backend = BrowserCDP(lambda: None, lambda _: None, None)
    backend.ws = socket = ScriptedSocket()
    backend.files = files = FrameFiles()
    backend.tabs = {"tab": {}}
    backend.sessions = {"tab": "page"}
    files.frames = {"tab": "tab"}
    decided: list[Any] = []

    async def event(method: str, params: dict[str, Any]) -> None:
        decided.append(params.get("tracked"))

    files.event = event  # type: ignore[method-assign]
    backend.reader = asyncio.create_task(backend._read())
    try:
        socket.push(
            {
                "method": "Page.frameNavigated",
                "sessionId": "page",
                "params": {"frame": {"id": "tab"}, "type": "BackForwardCacheRestore"},
            }
        )
        await settle(lambda: any(m["method"] == "Page.getFrameTree" for m in socket.sent))
        asked = next(m for m in socket.sent if m["method"] == "Page.getFrameTree")
        # In one read: a child of a restored frame attaches and downloads, the
        # tree says its parent is the tab's, and the child is removed.
        for message in [
            {
                "method": "Page.frameAttached",
                "sessionId": "page",
                "params": {"frameId": "kid", "parentFrameId": "r"},
            },
            {
                "method": "Browser.downloadWillBegin",
                "params": {"guid": "00000000-0000-0000-0000-000000000006", "frameId": "kid"},
            },
            {
                "id": asked["id"],
                "result": {
                    "frameTree": {"frame": {"id": "tab"}, "childFrames": [{"frame": {"id": "r"}}]}
                },
            },
            {
                "method": "Page.frameDetached",
                "sessionId": "page",
                "params": {"frameId": "kid", "reason": "remove"},
            },
        ]:
            socket.push(message)
        await settle(lambda: decided)
        assert decided == [True] and "kid" not in files.frames and "r" in files.frames
    finally:
        await socket.close()
        await backend.reader


#: Pages that open popups: from a click (scripting the popup's own window
#: while it is open), from script with no click, and from links with and
#: without an opener. Every popup URL contains "popup".
POPUP_PAGES = {
    "/click": (
        "<title>click</title><button style='position:absolute;left:0;top:0;width:400px;"
        "height:200px' onclick=\"const w=window.open('');if(!w){document.title='no popup';"
        "return}const out=['opened'];const blob=window.URL.createObjectURL(new Blob(["
        "'fetch(\\'http://127.0.0.1:'+location.port+'/popup-worker\\')']));"
        "try{w.fetch('/popup-fetch')}catch(e){};"
        "try{new w.Worker(blob);out.push('worker:made')}catch(e){out.push('worker:'+e.name)};"
        "try{const f=w.document.createElement('iframe');w.document.body.appendChild(f);"
        "new f.contentWindow.Worker(blob);out.push('frame-worker:made')}"
        "catch(e){out.push('frame-worker:'+e.name)};"
        "setTimeout(()=>{try{w.location='/popup-nav'}catch(e){}},50);"
        "document.title=out.join(' ')\">open</button>"
    ),
    "/navigate": (
        "<title>navigate</title><button style='position:absolute;left:0;top:0;width:400px;"
        "height:200px' onclick=\"const w=window.open('');if(w){w.location='/popup-nav'}"
        "document.title='opened'\">open</button>"
    ),
    "/script": "<title>script</title><script>window.open('/popup-url')</script>",
    "/online": (
        "<title>online</title><script>window.went=[];addEventListener('offline',"
        "()=>went.push('offline'))</script><button style='position:absolute;left:0;top:0;"
        "width:400px;height:200px' onclick=\"const w=window.open('');if(w){w.location="
        "'/popup-nav'}\">open</button>"
    ),
    "/link": (
        "<title>link</title><a href='/popup-link' target=_blank style='display:block;"
        "width:400px;height:200px'>open</a>"
    ),
    "/link-opener": (
        "<title>link</title><a href='/popup-link' target=_blank rel=opener "
        "style='display:block;width:400px;height:200px'>open</a>"
    ),
}


#: Neither the popup's window nor a frame made in it could start a worker.
POPUP_CLICK_RESULT = "opened worker:SecurityError frame-worker:SecurityError"


@pytest.fixture
def popups() -> Any:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            body = POPUP_PAGES.get(self.path, "<p>popup</p><script>fetch('/popup-sub')</script>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *_: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def popup_policy(_context: Any, url: str) -> None:
    if "popup" in url:
        raise ToolError("not permitted")


@pytest.mark.parametrize("page", list(POPUP_PAGES))
def test_sync_a_popup_is_refused_without_holding_up_its_opener(
    chrome: str, popups: Any, page: str
) -> None:
    computer, revoked = remote(chrome, False)
    port, hits = popups
    with MandalaBrowserToolset(computer, url_policy=popup_policy) as browser:
        assert_success(browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}{page}")))
        if page != "/script":
            began = time.monotonic()
            point = {"type": "coordinate", "x": 100, "y": 50}
            assert_success(browser.tool_result(use("left_click", target=point)))
            assert time.monotonic() - began < 5
        time.sleep(0.5)
        # The opener carries on: a further action on the same tab works, and
        # the popup is gone without having loaded anything.
        began = time.monotonic()
        assert_success(browser.tool_result(use("get_page_text")))
        assert time.monotonic() - began < 5
        assert len(tabs(assert_success(browser.tool_result(use("list_tabs"))))) == 1
        assert not browser._backend.failed
        if page == "/click":
            backend = browser._backend
            title = browser._run(backend.evaluate(backend.active, "document.title"))
            assert title == POPUP_CLICK_RESULT
    assert revoked == [IDENT]
    assert not [path for path in hits if "popup" in path]


@pytest.mark.asyncio
@pytest.mark.parametrize("page", list(POPUP_PAGES))
async def test_async_a_popup_is_refused_without_holding_up_its_opener(
    chrome: str, popups: Any, page: str
) -> None:
    computer, revoked = remote(chrome, True)
    port, hits = popups

    async def policy(context: Any, url: str) -> None:
        popup_policy(context, url)

    async with AsyncMandalaBrowserToolset(computer, url_policy=policy) as browser:
        assert_success(
            await browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}{page}"))
        )
        if page != "/script":
            point = {"type": "coordinate", "x": 100, "y": 50}
            assert_success(
                await asyncio.wait_for(browser.tool_result(use("left_click", target=point)), 5)
            )
        await asyncio.sleep(0.5)
        assert_success(await asyncio.wait_for(browser.tool_result(use("get_page_text")), 5))
        assert len(tabs(assert_success(await browser.tool_result(use("list_tabs"))))) == 1
        assert not browser._backend.failed
        if page == "/click":
            backend = browser._backend
            assert await backend.evaluate(backend.active, "document.title") == POPUP_CLICK_RESULT
    assert revoked == [IDENT]
    assert not [path for path in hits if "popup" in path]


def popup_attached(child: str = "popup-s", target: str = "popup-t") -> dict[str, Any]:
    return attached(child, target, "page", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "goes_after",
    ["Page.enable", "Network.emulateNetworkConditionsByRule", "Runtime.runIfWaitingForDebugger"],
)
async def test_a_popup_that_goes_while_refused_is_left_alone(goes_after: str) -> None:
    from mandala_computer._browser_cdp import BrowserError

    backend, sent = owned_backend()
    plain = backend.send
    gone = False

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        nonlocal gone
        if gone:
            # Chromium refuses a command for a session or target that has gone.
            raise BrowserError("Chromium could not complete the browser action.")
        result = await plain(method, params, session)
        if method == goes_after:
            gone = True
            backend._settle(
                {
                    "method": "Target.detachedFromTarget",
                    "params": {"sessionId": "popup-s", "targetId": "popup-t"},
                }
            )
            backend._settle({"method": "Target.targetDestroyed", "params": {"targetId": "popup-t"}})
        return result

    backend.send = send
    await backend._event(popup_attached())
    assert not backend.failed and backend.refused == {}
    assert [m for m, _, _ in sent][-1] == goes_after


@pytest.mark.asyncio
async def test_a_live_popup_that_cannot_be_closed_still_ends_the_session() -> None:
    from mandala_computer._browser_cdp import BrowserError

    backend, _ = owned_backend()
    closed: list[bool] = []

    async def close() -> None:
        closed.append(True)

    backend.ws = SimpleNamespace(close=close)
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Target.closeTarget":
            raise BrowserError("Chromium could not complete the browser action.")
        return await plain(method, params, session)

    backend.send = send
    await backend._event(popup_attached())
    assert backend.failed and closed


@pytest.mark.asyncio
async def test_a_refused_popups_dialogs_are_dismissed_and_not_reported() -> None:
    backend, sent = owned_backend()
    backend.refused = {"popup-s": "popup-t"}
    await backend._event(
        {
            "method": "Page.javascriptDialogOpening",
            "sessionId": "popup-s",
            "params": {"type": "alert", "message": "hi"},
        }
    )
    assert sent == [("Page.handleJavaScriptDialog", {"accept": False}, "popup-s")]
    assert list(backend.changes) == []


@pytest.mark.asyncio
async def test_a_chromium_without_per_rule_emulation_takes_the_popup_offline_whole() -> None:
    from mandala_computer._browser_cdp import _OFFLINE, BrowserError

    backend, sent = owned_backend()
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Network.emulateNetworkConditionsByRule":
            raise BrowserError("Chromium could not complete the browser action.")
        return await plain(method, params, session)

    backend.send = send
    await backend._event(popup_attached())
    assert ("Network.emulateNetworkConditions", _OFFLINE, "popup-s") in sent
    assert not backend.failed


@pytest.mark.asyncio
async def test_a_live_popup_that_cannot_be_fenced_is_closed_unreleased_and_ends_the_session() -> (
    None
):
    from mandala_computer._browser_cdp import BrowserError

    backend, sent = owned_backend()
    closed: list[bool] = []

    async def close() -> None:
        closed.append(True)

    backend.ws = SimpleNamespace(close=close)
    plain = backend.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        if method == "Network.enable" and session == "popup-s":
            raise BrowserError("Chromium could not complete the browser action.")
        return await plain(method, params, session)

    backend.send = send
    await backend._event(popup_attached())
    names = [m for m, _, _ in sent]
    assert "Runtime.runIfWaitingForDebugger" not in names and "Target.closeTarget" in names
    assert backend.failed and closed


#: Popups opened per leak test, two in three navigated by their opener at once.
#: Closing a popup detaches its session, and Chromium lets a request
#: intercepted on a session go on when it detaches: a fence that intercepted
#: rather than took the popup offline let 5 to 21 of 50 such navigations reach
#: the server (Chrome 145 to 154), which 30 make all but certain to show here.
POPUP_LEAK_RUNS = 45


def test_many_popups_load_nothing_and_never_take_their_opener_offline(
    chrome: str, popups: Any
) -> None:
    computer, revoked = remote(chrome, False)
    port, hits = popups
    point = {"type": "coordinate", "x": 100, "y": 50}
    with MandalaBrowserToolset(computer, url_policy=popup_policy) as browser:
        backend = browser._backend
        for run in range(POPUP_LEAK_RUNS):
            page = ("/navigate", "/navigate", "/link-opener")[run % 3]
            assert_success(
                browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}{page}"))
            )
            assert_success(browser.tool_result(use("left_click", target=point)))
            browser._run(asyncio.sleep(0.1))
        # The opener never goes offline, though its popups do.
        assert_success(browser.tool_result(use("navigate", url=f"http://127.0.0.1:{port}/online")))
        assert_success(browser.tool_result(use("left_click", target=point)))
        browser._run(asyncio.sleep(0.5))
        state = browser._run(
            backend.evaluate(backend.active, "JSON.stringify([window.went, navigator.onLine])")
        )
        assert json.loads(state) == [[], True]
        assert len(tabs(assert_success(browser.tool_result(use("list_tabs"))))) == 1
        assert not backend.failed
    time.sleep(0.5)
    assert revoked == [IDENT]
    assert [path for path in hits if "popup" in path] == []
