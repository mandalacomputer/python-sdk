"""The computer toolset driver (OPL-5851), run through Anthropic's own pipeline.

Every case goes in by ``tool_result()``, which is the same path the tool runner
takes — Anthropic's confirm gate, its input parsing and its rendering of the
result — and out through a real Computer onto a mocked transport, so what is
asserted is the request the platform would receive. The screenshot route is
modelled rather than stubbed: it crops and shrinks the way the platform does,
because the driver's arithmetic has to agree with the platform's to the pixel
and a stub that returned a fixed picture would agree with anything.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import subprocess
import sys
from typing import Any

import httpx
import pytest
import respx
from anthropic.tools import ToolsetConfigError
from anthropic.types.beta import BetaToolUseBlock

import mandala_computer as mc
from mandala_computer.anthropic import AsyncMandalaComputerToolset, MandalaComputerToolset

BASE = "https://api.test/api/v1"
SHOT = f"{BASE}/computers/vm-1/screenshot"
INPUT = f"{BASE}/computers/vm-1/input"


def png(width: int, height: int) -> bytes:
    """A PNG's signature and IHDR chunk, which is all of one the driver reads."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + bytes(9)
    )


def size_of(data: str) -> tuple[int, int]:
    raw = base64.b64decode(data)
    return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")


def fits(size: tuple[int, int]) -> bool:
    w, h = size
    return max(w, h) <= 2576 and math.ceil(w / 28) * math.ceil(h / 28) <= 4784


