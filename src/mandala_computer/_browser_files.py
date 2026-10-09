"""Explicit, context-bound file staging for a remote browser.

No model-supplied string causes a local read, remote read, or document retrieval.
The harness stages bytes first; the inherited confirmation gate approves their
immutable identities before the driver can put them into a file input.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from anthropic.tools import ToolError

FILE_ERROR = "Remote browser file operation was refused or could not complete."
MIME_EXTENSIONS = {
    "text/plain": (".txt",),
    "text/csv": (".csv",),
    "application/json": (".json",),
    "application/pdf": (".pdf",),
    "image/png": (".png",),
    "image/jpeg": (".jpg", ".jpeg"),
}


def canonical_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or len(value) > 4096
        or "\\" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(p in ("", ".", "..") for p in value.split("/")[1:])
    ):
        raise ValueError("Use a canonical absolute path without symlinks or dot segments")
    return value


def safe_filename(value: str) -> str:
    """A display name only; guest storage always uses generated identifiers."""
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Invalid file name")
    # Treat Windows separators and control/bidi characters as untrusted too.
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip(".")[:120]
    if not name or name in (".", ".."):
        raise ValueError("Invalid file name")
    return name


def content_type(name: str, data: bytes, allowed: tuple[str, ...]) -> str:
    extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = next((m for m in allowed if extension in MIME_EXTENSIONS[m]), None)
    if mime is None:
        raise ValueError("File type is not allowed")
    if mime == "application/pdf":
        valid = data.startswith(b"%PDF-")
    elif mime == "image/png":
        valid = data.startswith(b"\x89PNG\r\n\x1a\n")
    elif mime == "image/jpeg":
        valid = data.startswith(b"\xff\xd8\xff")
    else:
        text = data.decode("utf-8")
        valid = "\0" not in text
        if mime == "application/json":

            def reject_constant(value: str) -> Any:
                raise ValueError("JSON requires finite values")

            json.loads(text, parse_constant=reject_constant)
    if not valid:
        raise ValueError("File content does not match its type")
    return mime


@dataclass(frozen=True)
class BrowserStagedFile:
    """An immutable upload description. Pass ``id`` in ``document_ids``."""

    id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    source: str
    computer_id: str
    task_id: str
    browser_context_id: str


@dataclass(frozen=True)
class BrowserDownload:
    """A sealed download awaiting policy approval; contains no usable guest path."""

    id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    computer_id: str
    task_id: str
    browser_context_id: str
    url: str


@dataclass(frozen=True, init=False)
class BrowserFilePolicy:
    """Opt-in remote file rules, bound to exactly one Computer and one toolset.

    Stage through the toolset's ``stage_local_file``, ``stage_guest_file`` or
    ``stage_document`` methods. Document bytes must already have been retrieved
    with the caller's authorization. No URL fetching is performed here.

    Downloads require a Linux guest with a privileged tmpfs quarantine helper.
    ``approve_download`` receives a sealed file's immutable metadata and must
    return exactly True to expose a read-only guest path. Exceptions refuse.
    """

    task_id: str
    guest_upload_roots: tuple[str, ...]
    allowed_mime_types: tuple[str, ...]
    max_file_bytes: int
    max_total_bytes: int
    max_files: int
    downloads: bool
    approve_download: Callable[[BrowserDownload], Any] | None = field(repr=False)
    _computer: Any = field(repr=False, compare=False)
    _computer_id: str = field(repr=False)
    _binding: list[object] = field(repr=False, compare=False)
    _lock: Any = field(repr=False, compare=False)

    def __init__(
        self,
        computer: Any,
        *,
        task_id: str,
        guest_upload_roots: Sequence[str] = (),
        allowed_mime_types: Sequence[str] = ("text/plain",),
        max_file_bytes: int = 1024 * 1024,
        max_total_bytes: int = 4 * 1024 * 1024,
        max_files: int = 8,
        downloads: bool = False,
        approve_download: Callable[[BrowserDownload], Any] | None = None,
    ) -> None:
        if not isinstance(task_id, str) or not 1 <= len(task_id) <= 128:
            raise ValueError("task_id must be a nonempty string of at most 128 characters")
        for value, low, high in (
            (max_file_bytes, 1, 1024 * 1024),
            (max_total_bytes, max_file_bytes, 4 * 1024 * 1024),
            (max_files, 1, 8),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid browser file limit")
        if type(downloads) is not bool or (downloads and not callable(approve_download)):
            raise ValueError("downloads requires an approve_download callback")
        if isinstance(allowed_mime_types, str) or not allowed_mime_types:
            raise ValueError("Configure at least one supported MIME type")
        mime = tuple(allowed_mime_types)
        if any(value not in MIME_EXTENSIONS for value in mime) or len(set(mime)) != len(mime):
            raise ValueError("Unsupported or duplicate MIME type")
        roots: dict[str, tuple[str, ...]] = {}
        for key, given in (("guest_upload_roots", guest_upload_roots),):
            if isinstance(given, str) or len(given) > 16:
                raise ValueError("Use at most 16 canonical upload roots")
            roots[key] = tuple(canonical_path(p) for p in given)
        values = dict(
            task_id=task_id,
            **roots,
            allowed_mime_types=mime,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            max_files=max_files,
            downloads=downloads,
            approve_download=approve_download,
            _computer=computer,
            _computer_id=computer.id,
            _binding=[],
            _lock=threading.Lock(),
        )
        for key, value in values.items():
            object.__setattr__(self, key, value)

    def _bind(self, computer: Any, owner: object) -> None:
        with self._lock:
            if computer is not self._computer or computer.id != self._computer_id or self._binding:
                raise ValueError("A browser file policy belongs to one computer and one toolset")
            self._binding.append(owner)


class _StagedFiles:
    """Synchronous Anthropic policy adapter; never performs I/O from a hook."""

    def __init__(self, policy: BrowserFilePolicy) -> None:
        self.policy = policy
        self.context = ""
        self.closed = False
        self.files: dict[str, tuple[BrowserStagedFile, bytes]] = {}
        self.visible: set[str] = set()
        self.lock = threading.RLock()

    def add(self, name: str, data: bytes, source: str) -> BrowserStagedFile:
        with self.lock:
            if self.closed or not self.context:
                raise ValueError("Browser context is unavailable")
            data = bytes(data)
            if len(self.files) >= self.policy.max_files or len(data) > self.policy.max_file_bytes:
                raise ValueError("Browser file limit exceeded")
            if (
                sum(len(b) for _, b in self.files.values()) + len(data)
                > self.policy.max_total_bytes
            ):
                raise ValueError("Browser file limit exceeded")
            name = safe_filename(name)
            mime = content_type(name, data, self.policy.allowed_mime_types)
            digest = hashlib.sha256(data).hexdigest()
            # Labels make the exact selected name, size and digest visible in
            # the inherited confirmation input. Only exact registry keys work.
            ident = f"mandala-file:{uuid.uuid4().hex}:{name}:{len(data)}:{digest}"
            item = BrowserStagedFile(
                ident,
                name,
                mime,
                len(data),
                digest,
                source,
                self.policy._computer_id,
                self.policy.task_id,
                self.context,
            )
            self.files[ident] = (item, data)
            return item

    def selected(self, ids: Sequence[str]) -> list[tuple[BrowserStagedFile, bytes]]:
        with self.lock:
            if self.closed or not ids or isinstance(ids, str) or len(ids) > self.policy.max_files:
                raise ToolError(FILE_ERROR)
            if any(not isinstance(i, str) or i not in self.files for i in ids) or len(
                set(ids)
            ) != len(ids):
                raise ToolError(FILE_ERROR)
            return [self.files[i] for i in ids]

    def resolve_upload_paths(self, context: Any, paths: Sequence[str]) -> list[str]:
        raise ToolError("Stage guest or local files explicitly and use their document_ids handles.")

    def resolve_upload_documents(self, context: Any, document_ids: Sequence[str]) -> list[str]:
        if not context.tool_use_id:
            raise ToolError(FILE_ERROR)
        self.selected(document_ids)
        return list(document_ids)

    def is_path_visible(self, path: str) -> bool:
        with self.lock:
            return not self.closed and path in self.visible

    def clear(self) -> None:
        with self.lock:
            self.closed = True
            self.files.clear()
            self.visible.clear()
