"""Validation and receipts for Linux resumable uploads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import IO, Any


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
            raise ValueError(
                "Server returned invalid upload progress; read upload status before retrying"
            )
        return cls(path, upload_id, offset, size, complete)


class UploadInterruptedError(Exception):
    """Retain upload_id and the unchanged source to resume, or cancel staging."""

    def __init__(self, upload_id: str, cause: Exception):
        self.upload_id = upload_id
        super().__init__(
            f"Upload {upload_id} interrupted. Resume with this upload_id and the unchanged source, or cancel_file_upload to remove staging. {cause}"
        )


def read_chunk(source: IO[bytes], offset: int, length: int) -> bytes:
    source.seek(offset)
    data = source.read(length)
    if not isinstance(data, bytes) or len(data) != length:
        raise ValueError("Source returned a short or invalid chunk; keep the source unchanged")
    return data
