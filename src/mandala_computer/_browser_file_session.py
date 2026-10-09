"""Private asynchronous owner of one remote browser's file lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import re
import shlex
import uuid
from importlib.resources import files
from typing import Any

from anthropic.tools import ToolError

from ._browser_files import (
    FILE_ERROR,
    MIME_EXTENSIONS,
    BrowserDownload,
    BrowserFilePolicy,
    BrowserStagedFile,
    _StagedFiles,
    canonical_path,
    content_type,
    safe_filename,
)

_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


class BrowserFiles:
    def __init__(self, computer: Any, policy: BrowserFilePolicy, backend: Any) -> None:
        policy._bind(computer, self)
        self.computer, self.policy, self.backend = computer, policy, backend
        self.adapter = _StagedFiles(policy)
        self.scope = uuid.uuid4().hex
        self.root = "/run/mandala-browser-files/" + self.scope
        self.context = ""
        self.closed = False
        self.created = False
        self.cleanup_failed = False
        self.heartbeat: asyncio.Task[None] | None = None
        self.cleanup: asyncio.Task[None] | None = None
        self.acquisition: asyncio.Task[dict[str, Any]] | None = None
        self.frames: dict[str, str] = {}
        self.downloads: dict[str, dict[str, Any]] = {}
        self.prepared: dict[str, Any] | None = None
        self.script = base64.b64encode(
            files("mandala_computer").joinpath("_browser_guest.py").read_bytes()
        ).decode()

    def live(self) -> bool:
        return not self.closed and not self.backend.closed and not self.backend.failed

    def frame(self, ident: str, tab: str) -> bool:
        if ident not in self.frames and len(self.frames) >= 256:
            return False  # Extra frames' downloads are canceled without ending browsing.
        self.frames[ident] = tab
        return True

    def frame_tree(self, tree: dict[str, Any], tab: str) -> None:
        if not self.frame(tree["frame"]["id"], tab):
            return
        for child in tree.get("childFrames", []):
            self.frame_tree(child, tab)

    async def execute(self, command: str, *, desktop: bool = False) -> bytes:
        if (
            self.computer is not self.policy._computer
            or self.computer.id != self.policy._computer_id
        ):
            raise ToolError(FILE_ERROR)
        if len(command.encode()) > 120000:
            raise ToolError(FILE_ERROR)
        fn = self.computer.exec
        if inspect.iscoroutinefunction(fn):
            result = await asyncio.wait_for(fn(command, timeout=10, desktop=desktop), 15)
        else:
            result = await asyncio.wait_for(
                asyncio.to_thread(fn, command, timeout=10, desktop=desktop), 15
            )
        if (
            result.exit_code != 0
            or result.timed_out
            or result.truncated
            or result.output_unreadable
        ):
            raise ToolError(FILE_ERROR)
        if not isinstance(result.stdout, bytes) or len(result.stdout) > 2 * 1024 * 1024:
            raise ToolError(FILE_ERROR)
        return result.stdout

    async def remote(self, operation: str, **values: Any) -> dict[str, Any]:
        request = {
            "version": 1,
            "op": operation,
            "scope": self.scope,
            "context": self.context,
            "task": self.policy.task_id,
            "maximum": self.policy.max_file_bytes,
            **values,
        }
        argument = base64.b64encode(json.dumps(request).encode()).decode()
        code = f"import base64;exec(base64.b64decode('{self.script}'))"
        command = "python3 -c " + shlex.quote(code) + " " + shlex.quote(argument)
        value = json.loads(await self.execute(command))
        if not isinstance(value, dict) or "error" in value:
            raise ToolError(FILE_ERROR)
        return value

    def decode(self, value: dict[str, Any]) -> bytes:
        encoded = value.get("data")
        if not isinstance(encoded, str) or len(encoded) > 4 * (
            (self.policy.max_file_bytes + 2) // 3
        ):
            raise ToolError(FILE_ERROR)
        data = base64.b64decode(encoded, validate=True)
        if (
            type(value.get("size")) is not int
            or len(data) != value["size"]
            or len(data) > self.policy.max_file_bytes
            or hashlib.sha256(data).hexdigest() != value.get("sha256")
        ):
            raise ToolError(FILE_ERROR)
        return data

    async def setup(self, context: str) -> None:
        self.context = context
        self.adapter.context = context
        if not self.policy.downloads:
            return
        identities = json.loads(
            await self.execute(
                "python3 -c 'import os,json;print(json.dumps([os.getuid(),os.getgid()]))'",
                desktop=True,
            )
        )
        if (
            not isinstance(identities, list)
            or len(identities) != 2
            or any(type(i) is not int or i <= 0 for i in identities)
        ):
            raise ToolError(FILE_ERROR)
        if not self.live():
            raise ToolError(FILE_ERROR)
        self.created = True  # An uncertain create must still attempt cleanup.
        self.acquisition = asyncio.create_task(
            self.remote(
                "create", uid=identities[0], gid=identities[1], total=self.policy.max_total_bytes
            )
        )
        result = await asyncio.shield(self.acquisition)
        if not self.live() or result.get("path") != self.root + "/incoming":
            raise ToolError(FILE_ERROR)
        await self.backend.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allowAndName",
                "browserContextId": context,
                "downloadPath": result["path"],
                "eventsEnabled": True,
            },
        )
        self.heartbeat = asyncio.create_task(self.maintain())

    async def maintain(self) -> None:
        try:
            while self.live():
                await asyncio.sleep(60)
                if not self.live():
                    return
                await self.remote("heartbeat")
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - redact remote paths and credentials; fail closed
            self.backend.failed = True
            if self.backend.ws is not None:
                await self.backend.ws.close()

    async def stage(self, data: bytes, filename: str, source: str) -> BrowserStagedFile:
        try:
            if (
                not isinstance(data, (bytes, bytearray, memoryview))
                or len(data) > self.policy.max_file_bytes
            ):
                raise ToolError(FILE_ERROR)
            snapshot = bytes(data)
            # Validate before creating a browser or touching a guest.
            content_type(safe_filename(filename), snapshot, self.policy.allowed_mime_types)
            await self.backend.start()
            if not self.live():
                raise ToolError(FILE_ERROR)
            return self.adapter.add(filename, snapshot, source)
        except asyncio.CancelledError:
            await self.backend.close()
            raise
        except Exception:  # noqa: BLE001 - redact remote paths and credentials; fail closed
            raise ToolError(FILE_ERROR) from None

    async def stage_guest(self, path: str) -> BrowserStagedFile:
        try:
            canonical_path(path)
            if not any(path.startswith(root + "/") for root in self.policy.guest_upload_roots):
                raise ToolError(FILE_ERROR)
            await self.backend.start()
            data = self.decode(
                await self.remote("read", path=path, roots=list(self.policy.guest_upload_roots))
            )
            return await self.stage(data, path.rsplit("/", 1)[-1], "guest")
        except asyncio.CancelledError:
            await self.backend.close()
            raise
        except Exception:  # noqa: BLE001 - redact remote paths and credentials; fail closed
            raise ToolError(FILE_ERROR) from None

    async def stage_document(
        self, document_id: str, data: bytes, filename: str
    ) -> BrowserStagedFile:
        if (
            not isinstance(document_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,256}", document_id) is None
        ):
            raise ToolError(FILE_ERROR)
        return await self.stage(data, filename, "document:" + document_id)

    async def release_prepared(self) -> None:
        prepared, self.prepared = self.prepared, None
        if prepared and self.live():
            try:
                await self.backend.send(
                    "Runtime.releaseObject", {"objectId": prepared["object"]}, prepared["session"]
                )
            except Exception:  # noqa: BLE001, S110 - redact remote paths and credentials; fail closed
                pass

    async def prepare(self, context: Any) -> Any:
        """Run inside inherited confirm; pin current destination before prompting."""
        await self.release_prepared()
        try:
            call = context.tool_use.id if context.tool_use is not None else None
            data = context.input.model_dump(exclude_none=True)
            if not call or data.get("paths"):
                raise ToolError(FILE_ERROR)
            selected = self.adapter.selected(data.get("document_ids", []))
            await self.backend.start()
            tab = data.get("tab_id") or self.backend.active
            if tab not in self.backend.tabs:
                raise ToolError(FILE_ERROR)
            node = self.backend.refs.get(tab, {}).get(data["target"]["ref"])
            if node is None:
                raise ToolError(FILE_ERROR)
            session = self.backend.sessions[tab]
            tree = await self.backend.send("Page.getFrameTree", session=session)
            frame = tree["frameTree"]["frame"]
            world = await self.backend.send(
                "Page.createIsolatedWorld",
                {"frameId": frame["id"], "worldName": "mandala-approved-files"},
                session,
            )
            resolved = await self.backend.send(
                "DOM.resolveNode",
                {"backendNodeId": node, "executionContextId": world["executionContextId"]},
                session,
            )
            obj = resolved["object"]["objectId"]
            self.prepared = {
                "call": call,
                "ids": [f.id for f, _ in selected],
                "selected": selected,
                "tab": tab,
                "session": session,
                "object": obj,
                "approved": False,
                "context": self.context,
            }
            result = await self.backend.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": obj,
                    "functionDeclaration": "function(){if(!(this instanceof HTMLInputElement)||this.type!=='file'||!this.isConnected||this.ownerDocument!==document||this.disabled)throw Error();return {url:document.URL,multiple:this.multiple}}",
                    "returnByValue": True,
                },
                session,
            )
            if "exceptionDetails" in result or not self.live():
                raise ToolError(FILE_ERROR)
            destination = result["result"]["value"]
            if len(selected) > 1 and not destination["multiple"]:
                raise ToolError(FILE_ERROR)
            self.prepared["url"] = destination["url"]
            self.prepared["target"] = data["target"]
            return context.model_copy(update={"tab_id": tab, "tab_url": destination["url"]})
        except Exception:  # noqa: BLE001 - redact remote paths and credentials; fail closed
            await self.release_prepared()
            raise ToolError(FILE_ERROR) from None

    async def approved(self, allowed: bool) -> None:
        if allowed is True and self.prepared is not None and self.live():
            self.prepared["approved"] = True
        else:
            await self.release_prepared()

    async def upload(self, context: Any, data: dict[str, Any]) -> None:
        prepared = self.prepared
        self.prepared = None  # Consume even on failure; actions are never replayed.
        try:
            call = context.tool_use.id if context.tool_use is not None else None
            if (
                not prepared
                or not prepared["approved"]
                or not self.live()
                or prepared["context"] != self.context
                or prepared["call"] != call
                or data.get("paths")
                or data.get("document_ids") != prepared["ids"]
                or data.get("target") != prepared["target"]
                or (data.get("tab_id") or self.backend.active) != prepared["tab"]
            ):
                raise ToolError(FILE_ERROR)
            values = [
                {"name": f.filename, "type": f.mime_type, "data": base64.b64encode(b).decode()}
                for f, b in prepared["selected"]
            ]
            result = await self.backend.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": prepared["object"],
                    "functionDeclaration": "function(files,url){if(!(this instanceof HTMLInputElement)||this.type!=='file'||!this.isConnected||this.ownerDocument!==document||document.URL!==url||this.disabled||(!this.multiple&&files.length>1))throw Error();const dt=new DataTransfer();for(const f of files){const s=atob(f.data),b=new Uint8Array(s.length);for(let i=0;i<s.length;i++)b[i]=s.charCodeAt(i);dt.items.add(new File([b],f.name,{type:f.type}))}Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'files').set.call(this,dt.files);this.dispatchEvent(new Event('input',{bubbles:true}));this.dispatchEvent(new Event('change',{bubbles:true}));}",
                    "arguments": [{"value": values}, {"value": prepared["url"]}],
                    "returnByValue": True,
                },
                prepared["session"],
            )
            if "exceptionDetails" in result:
                raise ToolError(FILE_ERROR)
        except Exception:  # noqa: BLE001 - redact remote paths and credentials; fail closed
            raise ToolError(FILE_ERROR) from None
        finally:
            if prepared:
                # Staged bytes are single-use once an approved upload dispatches.
                with self.adapter.lock:
                    for ident in prepared["ids"]:
                        self.adapter.files.pop(ident, None)
                if self.live():
                    try:
                        await self.backend.send(
                            "Runtime.releaseObject",
                            {"objectId": prepared["object"]},
                            prepared["session"],
                        )
                    except Exception:  # noqa: BLE001, S110 - redact remote paths and credentials; fail closed
                        pass

    async def fail_download(self, guid: str) -> None:
        entry = self.downloads[guid]
        entry["state"] = "failed"
        if self.live():
            self.backend.changes.append(
                {
                    "type": "download_failed",
                    "download_id": guid,
                    "url": entry["url"],
                    "error": FILE_ERROR,
                }
            )
            try:
                await self.backend.send(
                    "Browser.cancelDownload", {"guid": guid, "browserContextId": self.context}
                )
            except Exception:  # noqa: BLE001, S110 - redact remote paths and credentials; fail closed
                pass
        try:
            await self.remote("discard", guid=guid)
        except Exception:  # noqa: BLE001, S110 - redact remote paths and credentials; fail closed
            pass  # The fixed quota and guardian still contain this context.

    async def cancel_untracked(self, guid: str) -> None:
        try:
            # Chromium looks up this GUID only inside the specified context.
            await self.backend.send(
                "Browser.cancelDownload", {"guid": guid, "browserContextId": self.context}
            )
        except Exception:  # noqa: BLE001 - foreign-context GUIDs cannot be canceled here
            return
        try:
            await self.remote("discard", guid=guid)
        except Exception:  # noqa: BLE001, S110 - quota and guardian still bound leftovers
            pass

    async def event(self, method: str, params: dict[str, Any]) -> None:
        if not self.policy.downloads or not self.live():
            return
        guid = params.get("guid")
        if not isinstance(guid, str) or _GUID.fullmatch(guid) is None:
            return
        if method == "Browser.downloadWillBegin":
            if guid in self.downloads:
                return
            if params.get("frameId") not in self.frames:
                await self.cancel_untracked(guid)
                return
            # Reserve before the first await; event handlers run concurrently.
            if len(self.downloads) >= self.policy.max_files:
                await self.cancel_untracked(guid)
                return
            entry = {"url": str(params.get("url", ""))[:4096], "state": "receiving"}
            self.downloads[guid] = entry
            try:
                entry["name"] = safe_filename(params["suggestedFilename"])
                extension = "." + entry["name"].rsplit(".", 1)[-1].lower()
                if not any(extension in MIME_EXTENSIONS[m] for m in self.policy.allowed_mime_types):
                    raise ValueError()
                self.backend.changes.append(
                    {"type": "download_started", "download_id": guid, "url": entry["url"]}
                )
            except Exception:  # noqa: BLE001 - redact remote paths and credentials; fail closed
                await self.fail_download(guid)
        elif method == "Browser.downloadProgress" and guid in self.downloads:
            entry = self.downloads[guid]
            if entry["state"] != "receiving":
                return
            size = params.get("receivedBytes")
            total = params.get("totalBytes")
            if (
                not isinstance(size, (int, float))
                or isinstance(size, bool)
                or not 0 <= size <= self.policy.max_file_bytes
                or not isinstance(total, (int, float))
                or isinstance(total, bool)
                or not 0 <= total <= self.policy.max_file_bytes
                or params.get("state") == "canceled"
            ):
                await self.fail_download(guid)
                return
            if params.get("state") != "completed":
                return
            entry["state"] = "checking"
            try:
                data = self.decode(await self.remote("seal", guid=guid))
                mime = content_type(entry["name"], data, self.policy.allowed_mime_types)
                digest = hashlib.sha256(data).hexdigest()
                info = BrowserDownload(
                    guid,
                    entry["name"],
                    mime,
                    len(data),
                    digest,
                    self.policy._computer_id,
                    self.policy.task_id,
                    self.context,
                    entry["url"],
                )
                callback = self.policy.approve_download
                assert callback is not None

                async def approve() -> Any:
                    result = await asyncio.to_thread(callback, info)
                    return await result if inspect.isawaitable(result) else result

                allowed = await asyncio.wait_for(approve(), 30)
                if allowed is not True or not self.live():
                    raise ValueError()
                published = await self.remote(
                    "publish", guid=guid, name=entry["name"], sha256=digest
                )
                path = self.root + "/approved/" + guid + "-" + entry["name"]
                if published.get("path") != path or not self.live():
                    raise ValueError()
                with self.adapter.lock:
                    if self.adapter.closed:
                        raise ToolError(FILE_ERROR)
                    self.adapter.visible.add(path)
                entry["state"] = "complete"
                self.backend.changes.append(
                    {
                        "type": "download_completed",
                        "download_id": guid,
                        "url": entry["url"],
                        "size_bytes": len(data),
                        "path": path,
                    }
                )
            except Exception:  # noqa: BLE001 - redact remote paths and credentials; fail closed
                await self.fail_download(guid)

    async def close(self) -> None:
        self.closed = True
        self.adapter.clear()
        self.prepared = None
        if self.heartbeat is not None and self.heartbeat is not asyncio.current_task():
            self.heartbeat.cancel()
        if self.created:

            async def cleanup() -> None:
                # Closing must not overtake acquisition and miss a late mount.
                confirmed = self.acquisition is None
                if self.acquisition is not None:
                    try:
                        acquired = await asyncio.shield(self.acquisition)
                        confirmed = acquired.get("path") == self.root + "/incoming"
                    except Exception:  # noqa: BLE001, S110 - uncertain create still needs cleanup
                        pass
                try:
                    result = await asyncio.wait_for(self.remote("close"), 10)
                    if not confirmed and result.get("removed") is not True:
                        # Exec timeout doesn't prove the guest command stopped.
                        raise ToolError(FILE_ERROR)
                    self.created = False
                    self.cleanup_failed = False
                except Exception:  # noqa: BLE001 - redact remote paths and credentials; fail closed
                    self.cleanup_failed = True

            if self.cleanup is None or (self.cleanup.done() and self.cleanup_failed):
                self.cleanup = asyncio.create_task(cleanup())
            await asyncio.shield(self.cleanup)
