"""Claude's computer toolset, driven against a Mandala computer (OPL-5851).

Anthropic's SDK ships the class a computer-use driver subclasses,
``BetaAbstractComputerToolset20260801``, and no desktop for it to drive. This
module is that driver. Give it a :class:`~mandala_computer.Computer` and pass
it as a ``tools`` entry to the tool runner, and every action the model asks for
runs on that computer::

    import anthropic
    from mandala_computer import Client
    from mandala_computer.anthropic import MandalaComputerToolset

    computer = Client().computers.get("vm-...")
    with MandalaComputerToolset(computer, confirm=lambda context: True) as desktop:
        runner = anthropic.Anthropic().beta.messages.tool_runner(
            model="claude-opus-5-5",
            max_tokens=16000,
            tools=[desktop],
            messages=[{"role": "user", "content": "Open a terminal and run date"}],
        )
        for message in runner:
            print(message.content)

:class:`AsyncMandalaComputerToolset` is the same over an
:class:`~mandala_computer.AsyncComputer`, for ``AsyncAnthropic``.

``anthropic`` is an optional dependency, ``pip install
'mandala-computer[anthropic]'``: ``mandala_computer`` itself imports without
it, and only this module needs it.

``confirm`` IS REQUIRED, by Anthropic's class rather than by this one: a
toolset that can type and press keys refuses to construct without a callable
that approves each call, unless ``configs`` turns those members off. Approving
everything, as above, is a decision to make about a throwaway computer only.

Closing the toolset does not stop or delete the computer. It belongs to the
caller, who may run several toolsets against it in turn.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

try:
    from anthropic.tools import ToolError
    from anthropic.tools.computer import (
        BetaAbstractComputerToolset20260801,
        BetaAsyncAbstractComputerToolset20260801,
        BetaAsyncComputerConfirmCallable,
        BetaComputerConfirmCallable,
        BetaComputerCursorPositionResult,
        BetaScreenshotResult,
        BetaToolConfigs,
        BetaToolsetCallContext,
    )
    from anthropic.types.beta import (
        BetaComputerCursorPositionInput,
        BetaComputerDoubleClickInput,
        BetaComputerHoldKeyInput,
        BetaComputerKeyInput,
        BetaComputerLeftClickDragInput,
        BetaComputerLeftClickInput,
        BetaComputerLeftMouseDownInput,
        BetaComputerLeftMouseUpInput,
        BetaComputerMiddleClickInput,
        BetaComputerMouseMoveInput,
        BetaComputerRightClickInput,
        BetaComputerScreenshotInput,
        BetaComputerScrollInput,
        BetaComputerToolsetConfigsParam,
        BetaComputerTripleClickInput,
        BetaComputerTypeInput,
        BetaComputerWaitInput,
        BetaComputerZoomInput,
    )
except ImportError as error:  # pragma: no cover - exercised by the import test
    raise ImportError(
        "mandala_computer.anthropic needs the anthropic package (1.12 or newer): "
        "pip install 'mandala-computer[anthropic]'"
    ) from error

from ._exceptions import APIError

if TYPE_CHECKING:
    from ._async_computer import AsyncComputer
    from ._computer import Computer
    from ._models import ScreenshotInfo

__all__ = ["AsyncMandalaComputerToolset", "MandalaComputerToolset"]

#: The largest image the toolset's models take: 2576 pixels on the long edge
#: and 4784 visual tokens, a token being a 28-pixel tile. The API refuses a
#: larger one outright rather than shrinking it, and a computer may be as large
#: as 3840x2160.
_MAX_EDGE = 2576
_MAX_TILES = 4784
_TILE = 28
#: The platform's floor for a shrunk screenshot's width.
_MIN_WIDTH = 64
#: How many times a zoom measures the screen and cuts its crop before giving up
#: on a screen whose capture keeps being replaced between the two.
_ZOOM_TRIES = 3

#: The platform's ceiling on one ``wait`` or one held key, in seconds.
_PLATFORM_HOLD = 30
#: The toolset's own ceiling on ``wait``, which this splits into platform waits.
_MAX_WAIT = 300
#: The toolset's ceiling on ``key``'s ``repeat``.
_MAX_REPEAT = 100
#: The platform's ceiling on one scroll's notches.
_MAX_SCROLL = 50
#: The most characters one platform ``type`` takes; longer text goes in pieces.
_TYPE_PIECE = 400

_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})


class _Size(NamedTuple):
    width: int
    height: int


def _fits(size: _Size) -> bool:
    tiles = math.ceil(size.width / _TILE) * math.ceil(size.height / _TILE)
    return max(size) <= _MAX_EDGE and tiles <= _MAX_TILES


def _largest_fit(size: _Size) -> _Size:
    """The largest picture of ``size`` the model takes: the size itself when it
    fits, otherwise the widest that does, with its height worked out as the
    platform works out the height for a width — scaled by the same ratio and
    rounded down. The two have to agree to the pixel, because the model aims in
    this picture."""
    if _fits(size):
        return size

    def height_at(w: int) -> int:
        return max(1, size.height * w // size.width)

    width = size.width - 1
    while width > _MIN_WIDTH and not _fits(_Size(width, height_at(width))):
        width -= 1
    return _Size(width, height_at(width))


def _crop_shrink(size: _Size) -> tuple[int | None, float | None]:
    """How to have the platform shrink a crop of ``size`` to a picture the model
    takes, as a width or a scale: neither when it fits already, a width when one
    of at least :data:`_MIN_WIDTH` does, and otherwise a scale.

    A width alone is not enough for a crop (found in review). The platform will
    not shrink below 64 pixels wide by ``w``, so a tall, narrow region — a strip
    down a portrait screen — came back 64 pixels wide and still taller than the
    model takes. A scale has no such floor. The platform rounds ``scale`` to the
    nearest pixel, halves away from zero, where it floors ``w``, so the scale is
    the largest whose ROUNDED size fits."""
    fit = _largest_fit(size)
    if fit == size:
        return None, None
    # At least the platform's floor as well as fitting: a crop one or two pixels
    # wide fits at a width of 1, which the platform raises back to the crop's own
    # width, so it would come back unshrunk (found in re-review).
    if _fits(fit) and fit.width >= _MIN_WIDTH:
        return fit.width, None

    def at(k: float) -> _Size:
        return _Size(
            max(1, math.floor(size.width * k + 0.5)), max(1, math.floor(size.height * k + 0.5))
        )

    scale = _MAX_EDGE / max(size)
    while not _fits(at(scale)):
        scale *= 0.99
    return None, scale


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_size(data: bytes) -> _Size | None:
    """A PNG's size, read from its header, or ``None`` when the bytes are not a
    PNG: the eight-byte signature, then the IHDR chunk, whose first two fields
    are the width and height."""
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return _Size(width, height) if width > 0 and height > 0 else None


def _number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _whole(v: object, field: str, low: int, high: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not low <= v <= high:
        raise ToolError(f"{field} must be a whole number from {low} to {high}")
    return v


def _seconds(v: object, field: str, high: int, hint: str = "") -> float:
    if not _number(v) or not 0 < float(v) <= high:  # type: ignore[arg-type]
        raise ToolError(f"{field} must be more than 0 and at most {high} seconds{hint}")
    return float(v)  # type: ignore[arg-type]


def _chord(text: object, field: str = "text") -> tuple[str, ...]:
    """``ctrl+shift+t`` as its keys. The toolset spells chords with ``+``; the
    SDK takes them as separate keys."""
    if text is None:
        return ()
    if not isinstance(text, str):
        raise ToolError(f"{field} must be a key or a key combination")
    if not text.strip():
        return ()
    keys = tuple(k.strip() for k in text.split("+"))
    # An empty part is refused, not dropped (found in review): `+Delete` would
    # otherwise press Delete alone and `ctrl++` press ctrl alone — an action
    # the model did not ask for, reported as done.
    if not all(keys):
        raise ToolError(
            f"{field} must be keys joined by +, such as ctrl+s; the + key itself is plus"
        )
    return keys


def _keys(text: object, example: str) -> tuple[str, ...]:
    keys = _chord(text)
    if not keys:
        raise ToolError(f"text must name a key, such as {example}")
    return keys


def _pieces(text: object) -> list[str]:
    """Text as the pieces the platform types, each at most 400 code points —
    counted as the platform counts, so a piece never ends half way through a
    character — and never ending between the two halves of a CRLF, which is one
    Return together and a refused bare CR apart (found in review)."""
    if not isinstance(text, str) or not text:
        raise ToolError("text must be the text to type")
    out = []
    i = 0
    while i < len(text):
        end = min(i + _TYPE_PIECE, len(text))
        if end < len(text) and text[end - 1] == "\r" and text[end] == "\n":
            end -= 1
        out.append(text[i:end])
        i = end
    return out


def _typed_then(typed: int, total: int, error: ToolError) -> ToolError:
    # Said, because what is already on the screen stays there: typing the whole
    # text again would type its start twice, and a newline in it would run a
    # command twice (found in review).
    return ToolError(
        f"typed {typed} of {total} characters, then: {error}. "
        "The piece that failed may have been typed in part."
    )


def _waits(duration: object) -> list[float]:
    """A toolset ``wait``, as the platform's 30-second pieces."""
    left = _seconds(duration, "duration", _MAX_WAIT)
    out = []
    while left > 0:
        piece = min(left, _PLATFORM_HOLD)
        out.append(piece)
        left -= piece
    return out


