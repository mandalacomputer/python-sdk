"""Short-lived credentials for a computer's managed Chromium CDP socket."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._exceptions import MandalaError


@dataclass(frozen=True)
class BrowserConnection:
    """A ten-minute, revocable CDP capability. Treat ``token`` as a secret.

    Attach with an Authorization Bearer header; never put the token in a URL.
    Expiration also closes an attached socket. Revoke with
    ``computer.revoke_browser_connection(connection.id)``. Neither revocation
    nor expiration stops Chromium or erases its managed profile.
    """

    id: str
    url: str
    token: str = field(repr=False)
    expires_at: datetime

    @classmethod
    def from_api(cls, data: Mapping[str, Any], base_url: str, path: str) -> BrowserConnection:
        message = "invalid browser connection response"
        ident = data.get("id")
        token = data.get("token")
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident):
            raise MandalaError(message)
        if not isinstance(token, str) or not re.fullmatch(r"bcdp_[0-9a-f]{64}", token):
            raise MandalaError(message)
        base = urlsplit(base_url)
        expected = urlunsplit(
            (
                "wss" if base.scheme == "https" else "ws",
                base.netloc,
                f"{base.path.rstrip('/')}/{path}/{ident}/cdp",
                "",
                "",
            )
        )
        # Never forward a capability to a response-selected origin or redirect.
        if data.get("url") != expected:
            raise MandalaError(message)
        try:
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise ValueError()
        except (KeyError, TypeError, ValueError, AttributeError):
            raise MandalaError(message) from None
        return cls(ident, expected, token, expires)


def connection_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("connection_id must be a 32-character lowercase hexadecimal id")
    return value
