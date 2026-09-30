"""The post-action window context on every input action, not only the clicks,
and the User-Agent naming this SDK and its version (OPL-5523)."""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
INPUT = f"{BASE}/computers/vm-1/input"
COMPUTER = {"id": "vm-1", "name": "vm-1", "status": "running", "os": "linux"}
WINDOW = {
    "id": "0x2a0002c",
    "title": "Example Domain",
    "class": "firefox-esr",
    "type": "normal",
    "x": 0,
    "y": 51,
    "width": 1280,
    "height": 749,
    "focused": True,
    "visible": True,
}
CONTEXT = {"windows": [WINDOW], "focused": WINDOW}
SAID = "no active desktop session"


def _computer() -> mc.Computer:
    return mc.Computer(mc.Client("gck_test", base_url=BASE)._t, COMPUTER)


def _async_computer() -> mc.AsyncComputer:
    return mc.AsyncComputer(mc.AsyncClient("gck_test", base_url=BASE)._t, COMPUTER)


def _sent(route: respx.Route) -> tuple[dict[str, object], dict[str, str]]:
    request = route.calls.last.request
    return json.loads(request.content), dict(request.url.params)


# Every action but the clicks (test_click_context.py) and ``type`` (below), as
# the action it sends and a call that takes ``context`` as a keyword. The same
# names and arguments serve the sync and the async computer.
ACTIONS: list[tuple[str, str, tuple[Any, ...], dict[str, Any]]] = [
    ("move", "move", (1, 2), {}),
    ("left_click_drag", "drag", (5, 6), {"from_x": 1, "from_y": 2}),
    ("left_mouse_down", "mouse_down", (1, 2), {}),
    ("left_mouse_up", "mouse_up", (1, 2), {}),
    ("scroll", "scroll", (1, 2), {"direction": "up"}),
    ("paste", "paste", ("hello",), {}),
    ("key", "key", ("ctrl", "l"), {}),
    ("hold_key", "hold_key", ("shift",), {"seconds": 1}),
    ("wait", "wait", (1,), {}),
]


@pytest.mark.parametrize(("action", "method", "args", "kwargs"), ACTIONS)
@respx.mock
def test_context_asks_and_answers_the_windows_after_the_action(
    action: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(200, json={"ok": True, "context": CONTEXT})
    )
    call: Callable[..., Any] = getattr(_computer(), method)
    ctx = call(*args, **kwargs, context=True)
    body, query = _sent(route)
    assert body["action"] == action
    assert query == {"context": "1"}
    assert isinstance(ctx, mc.InputContext)
    assert ctx.error is None
    assert [w.id for w in ctx.windows or []] == [WINDOW["id"]]
    assert ctx.focused is not None
    assert ctx.focused.wm_class == "firefox-esr"


@pytest.mark.parametrize(("action", "method", "args", "kwargs"), ACTIONS)
@respx.mock
def test_no_query_and_none_when_not_asked(
    action: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(200, json={"ok": True, "context": CONTEXT})
    )
    call: Callable[..., Any] = getattr(_computer(), method)
    assert call(*args, **kwargs) is None
    body, query = _sent(route)
    assert body["action"] == action
    assert query == {}
    assert call(*args, **kwargs, context=False) is None
    assert _sent(route)[1] == {}


@pytest.mark.parametrize(("action", "method", "args", "kwargs"), ACTIONS)
@respx.mock
def test_an_unread_context_says_why_and_the_action_still_happened(
    action: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    respx.post(INPUT).mock(
        return_value=httpx.Response(200, json={"ok": True, "context_error": SAID})
    )
    call: Callable[..., Any] = getattr(_computer(), method)
    assert call(*args, **kwargs, context=True) == mc.InputContext(
        windows=None, focused=None, error=SAID
    )


@pytest.mark.parametrize(("action", "method", "args", "kwargs"), ACTIONS)
@respx.mock
def test_an_answer_with_neither_is_refused(
    action: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    respx.post(INPUT).mock(return_value=httpx.Response(200, json={"ok": True}))
    call: Callable[..., Any] = getattr(_computer(), method)
    with pytest.raises(mc.MandalaError, match="neither context nor context_error"):
        call(*args, **kwargs, context=True)


@pytest.mark.parametrize(
    ("action", "method", "args", "kwargs"),
    [*ACTIONS, ("type", "type", ("hi",), {})],
)
@respx.mock
def test_a_context_that_is_not_a_bool_is_refused_before_sending(
    action: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    route = respx.post(INPUT).mock(return_value=httpx.Response(200, json={"ok": True}))
    call: Callable[..., Any] = getattr(_computer(), method)
    with pytest.raises(ValueError, match="context must be True or False"):
        call(*args, **kwargs, context="yes")
    assert not route.called


@respx.mock
def test_type_with_context_answers_a_type_result() -> None:
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "mechanism": "physical", "context": CONTEXT}
        )
    )
    c = _computer()
    res = c.type("hello", context=True)
    assert _sent(route) == ({"action": "type", "text": "hello"}, {"context": "1"})
    assert isinstance(res, mc.TypeResult)
    assert res.mechanism == "physical"
    assert res.context.focused is not None
    assert res.context.focused.id == WINDOW["id"]

    route.mock(
        return_value=httpx.Response(
            200, json={"ok": True, "mechanism": "physical", "context_error": SAID}
        )
    )
    assert c.type("hello", context=True) == mc.TypeResult(
        mechanism="physical", context=mc.InputContext(windows=None, focused=None, error=SAID)
    )