def _hold(duration: object) -> float:
    # A hold cannot be split the way a wait can: letting go half way is a
    # different gesture.
    return _seconds(duration, "duration", _PLATFORM_HOLD, "; a key is held for 30 seconds at most")


def _repeat(value: object) -> int:
    return 1 if value is None else _whole(value, "repeat", 1, _MAX_REPEAT)


def _scroll(input: BetaComputerScrollInput) -> tuple[str, int]:
    direction = input.scroll_direction
    if direction not in _SCROLL_DIRECTIONS:
        raise ToolError("scroll_direction must be up, down, left or right")
    return direction, _whole(input.scroll_amount, "scroll_amount", 1, _MAX_SCROLL)


def _encoded(data: bytes) -> BetaScreenshotResult:
    return BetaScreenshotResult(data=base64.b64encode(data).decode("ascii"), media_type="image/png")


class _View:
    """What the model has been shown, and how its points reach the screen.

    Every screenshot's size is MEASURED rather than assumed, because the
    picture can differ from the screen the computer reports: a desktop resumed
    from a capture taken at another size can answer screenshots at the
    capture's size while it takes pointer input at its own, until it is
    restarted. Points arrive in the pixels of the last screenshot and are
    scaled to the screen's.
    """

    def __init__(self, screen: tuple[int, int]) -> None:
        #: The screen the computer reports: the space the platform takes points in.
        self.screen = _Size(*screen)
        # The picture of it asked for before any has been measured.
        expected = _largest_fit(self.screen)
        #: The size of the last screenshot the model was shown.
        self.frame = expected
        #: The width to have the platform shrink a screenshot to, once one needs it.
        self.request: int | None = None if expected == self.screen else expected.width
        self.shown = False
        #: Set when a screenshot came back at a size other than the last one's.
        #: A point chosen before that is in the old picture's pixels, and
        #: nothing in a call says which picture it was aimed at — a model that
        #: asks for a screenshot and a click in one reply chose the click before
        #: it saw the screenshot. So the next action that carries a point is
        #: refused once, with the new size in the refusal, and the model aims
        #: again.
        self.resized = False

    def retake(self, size: _Size) -> int:
        """The width to take a picture again at, when ``size`` is too large.

        Worked out from a picture one row TALLER than the one measured: the
        platform scales the capture it holds, not the picture it last returned,
        and that picture's height was rounded down when it was shrunk, so the
        capture's true shape is somewhere below one row more."""
        self.request = _largest_fit(_Size(size.width, size.height + 1)).width
        return self.request

    def accept(self, size: _Size) -> None:
        if self.shown and size != self.frame:
            self.resized = True
        self.frame = size
        self.shown = True

    def aiming(self) -> None:
        if not self.resized:
            return
        self.resized = False
        raise ToolError(
            f"the screen changed size: screenshots are now {self.frame.width}x{self.frame.height}. "
            "Aim again in the latest screenshot."
        )

    def points(self, required: bool, *given: object) -> list[tuple[int, int] | None]:
        """The model's points as points on the screen. An optional point that is
        absent stays absent: a click with no coordinate clicks where the pointer
        is. A point outside the picture is refused rather than moved into it,
        which would be a click somewhere the model did not aim."""
        if required and any(v is None for v in given):
            raise ToolError("a coordinate is required, as [x, y] in the pixels of the screenshot")
        if all(v is None for v in given):
            return [None for _ in given]
        frame, screen = self.frame, self.screen
        out: list[tuple[int, int] | None] = []
        for v in given:
            if v is None:
                out.append(None)
                continue
            if not isinstance(v, (list, tuple)) or len(v) != 2 or not all(_number(n) for n in v):
                raise ToolError("a coordinate must be [x, y], in the pixels of the screenshot")
            x, y = v
            if x < 0 or y < 0 or x >= frame.width or y >= frame.height:
                raise ToolError(
                    f"[{x}, {y}] is outside the {frame.width}x{frame.height} screenshot"
                )
            out.append(
                (
                    math.floor(x * screen.width / frame.width),
                    math.floor(y * screen.height / frame.height),
                )
            )
        self.aiming()
        return out

    def zoom_box(self, region: object) -> tuple[float, float, float, float]:
        """The model's ``region``, checked against the last picture it saw."""
        if (
            not isinstance(region, (list, tuple))
            or len(region) != 4
            or not all(_number(n) for n in region)
        ):
            raise ToolError("region must be [x0, y0, x1, y1], in the pixels of the screenshot")
        x0, y0, x1, y1 = region
        frame = self.frame
        if not (0 <= x0 < x1 <= frame.width and 0 <= y0 < y1 <= frame.height):
            raise ToolError(
                f"region [{x0}, {y0}, {x1}, {y1}] is not a rectangle inside the "
                f"{frame.width}x{frame.height} screenshot"
            )
        self.aiming()
        if not self.shown:
            raise ToolError(
                "take a screenshot before zooming, so the region has a picture to be in"
            )
        return x0, y0, x1, y1

    def crop(
        self, box: tuple[float, float, float, float], native: _Size
    ) -> tuple[tuple[int, int, int, int], int | None, float | None]:
        """The rectangle of the capture for ``box``, and the width or the scale
        to shrink the crop by when it is too large for the model; see
        :func:`_crop_shrink`.

        In the capture's own pixels (found in review): the platform crops the
        capture it HOLDS, whose pixels are not the screen's when the two differ
        and not the picture's when the picture was shrunk — a 3200x1800 capture
        under a 3840x2160 record shrinks to the same 2576x1449 picture as a
        3840x2160 one."""
        x0, y0, x1, y1 = box
        frame = self.frame
        left = math.floor(x0 * native.width / frame.width)
        top = math.floor(y0 * native.height / frame.height)
        right = min(native.width, math.ceil(x1 * native.width / frame.width))
        bottom = min(native.height, math.ceil(y1 * native.height / frame.height))
        crop = _Size(max(1, right - left), max(1, bottom - top))
        return ((left, top, crop.width, crop.height), *_crop_shrink(crop))

    def cursor(self, at: tuple[int, int] | None) -> BetaComputerCursorPositionResult:
        if at is None:
            raise ToolError(
                "the pointer has not been placed yet, so it has no position; move it first"
            )
        # In the pixels of the newest screenshot, which is where the model will
        # aim its next point.
        frame, screen = self.frame, self.screen
        return BetaComputerCursorPositionResult(
            x=math.floor(at[0] * frame.width / screen.width),
            y=math.floor(at[1] * frame.height / screen.height),
        )


