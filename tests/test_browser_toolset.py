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
