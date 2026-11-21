"""Validation and receipts for Linux resumable uploads."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import IO, Any

from ._exceptions import APIError, MandalaError, RateLimitError

CHUNK_BUSY_WAITS = 12
"""How many busy refusals in a row (chunk 429s and busy status reads)
:meth:`Computer.upload_file` waits out before raising."""

CHUNK_BUSY_DEFAULT_WAIT = 5.0
"""Seconds to wait after a chunk 429 that carries no usable ``Retry-After``."""

CHUNK_BUSY_MAX_WAIT = 30.0
"""The longest single wait after a chunk 429, whatever ``Retry-After`` says."""


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


def chunk_busy_wait(error: RateLimitError, waits: int) -> float | None:
    """Seconds to wait out a 429 on a chunk, or None once ``waits`` are spent.

    The platform refuses a chunk with 429 while another chunk for the same
    computer, or too many for the account, are still in flight. Nothing was
    staged, so the remedy is to wait ``Retry-After``, read status and resend.
    """
    if waits >= CHUNK_BUSY_WAITS:
        return None
    delay = error.retry_after
    if delay is None or not math.isfinite(delay) or delay < 0:
        delay = CHUNK_BUSY_DEFAULT_WAIT
    return min(delay, CHUNK_BUSY_MAX_WAIT)


def status_busy_wait(error: APIError, waits: int) -> float | None:
    """Seconds to wait out a busy upload status read, or None to raise ``error``.

    While another chunk for the same computer is being written, the status
    read can be refused with 429, or with 409 ``contention`` because that
    chunk holds the computer's file channel. Both clear by waiting and share
    the chunk budget. Every other error answers None and is raised as is.
    """
    if isinstance(error, RateLimitError):
        return chunk_busy_wait(error, waits)
    if error.status == 409 and error.reason == "contention" and waits < CHUNK_BUSY_WAITS:
        return CHUNK_BUSY_DEFAULT_WAIT
    return None


def resumed_offset(progress: FileUpload, size: int) -> int:
    """The offset to resend from after a wait, checked against the source size."""
    if progress.size != size:
        raise MandalaError(
            "Upload status no longer matches the source size; read upload status before retrying"
        )
    return progress.offset


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