class Desktop:
    """A computer whose record says ``screen`` and whose display is ``capture``
    — the two differ on a desktop resumed from a capture taken at another size.
    ``captures`` moves the display on, one per screenshot taken."""

    def __init__(
        self,
        screen: tuple[int, int],
        *,
        capture: tuple[int, int] | None = None,
        captures: list[tuple[int, int]] | None = None,
        ignore_width: bool = False,
        answer: Any = None,
    ) -> None:
        self.screen = screen
        self.capture = capture or screen
        self.captures = list(captures or [])
        self.ignore_width = ignore_width
        self.answer = answer
        self.shots: list[dict[str, str]] = []
        self.inputs: list[dict[str, Any]] = []

    def record(self) -> dict[str, object]:
        w, h = self.screen
        return {
            "id": "vm-1",
            "name": "vm-1",
            "status": "running",
            "os": "linux",
            "resolution": f"{w}x{h}x24",
        }

    def shoot(self, request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        self.shots.append(query)
        src = self.capture
        if self.captures:
            self.capture = self.captures.pop(0)
        if "region" in query:
            x, y, w, h = (int(n) for n in query["region"].split(","))
            if x + w > src[0] or y + h > src[1]:
                return httpx.Response(400, json={"error": "region reaches past the screen"})
            src = (w, h)
        out = src
        if "w" in query and not self.ignore_width:
            w = min(max(int(query["w"]), 64), src[0])
            out = (w, max(1, src[1] * w // src[0]))
        return httpx.Response(200, content=png(*out), headers={"content-type": "image/png"})

    def act(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.inputs.append(body)
        if self.answer is not None:
            answered = self.answer(body)
            if answered is not None:
                return answered
        return httpx.Response(200, json={"ok": True})

    def mount(self) -> None:
        respx.get(SHOT).mock(side_effect=self.shoot)
        respx.post(INPUT).mock(side_effect=self.act)

    def computer(self) -> mc.Computer:
        self.mount()
        return mc.Computer(mc.Client("gck_test", base_url=BASE)._t, self.record())

    def async_computer(self) -> mc.AsyncComputer:
        self.mount()
        return mc.AsyncComputer(mc.AsyncClient("gck_test", base_url=BASE)._t, self.record())

    def toolset(self) -> MandalaComputerToolset:
        return MandalaComputerToolset(self.computer(), confirm=lambda context: True)

    def at(self, n: int = 0) -> list[int]:
        body = self.inputs[n]
        return body["coordinate"] if "coordinate" in body else [body["x"], body["y"]]


_n = 0


def use(name: str, **input: Any) -> BetaToolUseBlock:
    global _n
    _n += 1
    return BetaToolUseBlock(
        type="tool_use", id=f"toolu_{_n}", name=name, toolset_name="computer", input=input
    )


def text(result: Any) -> str:
    content = result["content"]
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in content if b["type"] == "text")


def image(result: Any) -> str:
    return next(b for b in result["content"] if b["type"] == "image")["source"]["data"]


# --- construction -------------------------------------------------------------


@respx.mock
def test_needs_a_confirm_callable_while_it_can_type() -> None:
    computer = Desktop((1280, 800)).computer()
    with pytest.raises(ToolsetConfigError):
        MandalaComputerToolset(computer)
    quiet = MandalaComputerToolset(
        computer,
        configs={
            "type": {"enabled": False},
            "key": {"enabled": False},
            "hold_key": {"enabled": False},
        },
    )
    assert quiet.to_dict()["configs"]["type"] == {"enabled": False}


@respx.mock
def test_serves_every_member() -> None:
    assert Desktop((1280, 800)).toolset().to_dict() == {"type": "computer_toolset_20260801"}


@respx.mock
def test_closing_leaves_the_computer_alone() -> None:
    d = Desktop((1280, 800))
    d.toolset().close()
    assert d.shots == [] and d.inputs == []


# --- screenshots and points ---------------------------------------------------


@respx.mock
def test_a_screen_that_fits_is_photographed_whole_and_fresh() -> None:
    d = Desktop((1920, 1080))
    t = d.toolset()
    assert size_of(image(t.tool_result(use("screenshot")))) == (1920, 1080)
    assert d.shots == [{"fresh": "1"}]
    t.tool_result(use("left_click", coordinate=[100, 200], text="ctrl+shift"))
    assert d.inputs[0]["action"] == "left_click"
    assert d.at() == [100, 200]
    assert "shift" in json.dumps(d.inputs[0])


@respx.mock
def test_a_screen_too_large_is_shrunk_and_points_scaled_back_up() -> None:
    d = Desktop((3840, 2160))
    t = d.toolset()
    shot = size_of(image(t.tool_result(use("screenshot"))))
    assert fits(shot)
    assert d.shots[0] == {"fresh": "1", "w": str(shot[0]), "format": "png"}
    t.tool_result(use("left_click", coordinate=[shot[0] // 2, shot[1] // 2]))
    x, y = d.at()
    assert abs(x - 1920) <= 2 and abs(y - 1080) <= 2


@respx.mock
def test_points_scale_from_the_picture_measured_not_the_one_asked_for() -> None:
    d = Desktop((1920, 1080), capture=(1280, 800))
    t = d.toolset()
    assert size_of(image(t.tool_result(use("screenshot")))) == (1280, 800)
    t.tool_result(use("left_click", coordinate=[640, 400]))
    assert d.at() == [960, 540]


@respx.mock
def test_a_retake_is_sized_past_the_platforms_rounding() -> None:
    d = Desktop((3840, 2160), capture=(3008, 2000))
    t = d.toolset()
    assert fits(size_of(image(t.tool_result(use("screenshot")))))
    assert len(d.shots) == 2
    t.tool_result(use("screenshot"))
    assert d.shots[2]["w"] == d.shots[1]["w"]


@respx.mock
def test_a_picture_that_still_does_not_fit_is_refused() -> None:
    d = Desktop((1280, 800), capture=(3840, 2160), ignore_width=True)
    r = d.toolset().tool_result(use("screenshot"))
    assert r["is_error"] is True
    assert "larger than the model can be shown" in text(r)


@respx.mock
def test_the_first_point_after_a_resize_is_refused_then_the_next_taken() -> None:
    d = Desktop((1920, 1080), capture=(1280, 800), captures=[(1920, 1080)])
    t = d.toolset()
    t.tool_result(use("screenshot"))
    t.tool_result(use("screenshot"))
    refused = t.tool_result(use("left_click", coordinate=[640, 400]))
    assert refused["is_error"] is True
    assert "screenshots are now 1920x1080" in text(refused)
    assert d.inputs == []
    t.tool_result(use("left_click", coordinate=[640, 400]))
    assert d.at() == [640, 400]


@respx.mock
@pytest.mark.parametrize(
    ("coordinate", "says"),
    [([1280, 0], "outside the 1280x800 screenshot"), ([-1, 5], "outside"), ([10], "")],
)
def test_a_point_that_cannot_be_placed_is_refused(coordinate: list[int], says: str) -> None:
    d = Desktop((1280, 800))
    r = d.toolset().tool_result(use("left_click", coordinate=coordinate))
    assert r["is_error"] is True
    assert says in text(r)
    assert d.inputs == []


@respx.mock
def test_mouse_move_and_drag_require_their_points() -> None:
    d = Desktop((1280, 800))
    t = d.toolset()
    assert t.tool_result(use("mouse_move"))["is_error"] is True
    assert t.tool_result(use("left_click_drag", coordinate=[5, 5]))["is_error"] is True
    assert d.inputs == []
    t.tool_result(
        use("left_click_drag", start_coordinate=[1, 2], coordinate=[30, 40], text="shift")
    )
    assert d.inputs[0]["action"] == "left_click_drag"
    assert d.inputs[0]["start_coordinate"] == [1, 2] and d.inputs[0]["coordinate"] == [30, 40]


@respx.mock
def test_the_pointer_is_answered_in_the_pictures_pixels() -> None:
    known = {"known": True}

    def answer(body: dict[str, Any]) -> httpx.Response | None:
        if body["action"] == "cursor_position":
            return httpx.Response(200, json={**known, "x": 1920, "y": 1080})
        return None

    d = Desktop((3840, 2160), answer=answer)
    t = d.toolset()
    shot = size_of(image(t.tool_result(use("screenshot"))))
    at = t.tool_result(use("cursor_position"))
    assert text(at) == f"X={1920 * shot[0] // 3840},Y={1080 * shot[1] // 2160}"
    known["known"] = False
    assert t.tool_result(use("cursor_position"))["is_error"] is True


# --- zoom ---------------------------------------------------------------------


@respx.mock
def test_zoom_crops_the_screens_pixels_shrunk_to_fit() -> None:
    d = Desktop((3840, 2160))
    t = d.toolset()
    shot = size_of(image(t.tool_result(use("screenshot"))))
    r = t.tool_result(use("zoom", region=[0, 0, shot[0], shot[1]]))
    assert fits(size_of(image(r)))
    assert d.shots[1]["region"] == "0,0,3840,2160"


@respx.mock
def test_zoom_takes_a_small_region_whole() -> None:
    d = Desktop((1280, 800))
    r = d.toolset().tool_result(use("zoom", region=[100, 100, 300, 200]))
    assert size_of(image(r)) == (200, 100)
    assert d.shots[0]["region"] == "100,100,200,100"
    assert "w" not in d.shots[0]


@respx.mock
def test_zoom_is_refused_outside_the_picture_and_on_a_mismatched_screen() -> None:
    d = Desktop((1920, 1080), capture=(1280, 800))
    t = d.toolset()
    assert t.tool_result(use("zoom", region=[0, 0, 2000, 10]))["is_error"] is True
    t.tool_result(use("screenshot"))
    r = t.tool_result(use("zoom", region=[0, 0, 100, 100]))
    assert r["is_error"] is True
    assert "not at the size its computer reports" in text(r)


# --- the keyboard ---------------------------------------------------------------


@respx.mock
def test_a_repeated_key_is_pressed_once_per_press() -> None:
    d = Desktop((1280, 800))
    d.toolset().tool_result(use("key", text="ctrl+Tab", repeat=3))
    assert len(d.inputs) == 3
    assert all(b["action"] == "key" for b in d.inputs)
    assert "repeat" not in json.dumps(d.inputs[0])


@respx.mock
@pytest.mark.parametrize("repeat", [0, 101])
def test_a_repeat_out_of_range_presses_nothing(repeat: int) -> None:
    d = Desktop((1280, 800))
    r = d.toolset().tool_result(use("key", text="Tab", repeat=repeat))
    assert r["is_error"] is True
    assert text(r) == "repeat must be a whole number from 1 to 100"
    assert d.inputs == []


@respx.mock
def test_a_repeated_key_says_how_far_it_got() -> None:
    pressed = {"n": 0}

    def answer(body: dict[str, Any]) -> httpx.Response | None:
        pressed["n"] += 1
        return (
            httpx.Response(409, json={"error": "the guest went away"}) if pressed["n"] > 2 else None
        )

    r = Desktop((1280, 800), answer=answer).toolset().tool_result(use("key", text="Tab", repeat=4))
    assert r["is_error"] is True
    assert text(r).startswith("pressed 2 of 4 times, then: ")
    assert "the guest went away" in text(r)


@respx.mock
def test_long_text_is_typed_in_pieces_without_splitting_a_character() -> None:
    d = Desktop((1280, 800))
    long = "a" * 399 + "\U0001f600" + "b" * 500
    d.toolset().tool_result(use("type", text=long))
    pieces = [b["text"] for b in d.inputs]
    assert [len(p) for p in pieces] == [400, 400, 100]
    assert "".join(pieces) == long


@respx.mock
def test_a_key_is_held_for_at_most_30_seconds() -> None:
    d = Desktop((1280, 800))
    t = d.toolset()
    assert t.tool_result(use("hold_key", text="shift", duration=31))["is_error"] is True
    t.tool_result(use("hold_key", text="shift", duration=2))
    assert [(b["action"], b["duration"]) for b in d.inputs] == [("hold_key", 2)]


# --- scrolling and waiting ------------------------------------------------------


@respx.mock
def test_scroll_goes_where_it_is_told_holding_what_it_is_told() -> None:
    d = Desktop((1280, 800))
    t = d.toolset()
    t.tool_result(
        use("scroll", coordinate=[10, 20], scroll_direction="up", scroll_amount=5, text="ctrl")
    )
    assert d.inputs[0]["action"] == "scroll"
    assert d.at() == [10, 20]
    for bad in (
        {"scroll_direction": "sideways", "scroll_amount": 3},
        {"scroll_direction": "down", "scroll_amount": 51},
        {"scroll_direction": "down", "scroll_amount": 0},
    ):
        assert t.tool_result(use("scroll", **bad))["is_error"] is True
    assert len(d.inputs) == 1


@respx.mock
def test_long_waits_go_in_the_platforms_30_second_pieces() -> None:
    d = Desktop((1280, 800))
    t = d.toolset()
    t.tool_result(use("wait", duration=75))
    assert [b["duration"] for b in d.inputs] == [30, 30, 15]
    for duration in (0, -1, 301):
        assert t.tool_result(use("wait", duration=duration))["is_error"] is True
    assert len(d.inputs) == 3


# --- failures -------------------------------------------------------------------


@respx.mock
def test_a_platform_failure_is_an_error_result_in_its_own_words() -> None:
    d = Desktop(
        (1280, 800),
        answer=lambda body: httpx.Response(409, json={"error": "computer vm-1 is stopped"}),
    )
    r = d.toolset().tool_result(use("left_click", coordinate=[1, 1]))
    assert r["is_error"] is True
    assert r["toolset_name"] == "computer"
    assert "computer vm-1 is stopped" in text(r)


# --- async ----------------------------------------------------------------------


@respx.mock
async def test_the_async_driver_scales_presses_and_waits_the_same_way() -> None:
    d = Desktop((1920, 1080), capture=(1280, 800))
    t = AsyncMandalaComputerToolset(d.async_computer(), confirm=lambda context: True)
    shot = await t.tool_result(use("screenshot"))
    assert size_of(image(shot)) == (1280, 800)
    await t.tool_result(use("left_click", coordinate=[640, 400]))
    assert d.at() == [960, 540]
    await t.tool_result(use("key", text="Tab", repeat=2))
    await t.tool_result(use("wait", duration=31))
    assert [b["action"] for b in d.inputs] == ["left_click", "key", "key", "wait", "wait"]
    assert [b["duration"] for b in d.inputs[3:]] == [30, 1]
    await t.close()


@respx.mock
async def test_the_async_driver_lets_a_cancellation_through() -> None:
    started = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json={"ok": True})

    d = Desktop((1280, 800))
    computer = d.async_computer()
    respx.post(INPUT).mock(side_effect=slow)
    t = AsyncMandalaComputerToolset(computer, confirm=lambda context: True)
    task = asyncio.create_task(t.tool_result(use("left_click", coordinate=[1, 1])))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- under the tool runner ------------------------------------------------------


@respx.mock
def test_it_is_a_tools_entry_the_runner_sends_and_answers() -> None:
    import anthropic

    d = Desktop((1280, 800))
    t = d.toolset()

    def message(content: list[dict[str, Any]], stop: str) -> dict[str, Any]:
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    replies = [
        message(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_a",
                    "name": "left_click",
                    "toolset_name": "computer",
                    "input": {"coordinate": [5, 5]},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_b",
                    "name": "screenshot",
                    "toolset_name": "computer",
                    "input": {},
                },
            ],
            "tool_use",
        ),
        message([{"type": "text", "text": "done"}], "end_turn"),
    ]
    sent: list[dict[str, Any]] = []

    # The Anthropic client speaks over its own HTTP library, which respx does
    # not reach; its mock transport answers instead.
    import httpx2

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(200, json=replies.pop(0))

    client = anthropic.Anthropic(
        api_key="sk-ant-fixture-only",
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(answer)),
    )
    runner = client.beta.messages.tool_runner(
        model="claude-opus-5-5",
        max_tokens=1024,
        tools=[t],
        messages=[{"role": "user", "content": "go"}],
    )
    for _ in runner:
        pass
    assert sent[0]["tools"] == [{"type": "computer_toolset_20260801"}]
    answers = sent[1]["messages"][-1]["content"]
    assert [a["toolset_name"] for a in answers] == ["computer", "computer"]
    assert size_of(image(answers[1])) == (1280, 800)


# --- the optional dependency ------------------------------------------------------


def test_the_package_imports_without_anthropic() -> None:
    probe = (
        "import sys, builtins\n"
        "real = builtins.__import__\n"
        "def guard(name, *a, **k):\n"
        "    if name == 'anthropic' or name.startswith('anthropic.'):\n"
        "        raise ImportError('blocked')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "import mandala_computer\n"
        "try:\n"
        "    import mandala_computer.anthropic\n"
        "except ImportError as e:\n"
        "    print('refused:', e)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout
    assert "refused: mandala_computer.anthropic needs the anthropic package" in out
    assert "pip install 'mandala-computer[anthropic]'" in out


def test_the_readme_example_compiles_and_names_what_exists() -> None:
    from pathlib import Path

    readme = (Path(__file__).parent.parent / "README.md").read_text()
    section = readme.split("### Claude's computer toolset", 1)[1].split("\n### ", 1)[0]
    example = section.split("```python\n", 1)[1].split("```", 1)[0]
    compile(example, "README.md", "exec")
    import mandala_computer.anthropic as driver

    for name in ("MandalaComputerToolset", "AsyncMandalaComputerToolset"):
        assert name in section and hasattr(driver, name)
    assert "from mandala_computer.anthropic import MandalaComputerToolset" in example
