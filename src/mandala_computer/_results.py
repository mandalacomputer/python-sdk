"""Finite immutable retained results; metadata never contains command output."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict, cast

from . import _api
from ._exceptions import MandalaError

__all__ = [
    "BackgroundResult",
    "ResultDiagnostic",
    "ResultObservation",
    "ResultOutput",
    "ResultPrefix",
    "ResultStream",
    "RetainOutputOptions",
    "RetainedResult",
    "SynchronousResult",
    "SynchronousResultPrefix",
]


class RetainOutputOptions(TypedDict, total=False):
    max_bytes_per_stream: int
    retention_seconds: int


@dataclass(frozen=True)
class ResultObservation:
    status: Literal["running", "exited"]
    observed_at: str
    exit_code: int | None


@dataclass(frozen=True)
class ResultPrefix:
    bytes: int
    sha256: str
    source_offset: Literal[0]
    next_source_offset: int
    end_reason: Literal["observed_eof", "byte_limit"]


@dataclass(frozen=True)
class SynchronousResultPrefix:
    bytes: int
    sha256: str
    source_offset: Literal[0]
    next_source_offset: int
    source_response_bytes: int
    end_reason: Literal["response_end", "byte_limit"]
    upstream_truncated: bool


@dataclass(frozen=True)
class ResultDiagnostic:
    bytes: int
    sha256: str
    source: Literal["wrapper"]
    diagnostic_truncated: bool


@dataclass(frozen=True)
class _ResultFields:
    version: Literal[1]
    result_id: str
    state: Literal["ready"]
    account_id: str
    computer_id: str
    workspace_id: str | None
    capture_started_at: str
    captured_at: str
    expires_at: str
    execution_observation: ResultObservation


@dataclass(frozen=True)
class BackgroundResult(_ResultFields):
    kind: Literal["background-output"]
    execution_id: str
    source: Literal["volatile_guest_files"]
    stdout: ResultPrefix
    stderr: ResultPrefix
    diagnostic: ResultDiagnostic


@dataclass(frozen=True)
class SynchronousResult(_ResultFields):
    kind: Literal["synchronous-output"]
    execution_id: None
    source: Literal["exec_response"]
    stdout: SynchronousResultPrefix
    stderr: SynchronousResultPrefix
    diagnostic: None


RetainedResult = BackgroundResult | SynchronousResult
ResultStream = Literal["stdout", "stderr", "diagnostic"]


@dataclass(frozen=True)
class ResultOutput:
    """One immutable byte page; EOF does not establish task completion."""

    result_id: str
    stream: ResultStream
    offset: int
    next_offset: int
    eof: bool
    data: bytes


def invalid(field: str) -> MandalaError:
    return MandalaError(f"retained response has invalid or mismatched {field}")


def record(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise invalid(field)
    return value


def text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise invalid(field)
    return str.__str__(value)


def integer(value: object, field: str, low: int = 0, high: int = 9_007_199_254_740_991) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid(field)
    number = int.__index__(value)
    if not low <= number <= high:
        raise invalid(field)
    return number


def boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise invalid(field)
    return value


def scope(value: object, field: str) -> str:
    result = text(value, field)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", result):
        raise invalid(field)
    return result


def digest(value: object) -> str:
    result = text(value, "sha256")
    if not re.fullmatch(r"[a-f0-9]{64}", result):
        raise invalid("sha256")
    return result


def timestamp(value: object, field: str) -> tuple[str, int]:
    original = text(value, field)
    match = re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]{1,9}))?Z", original
    )
    if not match:
        raise invalid(field)
    try:
        # Calendar validation uses whole seconds on Python 3.10 too. Keep the
        # original fraction and use integer nanoseconds for ordering.
        when = datetime.fromisoformat(match[1])
    except ValueError:
        raise invalid(field) from None
    seconds = when.toordinal() * 86400 + when.hour * 3600 + when.minute * 60 + when.second
    return original, seconds * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def identity(value: object, expected: str | None, family: str) -> str:
    result = text(value, family)
    prefix = {"result_id": "res", "artifact_id": "art", "execution_id": "exec"}[family]
    if not re.fullmatch(prefix + r"_[a-f0-9]{32}", result) or (
        expected is not None and result != expected
    ):
        raise invalid(family)
    return result


def _prefix(value: object, synchronous: bool) -> ResultPrefix | SynchronousResultPrefix:
    p = record(value, "prefix")
    count = integer(p.get("bytes"), "bytes", high=_api.RESULT_STREAM_MAX)
    sha = digest(p.get("sha256"))
    if (
        integer(p.get("source_offset"), "source_offset") != 0
        or integer(p.get("next_source_offset"), "next_source_offset") != count
    ):
        raise invalid("prefix offsets")
    if synchronous:
        total = integer(
            p.get("source_response_bytes"), "source_response_bytes", count, 16 * 1024 * 1024
        )
        end = "response_end" if count == total else "byte_limit"
        if p.get("end_reason") != end:
            raise invalid("end_reason")
        return SynchronousResultPrefix(
            count,
            sha,
            0,
            count,
            total,
            cast(Literal["response_end", "byte_limit"], end),
            boolean(p.get("upstream_truncated"), "upstream_truncated"),
        )
    if p.get("end_reason") not in ("observed_eof", "byte_limit"):
        raise invalid("end_reason")
    return ResultPrefix(count, sha, 0, count, p["end_reason"])


def decode_result(
    data: Mapping[str, Any],
    *,
    computer_id: str,
    result_id: str | None = None,
    execution_id: str | None = None,
) -> RetainedResult:
    rid = identity(data.get("result_id"), result_id, "result_id")
    if integer(data.get("version"), "version") != 1 or data.get("state") != "ready":
        raise invalid("version or state")
    kind = data.get("kind")
    if kind not in ("background-output", "synchronous-output"):
        raise invalid("kind")
    synchronous = kind == "synchronous-output"
    account = scope(data.get("account_id"), "account_id")
    computer = scope(data.get("computer_id"), "computer_id")
    if computer != computer_id:
        raise invalid("computer_id")
    workspace = (
        None
        if data.get("workspace_id") is None and "workspace_id" in data
        else scope(data.get("workspace_id"), "workspace_id")
    )
    start, start_ns = timestamp(data.get("capture_started_at"), "capture_started_at")
    captured, captured_ns = timestamp(data.get("captured_at"), "captured_at")
    expires, expires_ns = timestamp(data.get("expires_at"), "expires_at")
    if (
        not start_ns <= captured_ns < expires_ns
        or expires_ns - start_ns > _api.RESULT_RETENTION_MAX * 1_000_000_000
    ):
        raise invalid("capture chronology")
    observed = record(data.get("execution_observation"), "execution_observation")
    observed_at, observed_ns = timestamp(observed.get("observed_at"), "observed_at")
    if not start_ns <= observed_ns <= captured_ns:
        raise invalid("observation chronology")
    status = observed.get("status")
    if status == "exited":
        code = integer(observed.get("exit_code"), "exit_code", -2147483648, 2147483647)
    elif status == "running" and "exit_code" not in observed and not synchronous:
        code = None
    else:
        raise invalid("execution observation")
    observation = ResultObservation(status, observed_at, code)
    common = {
        "version": 1,
        "result_id": rid,
        "state": "ready",
        "account_id": account,
        "computer_id": computer,
        "workspace_id": workspace,
        "capture_started_at": start,
        "captured_at": captured,
        "expires_at": expires,
        "execution_observation": observation,
    }
    stdout, stderr = (
        _prefix(data.get("stdout"), synchronous),
        _prefix(data.get("stderr"), synchronous),
    )
    if synchronous:
        if (
            data.get("source") != "exec_response"
            or "execution_id" not in data
            or data["execution_id"] is not None
            or "diagnostic" not in data
            or data["diagnostic"] is not None
            or execution_id is not None
        ):
            raise invalid("synchronous evidence")
        return SynchronousResult(
            **cast(Any, common),
            kind="synchronous-output",
            execution_id=None,
            source="exec_response",
            stdout=cast(SynchronousResultPrefix, stdout),
            stderr=cast(SynchronousResultPrefix, stderr),
            diagnostic=None,
        )
    execution = identity(data.get("execution_id"), execution_id, "execution_id")
    if data.get("source") != "volatile_guest_files":
        raise invalid("source")
    d = record(data.get("diagnostic"), "diagnostic")
    if d.get("source") != "wrapper":
        raise invalid("diagnostic source")
    diagnostic = ResultDiagnostic(
        integer(d.get("bytes"), "diagnostic bytes", high=65536),
        digest(d.get("sha256")),
        "wrapper",
        boolean(d.get("diagnostic_truncated"), "diagnostic_truncated"),
    )
    return BackgroundResult(
        **cast(Any, common),
        kind="background-output",
        execution_id=execution,
        source="volatile_guest_files",
        stdout=cast(ResultPrefix, stdout),
        stderr=cast(ResultPrefix, stderr),
        diagnostic=diagnostic,
    )


def decode_result_output(
    data: bytes, headers: Mapping[str, str], *, result_id: str, stream: str, offset: int, limit: int
) -> ResultOutput:
    def number(name: str) -> int:
        value = headers.get(name, "")
        if not re.fullmatch(r"[0-9]{1,16}", value):
            raise invalid(name)
        return integer(int(value), name)

    start = number("x-result-offset")
    end = number("x-result-next-offset")
    eof = headers.get("x-result-eof")
    if (
        start != offset
        or end != offset + len(data)
        or len(data) > limit
        or eof not in ("true", "false")
        or (not data and eof != "true")
    ):
        raise invalid("output offsets or EOF")
    return ResultOutput(result_id, cast(ResultStream, stream), offset, end, eof == "true", data)
