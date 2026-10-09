"""Anthropic browser toolsets backed by the platform's authenticated CDP endpoint."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar, cast

from anthropic.tools import ToolError
from anthropic.tools.browser import (
    BetaAbstractBrowserToolset20260801,
    BetaAsyncAbstractBrowserToolset20260801,
    BetaAsyncConfirmCallable,
    BetaAsyncURLPolicy,
    BetaBrowserNavigateResult,
    BetaBrowserState,
    BetaConfirmCallable,
    BetaScreenshotResult,
    BetaToolConfigs,
    BetaToolsetCallContext,
    BetaURLContext,
    BetaURLPolicy,
)
from anthropic.types import beta
from pydantic import ValidationError

from ._async_computer import AsyncComputer
from ._browser_cdp import BrowserCDP, BrowserError
from ._computer import Computer

T = TypeVar("T")


async def _perform(backend: BrowserCDP, name: str, input: Any) -> Any:
    try:
        try:
            data = (
                type(input)
                .model_validate(input.model_dump(by_alias=True, exclude_none=True), strict=True)
                .model_dump(by_alias=True, exclude_none=True)
            )
        except ValidationError:
            raise BrowserError(
                "Invalid browser action input. Check the required fields and their types."
            ) from None
        result = await asyncio.wait_for(backend.perform(name, data), 45)
        if name == "navigate":
            return BetaBrowserNavigateResult(**result)
        if name in ("screenshot", "zoom"):
            return BetaScreenshotResult(**result)
        return result
    except BrowserError as error:
        raise ToolError(str(error)) from None
    except (asyncio.CancelledError, KeyboardInterrupt):
        await backend.close()
        raise
    except Exception:  # noqa: BLE001 - never expose backend credentials in exceptions
        try:
            await backend.close()
        except Exception:  # noqa: BLE001, S110 - never expose backend credentials in exceptions
            pass
        raise ToolError(
            "Browser action failed and the session was closed. Create a new toolset to continue."
        ) from None


class _Loop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="mandala-browser", daemon=True
        )
        self.thread.start()

    def run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()


class MandalaBrowserToolset(BetaAbstractBrowserToolset20260801):
    """An isolated ten-minute browser session on a running Mandala computer.

    HTTP(S) navigations and intercepted requests use ``url_policy``. It is
    not network isolation: configure guest egress controls for that. Popups,
    workers, cross-process frames, uploads and downloads are unsupported.
    JavaScript execution is off by default; enabling it requires ``confirm``.
    Closing disposes this context and revokes its grant, not the computer.
    Sync callbacks may run on a worker thread.
    """

    def __init__(
        self,
        computer: Computer,
        *,
        configs: beta.BetaBrowserToolsetConfigsParam | None = None,
        confirm: BetaConfirmCallable | None = None,
        url_policy: BetaURLPolicy | None = None,
        tool_configs: BetaToolConfigs | None = None,
    ) -> None:
        options: dict[str, Any] = {
            "configs": configs,
            "confirm": confirm,
            "tool_configs": tool_configs,
        }
        if url_policy is not None:
            options["url_policy"] = url_policy
        super().__init__(**options)

        async def policy(tab: str | None, url: str) -> None:
            if url_policy is not None:
                context = BetaURLContext(tab_id=tab)
                callback: Any = url_policy
                returned = await asyncio.to_thread(callback, context, url)
                if inspect.isawaitable(returned):
                    returned = await returned
                if returned is not None:
                    raise BrowserError(
                        "URL policy must allow with no return value or refuse by throwing."
                    )

        async def create() -> Any:
            return await asyncio.to_thread(computer.create_browser_connection)

        async def revoke(ident: str) -> None:
            await asyncio.to_thread(computer.revoke_browser_connection, ident)

        self._revoke_connection = computer.revoke_browser_connection
        self._backend = BrowserCDP(create, revoke, policy)
        self._worker: _Loop | None = None

    def _run(self, coroutine: Coroutine[Any, Any, T]) -> T:
        if self._worker is None:
            self._worker = _Loop()
        return self._worker.run(coroutine)

    def _browser_state(self, context: BetaToolsetCallContext) -> BetaBrowserState:
        async def state() -> BetaBrowserState:
            return BetaBrowserState(**self._backend.state())

        return self._run(state())

    def close(self) -> None:
        super().close()
        if self._worker is None:
            # A prior close already released the worker/socket. Retry only the
            # failed HTTP revocation, without reviving that event loop.
            if self._backend.grant is not None:
                try:
                    self._revoke_connection(self._backend.grant.id)
                except Exception:  # noqa: BLE001 - never expose backend credentials
                    raise ToolError(
                        "Browser disconnected, but its grant could not be revoked; it expires within ten minutes."
                    ) from None
                self._backend.grant = None
            return
        try:
            self._run(self._backend.close())
        except Exception:  # noqa: BLE001 - never expose backend credentials in exceptions
            raise ToolError(
                "Browser disconnected, but its grant could not be revoked; it expires within ten minutes."
            ) from None
        finally:
            self._worker.close()
            self._worker = None

    def navigate(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserNavigateInput
    ) -> BetaBrowserNavigateResult:
        return cast(
            BetaBrowserNavigateResult, self._run(_perform(self._backend, "navigate", input))
        )

    def screenshot(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserScreenshotInput
    ) -> BetaScreenshotResult:
        return cast(BetaScreenshotResult, self._run(_perform(self._backend, "screenshot", input)))

    def zoom(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserZoomInput
    ) -> BetaScreenshotResult:
        return cast(BetaScreenshotResult, self._run(_perform(self._backend, "zoom", input)))

    def left_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserLeftClickInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "left_click", input)))

    def right_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserRightClickInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "right_click", input)))

    def middle_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserMiddleClickInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "middle_click", input)))

    def double_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserDoubleClickInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "double_click", input)))

    def triple_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserTripleClickInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "triple_click", input)))

    def hover(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserHoverInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "hover", input)))

    def left_click_drag(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserLeftClickDragInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "left_click_drag", input)))

    def left_mouse_down(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserLeftMouseDownInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "left_mouse_down", input)))

    def left_mouse_up(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserLeftMouseUpInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "left_mouse_up", input)))

    def mouse_move(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserMouseMoveInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "mouse_move", input)))

    def scroll(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserScrollInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "scroll", input)))

    def scroll_to(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserScrollToInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "scroll_to", input)))

    def type(self, context: BetaToolsetCallContext, input: beta.BetaBrowserTypeInput) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "type", input)))

    def key(self, context: BetaToolsetCallContext, input: beta.BetaBrowserKeyInput) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "key", input)))

    def hold_key(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserHoldKeyInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "hold_key", input)))

    def form_input(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserFormInputInput
    ) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "form_input", input)))

    def read_page(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserReadPageInput
    ) -> str:
        return cast(str, self._run(_perform(self._backend, "read_page", input)))

    def find(self, context: BetaToolsetCallContext, input: beta.BetaBrowserFindInput) -> str:
        return cast(str, self._run(_perform(self._backend, "find", input)))

    def get_page_text(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserGetPageTextInput
    ) -> str:
        return cast(str, self._run(_perform(self._backend, "get_page_text", input)))

    def wait(self, context: BetaToolsetCallContext, input: beta.BetaBrowserWaitInput) -> str | None:
        return cast(str | None, self._run(_perform(self._backend, "wait", input)))

    def read_console(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserReadConsoleInput
    ) -> str:
        return cast(str, self._run(_perform(self._backend, "read_console", input)))

    def read_network(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserReadNetworkInput
    ) -> str:
        return cast(str, self._run(_perform(self._backend, "read_network", input)))

    def javascript_exec(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserJavascriptExecInput
    ) -> str:
        return cast(str, self._run(_perform(self._backend, "javascript_exec", input)))

    def new_tab(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserNewTabInput
    ) -> beta.BetaBrowserStateTabEntryParam:
        return cast(
            beta.BetaBrowserStateTabEntryParam, self._run(_perform(self._backend, "new_tab", input))
        )

    def list_tabs(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserListTabsInput
    ) -> list[beta.BetaBrowserStateTabEntryParam]:
        return cast(
            list[beta.BetaBrowserStateTabEntryParam],
            self._run(_perform(self._backend, "list_tabs", input)),
        )

    def switch_tab(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserSwitchTabInput
    ) -> beta.BetaBrowserStateTabEntryParam:
        return cast(
            beta.BetaBrowserStateTabEntryParam,
            self._run(_perform(self._backend, "switch_tab", input)),
        )

    def close_tab(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserCloseTabInput
    ) -> None:
        return cast(None, self._run(_perform(self._backend, "close_tab", input)))


class AsyncMandalaBrowserToolset(BetaAsyncAbstractBrowserToolset20260801):
    """An isolated ten-minute browser session on a running Mandala computer.

    HTTP(S) navigations and intercepted requests use ``url_policy``. It is
    not network isolation: configure guest egress controls for that. Popups,
    workers, cross-process frames, uploads and downloads are unsupported.
    JavaScript execution is off by default; enabling it requires ``confirm``.
    Closing disposes this context and revokes its grant, not the computer.
    """

    def __init__(
        self,
        computer: AsyncComputer,
        *,
        configs: beta.BetaBrowserToolsetConfigsParam | None = None,
        confirm: BetaAsyncConfirmCallable | None = None,
        url_policy: BetaAsyncURLPolicy | None = None,
        tool_configs: BetaToolConfigs | None = None,
    ) -> None:
        async def apply_policy(context: BetaURLContext, url: str) -> None:
            if url_policy is not None:
                callback: Any = url_policy
                returned = await asyncio.to_thread(callback, context, url)
                if inspect.isawaitable(returned):
                    returned = await returned
                if returned is not None:
                    raise BrowserError(
                        "URL policy must allow with no return value or refuse by throwing."
                    )

        options: dict[str, Any] = {
            "configs": configs,
            "confirm": confirm,
            "tool_configs": tool_configs,
        }
        if url_policy is not None:
            options["url_policy"] = apply_policy
        super().__init__(**options)

        async def policy(tab: str | None, url: str) -> None:
            await apply_policy(BetaURLContext(tab_id=tab), url)

        self._backend = BrowserCDP(
            computer.create_browser_connection, computer.revoke_browser_connection, policy
        )

    async def _browser_state(self, context: BetaToolsetCallContext) -> BetaBrowserState:
        return BetaBrowserState(**self._backend.state())

    async def close(self) -> None:
        try:
            try:
                await super().close()
            finally:
                await self._backend.close()
        except Exception:  # noqa: BLE001 - never expose backend credentials in exceptions
            raise ToolError(
                "Browser disconnected, but its grant could not be revoked; it expires within ten minutes."
            ) from None

    async def navigate(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserNavigateInput
    ) -> BetaBrowserNavigateResult:
        return cast(BetaBrowserNavigateResult, await _perform(self._backend, "navigate", input))

    async def screenshot(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserScreenshotInput
    ) -> BetaScreenshotResult:
        return cast(BetaScreenshotResult, await _perform(self._backend, "screenshot", input))

    async def zoom(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserZoomInput
    ) -> BetaScreenshotResult:
        return cast(BetaScreenshotResult, await _perform(self._backend, "zoom", input))

    async def left_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserLeftClickInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "left_click", input))

    async def right_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserRightClickInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "right_click", input))

    async def middle_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserMiddleClickInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "middle_click", input))

    async def double_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserDoubleClickInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "double_click", input))

    async def triple_click(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserTripleClickInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "triple_click", input))

    async def hover(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserHoverInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "hover", input))

    async def left_click_drag(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserLeftClickDragInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "left_click_drag", input))

    async def left_mouse_down(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserLeftMouseDownInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "left_mouse_down", input))

    async def left_mouse_up(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserLeftMouseUpInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "left_mouse_up", input))

    async def mouse_move(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserMouseMoveInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "mouse_move", input))

    async def scroll(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserScrollInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "scroll", input))

    async def scroll_to(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserScrollToInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "scroll_to", input))

    async def type(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserTypeInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "type", input))

    async def key(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserKeyInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "key", input))

    async def hold_key(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserHoldKeyInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "hold_key", input))

    async def form_input(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserFormInputInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "form_input", input))

    async def read_page(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserReadPageInput
    ) -> str:
        return cast(str, await _perform(self._backend, "read_page", input))

    async def find(self, context: BetaToolsetCallContext, input: beta.BetaBrowserFindInput) -> str:
        return cast(str, await _perform(self._backend, "find", input))

    async def get_page_text(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserGetPageTextInput
    ) -> str:
        return cast(str, await _perform(self._backend, "get_page_text", input))

    async def wait(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserWaitInput
    ) -> str | None:
        return cast(str | None, await _perform(self._backend, "wait", input))

    async def read_console(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserReadConsoleInput
    ) -> str:
        return cast(str, await _perform(self._backend, "read_console", input))

    async def read_network(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserReadNetworkInput
    ) -> str:
        return cast(str, await _perform(self._backend, "read_network", input))

    async def javascript_exec(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserJavascriptExecInput
    ) -> str:
        return cast(str, await _perform(self._backend, "javascript_exec", input))

    async def new_tab(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserNewTabInput
    ) -> beta.BetaBrowserStateTabEntryParam:
        return cast(
            beta.BetaBrowserStateTabEntryParam, await _perform(self._backend, "new_tab", input)
        )

    async def list_tabs(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserListTabsInput
    ) -> list[beta.BetaBrowserStateTabEntryParam]:
        return cast(
            list[beta.BetaBrowserStateTabEntryParam],
            await _perform(self._backend, "list_tabs", input),
        )

    async def switch_tab(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserSwitchTabInput
    ) -> beta.BetaBrowserStateTabEntryParam:
        return cast(
            beta.BetaBrowserStateTabEntryParam, await _perform(self._backend, "switch_tab", input)
        )

    async def close_tab(
        self, context: BetaToolsetCallContext, input: beta.BetaBrowserCloseTabInput
    ) -> None:
        return cast(None, await _perform(self._backend, "close_tab", input))
