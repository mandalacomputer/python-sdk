"""Finite, identity-bound projections for volatile background execution reads."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from . import _api
from ._exceptions import MandalaError

__all__ = ["ExecutionMetadata", "ExecutionOutput"]

_DIAGNOSTIC_MAX = 65_536
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


@dataclass(frozen=True)
class ExecutionMetadata:
    """The last observed state of one background execution.

    ``running`` does not establish that the computer is awake. Only ``exited``
    carries an observed signed exit code and end time; ``lost`` establishes no
    outcome. Identity and metadata are volatile and may disappear on cleanup,
    PID replacement, deletion or a daemon restart.
    """

    execution_id: str
    computer_id: str
    pid: int
    status: Literal["running", "exited", "lost"]
    started_at: str
    ended_at: str | None
    exit_code: int | None
    output_source: Literal["volatile_guest_files"]


@dataclass(frozen=True)
class ExecutionOutput:
    """One independent read of existing guest files, as exact bytes.

    Returned offsets are the next byte positions for this reader. False ``more``
    flags mean current EOF, including while a command is running. They do not
    establish completion. The separate wrapper diagnostic repeats on every read
    and advances neither stream. No shared SDK cursor is stored.
    """

    execution_id: str
    stdout: bytes
    stderr: bytes
    stdout_offset: int
    stderr_offset: int
    stdout_more: bool
    stderr_more: bool
    diagnostic: bytes
    diagnostic_truncated: bool


def _invalid(field: str) -> MandalaError:
    # Never echo arbitrary response content into an exception.
    return MandalaError(f"execution response has invalid or mismatched {field}")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise _invalid(field)
    return str.__str__(value)


def _integer(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(field)
    number = int.__index__(value)
    if not minimum <= number <= _api.EXECUTION_MAX_OFFSET:
        raise _invalid(field)
    return number


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise _invalid(field)
    return value


def _time(value: object, field: str) -> str:
    text = _text(value, field)
    if not _TIMESTAMP.fullmatch(text):
        raise _invalid(field)
    try:
        # Python 3.10 only accepts three or six fractional digits. Normalize
        # to six for calendar validation, keeping the original wire spelling.
        validation = re.sub(r"\.([0-9]+)", lambda match: "." + match[1][:6].ljust(6, "0"), text)
        datetime.fromisoformat(validation.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid(field) from None
    return text


def _identity(data: Mapping[str, Any], expected: str) -> str:
    try:
        actual = _api.execution_id(_text(data.get("execution_id"), "execution_id"))
    except ValueError:
        raise _invalid("execution_id") from None
    if actual != expected:
        raise _invalid("execution_id")
    return actual


def decode_metadata(
    data: Mapping[str, Any], *, execution_id: str, computer_id: str
) -> ExecutionMetadata:
    """Validate identity and observation evidence before exposing any state."""
    identity = _identity(data, execution_id)
    computer = _text(data.get("computer_id"), "computer_id")
    if computer != computer_id:
        raise _invalid("computer_id")
    status = _text(data.get("status"), "status")
    if status not in ("running", "exited", "lost"):
        raise _invalid("status")
    if data.get("output_source") != "volatile_guest_files":
        raise _invalid("output_source")
    ended_at, exit_code = None, None
    if status == "exited":
        ended_at = _time(data.get("ended_at"), "ended_at")
        exit_code = _integer(data.get("exit_code"), "exit_code", minimum=-_api.EXECUTION_MAX_OFFSET)
    elif "ended_at" in data or "exit_code" in data:
        raise _invalid("exit evidence")
    return ExecutionMetadata(
        execution_id=identity,
        computer_id=computer,
        pid=_integer(data.get("pid"), "pid", minimum=1),
        status=cast(Literal["running", "exited", "lost"], status),
        started_at=_time(data.get("started_at"), "started_at"),
        ended_at=ended_at,
        exit_code=exit_code,
        output_source="volatile_guest_files",
    )


def _bytes(value: object, field: str, limit: int) -> bytes:
    text = _text(value, field)
    if len(text) > 4 * ((limit + 2) // 3):
        raise _invalid(field)
    try:
        result = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error):
        raise _invalid(field) from None
    # validate=True alone permits nonzero padding bits and extra padding.
    if len(result) > limit or base64.b64encode(result).decode("ascii") != text:
        raise _invalid(field)
    return result


def decode_output(
    data: Mapping[str, Any],
    *,
    execution_id: str,
    stdout_offset: int,
    stderr_offset: int,
    limit: int,
) -> ExecutionOutput:
    """A malformed response never turns into empty output or a new cursor."""
    identity = _identity(data, execution_id)
    stdout = _bytes(data.get("stdout_b64"), "stdout_b64", limit)
    stderr = _bytes(data.get("stderr_b64"), "stderr_b64", limit)
    out_next = _integer(data.get("stdout_offset"), "stdout_offset")
    err_next = _integer(data.get("stderr_offset"), "stderr_offset")
    if out_next != stdout_offset + len(stdout):
        raise _invalid("stdout_offset")
    if err_next != stderr_offset + len(stderr):
        raise _invalid("stderr_offset")
    out_more = _boolean(data.get("stdout_more"), "stdout_more")
    err_more = _boolean(data.get("stderr_more"), "stderr_more")
    if (out_more and len(stdout) != limit) or (err_more and len(stderr) != limit):
        raise _invalid("more evidence")
    return ExecutionOutput(
        execution_id=identity,
        stdout=stdout,
        stderr=stderr,
        stdout_offset=out_next,
        stderr_offset=err_next,
        stdout_more=out_more,
        stderr_more=err_more,
        diagnostic=_bytes(data.get("diagnostic_b64"), "diagnostic_b64", _DIAGNOSTIC_MAX),
        diagnostic_truncated=_boolean(data.get("diagnostic_truncated"), "diagnostic_truncated"),
    )
