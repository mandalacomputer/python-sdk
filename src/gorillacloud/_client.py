"""HTTP transports for the GorillaCloud API.

The sync and async transports differ only in where the awaits go. Everything
that decides *meaning* — key resolution, URL building, and which exception a
status maps to — lives on the shared base, so the two can never disagree about
what a 402 is or where a request goes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

from ._exceptions import (
    APIError,
    AuthenticationError,
    GorillaCloudError,
    NotFoundError,
    PermissionDeniedError,
    PlanLimitError,
)

DEFAULT_BASE_URL = "https://gorillacloud.ai/api/v1"

_STATUS_ERRORS = {
    401: AuthenticationError,
    402: PlanLimitError,
    403: PermissionDeniedError,
    404: NotFoundError,
}


class _BaseTransport:
    """Auth, URL, and error rules — everything about a request except the IO."""

    def __init__(self, api_key: str | None, base_url: str | None) -> None:
        key = api_key or os.environ.get("GORILLACLOUD_API_KEY")
        if not key:
            raise GorillaCloudError(
                "No API key. Pass api_key=... or set GORILLACLOUD_API_KEY "
                "(create one at Settings -> API keys)."
            )
        self.base_url = (
            base_url or os.environ.get("GORILLACLOUD_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _parse(resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    @staticmethod
    def _error(resp: httpx.Response) -> APIError:
        body: Any = None
        message = f"HTTP {resp.status_code}"
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("error"):
                message = str(body["error"])
        except ValueError:
            text = resp.text.strip()
            if text:
                message = text[:500]
        cls = _STATUS_ERRORS.get(resp.status_code, APIError)
        return cls(message, status=resp.status_code, body=body)


class Transport(_BaseTransport):
    """Blocking transport."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(api_key, base_url)
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        resp = self._http.request(
            method, self._url(path), json=json, params=params, headers=self._headers
        )
        if resp.is_success:
            return resp
        raise self._error(resp)

    def json(self, method: str, path: str, **kw: Any) -> Any:
        return self._parse(self.request(method, path, **kw))

    def close(self) -> None:
        if self._owns_client:
            self._http.close()


class AsyncTransport(_BaseTransport):
    """Non-blocking transport."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(api_key, base_url)
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=timeout)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        resp = await self._http.request(
            method, self._url(path), json=json, params=params, headers=self._headers
        )
        if resp.is_success:
            return resp
        raise self._error(resp)

    async def json(self, method: str, path: str, **kw: Any) -> Any:
        return self._parse(await self.request(method, path, **kw))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()
