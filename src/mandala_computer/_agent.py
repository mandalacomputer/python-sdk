"""The platform's own agent loop, as types.

``POST computers/:id/agent`` is not a call to a hypervisor — it is many of them,
interleaved with calls to a model API, running for minutes. So it answers with a
stream of steps rather than a result, and this file is the shape of that stream.

It runs on **your** Anthropic key, which the platform never stores: pass it as
``model_key`` and it travels on that one request as ``X-Model-Key``. Every step
is a model call plus a screenshot billed to that key, which is why ``max_steps``
bounds spending as much as it bounds the loop.

Everything here is built by :func:`to_agent_event` out of whatever a frame
carried, and never by asserting a shape. A frame this SDK does not model is
skipped rather than raised on: the platform is free to add event types, and a
client that fell over on the first unrecognised one would turn a
forward-compatible addition into an outage.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "AgentDone",
    "AgentEvent",
    "AgentFailed",
    "AgentResult",
    "AgentStep",
    "AgentStepEvent",
    "AgentText",
    "AgentUsage",
]


def _num(value: Any) -> int:
    """A count off the wire, or ``0``.

    Never raises, and the word is load-bearing: a malformed number in one field
    must not lose the run's result along with it — a step count that arrived as
    ``null`` is worth reporting as zero, and is not worth discarding the model's
    answer over.

    ``OverflowError`` is caught alongside the two obvious ones because JSON has
    no integer ceiling and Python's ``int`` has none either, so a 400-digit
    literal parses fine and only fails on the way to a ``float``. It is a
    malformed number like any other here.
    """
    try:
        n = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0
    # NaN and the infinities parse as floats and are not counts. int(nan) raises
    # and int(inf) raises too, so this is the guard as much as the filter.
    return int(n) if math.isfinite(n) else 0


def _text(value: Any) -> str:
    return "" if value is None else str(value)


@dataclass(frozen=True)
class AgentStep:
    """One action the loop took."""

    #: Which step this was, counting from 1.
    n: int
    #: The tool the model reached for, e.g. ``"computer"`` or ``"bash"``.
    tool: str = ""
    #: The action within it, e.g. ``"left_click"``. Empty for bash.
    action: str = ""
    #: What the platform did with it, in one line.
    detail: str = ""
    #: Set when the action was refused. The loop continues and the model adapts,
    #: so this is a step that did not work rather than a run that failed.
    error: str = ""
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d: Any, fallback_n: int) -> AgentStep:
        """This step, with ``fallback_n`` standing in for a number it did not give.

        "Did not give" is any unusable number, not only a missing key: steps
        count from 1, so a ``null``, a string, or a zero all mean the same thing
        — nothing to number this step by — and reading one as ``0`` would put
        the "0." in a caller's progress line that the fallback exists to
        prevent.
        """
        r = d if isinstance(d, Mapping) else {}
        n = _num(r.get("n"))
        return cls(
            n=n if n > 0 else fallback_n,
            tool=_text(r.get("tool")),
            action=_text(r.get("action")),
            detail=_text(r.get("detail")),
            error=_text(r.get("error")),
            raw=dict(r),
        )


@dataclass(frozen=True)
class AgentUsage:
    """What a run cost on your key.

    :attr:`input_tokens` includes the two cache halves, which are most of a long
    run — the rolling breakpoint means step ten's prompt is almost entirely
    cache reads. They are broken out as well, because they are priced
    differently and reconciling against an Anthropic bill needs to see them.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @classmethod
    def from_api(cls, d: Any) -> AgentUsage:
        r = d if isinstance(d, Mapping) else {}
        return cls(
            input_tokens=_num(r.get("input_tokens")),
            output_tokens=_num(r.get("output_tokens")),
            cache_read_tokens=_num(r.get("cache_read_tokens")),
            cache_write_tokens=_num(r.get("cache_write_tokens")),
        )


