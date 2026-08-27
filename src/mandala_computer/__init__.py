"""Python SDK for Mandala Computer — cloud desktops for AI agents.

    from mandala_computer import Client

    client = Client()                                   # MANDALA_API_KEY
    with client.computers.ephemeral(template="base") as c:
        c.wait_for_guest()
        c.open("https://example.com")           # on the screen, not as root
        png = c.screenshot()
        c.click(640, 400)
        c.type("hello")

``AsyncClient`` mirrors it method for method:

    from mandala_computer import AsyncClient

    async with AsyncClient() as client:
        async with client.computers.ephemeral(template="base") as c:
            await c.wait_for_guest()
            png = await c.screenshot()

This binds only to the platform's curated ``/api/v1`` surface, never to the
hypervisor daemon's own routes — see the README for why that boundary exists.
"""

from __future__ import annotations

import httpx

from ._agent import (
    AgentDone,
    AgentEvent,
    AgentFailed,
    AgentResult,
    AgentStep,
    AgentStepEvent,
    AgentText,
    AgentUsage,
)
from ._async_computer import AsyncBackgroundCommand, AsyncComputer
from ._async_resources import (
    AsyncBuilds,
    AsyncComputers,
    AsyncMoves,
    AsyncSizes,
    AsyncSnapshots,
    AsyncTemplates,
    AsyncUsage,
)
from ._client import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, AsyncTransport, Transport
from ._computer import SCREEN_HEIGHT, SCREEN_WIDTH, BackgroundCommand, Computer
from ._exceptions import (
    APIError,
    AuthenticationError,
    ConflictError,
    ConnectionError,
    FileTooLargeError,
    GatewayTimeoutError,
    MandalaError,
    MoveRequiredError,
    NotFoundError,
    OriginResponseError,
    OriginTLSError,
    OriginUnreachableError,
    PermissionDeniedError,
    PlanLimitError,
    RangeNotSatisfiableError,
    RateLimitError,
    TimeoutError,
    UnavailableError,
    is_transient,
)
from ._models import (
    BuildProgress,
    BuildStep,
    ComputerUsage,
    ExecResult,
    ExecStatus,
    FilePart,
    Listing,
    Move,
    PublishedTemplate,
    Retention,
    RetiredTemplates,
    Size,
    Snapshot,
    SnapshotHoldings,
    Template,
    TemplateBuild,
    TemplateCheck,
    UsagePeriod,
    UsageReport,
    UsageTotals,
    VncConnect,
    Window,
    WindowResult,
)
from ._resources import Builds, Computers, Moves, Sizes, Snapshots, Templates, Usage

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_BASE_URL",
    "SCREEN_HEIGHT",
    "SCREEN_WIDTH",
    "APIError",
    "AgentDone",
    "AgentEvent",
    "AgentFailed",
    "AgentResult",
    "AgentStep",
    "AgentStepEvent",
    "AgentText",
    "AgentUsage",
    "AsyncBackgroundCommand",
    "AsyncClient",
    "AsyncComputer",
    "AuthenticationError",
    "BackgroundCommand",
    "BuildProgress",
    "BuildStep",
    "Client",
    "Computer",
    "ComputerUsage",
    "ConflictError",
    "ConnectionError",
    "ExecResult",
    "ExecStatus",
    "FilePart",
    "FileTooLargeError",
    "GatewayTimeoutError",
    "Listing",
    "MandalaError",
    "Move",
    "MoveRequiredError",
    "NotFoundError",
    "OriginResponseError",
    "OriginTLSError",
    "OriginUnreachableError",
    "PermissionDeniedError",
    "PlanLimitError",
    "PublishedTemplate",
    "RangeNotSatisfiableError",
    "RateLimitError",
    "Retention",
    "RetiredTemplates",
    "Size",
    "Snapshot",
    "SnapshotHoldings",
    "Template",
    "TemplateBuild",
    "TemplateCheck",
    "TimeoutError",
    "UnavailableError",
    "UsagePeriod",
    "UsageReport",
    "UsageTotals",
    "VncConnect",
    "Window",
    "WindowResult",
    "__version__",
    "is_transient",
]


class Client:
    """Entry point to the Mandala Computer API.

    :param api_key: defaults to ``MANDALA_API_KEY``.
    :param base_url: defaults to ``MANDALA_BASE_URL``, then the public API.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._t = Transport(api_key, base_url=base_url, timeout=timeout, client=http_client)
        self.builds = Builds(self._t)
        self.computers = Computers(self._t)
        self.moves = Moves(self._t)
        self.snapshots = Snapshots(self._t)
        self.templates = Templates(self._t)
        self.sizes = Sizes(self._t)
        self.usage = Usage(self._t)

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
    """Entry point to the Mandala Computer API, driven with ``await``.

    Same arguments and behaviour as :class:`Client`; every method that performs
    IO is a coroutine.

    :param api_key: defaults to ``MANDALA_API_KEY``.
    :param base_url: defaults to ``MANDALA_BASE_URL``, then the public API.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._t = AsyncTransport(api_key, base_url=base_url, timeout=timeout, client=http_client)
        self.builds = AsyncBuilds(self._t)
        self.computers = AsyncComputers(self._t)
        self.moves = AsyncMoves(self._t)
        self.snapshots = AsyncSnapshots(self._t)
        self.templates = AsyncTemplates(self._t)
        self.sizes = AsyncSizes(self._t)
        self.usage = AsyncUsage(self._t)

    @property
    def base_url(self) -> str:
        return self._t.base_url

    async def aclose(self) -> None:
        await self._t.aclose()

    async def __aenter__(self) -> AsyncClient:  # noqa: PYI034
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
