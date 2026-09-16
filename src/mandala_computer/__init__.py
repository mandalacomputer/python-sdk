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

from collections.abc import Mapping

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
from ._artifacts import Artifact, ArtifactAssociation
from ._async_computer import AsyncBackgroundCommand, AsyncComputer
from ._async_resources import (
    AsyncAccount,
    AsyncBuilds,
    AsyncComputers,
    AsyncMoves,
    AsyncSizes,
    AsyncSnapshots,
    AsyncTemplates,
    AsyncUsage,
    AsyncWebhooks,
)
from ._client import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, AsyncTransport, Transport
from ._computer import SCREEN_HEIGHT, SCREEN_WIDTH, BackgroundCommand, Computer
from ._events import (
    CHANNEL_EVENT_TYPES,
    DESKTOP_EVENT_TYPES,
    GUEST_EVENT_TYPES,
    STREAM_FRAME_TYPES,
    WATCH_EVENT_TYPE,
    AsyncEventStream,
    ComputerEvent,
    EventStream,
    Hello,
    WatchedTree,
)
from ._exceptions import (
    APIError,
    AuthenticationError,
    ConflictError,
    ConnectionError,
    ConnectionInterruptedError,
    FileTooLargeError,
    GatewayTimeoutError,
    MandalaError,
    MethodNotAllowedError,
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
from ._executions import ExecutionMetadata, ExecutionOutput
from ._models import (
    AccountCapabilities,
    AccountCompleteness,
    AccountLimits,
    AccountPerComputer,
    AccountPlan,
    AccountQuota,
    AccountRemaining,
    AccountUsage,
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
    Webhook,
    WebhookCreated,
    WebhookDelivery,
    Window,
    WindowResult,
)
from ._resources import (
    Account,
    Builds,
    Computers,
    Moves,
    Sizes,
    Snapshots,
    Templates,
    Usage,
    Webhooks,
)
from ._results import (
    BackgroundResult,
    ResultDiagnostic,
    ResultObservation,
    ResultOutput,
    ResultPrefix,
    ResultStream,
    RetainedResult,
    RetainOutputOptions,
    SynchronousResult,
    SynchronousResultPrefix,
)
from ._webhooks import REPLAY_WINDOW_S, verify

__version__ = "0.4.0"

__all__ = [
    "CHANNEL_EVENT_TYPES",
    "DEFAULT_BASE_URL",
    "DESKTOP_EVENT_TYPES",
    "GUEST_EVENT_TYPES",
    "REPLAY_WINDOW_S",
    "SCREEN_HEIGHT",
    "SCREEN_WIDTH",
    "STREAM_FRAME_TYPES",
    "WATCH_EVENT_TYPE",
    "APIError",
    "Account",
    "AccountCapabilities",
    "AccountCompleteness",
    "AccountLimits",
    "AccountPerComputer",
    "AccountPlan",
    "AccountQuota",
    "AccountRemaining",
    "AccountUsage",
    "AgentDone",
    "AgentEvent",
    "AgentFailed",
    "AgentResult",
    "AgentStep",
    "AgentStepEvent",
    "AgentText",
    "AgentUsage",
    "Artifact",
    "ArtifactAssociation",
    "AsyncAccount",
    "AsyncBackgroundCommand",
    "AsyncClient",
    "AsyncComputer",
    "AsyncEventStream",
    "AuthenticationError",
    "BackgroundCommand",
    "BackgroundResult",
    "BuildProgress",
    "BuildStep",
    "Client",
    "Computer",
    "ComputerEvent",
    "ComputerUsage",
    "ConflictError",
    "ConnectionError",
    "ConnectionInterruptedError",
    "EventStream",
    "ExecResult",
    "ExecStatus",
    "ExecutionMetadata",
    "ExecutionOutput",
    "FilePart",
    "FileTooLargeError",
    "GatewayTimeoutError",
    "Hello",
    "Listing",
    "MandalaError",
    "MethodNotAllowedError",
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
    "ResultDiagnostic",
    "ResultObservation",
    "ResultOutput",
    "ResultPrefix",
    "ResultStream",
    "RetainOutputOptions",
    "RetainedResult",
    "Retention",
    "RetiredTemplates",
    "Size",
    "Snapshot",
    "SnapshotHoldings",
    "SynchronousResult",
    "SynchronousResultPrefix",
    "Template",
    "TemplateBuild",
    "TemplateCheck",
    "TimeoutError",
    "UnavailableError",
    "UsagePeriod",
    "UsageReport",
    "UsageTotals",
    "VncConnect",
    "WatchedTree",
    "Webhook",
    "WebhookCreated",
    "WebhookDelivery",
    "Window",
    "WindowResult",
    "__version__",
    "is_transient",
    "verify",
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
        retries: Mapping[str, int] | None = None,
    ) -> None:
        self._t = Transport(
            api_key, base_url=base_url, timeout=timeout, client=http_client, retries=retries
        )
        self.account = Account(self._t)
        self.builds = Builds(self._t)
        self.computers = Computers(self._t)
        self.moves = Moves(self._t)
        self.snapshots = Snapshots(self._t)
        self.templates = Templates(self._t)
        self.sizes = Sizes(self._t)
        self.usage = Usage(self._t)
        self.webhooks = Webhooks(self._t)

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
        retries: Mapping[str, int] | None = None,
    ) -> None:
        self._t = AsyncTransport(
            api_key, base_url=base_url, timeout=timeout, client=http_client, retries=retries
        )
        self.account = AsyncAccount(self._t)
        self.builds = AsyncBuilds(self._t)
        self.computers = AsyncComputers(self._t)
        self.moves = AsyncMoves(self._t)
        self.snapshots = AsyncSnapshots(self._t)
        self.templates = AsyncTemplates(self._t)
        self.sizes = AsyncSizes(self._t)
        self.usage = AsyncUsage(self._t)
        self.webhooks = AsyncWebhooks(self._t)

    @property
    def base_url(self) -> str:
        return self._t.base_url

    async def aclose(self) -> None:
        await self._t.aclose()

    async def __aenter__(self) -> AsyncClient:  # noqa: PYI034
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