@dataclass(frozen=True)
class AgentResult:
    """How a run ended, and what it did."""

    #: How many steps it took.
    steps: int = 0
    #: Why it ended: ``end_turn``, ``max_steps``, ``rate_limited``, ``refusal``
    #: — or something added since, which is why this is a string and not an
    #: enum.
    stop: str = ""
    #: The model's closing text — its answer, or why it could not get there.
    text: str = ""
    usage: AgentUsage = field(default_factory=AgentUsage)
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def finished(self) -> bool:
        """True only for ``end_turn`` — the model deciding it was done.

        The check most callers actually want, and the reason it is here rather
        than left to everyone to write: treating every ending as success reports
        a run that hit its step cap as one that completed the task.
        """
        return self.stop == "end_turn"

    @classmethod
    def from_api(cls, d: Any) -> AgentResult:
        r = d if isinstance(d, Mapping) else {}
        return cls(
            steps=_num(r.get("steps")),
            stop=_text(r.get("stop")) or "unknown",
            text=_text(r.get("text")),
            usage=AgentUsage.from_api(r.get("usage")),
            raw=dict(r),
        )


@dataclass(frozen=True)
class AgentStepEvent:
    """The loop did something. Tell your user about it."""

    step: AgentStep


@dataclass(frozen=True)
class AgentText:
    """The model said something on its way through."""

    text: str


@dataclass(frozen=True)
class AgentDone:
    """The run ended, however it ended. Carries the result."""

    result: AgentResult


@dataclass(frozen=True)
class AgentFailed:
    """The run went wrong, mid-stream.

    Distinct from a run that ended unfinished: ``max_steps`` and
    ``rate_limited`` arrive as an :class:`AgentDone` carrying a result, because
    the steps already taken are real and what they did to the desktop stands.
    This is the platform saying the run itself could not continue.
    """

    error: str
    #: The HTTP status the failure would have had, or ``0`` where the platform
    #: did not name one. Carried so a 401 arriving mid-run can be raised as the
    #: same :class:`~mandala_computer.AuthenticationError` a 401 anywhere else
    #: raises, rather than as something a caller's handler cannot classify.
    status: int = 0
    #: What the run had already spent on your key when it went wrong. A failure
    #: at step eight has been billed for eight steps whether or not anything is
    #: told about it, and this is the only place that number is ever reported —
    #: the platform meters nothing on your model key, so there is no invoice to
    #: reconcile it against later.
    usage: AgentUsage = field(default_factory=AgentUsage)
    #: What the run had already done to the desktop. Those actions stand: the
    #: failure stopped the loop, it did not undo the clicks. Left empty where
    #: the platform could not say — its own last-resort handler reports the
    #: error and the status alone — so this being empty means "not reported"
    #: rather than "nothing happened".
    steps: tuple[AgentStep, ...] = ()


#: One event out of :meth:`~mandala_computer.Computer.agent_stream`. Match on it
#: with ``isinstance`` — the four are a closed set as far as this SDK models the
#: stream, and anything else the platform sends is skipped before it gets here.
AgentEvent = AgentStepEvent | AgentText | AgentDone | AgentFailed


def to_agent_event(event: str, data: Any, step_count: int) -> AgentEvent | None:
    """One frame as an event, or ``None`` for a frame this SDK does not model."""
    if event == "step":
        return AgentStepEvent(AgentStep.from_api(data, step_count + 1))
    if event == "text":
        text = _text(data.get("text") if isinstance(data, Mapping) else data)
        # A frame that said nothing is skipped like any other frame this SDK
        # cannot read. The README's loop prints what it is handed, and an empty
        # AgentText is a blank line in a caller's output standing for a payload
        # whose shape we did not recognise.
        return AgentText(text) if text else None
    if event == "done":
        return AgentDone(AgentResult.from_api(data))
    if event == "error":
        r = data if isinstance(data, Mapping) else {}
        taken = r.get("steps")
        return AgentFailed(
            _text(r.get("error")) or "the run failed",
            _num(r.get("status")),
            AgentUsage.from_api(r.get("usage")),
            tuple(
                AgentStep.from_api(step, i + 1)
                for i, step in enumerate(taken if isinstance(taken, list) else ())
            ),
        )
    return None