@respx.mock
def test_type_without_context_answers_the_mechanism_as_it_did() -> None:
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(200, json={"ok": True, "mechanism": "physical"})
    )
    assert _computer().type("hello") == "physical"
    assert _sent(route)[1] == {}


@pytest.mark.parametrize(("action", "method", "args", "kwargs"), ACTIONS)
@respx.mock
async def test_the_async_client_does_the_same(
    action: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(200, json={"ok": True, "context": CONTEXT})
    )
    call: Callable[..., Awaitable[Any]] = getattr(_async_computer(), method)
    ctx = await call(*args, **kwargs, context=True)
    assert _sent(route)[0]["action"] == action
    assert _sent(route)[1] == {"context": "1"}
    assert isinstance(ctx, mc.InputContext)
    assert ctx.focused is not None
    assert ctx.focused.id == WINDOW["id"]
    assert await call(*args, **kwargs) is None
    assert _sent(route)[1] == {}
    route.mock(return_value=httpx.Response(200, json={"ok": True, "context_error": SAID}))
    assert await call(*args, **kwargs, context=True) == mc.InputContext(
        windows=None, focused=None, error=SAID
    )


@respx.mock
async def test_the_async_type_does_the_same() -> None:
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "mechanism": "physical", "context": CONTEXT}
        )
    )
    c = _async_computer()
    res = await c.type("hello", context=True)
    assert _sent(route)[1] == {"context": "1"}
    assert isinstance(res, mc.TypeResult)
    assert res.mechanism == "physical"
    assert await c.type("hello") == "physical"
    assert _sent(route)[1] == {}


def _runtime() -> str:
    v = sys.version_info
    return f"python/{v.major}.{v.minor}.{v.micro} httpx/{httpx.__version__}"


@respx.mock
def test_every_request_names_this_sdk_and_its_version() -> None:
    route = respx.get(f"{BASE}/computers/vm-1").mock(
        return_value=httpx.Response(200, json=COMPUTER)
    )
    mc.Client("gck_test", base_url=BASE).computers.get("vm-1")
    assert route.calls.last.request.headers["user-agent"] == (
        f"mandala-computer-py/{mc.__version__} {_runtime()}"
    )


@respx.mock
async def test_the_caller_token_is_appended_on_both_clients() -> None:
    route = respx.get(f"{BASE}/computers/vm-1").mock(
        return_value=httpx.Response(200, json=COMPUTER)
    )
    want = f"mandala-computer-py/{mc.__version__} {_runtime()} my-app/1.2 (+ops)"
    mc.Client("gck_test", base_url=BASE, user_agent="my-app/1.2 (+ops)").computers.get("vm-1")
    assert route.calls.last.request.headers["user-agent"] == want
    async with mc.AsyncClient("gck_test", base_url=BASE, user_agent="my-app/1.2 (+ops)") as ac:
        await ac.computers.get("vm-1")
    assert route.calls.last.request.headers["user-agent"] == want


@respx.mock
def test_the_sdk_token_replaces_one_set_on_the_http_client() -> None:
    route = respx.get(f"{BASE}/computers/vm-1").mock(
        return_value=httpx.Response(200, json=COMPUTER)
    )
    http = httpx.Client(headers={"User-Agent": "custom/1"})
    mc.Client("gck_test", base_url=BASE, http_client=http).computers.get("vm-1")
    assert route.calls.last.request.headers["user-agent"].startswith("mandala-computer-py/")


@pytest.mark.parametrize("user_agent", ["", " my-app", "my-app\r\nX-Evil: 1", "café/1", 3])
def test_a_token_that_is_not_printable_ascii_is_refused(user_agent: object) -> None:
    with pytest.raises(ValueError, match="user_agent must be printable ASCII"):
        mc.Client("gck_test", base_url=BASE, user_agent=user_agent)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="user_agent must be printable ASCII"):
        mc.AsyncClient("gck_test", base_url=BASE, user_agent=user_agent)  # type: ignore[arg-type]
