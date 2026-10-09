"""Remote file safety boundaries, including the real inherited upload pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.tools import ToolError, ToolsetConfigError
from tests.test_browser_toolset import assert_success, remote, text, use
from tests.test_browser_toolset import chrome as chrome  # noqa: PLC0414 - pytest fixtures
from tests.test_browser_toolset import website as website  # noqa: PLC0414 - pytest fixture

from mandala_computer._browser_files import _StagedFiles, safe_filename
from mandala_computer._browser_guest import read_upload
from mandala_computer.anthropic import (
    AsyncMandalaBrowserToolset,
    BrowserFilePolicy,
    MandalaBrowserToolset,
)


def test_file_policy_bindings_and_limits() -> None:
    computer, _ = remote("", False)
    computer.id = "vm-one"
    policy = BrowserFilePolicy(computer, task_id="task-a", max_file_bytes=4, max_total_bytes=4)
    with pytest.raises(ToolsetConfigError):
        MandalaBrowserToolset(
            computer, remote_file_policy=policy, configs={"file_upload": {"enabled": True}}
        )
    other = SimpleNamespace(**vars(computer))
    with pytest.raises(ValueError):
        MandalaBrowserToolset(other, remote_file_policy=policy)
    with MandalaBrowserToolset(computer, remote_file_policy=policy), pytest.raises(ValueError):
        MandalaBrowserToolset(computer, remote_file_policy=policy)
    adapter = _StagedFiles(policy)
    adapter.context = "context"
    item = adapter.add("../../a.txt", b"abc", "local")
    assert item.filename == "a.txt" and item.sha256 == hashlib.sha256(b"abc").hexdigest()
    assert item.filename in item.id and item.sha256 in item.id
    assert adapter.selected([item.id])[0][1] == b"abc"
    for ids in ([item.id, item.id], ["file_unauthorized"], ["https://evil.test/file"]):
        with pytest.raises(ToolError):
            adapter.selected(ids)
    with pytest.raises(ValueError):
        adapter.add("next.txt", b"ab", "local")
    with pytest.raises(ValueError, match="File type is not allowed"):
        adapter.add("evil.exe", b"M", "local")
    with pytest.raises(ToolError):
        adapter.resolve_upload_paths(None, ["/etc/passwd"])
    adapter.clear()
    assert not adapter.is_path_visible("/anything")
    with pytest.raises(ToolError):
        adapter.selected([item.id])


def test_guest_snapshot_refuses_links_special_files_and_escape(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    allowed = root / "allowed"
    allowed.mkdir()
    original = allowed / "safe.txt"
    original.write_bytes(b"approved")
    assert read_upload(str(original), [str(allowed)], 8) == b"approved"
    with pytest.raises(ValueError):
        read_upload(str(original), [str(allowed)], 7)
    secret = root / "secret.txt"
    secret.write_bytes(b"secret")
    (allowed / "link.txt").symlink_to(secret)
    (allowed / "dir").symlink_to(root, target_is_directory=True)
    os.mkfifo(allowed / "pipe.txt")
    os.link(secret, allowed / "hard.txt")
    for path in [
        allowed / "link.txt",
        allowed / "dir" / "secret.txt",
        allowed / "pipe.txt",
        allowed / "hard.txt",
        allowed,
        secret,
    ]:
        with pytest.raises((OSError, ValueError)):
            read_upload(str(path), [str(allowed)], 100)
    for path in [str(allowed) + "/../secret.txt", str(allowed) + "//safe.txt"]:
        with pytest.raises(ValueError):
            read_upload(path, [str(allowed)], 100)
    snapshot = read_upload(str(original), [str(allowed)], 8)
    original.unlink()
    original.symlink_to(secret)
    assert snapshot == b"approved"
    with pytest.raises(OSError):
        read_upload(str(original), [str(allowed)], 8)


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("..\\evil.txt", "evil.txt"),
        ("../../safe.txt", "safe.txt"),
        ("a\u202etxt.exe", "a_txt.exe"),
        ("\x00x.txt", "_x.txt"),
    ],
)
def test_filenames(filename: str, expected: str) -> None:
    assert safe_filename(filename) == expected


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("mutation", ["", "replace", "url", "refuse"])
def test_chromium_upload_pins_bytes_and_destination(
    chrome: str, website: Any, asynchronous: bool, mutation: str
) -> None:
    computer, _ = remote(chrome, asynchronous)
    computer.id = "vm-files"
    seen = []
    browser: Any

    async def change() -> None:
        tab = browser._backend.active
        session = browser._backend.sessions[tab]
        expression = (
            "document.querySelector('input').replaceWith(document.createElement('input'))"
            if mutation == "replace"
            else "history.pushState({},'', '/new-destination')"
        )
        await browser._backend.send("Runtime.evaluate", {"expression": expression}, session)

    def confirm(context: Any) -> bool:
        if context.member != "file_upload":
            return True
        seen.append(context)
        if not asynchronous and mutation in ("replace", "url"):
            browser._run(change())
        return mutation != "refuse"

    async def async_confirm(context: Any) -> bool:
        if context.member == "file_upload" and mutation in ("replace", "url"):
            await change()
        return confirm(context)

    policy = BrowserFilePolicy(computer, task_id="upload-test")
    options = {
        "remote_file_policy": policy,
        "confirm": async_confirm if asynchronous else confirm,
        "configs": {"file_upload": {"enabled": True}, "javascript_exec": {"enabled": True}},
    }

    async def run_async() -> None:
        nonlocal browser
        async with AsyncMandalaBrowserToolset(computer, **options) as browser:
            await scenario(browser.tool_result, browser.stage_local_file)

    async def scenario(call: Any, stage: Any) -> None:
        assert_success(await call(use("navigate", url=website[0])))
        assert_success(
            await call(
                use(
                    "javascript_exec",
                    text="document.body.innerHTML='<label>Upload <input id=upload type=file></label><p id=result></p>';document.querySelector('input').onchange=async e=>{document.querySelector('p').textContent=await e.target.files[0].text()}",
                )
            )
        )
        page = assert_success(await call(use("read_page", filter="all")))
        import re

        target = {
            "type": "ref",
            "ref": re.search(r"\[(e\d+)\] button.*(?:Upload|upload)", text(page)).group(1),
        }
        content = bytearray(b"approved bytes")
        item = await stage(content, filename="../../exact.txt")
        content[:] = b"changed bytes!"
        refused = await call(use("file_upload", target=target, paths=["/etc/passwd"]))
        assert refused.get("is_error") and not seen
        refused = await call(use("file_upload", target=target, document_ids=["file_not_staged"]))
        assert refused.get("is_error") and not seen
        result = await call(use("file_upload", target=target, document_ids=[item.id]))
        assert len(seen) == 1 and seen[0].tab_url == website[0] + "/", (
            text(page) + "\n" + text(result)
        )
        assert item.id in seen[0].input.document_ids
        if mutation:
            assert result.get("is_error"), text(result)
        else:
            assert_success(result)
            await asyncio.sleep(0.05)
            value = assert_success(await call(use("get_page_text")))
            assert "approved bytes" in text(value) and "changed bytes" not in text(value)
            assert (await call(use("file_upload", target=target, document_ids=[item.id]))).get(
                "is_error"
            )

    if asynchronous:
        asyncio.run(run_async())
    else:
        with MandalaBrowserToolset(computer, **options) as browser:
            # Exercise the sync inherited pipeline from a thread, while CDP work stays on its owner loop.
            async def call(input: Any) -> Any:
                return await asyncio.to_thread(browser.tool_result, input)

            async def stage(content: bytes, **kwargs: Any) -> Any:
                return await asyncio.to_thread(browser.stage_local_file, content, **kwargs)

            browser._run(scenario(call, stage))


@pytest.mark.asyncio
async def test_parallel_staging_and_close_during_start(monkeypatch: Any) -> None:
    from mandala_computer._browser_cdp import BrowserCDP
    from mandala_computer._browser_file_session import BrowserFiles

    begun, release = asyncio.Event(), asyncio.Event()
    creates, revoked = [], []

    async def create() -> Any:
        creates.append(1)
        begun.set()
        await release.wait()
        return SimpleNamespace(id="late-grant", url="ws://unused", token="unused")

    async def revoke(ident: str) -> None:
        revoked.append(ident)

    backend = BrowserCDP(create, revoke, lambda *args: None)
    computer = SimpleNamespace(id="vm")
    backend.files = files = BrowserFiles(
        computer, BrowserFilePolicy(computer, task_id="task"), backend
    )
    one = asyncio.create_task(files.stage(b"a", "a.txt", "local"))
    two = asyncio.create_task(files.stage(b"b", "b.txt", "local"))
    await begun.wait()
    closed = asyncio.create_task(backend.close())
    await asyncio.sleep(0)
    release.set()
    result = await asyncio.gather(one, two, return_exceptions=True)
    await closed
    assert len(creates) == 1 and revoked == ["late-grant"] and backend.ws is None
    assert all(isinstance(item, ToolError) for item in result)
    assert not files.adapter.files


@pytest.mark.asyncio
async def test_cleanup_failure_is_visible_and_retryable() -> None:
    computer = SimpleNamespace(
        id="vm", create_browser_connection=lambda: None, revoke_browser_connection=lambda _: None
    )
    browser = AsyncMandalaBrowserToolset(
        computer, remote_file_policy=BrowserFilePolicy(computer, task_id="task")
    )
    files = browser._files()
    files.created = True
    calls = []

    async def remote(op: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(op)
        if len(calls) == 1:
            raise OSError("unreachable")
        return {}

    files.remote = remote
    with pytest.raises(ToolError, match="cleanup"):
        await browser.close()
    assert browser.session_status["file_cleanup_failed"]
    await browser.close()
    assert calls == ["close", "close"] and not files.created
    assert not browser.session_status["file_cleanup_failed"]


def test_sync_ended_state_is_readable_without_reviving_a_worker() -> None:
    computer, _ = remote("", False)
    computer.id = "vm"
    browser = MandalaBrowserToolset(
        computer, remote_file_policy=BrowserFilePolicy(computer, task_id="task")
    )
    browser.close()
    with pytest.raises(ToolError):
        browser.stage_local_file(b"a", filename="a.txt")
    assert browser._worker is None
    browser = MandalaBrowserToolset(computer)
    browser._backend.failed = True
    browser._backend.terminal_reason = "Browser renewal failed."
    assert "renewal failed" in text(browser.tool_result(use("list_tabs")))
    assert browser._worker is None
    browser.close()


def test_sync_close_cannot_miss_worker_under_construction(monkeypatch: Any) -> None:
    import threading

    from mandala_computer import _anthropic_browser as driver

    begun, release, closed = threading.Event(), threading.Event(), threading.Event()
    original = driver._Loop
    workers = []

    class PausedLoop(original):
        def __init__(self) -> None:
            super().__init__()
            workers.append(self)
            begun.set()
            assert release.wait(5)

    monkeypatch.setattr(driver, "_Loop", PausedLoop)
    computer, _ = remote("", False)
    computer.id = "vm"
    browser = MandalaBrowserToolset(
        computer, remote_file_policy=BrowserFilePolicy(computer, task_id="task")
    )
    failures = []

    def stage() -> None:
        try:
            browser.stage_local_file(b"a", filename="a.txt")
        except ToolError:
            failures.append(1)

    def close() -> None:
        browser.close()
        closed.set()

    staging = threading.Thread(target=stage)
    staging.start()
    assert begun.wait(5)
    closing = threading.Thread(target=close)
    closing.start()
    assert not closed.wait(0.05)
    release.set()
    staging.join(5)
    closing.join(5)
    assert not staging.is_alive() and not closing.is_alive()
    assert len(workers) == 1 and not workers[0].thread.is_alive()
    assert browser._worker is None and len(failures) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "allow",
        "deny",
        "truthy",
        "content",
        "extension",
        "oversize",
        "canceled",
        "wrongpath",
        "throw",
        "unknown",
        "foreign",
        "timeout",
    ],
)
async def test_download_approval_and_path_visibility(mode: str, monkeypatch: Any) -> None:
    import base64
    from unittest.mock import AsyncMock

    from mandala_computer._browser_file_session import BrowserFiles

    seen = []

    async def approve(file: Any) -> Any:
        assert not files.adapter.visible and not hasattr(file, "path")
        seen.append(file)
        if mode == "throw":
            raise RuntimeError("private callback error")
        if mode == "timeout":
            await asyncio.Event().wait()
        return True if mode == "allow" or mode == "wrongpath" else 1 if mode == "truthy" else False

    computer = SimpleNamespace(id="vm")
    backend = SimpleNamespace(
        closed=False, failed=False, changes=[], send=AsyncMock(return_value={})
    )
    files = BrowserFiles(
        computer,
        BrowserFilePolicy(
            computer,
            task_id="task",
            downloads=True,
            approve_download=approve,
            max_file_bytes=32,
            max_total_bytes=64,
        ),
        backend,
    )
    files.context = files.adapter.context = "context"
    files.frames["frame"] = "tab"
    guid = "00000000-0000-0000-0000-000000000001"
    data = b"hello" if mode != "content" else b"\0invalid"
    operations = []

    async def remote(op: str, **kwargs: Any) -> dict[str, Any]:
        operations.append(op)
        if op == "seal":
            return {
                "data": base64.b64encode(data).decode(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        if op == "publish":
            return {
                "path": "/outside.txt"
                if mode == "wrongpath"
                else files.root + "/approved/" + guid + "-safe.txt"
            }
        return {}

    files.remote = remote
    if mode == "foreign":
        backend.send.side_effect = RuntimeError("foreign GUID")
    if mode == "timeout":
        original = asyncio.wait_for

        async def short_wait(awaitable: Any, seconds: float) -> Any:
            return await original(awaitable, 0.01 if seconds == 30 else seconds)

        monkeypatch.setattr(asyncio, "wait_for", short_wait)
    await files.event(
        "Browser.downloadWillBegin",
        {
            "guid": guid,
            "frameId": "unknown" if mode in ("unknown", "foreign") else "frame",
            "url": "https://source.test/file",
            "suggestedFilename": "evil.exe" if mode == "extension" else "../../safe.txt",
        },
    )
    assert not files.adapter.visible
    await files.event(
        "Browser.downloadProgress",
        {
            "guid": guid,
            "state": "canceled" if mode == "canceled" else "completed",
            "receivedBytes": 33 if mode == "oversize" else len(data),
            "totalBytes": len(data),
            "filePath": "/untrusted-browser-path",
        },
    )
    assert len(files.adapter.visible) == int(mode == "allow")
    if mode == "allow":
        path = next(iter(files.adapter.visible))
        assert files.adapter.is_path_visible(path)
        assert backend.changes[-1]["path"] == path
        assert seen[0].sha256 == hashlib.sha256(data).hexdigest()
    else:
        assert all("path" not in event for event in backend.changes)
    if mode in ("content", "extension", "oversize", "canceled", "unknown", "foreign"):
        assert not seen and "publish" not in operations
    if mode == "foreign":
        assert not operations
    if mode == "unknown":
        assert operations == ["discard"]
    assert "/untrusted-browser-path" not in str(backend.changes)
    await files.close()
    assert not files.adapter.visible


@pytest.mark.parametrize(
    "content", [b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b"{oops}", b"\xff"]
)
def test_json_content_rejects_nonstandard_or_invalid_values(content: bytes) -> None:
    from mandala_computer._browser_files import content_type

    with pytest.raises(ValueError):
        content_type("data.json", content, ("application/json",))


@pytest.mark.parametrize(
    "destination,valid,count",
    [
        ({"url": 42, "multiple": True}, False, 1),
        ({"url": "https://example.test/", "multiple": "yes"}, False, 1),
        ({"url": "https://example.test/", "multiple": 1}, False, 1),
        ({"url": "https://example.test/"}, False, 1),
        (None, False, 1),
        ({"url": "https://example.test/", "multiple": False}, True, 1),
        ({"url": "https://example.test/", "multiple": False}, False, 2),
        ({"url": "https://example.test/", "multiple": True}, True, 2),
    ],
)
async def test_upload_confirmation_requires_typed_destination(
    destination: Any, valid: bool, count: int
) -> None:
    from mandala_computer._browser_file_session import BrowserFiles

    operations: list[str] = []

    async def start() -> None:
        pass

    async def send(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        operations.append(method)
        return {
            "Page.getFrameTree": {"frameTree": {"frame": {"id": "frame"}}},
            "Page.createIsolatedWorld": {"executionContextId": 1},
            "DOM.resolveNode": {"object": {"objectId": "input"}},
            "Runtime.callFunctionOn": {"result": {"value": destination}},
        }.get(method, {})

    backend = SimpleNamespace(
        closed=False,
        failed=False,
        active="tab",
        tabs={"tab": {}},
        refs={"tab": {"upload": 1}},
        sessions={"tab": "session"},
        start=start,
        send=send,
    )
    computer = SimpleNamespace(id="vm")
    files = BrowserFiles(computer, BrowserFilePolicy(computer, task_id="task"), backend)
    files.context = files.adapter.context = "context"
    items = [files.adapter.add(f"upload-{i}.txt", b"approved", "local") for i in range(count)]
    context = SimpleNamespace(
        tool_use=SimpleNamespace(id="call"),
        input=SimpleNamespace(
            model_dump=lambda **_: {
                "target": {"ref": "upload"},
                "document_ids": [item.id for item in items],
            }
        ),
        model_copy=lambda *, update: SimpleNamespace(**update),
    )
    if valid:
        reviewed = await files.prepare(context)
        assert reviewed.tab_url == "https://example.test/"
        await files.approved(False)
    else:
        with pytest.raises(ToolError, match="Remote browser file operation"):
            await files.prepare(context)
    assert "Runtime.releaseObject" in operations
    assert files.prepared is None
    await files.close()
