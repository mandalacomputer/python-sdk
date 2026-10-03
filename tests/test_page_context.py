"""The page in the focused Chromium window, in an input action's context.

The platform answers ``context.dom`` when the window holding the keyboard is
Chromium, and ``context_error`` beside a context that has none.
"""

from __future__ import annotations

from typing import Any

import pytest

from mandala_computer import InputContext, MandalaError, PageContext, PageElement
from mandala_computer._computer import _input_context

WINDOW = {
    "id": "0x3",
    "title": "Inbox - Chromium",
    "class": "Chromium",
    "type": "normal",
    "x": 0,
    "y": 0,
    "width": 1280,
    "height": 800,
    "focused": True,
    "visible": True,
}

DOM: dict[str, Any] = {
    "url": "https://mail.example/inbox",
    "title": "Inbox",
    "elements": [
        {
            "tag": "button",
            "role": "",
            "name": "Compose",
            "text": "Compose",
            "x": 24,
            "y": 188,
            "width": 96,
            "height": 32,
        },
        {
            "tag": "a",
            "role": "",
            "name": "",
            "text": "Next",
            "href": "https://mail.example/2",
            "x": 1180,
            "y": 188,
            "width": 40,
            "height": 18,
        },
    ],
    "truncated": False,
}


def test_the_page_is_decoded_when_chromium_is_focused() -> None:
    ctx = _input_context(
        {"ok": True, "context": {"windows": [WINDOW], "focused": WINDOW, "dom": DOM}}
    )
    assert ctx.error is None
    assert ctx.focused is not None and ctx.focused.id == "0x3"
    assert ctx.dom == PageContext(
        url="https://mail.example/inbox",
        title="Inbox",
        elements=[
            PageElement(
                tag="button",
                role="",
                name="Compose",
                text="Compose",
                x=24,
                y=188,
                width=96,
                height=32,
            ),
            PageElement(
                tag="a",
                role="",
                name="",
                text="Next",
                x=1180,
                y=188,
                width=40,
                height=18,
                href="https://mail.example/2",
            ),
        ],
        truncated=False,
    )


def test_the_windows_are_kept_and_the_reason_given_when_there_is_no_page() -> None:
    said = "the focused window is not Chromium, so no page elements were read"
    ctx = _input_context(
        {"ok": True, "context": {"windows": [WINDOW], "focused": WINDOW}, "context_error": said}
    )
    assert ctx.windows is not None and [w.id for w in ctx.windows] == ["0x3"]
    assert ctx.dom is None
    assert ctx.error == said


def test_a_context_from_a_platform_without_page_context_decodes_as_before() -> None:
    ctx = _input_context({"ok": True, "context": {"windows": [], "focused": None}})
    assert ctx == InputContext(windows=[], focused=None, error=None, dom=None)
    # Positional construction, as before the field existed.
    assert InputContext([], None, None) == ctx


def test_no_windows_still_means_the_windows_could_not_be_read() -> None:
    said = "no active desktop session"
    ctx = _input_context({"ok": True, "context_error": said})
    assert ctx == InputContext(windows=None, focused=None, error=said)


@pytest.mark.parametrize(
    "dom",
    [
        "page",
        {**DOM, "url": 7},
        {k: v for k, v in DOM.items() if k != "title"},
        {**DOM, "truncated": "no"},
        {**DOM, "elements": None},
        {**DOM, "elements": ["x"]},
        {**DOM, "elements": [{**DOM["elements"][0], "tag": 1}]},
        {**DOM, "elements": [{k: v for k, v in DOM["elements"][0].items() if k != "text"}]},
        {**DOM, "elements": [{**DOM["elements"][0], "x": "24"}]},
        {**DOM, "elements": [{**DOM["elements"][0], "width": 1.5}]},
        {**DOM, "elements": [{**DOM["elements"][0], "height": True}]},
        {**DOM, "elements": [{**DOM["elements"][0], "href": 3}]},
    ],
)
def test_a_page_that_is_not_one_is_refused(dom: object) -> None:
    with pytest.raises(MandalaError):
        _input_context({"ok": True, "context": {"windows": [], "focused": None, "dom": dom}})


def test_a_context_error_that_is_not_a_string_beside_a_context_is_refused() -> None:
    with pytest.raises(MandalaError):
        _input_context(
            {"ok": True, "context": {"windows": [], "focused": None}, "context_error": 3}
        )
