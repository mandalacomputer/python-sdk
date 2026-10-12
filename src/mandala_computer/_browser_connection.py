"""Short-lived credentials for a computer's managed Chromium CDP socket."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import IPv6Address
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._api import (
    BROWSER_DEFAULT_LEASE_SECONDS,
    BROWSER_MAX_SESSION_SECONDS,
    BROWSER_MIN_LEASE_SECONDS,
)
from ._exceptions import MandalaError
from ._timestamps import parse_rfc3339


@dataclass(frozen=True)
class BrowserSessionPolicy:
    """Opt in to version 2 sessions. Auto-renewal applies to browser toolsets only."""

    lease_seconds: int = BROWSER_DEFAULT_LEASE_SECONDS
    max_duration_seconds: int = BROWSER_MAX_SESSION_SECONDS
    auto_renew: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.lease_seconds) is not int
            or type(self.max_duration_seconds) is not int
            or not BROWSER_MIN_LEASE_SECONDS
            <= self.lease_seconds
            <= self.max_duration_seconds
            <= BROWSER_MAX_SESSION_SECONDS
            or type(self.auto_renew) is not bool
        ):
            raise ValueError(
                "browser lease/max duration must be integers with 60 <= lease <= maximum <= 7200; auto_renew must be boolean"
            )

    def to_api(self) -> dict[str, int]:
        return {
            "lifecycle_version": 2,
            "lease_seconds": self.lease_seconds,
            "max_duration_seconds": self.max_duration_seconds,
        }


#: How long releasing a grant this SDK refused to hand over may take. The
#: caller is already getting an error; the server's own deadline covers the
#: grant if this gives up.
CLEANUP_REVOKE_TIMEOUT = 10.0


def _timestamp(value: object) -> datetime:
    """A platform timestamp: up to nine fractional digits, ``Z`` or an offset."""
    return parse_rfc3339(value)


def _date(data: Mapping[str, Any], field_name: str) -> datetime:
    try:
        return _timestamp(data.get(field_name))
    except ValueError:
        raise MandalaError("invalid browser session lease response") from None


def _socket_origin(base_url: str, *, canonical_ipv6: bool = True) -> str:
    """The WebSocket origin the platform names for this API base URL.

    The platform serializes its origin the way a browser does: lowercase host,
    no default port, compressed IPv6. A differently spelled base URL must not
    make every grant look as if it pointed somewhere else. The previous IPv6
    spelling is retained only to recognize responses from older servers.
    """
    base = urlsplit(base_url)
    secure = base.scheme.lower() == "https"
    host = base.hostname or ""
    if ":" in host:
        # Scoped literals are not browser URL hosts; keep their existing exact
        # spelling rather than change transport-specific scope handling here.
        if canonical_ipv6 and "%" not in host:
            address = IPv6Address(host)
            mapped = address.ipv4_mapped
            # Browser URLs use hex tails even when ipaddress spells them as IPv4.
            host = (
                f"::ffff:{int(mapped) >> 16:x}:{int(mapped) & 0xFFFF:x}"
                if mapped is not None
                else address.compressed
            )
        host = f"[{host}]"
    port = base.port
    if port is not None and port != (443 if secure else 80):
        host = f"{host}:{port}"
    return urlunsplit(("wss" if secure else "ws", host, "", "", ""))


@dataclass(frozen=True)
class BrowserSessionLease:
    """Server deadlines for one connection; renewal does not change its identity."""

    id: str
    server_time: datetime
    attach_expires_at: datetime
    lease_expires_at: datetime
    absolute_expires_at: datetime
    lease_seconds: int
    idle_timeout_seconds: int = 0

    @classmethod
    def from_api(cls, data: Mapping[str, Any], ident: str) -> BrowserSessionLease:
        if (
            data.get("id") != ident
            or data.get("lifecycle_version") != 2
            or type(data.get("lifecycle_version")) is not int
            or type(data.get("lease_seconds")) is not int
            or not BROWSER_MIN_LEASE_SECONDS <= data["lease_seconds"] <= BROWSER_MAX_SESSION_SECONDS
            or type(data.get("idle_timeout_seconds")) is not int
            or data["idle_timeout_seconds"] != 0
        ):
            raise MandalaError("invalid browser session lease response")
        result = cls(
            ident,
            _date(data, "server_time"),
            _date(data, "attach_expires_at"),
            _date(data, "lease_expires_at"),
            _date(data, "absolute_expires_at"),
            data["lease_seconds"],
        )
        if not (
            result.attach_expires_at <= result.lease_expires_at <= result.absolute_expires_at
            and result.server_time < result.lease_expires_at
            and (result.absolute_expires_at - result.server_time).total_seconds()
            <= BROWSER_MAX_SESSION_SECONDS
            and (result.lease_expires_at - result.server_time).total_seconds()
            <= result.lease_seconds
        ):
            raise MandalaError("invalid browser session lease response")
        return result


@dataclass(frozen=True)
class BrowserConnection:
    """A revocable CDP capability. Treat ``token`` as a secret.

    Attach with an Authorization Bearer header; never put the token in a URL.
    Legacy expiration closes the socket. With a session policy, expires_at is
    only the attachment deadline; lease contains the active/absolute deadlines.
    Revoke with
    ``computer.revoke_browser_connection(connection.id)``. Neither revocation
    nor expiration stops Chromium or erases its managed profile.
    """

    id: str
    url: str
    token: str = field(repr=False)
    expires_at: datetime
    lease: BrowserSessionLease | None = None

    @classmethod
    def from_api(
        cls,
        data: Mapping[str, Any],
        base_url: str,
        path: str,
        session_policy: BrowserSessionPolicy | None = None,
    ) -> BrowserConnection:
        message = "invalid browser connection response"
        ident = data.get("id")
        token = data.get("token")
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise MandalaError(message)
        if not isinstance(token, str) or not re.fullmatch(r"bcdp_[0-9a-f]{64}", token):
            raise MandalaError(message)
        try:
            origin = _socket_origin(base_url)
            previous_origin = _socket_origin(base_url, canonical_ipv6=False)
        except ValueError:
            raise MandalaError(message) from None
        suffix = f"{urlsplit(base_url).path.rstrip('/')}/{path}/{ident}/cdp"
        expected = origin + suffix
        # Never forward a capability to a response-selected origin or redirect.
        # Both accepted spellings come only from the caller's configured base;
        # even an older server's response is returned as the canonical URL.
        if data.get("url") not in (expected, previous_origin + suffix):
            raise MandalaError(message)
        try:
            expires = _timestamp(data.get("expires_at"))
        except ValueError:
            raise MandalaError(message) from None
        lease = None
        if "lifecycle_version" in data or session_policy is not None:
            lease = BrowserSessionLease.from_api(data, ident)
            if lease.attach_expires_at != expires or (
                session_policy is not None
                and (
                    lease.lease_seconds != session_policy.lease_seconds
                    or (lease.absolute_expires_at - lease.server_time).total_seconds()
                    > session_policy.max_duration_seconds
                )
            ):
                raise MandalaError(message)
        return cls(ident, expected, token, expires, lease)


def connection_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("connection_id must be a 32-character lowercase hexadecimal id")
    return value
