"""HTTP transport for the GorillaCloud API."""

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


class Transport:
    """Thin wrapper over httpx that applies auth and turns failures into exceptions.

    Kept separate from the resource classes so an async client can reuse the URL,
    auth, and error-mapping rules without duplicating them.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        key = api_key or os.environ.get("GORILLACLOUD_API_KEY")
        if not key:
            raise GorillaCloudError(
                "No API key. Pass api_key=... or set GORILLACLOUD_API_KEY "
                "(create one at Settings -> API keys)."
            )
        self.base_url = (base_url or os.environ.get("GORILLACLOUD_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=timeout)
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        resp = self._http.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            json=json,
            params=params,
            headers=self._headers,
        )
        if resp.is_success:
            return resp
        raise self._error(resp)

    def json(self, method: str, path: str, **kw: Any) -> Any:
        resp = self.request(method, path, **kw)
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

    def close(self) -> None:
        if self._owns_client:
            self._http.close()
