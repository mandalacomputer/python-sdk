"""Validation and receipts for Linux resumable uploads."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import IO, Any

from ._exceptions import MandalaError


@dataclass(frozen=True)
class FileUpload:
    path: str
    upload_id: str
    offset: int
    size: int
    complete: bool

    @classmethod
    def from_api(cls, data: Mapping[str, Any], path: str, upload_id: str) -> FileUpload:
        offset, size, complete = data.get("offset"), data.get("size"), data.get("complete")
        if (
            data.get("path") != path
            or data.get("upload_id") != upload_id
            or type(offset) is not int
            or type(size) is not int
            or not 0 <= offset <= size <= 2147483648
            or type(complete) is not bool
            or (complete and offset != size)
        ):
            raise MandalaError(
                "Server returned invalid upload progress; read upload status before retrying"
            )
        return cls(path, upload_id, offset, size, complete)


class UploadInterruptedError(MandalaError):
    """Retain upload_id and the unchanged source to resume, or cancel staging."""

    def __init__(self, upload_id: str, cause: Exception):
        self.upload_id = upload_id
        self.cause = cause
        super().__init__(
            f"Upload {upload_id} interrupted. Resume with this upload_id and the unchanged source, or cancel_file_upload to remove staging. {cause}"
        )


def read_chunk(source: IO[bytes], offset: int, length: int) -> bytes:
    source.seek(offset)
    chunk = bytearray()
    remaining = length
    while remaining:
        data = source.read(remaining)
        if not isinstance(data, bytes) or not data or len(data) > remaining:
            raise ValueError("Source returned a short or invalid chunk; keep the source unchanged")
        chunk.extend(data)
        remaining -= len(data)
    return bytes(chunk)


def attach_upload_id(error: BaseException, identity: str) -> None:
    """Keep cancellation/validation identity, including user-defined exceptions."""
    with suppress(AttributeError, TypeError):
        setattr(error, "upload_id", identity)  # noqa: B010 - BaseException has no typed field


async def read_chunk_async(source: IO[bytes], offset: int, length: int) -> bytes:
    """Do not return cancellation while a worker still owns the source cursor."""
    worker = asyncio.create_task(asyncio.to_thread(read_chunk, source, offset, length))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Python cannot stop a file-reading thread. Drain it before the caller
        # closes/reuses the source; repeated task cancellation must not detach it.
        while not worker.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(worker)
        if not worker.cancelled():
            worker.exception()  # Retrieve a source failure without replacing cancellation.
        raise
