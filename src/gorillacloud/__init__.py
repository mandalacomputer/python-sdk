"""Python SDK for GorillaCloud — cloud desktops for AI agents.

    from gorillacloud import Client

    client = Client()                                   # GORILLACLOUD_API_KEY
    with client.computers.ephemeral(template="base") as c:
        c.wait_for_guest()
        # desktop=True puts it on the screen; without it exec() runs as root
        c.exec("nohup xdg-open https://example.com >/dev/null 2>&1 &", desktop=True)
        png = c.screenshot()
        c.click(640, 400)
        c.type("hello")

``AsyncClient`` mirrors it method for method:

    from gorillacloud import AsyncClient

    async with AsyncClient() as client:
        async with client.computers.ephemeral(template="base") as c:
            await c.wait_for_guest()
            png = await c.screenshot()

This binds only to the platform's curated ``/api/v1`` surface, never to the
hypervisor daemon's own routes — see the README for why that boundary exists.
"""

from __future__ import annotations

import httpx

from ._async_computer import AsyncComputer
from ._async_resources import AsyncComputers, AsyncSnapshots, AsyncTemplates
from ._client import DEFAULT_BASE_URL, AsyncTransport, Transport
from ._computer import SCREEN_HEIGHT, SCREEN_WIDTH, Computer
from ._exceptions import (
    APIError,
    AuthenticationError,
    ConflictError,
    GorillaCloudError,
    NotFoundError,
    PermissionDeniedError,
    PlanLimitError,
    TimeoutError,
)
from ._models import ExecResult, Snapshot, Template
from ._resources import Computers, Snapshots, Templates

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_BASE_URL",
    "SCREEN_HEIGHT",
    "SCREEN_WIDTH",
    "APIError",
    "AsyncClient",
    "AsyncComputer",
    "AuthenticationError",
    "Client",
    "Computer",
    "ConflictError",
    "ExecResult",
    "GorillaCloudError",
    "NotFoundError",
    "PermissionDeniedError",
    "PlanLimitError",
    "Snapshot",
    "Template",
    "TimeoutError",
    "__version__",
]


class Client:
    """Entry point to the GorillaCloud API.

    :param api_key: defaults to ``GORILLACLOUD_API_KEY``.
    :param base_url: defaults to ``GORILLACLOUD_BASE_URL``, then the public API.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._t = Transport(api_key, base_url=base_url, timeout=timeout, client=http_client)
        self.computers = Computers(self._t)
        self.snapshots = Snapshots(self._t)
        self.templates = Templates(self._t)

    @property
    def base_url(self) -> str:
        return self._t.base_url

    def close(self) -> None:
        self._t.close()

    # typing.Self is 3.11+; the floor here is 3.10, so name the class instead.
    def __enter__(self) -> Client:  # noqa: PYI034
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncClient:
    """Entry point to the GorillaCloud API, driven with ``await``.

    Same arguments and behaviour as :class:`Client`; every method that performs
    IO is a coroutine.

    :param api_key: defaults to ``GORILLACLOUD_API_KEY``.
    :param base_url: defaults to ``GORILLACLOUD_BASE_URL``, then the public API.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._t = AsyncTransport(api_key, base_url=base_url, timeout=timeout, client=http_client)
        self.computers = AsyncComputers(self._t)
        self.snapshots = AsyncSnapshots(self._t)
        self.templates = AsyncTemplates(self._t)

    @property
    def base_url(self) -> str:
        return self._t.base_url

    async def aclose(self) -> None:
        await self._t.aclose()

    async def __aenter__(self) -> AsyncClient:  # noqa: PYI034
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
