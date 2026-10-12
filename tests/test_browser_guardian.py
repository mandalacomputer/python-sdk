"""Opt-in integration with a real backend CDP connection guardian.

The public SDK does not contain or download that backend. Set
MANDALA_TEST_CDP_PROXY to a local Python launcher that accepts Chromium's root
WebSocket URL and prints {"url": "ws://127.0.0.1:port/<same browser path>"} on
its first stdout line. The launcher must keep the production guardian alive
when its downstream WebSocket closes; diagnostics belong on stderr.

These cases cut the SDK's downstream connection only. Advertising host-link
protection is checked for compatibility, but exercising that separate failure
mode belongs to the backend integration suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import selectors
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from anthropic.tools import ToolError
from tests.test_browser_request_policy import policy_site, raw_browser, until  # noqa: F401
from tests.test_browser_toolset import (  # noqa: F401
    IDENT,
    assert_success,
    chrome,
    remote,
    tabs,
    use,
)

from mandala_computer.anthropic import AsyncMandalaBrowserToolset, MandalaBrowserToolset

pytestmark = pytest.mark.skipif(
    not os.environ.get("MANDALA_TEST_CDP_PROXY"),
    reason="set MANDALA_TEST_CDP_PROXY for real backend guardian integration",
)


@pytest.fixture
def guardian(chrome: str, tmp_path: Any) -> Any:  # noqa: F811
    script = Path(os.environ["MANDALA_TEST_CDP_PROXY"]).expanduser()
    assert script.is_file(), "MANDALA_TEST_CDP_PROXY must name a local Python launcher"
    stderr_path = tmp_path / "guardian-stderr.log"
    with stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, str(script), chrome],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr,
        )
        try:
            assert process.stdout is not None
            line = b""
            deadline = time.monotonic() + 15
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while b"\n" not in line and len(line) <= 8192:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        break
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    line += chunk
            assert b"\n" in line and len(line) <= 8192, (
                "guardian launcher did not publish its URL: "
                + stderr_path.read_text(errors="replace")[-2000:]
            )
            url = json.loads(line.split(b"\n", 1)[0])["url"]
            parsed = urlsplit(url)
            assert (
                parsed.scheme == "ws"
                and parsed.hostname == "127.0.0.1"
                and parsed.port is not None
                and parsed.username is None
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
                and parsed.path == urlsplit(chrome).path
            ), "guardian launcher must expose the same browser path on loopback"
            yield url, process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("ending", ["abort", "close"])
async def test_guardian_disposes_owned_context_before_releasing_held_http(
    chrome: str,  # noqa: F811
    guardian: Any,
    policy_site: Any,  # noqa: F811
    asynchronous: bool,
    ending: str,
) -> None:
    """Cut only the SDK link; the guardian and its Chromium link stay alive."""
    url, process = guardian
    base, hits = policy_site
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    seen: list[tuple[str | None, str]] = []
    callback_timeout = 10.0

    def sync_policy(context: Any, destination: str) -> None:
        seen.append((context.tab_id, destination))
        if destination == base + "/held":
            entered.set()
            if not release.wait(callback_timeout):
                raise ToolError("test callback was not released")
            returned.set()

    async def async_policy(context: Any, destination: str) -> None:
        seen.append((context.tab_id, destination))
        if destination == base + "/held":
            entered.set()
            deadline = asyncio.get_running_loop().time() + callback_timeout
            while not release.is_set():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ToolError("test callback was not released")
                await asyncio.sleep(min(0.02, remaining))
            returned.set()

    computer, revoked = remote(url, asynchronous)
    browser: Any = (
        AsyncMandalaBrowserToolset(computer, url_policy=async_policy)
        if asynchronous
        else MandalaBrowserToolset(computer, url_policy=sync_policy)
    )
    driver = browser._backend
    commands: list[str] = []
    plain_send = driver.send

    async def send(method: str, params: Any = None, session: Any = None) -> Any:
        commands.append(method)
        return await plain_send(method, params, session)

    driver.send = send

    async def on_driver(coroutine: Any) -> Any:
        if asynchronous:
            return await coroutine
        return await asyncio.to_thread(browser._run, coroutine)

    async def tool(name: str, **kwargs: Any) -> Any:
        if asynchronous:
            return await browser.tool_result(use(name, **kwargs))
        return await asyncio.to_thread(browser.tool_result, use(name, **kwargs))

    async with raw_browser(chrome) as foreign:
        # This direct observer never enables Fetch. Its separate incognito
        # context is owned by a different root connection and must survive.
        session = await foreign.page(incognito=True)
        foreign_context = (await foreign.send("Target.getTargetInfo", session=session))[
            "targetInfo"
        ]["browserContextId"]
        try:
            await on_driver(driver.start())
            capabilities = await on_driver(driver.send("Mandala.getConnectionCapabilities"))
            assert capabilities.get("context_guard_version") == 1, capabilities
            assert {"downstream", "host_link"} <= set(
                capabilities.get("protected_disconnects", [])
            ), capabilities
            nav = assert_success(await tool("navigate", url=base + "/owned"))
            tab = tabs(nav)[0]["tab_id"]
            owned_context = driver.context
            assert owned_context and owned_context != foreign_context
            assert commands.count("Fetch.enable") == 1
            await on_driver(driver.evaluate(tab, f"void fetch('{base}/held').catch(()=>null)"))
            await until(entered.is_set)
            assert "/held" not in hits
            assert seen.count((tab, base + "/held")) == 1

            async def disconnect() -> None:
                if ending == "abort":
                    driver.ws.transport.abort()
                else:
                    await driver.ws.close()
                await asyncio.wait_for(driver.reader, 5)

            await on_driver(disconnect())
            after_loss = len(commands)
            assert driver.failed and browser.session_status["state"] == "ended"
            # Complete the caller's decision only after its transport has
            # ended. No SDK continuation may be sent by this late result.
            release.set()
            await until(returned.is_set)
            await until(lambda: not driver.policy_lane.running)
            assert "Fetch.continueRequest" not in commands[after_loss:]
            assert process.poll() is None, "the guardian launcher died instead of protecting loss"
            for _ in range(200):
                contexts = (await foreign.send("Target.getBrowserContexts"))["browserContextIds"]
                if owned_context not in contexts:
                    break
                await asyncio.sleep(0.02)
            assert owned_context not in contexts
            assert foreign_context in contexts
            await foreign.send("Page.navigate", {"url": base + "/foreign-after-loss"}, session)
            await until(lambda: "/foreign-after-loss" in hits)
            await asyncio.sleep(1)
            assert not [destination for _, destination in seen if "/foreign-" in destination]
            assert "/held" not in hits, "the protected disconnect released a pending HTTP request"
            assert (await tool("list_tabs")).get("is_error")
        finally:
            release.set()
            if asynchronous:
                await browser.close()
            else:
                await asyncio.to_thread(browser.close)
    assert revoked == [IDENT]
    assert "/held" not in hits
