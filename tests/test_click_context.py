"""A click's repeat count and the post-action window context (OPL-5472)."""

from __future__ import annotations

import json

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


def _computer() -> mc.Computer:
    return mc.Computer(mc.Client("gck_test", base_url=BASE)._t, COMPUTER)


def _async_computer() -> mc.AsyncComputer:
    return mc.AsyncComputer(mc.AsyncClient("gck_test", base_url=BASE)._t, COMPUTER)


def _sent(route: respx.Route) -> tuple[dict[str, object], dict[str, str]]:
    request = route.calls.last.request
    return json.loads(request.content), dict(request.url.params)


@respx.mock
def test_count_goes_on_the_three_single_clicks() -> None:
    route = respx.post(INPUT).mock(return_value=httpx.Response(200, json={"ok": True}))
    c = _computer()
    assert c.click(1, 2, count=4) is None
    assert _sent(route) == ({"action": "left_click", "x": 1, "y": 2, "count": 4}, {})
    c.right_click(1, 2, "ctrl", count=2)
    assert _sent(route)[0] == {
        "action": "right_click",
        "x": 1,
        "y": 2,
        "text": "ctrl",
        "count": 2,
    }
    c.middle_click(count=10)
    assert _sent(route)[0] == {"action": "middle_click", "count": 10}
    c.click(1, 2)
    assert "count" not in _sent(route)[0]


@pytest.mark.parametrize("count", [0, 11, -1, 2.5, True, "3"])
@respx.mock
def test_a_count_the_platform_would_refuse_is_refused_before_sending(count: object) -> None:
    route = respx.post(INPUT).mock(return_value=httpx.Response(200, json={"ok": True}))
    with pytest.raises(ValueError, match="count must be a whole number from 1 to 10"):
        _computer().click(1, 2, count=count)  # type: ignore[arg-type]
    assert not route.called


def test_the_named_multi_clicks_take_no_count() -> None:
    from mandala_computer import _api

    with pytest.raises(ValueError, match="double_click takes no count"):
        _api.click_body("double_click", 1, 2, (), 3)
    with pytest.raises(TypeError):
        _computer().triple_click(1, 2, count=2)  # type: ignore[call-arg]


@respx.mock
def test_context_asks_and_answers_the_windows_after_the_click() -> None:
    other = {**WINDOW, "id": "0x1", "title": "terminal", "focused": False}
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "context": {"windows": [other, WINDOW], "focused": WINDOW}}
        )
    )
    ctx = _computer().click(640, 400, context=True)
    assert _sent(route)[1] == {"context": "1"}
    assert ctx is not None
    assert ctx.error is None
    assert [w.id for w in ctx.windows or []] == ["0x1", WINDOW["id"]]
    assert ctx.focused is not None
    assert ctx.focused.id == WINDOW["id"]
    assert ctx.focused.wm_class == "firefox-esr"


@respx.mock
def test_every_click_takes_context_and_a_null_focus_is_none() -> None:
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "context": {"windows": [], "focused": None}}
        )
    )
    c = _computer()
    for click in (c.right_click, c.middle_click, c.double_click, c.triple_click):
        assert click(1, 2, context=True) == mc.InputContext(windows=[], focused=None, error=None)
        assert _sent(route)[1] == {"context": "1"}


@respx.mock
def test_an_unread_context_says_why_and_the_click_still_happened() -> None:
    said = "listing windows is not supported on Windows guests yet"
    respx.post(INPUT).mock(
        return_value=httpx.Response(200, json={"ok": True, "context_error": said})
    )
    assert _computer().click(1, 2, context=True) == mc.InputContext(
        windows=None, focused=None, error=said
    )


@respx.mock
def test_an_answer_with_neither_is_refused_rather_than_read_as_an_empty_desktop() -> None:
    route = respx.post(INPUT).mock(return_value=httpx.Response(200, json={"ok": True}))
    with pytest.raises(mc.MandalaError, match="neither context nor context_error"):
        _computer().click(1, 2, context=True)
    route.mock(
        return_value=httpx.Response(
            200, json={"ok": True, "context": {"windows": {}, "focused": None}}
        )
    )
    with pytest.raises(mc.MandalaError, match="windows is not an array"):
        _computer().click(1, 2, context=True)


@respx.mock
def test_no_query_and_none_when_not_asked() -> None:
    route = respx.post(INPUT).mock(return_value=httpx.Response(200, json={"ok": True}))
    c = _computer()
    assert c.click(1, 2) is None
    assert _sent(route)[1] == {}
    assert c.click(1, 2, context=False) is None
    assert _sent(route)[1] == {}
    with pytest.raises(ValueError, match="context must be True or False"):
        c.click(1, 2, context="yes")  # type: ignore[arg-type]


@respx.mock
async def test_the_async_client_does_the_same() -> None:
    route = respx.post(INPUT).mock(
        return_value=httpx.Response(
            200, json={"ok": True, "context": {"windows": [WINDOW], "focused": WINDOW}}
        )
    )
    c = _async_computer()
    ctx = await c.click(1, 2, count=3, context=True)
    assert _sent(route) == ({"action": "left_click", "x": 1, "y": 2, "count": 3}, {"context": "1"})
    assert ctx is not None
    assert ctx.focused is not None
    assert ctx.focused.id == WINDOW["id"]
    route.mock(return_value=httpx.Response(200, json={"ok": True}))
    assert await c.double_click(1, 2) is None