def _measured(data: bytes, retaking: bool) -> _Size:
    size = _png_size(data)
    if size is None:
        raise ToolError("the screenshot came back in a format other than PNG")
    if retaking and not _fits(size):
        raise ToolError("the screen came back larger than the model can be shown")
    return size


def _pinned(measured: ScreenshotInfo) -> tuple[str, _Size]:
    """The capture a zoom is cut from, and its own size, read off the
    measurement's headers rather than its pixels — which is why the measurement
    can be the smallest picture the platform makes."""
    if measured.capture is None or measured.capture_size is None:
        # Never an unpinned crop instead: that is the crop of whatever capture
        # the platform holds when it arrives, which a resize in between makes
        # the wrong part of the screen, reported as a success.
        raise ToolError(
            "zoom needs a platform that names its screenshot captures, so the crop can be "
            "cut from the capture it was measured on, and this one does not; "
            "take a screenshot instead"
        )
    return measured.capture, _Size(*measured.capture_size)


def _stale(error: ToolError) -> bool:
    """A crop refused because the capture it named was replaced before it
    arrived. Read off the platform's error that :func:`_platform` wraps."""
    cause = error.__cause__
    # The 409 and the word together (found in review). The platform sends the
    # word only on a 409; on another status it is some other failure, and
    # measuring again would bury it under a race that did not happen.
    return isinstance(cause, APIError) and cause.status == 409 and cause.reason == "stale_capture"


def _zoomed(cut: ScreenshotInfo, capture: str) -> BetaScreenshotResult:
    # Checked rather than trusted: the one answer a pinned crop exists to rule
    # out is a crop of some other capture, so a picture that does not say it is
    # of this one is not handed to the model as though it were.
    if cut.capture != capture:
        raise ToolError("the zoom came back cut from another capture than the one measured")
    size = _png_size(cut.data)
    if size is None or not _fits(size):
        raise ToolError("the zoomed picture came back larger than the model can be shown")
    return _encoded(cut.data)


#: What a zoom says when every try found its capture replaced before the crop.
_KEPT_CHANGING = (
    "the screen was captured again between measuring it and cutting the zoom, "
    f"{_ZOOM_TRIES} times running; take a screenshot and zoom again"
)


_T = TypeVar("_T")


def _platform(call: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """A platform call, with its failure told to the model in this SDK's own
    words rather than as an exception's class name."""
    try:
        return call(*args, **kwargs)
    except ToolError:
        raise
    except Exception as error:
        raise ToolError(str(error) or type(error).__name__) from error


class MandalaComputerToolset(BetaAbstractComputerToolset20260801):
    """A Mandala computer as Claude's computer toolset, ``computer_toolset_20260801``.

    Every member is served. Screenshots are always fresh, because a cached
    frame can predate the action it is meant to show and the model then repeats
    the action. A screen larger than the model will take a picture of is
    photographed smaller, by the platform, and the model's points are scaled
    back up. ``wait`` is spread over the platform's 30-second waits, ``key``'s
    ``repeat`` is pressed one at a time, and long text is typed in pieces.

    ``zoom`` is cut from the capture it was measured on (OPL-5852). It measures
    a fresh capture of the screen, maps the model's region into that capture's
    own pixels, and asks for the crop of THAT capture by the name the platform
    gave it, so a display that changes size in between cannot put the crop in
    the wrong place. A capture replaced before the crop arrives is measured
    again, up to three times. It needs a platform that names its captures; on
    one that does not, a zoom is an error result and the model takes a
    screenshot instead.
    """

    def __init__(
        self,
        computer: Computer,
        *,
        configs: BetaComputerToolsetConfigsParam | None = None,
        confirm: BetaComputerConfirmCallable | None = None,
        tool_configs: BetaToolConfigs | None = None,
    ) -> None:
        super().__init__(configs=configs, confirm=confirm, tool_configs=tool_configs)
        #: The computer the actions run on. Not stopped or deleted by ``close``.
        self.computer = computer
        # Raises when the computer reports no resolution, which is a record that
        # cannot be driven yet — better here than on the model's first click.
        self._view = _View(computer.screen)

    def _shoot(self, width: int | None) -> bytes:
        if width is None:
            return _platform(self.computer.screenshot, fresh=True)
        return _platform(self.computer.screenshot, width, fresh=True, format="png")

    def screenshot(
        self, context: BetaToolsetCallContext, input: BetaComputerScreenshotInput
    ) -> BetaScreenshotResult:
        data = self._shoot(self._view.request)
        size = _measured(data, retaking=False)
        if not _fits(size):
            # A screen whose real size is not the one its computer reports.
            # Taken again at a size that fits, and asked for at that size from
            # then on.
            data = self._shoot(self._view.retake(size))
            size = _measured(data, retaking=True)
        self._view.accept(size)
        return _encoded(data)

    def zoom(
        self, context: BetaToolsetCallContext, input: BetaComputerZoomInput
    ) -> BetaScreenshotResult:
        box = self._view.zoom_box(input.region)
        for _ in range(_ZOOM_TRIES):
            # Measured on the smallest picture the platform makes: what is
            # needed is the capture's name and size, which are in the headers.
            measured = _platform(self.computer.screenshot_info, _MIN_WIDTH, fresh=True)
            capture, native = _pinned(measured)
            region, width, scale = self._view.crop(box, native)
            try:
                cut = _platform(
                    self.computer.screenshot_info,
                    width,
                    capture=capture,
                    region=region,
                    scale=scale,
                    format="png",
                )
            except ToolError as error:
                if _stale(error):
                    continue
                raise
            return _zoomed(cut, capture)
        raise ToolError(_KEPT_CHANGING)

    def cursor_position(
        self, context: BetaToolsetCallContext, input: BetaComputerCursorPositionInput
    ) -> BetaComputerCursorPositionResult:
        return self._view.cursor(_platform(self.computer.cursor_position))

    def mouse_move(
        self, context: BetaToolsetCallContext, input: BetaComputerMouseMoveInput
    ) -> None:
        (at,) = self._view.points(True, input.coordinate)
        assert at is not None
        _platform(self.computer.move, *at)

    def _click(self, name: str, coordinate: object, text: object) -> None:
        (at,) = self._view.points(False, coordinate)
        held = _chord(text)
        method = getattr(self.computer, name)
        x, y = at if at is not None else (None, None)
        _platform(method, x, y, *held)

    def left_click(
        self, context: BetaToolsetCallContext, input: BetaComputerLeftClickInput
    ) -> None:
        self._click("click", input.coordinate, input.text)

    def right_click(
        self, context: BetaToolsetCallContext, input: BetaComputerRightClickInput
    ) -> None:
        self._click("right_click", input.coordinate, input.text)

    def middle_click(
        self, context: BetaToolsetCallContext, input: BetaComputerMiddleClickInput
    ) -> None:
        self._click("middle_click", input.coordinate, input.text)

    def double_click(
        self, context: BetaToolsetCallContext, input: BetaComputerDoubleClickInput
    ) -> None:
        self._click("double_click", input.coordinate, input.text)

    def triple_click(
        self, context: BetaToolsetCallContext, input: BetaComputerTripleClickInput
    ) -> None:
        self._click("triple_click", input.coordinate, input.text)

    def left_click_drag(
        self, context: BetaToolsetCallContext, input: BetaComputerLeftClickDragInput
    ) -> None:
        start, end = self._view.points(True, input.start_coordinate, input.coordinate)
        assert start is not None and end is not None
        held = _chord(input.text)
        _platform(
            self.computer.drag, end[0], end[1], from_x=start[0], from_y=start[1], modifiers=held
        )

    def left_mouse_down(
        self, context: BetaToolsetCallContext, input: BetaComputerLeftMouseDownInput
    ) -> None:
        _platform(self.computer.mouse_down)

    def left_mouse_up(
        self, context: BetaToolsetCallContext, input: BetaComputerLeftMouseUpInput
    ) -> None:
        _platform(self.computer.mouse_up)

    def scroll(self, context: BetaToolsetCallContext, input: BetaComputerScrollInput) -> None:
        direction, amount = _scroll(input)
        (at,) = self._view.points(False, input.coordinate)
        held = _chord(input.text)
        x, y = at if at is not None else (None, None)
        _platform(self.computer.scroll, x, y, direction=direction, amount=amount, modifiers=held)

    def type(self, context: BetaToolsetCallContext, input: BetaComputerTypeInput) -> None:
        typed = 0
        for piece in _pieces(input.text):
            try:
                _platform(self.computer.type, piece)
            except ToolError as error:
                if typed == 0:
                    raise
                raise _typed_then(typed, len(input.text), error) from error
            typed += len(piece)

    def key(self, context: BetaToolsetCallContext, input: BetaComputerKeyInput) -> None:
        keys = _keys(input.text, "Return or ctrl+s")
        times = _repeat(input.repeat)
        for pressed in range(times):
            try:
                _platform(self.computer.key, *keys)
            except ToolError as error:
                if pressed == 0:
                    raise
                raise ToolError(f"pressed {pressed} of {times} times, then: {error}") from error

    def hold_key(self, context: BetaToolsetCallContext, input: BetaComputerHoldKeyInput) -> None:
        keys = _keys(input.text, "shift")
        seconds = _hold(input.duration)
        _platform(self.computer.hold_key, *keys, seconds=seconds)

    def wait(self, context: BetaToolsetCallContext, input: BetaComputerWaitInput) -> None:
        # Waited out on the platform, which counts as use of the computer.
        for piece in _waits(input.duration):
            _platform(self.computer.wait, piece)


async def _aplatform(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """:func:`_platform` for a coroutine. A cancellation is not an ``Exception``
    and is left to propagate, so the run sees itself cancelled rather than an
    action that failed."""
    try:
        return await call(*args, **kwargs)
    except ToolError:
        raise
    except Exception as error:
        raise ToolError(str(error) or type(error).__name__) from error


class AsyncMandalaComputerToolset(BetaAsyncAbstractComputerToolset20260801):
    """:class:`MandalaComputerToolset` over an :class:`~mandala_computer.AsyncComputer`."""

    def __init__(
        self,
        computer: AsyncComputer,
        *,
        configs: BetaComputerToolsetConfigsParam | None = None,
        confirm: BetaAsyncComputerConfirmCallable | None = None,
        tool_configs: BetaToolConfigs | None = None,
    ) -> None:
        super().__init__(configs=configs, confirm=confirm, tool_configs=tool_configs)
        #: The computer the actions run on. Not stopped or deleted by ``close``.
        self.computer = computer
        self._view = _View(computer.screen)

    async def _shoot(self, width: int | None) -> bytes:
        if width is None:
            data: bytes = await _aplatform(self.computer.screenshot, fresh=True)
        else:
            data = await _aplatform(self.computer.screenshot, width, fresh=True, format="png")
        return data

    async def screenshot(
        self, context: BetaToolsetCallContext, input: BetaComputerScreenshotInput
    ) -> BetaScreenshotResult:
        data = await self._shoot(self._view.request)
        size = _measured(data, retaking=False)
        if not _fits(size):
            data = await self._shoot(self._view.retake(size))
            size = _measured(data, retaking=True)
        self._view.accept(size)
        return _encoded(data)

    async def zoom(
        self, context: BetaToolsetCallContext, input: BetaComputerZoomInput
    ) -> BetaScreenshotResult:
        box = self._view.zoom_box(input.region)
        for _ in range(_ZOOM_TRIES):
            measured = await _aplatform(self.computer.screenshot_info, _MIN_WIDTH, fresh=True)
            capture, native = _pinned(measured)
            region, width, scale = self._view.crop(box, native)
            try:
                cut = await _aplatform(
                    self.computer.screenshot_info,
                    width,
                    capture=capture,
                    region=region,
                    scale=scale,
                    format="png",
                )
            except ToolError as error:
                if _stale(error):
                    continue
                raise
            return _zoomed(cut, capture)
        raise ToolError(_KEPT_CHANGING)

    async def cursor_position(
        self, context: BetaToolsetCallContext, input: BetaComputerCursorPositionInput
    ) -> BetaComputerCursorPositionResult:
        at: tuple[int, int] | None = await _aplatform(self.computer.cursor_position)
        return self._view.cursor(at)

    async def mouse_move(
        self, context: BetaToolsetCallContext, input: BetaComputerMouseMoveInput
    ) -> None:
        (at,) = self._view.points(True, input.coordinate)
        assert at is not None
        await _aplatform(self.computer.move, *at)

    async def _click(self, name: str, coordinate: object, text: object) -> None:
        (at,) = self._view.points(False, coordinate)
        held = _chord(text)
        method = getattr(self.computer, name)
        x, y = at if at is not None else (None, None)
        await _aplatform(method, x, y, *held)

    async def left_click(
        self, context: BetaToolsetCallContext, input: BetaComputerLeftClickInput
    ) -> None:
        await self._click("click", input.coordinate, input.text)

    async def right_click(
        self, context: BetaToolsetCallContext, input: BetaComputerRightClickInput
    ) -> None:
        await self._click("right_click", input.coordinate, input.text)

    async def middle_click(
        self, context: BetaToolsetCallContext, input: BetaComputerMiddleClickInput
    ) -> None:
        await self._click("middle_click", input.coordinate, input.text)

    async def double_click(
        self, context: BetaToolsetCallContext, input: BetaComputerDoubleClickInput
    ) -> None:
        await self._click("double_click", input.coordinate, input.text)

    async def triple_click(
        self, context: BetaToolsetCallContext, input: BetaComputerTripleClickInput
    ) -> None:
        await self._click("triple_click", input.coordinate, input.text)

    async def left_click_drag(
        self, context: BetaToolsetCallContext, input: BetaComputerLeftClickDragInput
    ) -> None:
        start, end = self._view.points(True, input.start_coordinate, input.coordinate)
        assert start is not None and end is not None
        held = _chord(input.text)
        await _aplatform(
            self.computer.drag, end[0], end[1], from_x=start[0], from_y=start[1], modifiers=held
        )

    async def left_mouse_down(
        self, context: BetaToolsetCallContext, input: BetaComputerLeftMouseDownInput
    ) -> None:
        await _aplatform(self.computer.mouse_down)

    async def left_mouse_up(
        self, context: BetaToolsetCallContext, input: BetaComputerLeftMouseUpInput
    ) -> None:
        await _aplatform(self.computer.mouse_up)

    async def scroll(self, context: BetaToolsetCallContext, input: BetaComputerScrollInput) -> None:
        direction, amount = _scroll(input)
        (at,) = self._view.points(False, input.coordinate)
        held = _chord(input.text)
        x, y = at if at is not None else (None, None)
        await _aplatform(
            self.computer.scroll, x, y, direction=direction, amount=amount, modifiers=held
        )

    async def type(self, context: BetaToolsetCallContext, input: BetaComputerTypeInput) -> None:
        typed = 0
        for piece in _pieces(input.text):
            try:
                await _aplatform(self.computer.type, piece)
            except ToolError as error:
                if typed == 0:
                    raise
                raise _typed_then(typed, len(input.text), error) from error
            typed += len(piece)

    async def key(self, context: BetaToolsetCallContext, input: BetaComputerKeyInput) -> None:
        keys = _keys(input.text, "Return or ctrl+s")
        times = _repeat(input.repeat)
        for pressed in range(times):
            try:
                await _aplatform(self.computer.key, *keys)
            except ToolError as error:
                if pressed == 0:
                    raise
                raise ToolError(f"pressed {pressed} of {times} times, then: {error}") from error

    async def hold_key(
        self, context: BetaToolsetCallContext, input: BetaComputerHoldKeyInput
    ) -> None:
        keys = _keys(input.text, "shift")
        seconds = _hold(input.duration)
        await _aplatform(self.computer.hold_key, *keys, seconds=seconds)

    async def wait(self, context: BetaToolsetCallContext, input: BetaComputerWaitInput) -> None:
        for piece in _waits(input.duration):
            await _aplatform(self.computer.wait, piece)
