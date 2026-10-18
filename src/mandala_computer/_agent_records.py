"""What the platform keeps of agent runs: threads, and background runs.

A run on :meth:`~mandala_computer.Computer.agent` is kept as one exchange — the
prompt, and how the run answered it — in a THREAD on that computer, unless it
was sent with ``store=False``. Its id comes back as
:attr:`~mandala_computer.AgentResult.thread_id`, and sending it as
``thread_id=`` on the next run continues the conversation.
``client.agent_threads`` lists, reads and deletes them.

A BACKGROUND RUN is the same loop started and walked away from:
``client.agent_runs.start`` answers once the run holds the computer, and the
run goes on without the request. It is read back by id, polled to its end with
``client.agent_runs.wait``, or cancelled.

Read like the SDK's other records rather than like the stream in ``_agent``:
strict on the fields a caller's loop decides on — an id it pages or polls by, a
status a wait ends on, a ``has_more`` a walk continues on — and permissive on
the rest, which stays in ``raw`` as it arrived.
"""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ._agent import AgentResult, AgentStep, _usd
from ._exceptions import MandalaError, top_up_link

__all__ = [
    "AgentRun",
    "AgentRunError",
    "AgentRunPage",
    "AgentRunStatus",
    "AgentThread",
    "AgentThreadPage",
    "AgentThreadSummary",
    "AgentThreadTurn",
]

#: A background run's :attr:`~AgentRun.status`, and the ``status=`` filter of
#: ``client.agent_runs.list``. ``running`` while it goes; the other four are
#: final (see :data:`AGENT_RUN_ENDED`).
AgentRunStatus = Literal["running", "completed", "failed", "cancelled", "interrupted"]

#: The statuses a background run ends in. Nothing more happens to one of these.
AGENT_RUN_ENDED = frozenset({"completed", "failed", "cancelled", "interrupted"})


def _id(d: Mapping[str, Any], key: str, where: str) -> str:
    """An id a caller pages or polls by, or :class:`MandalaError`.

    Strict, because a missing one is not a smaller answer: sent back as
    ``before=`` it would be refused, and polled by it would read nothing.
    """
    value = d.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise MandalaError(f"{where}: no usable {key}")
    return str.__str__(value)


def _text(value: Any) -> str:
    """A string field, with anything that is not one read as empty."""
    return str.__str__(value) if isinstance(value, str) else ""


def _opt_text(value: Any) -> str | None:
    """An optional string field: ``None`` unless it is a non-empty string."""
    return str.__str__(value) if isinstance(value, str) and value else None


def _count(value: Any) -> int:
    """A non-negative whole number, or ``0`` where there is none to read."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _opt_count(value: Any) -> int | None:
    """:func:`_count` for a field the platform may leave out: ``None`` then."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _thread_ref(d: Mapping[str, Any], where: str) -> str | None:
    """A run's ``thread_id``: a string, or ``null`` before there is a thread.

    Anything else is refused rather than read as ``null``: that would say the
    run kept no conversation when the platform said it did.
    """
    value = d.get("thread_id")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise MandalaError(f"{where}: thread_id is neither a string nor null")
    return str.__str__(value)


def _rows(d: Mapping[str, Any], key: str, where: str) -> builtins.list[Mapping[str, Any]]:
    """A page's rows, every one an object, or :class:`MandalaError`.

    A row that cannot be read is never dropped: a page one row short reads
    exactly like a thread or a run that does not exist.
    """
    rows = d.get(key)
    if not isinstance(rows, builtins.list):
        raise MandalaError(f"{where}: expected a list of {key}")
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise MandalaError(f"{where}: {key} {i} is not an object")
    return rows


def _has_more(d: Mapping[str, Any], rows: builtins.list[Any], where: str) -> bool:
    """A page's ``has_more``: a real boolean, or :class:`MandalaError`.

    Strict, because a walk runs on it: ``false`` read where the platform meant
    ``true`` ends a walk with pages still owed. And ``true`` on a page with no
    rows is refused too, since the next page is asked for by the last row's id
    and there is none to ask by.
    """
    value = d.get("has_more")
    if not isinstance(value, bool):
        raise MandalaError(f"{where}: has_more is missing or not a boolean")
    if value and not rows:
        raise MandalaError(f"{where}: has_more on a page with no rows to continue from")
    return value


def _steps(d: Mapping[str, Any], where: str) -> tuple[AgentStep, ...]:
    """A run's ``steps_taken``: absent or ``null`` is none, a list is read, and
    anything else is refused — it is the record of what was done to the desktop."""
    value = d.get("steps_taken")
    if value is None:
        return ()
    if not isinstance(value, builtins.list):
        raise MandalaError(f"{where}: steps_taken is not a list")
    return tuple(AgentStep.from_api(step, i + 1) for i, step in enumerate(value))


# --- threads -------------------------------------------------------------------


@dataclass(frozen=True)
class AgentThreadTurn:
    """One turn of a thread: a prompt, or how a run answered it."""

    #: ``"user"`` for a prompt, ``"assistant"`` for a run's answer.
    role: str
    #: The prompt, or the run's closing text. The platform keeps at most 32,000
    #: characters of each.
    text: str
    created_at: str
    #: Assistant turns: how the run ended — any
    #: :attr:`~mandala_computer.AgentResult.stop`, or ``error`` for a run that
    #: failed, ``aborted`` for one stopped part way, ``running`` while its run
    #: is still going, and ``interrupted`` for one the platform restarted under.
    #: ``running``, ``aborted`` and ``interrupted`` are runs that may have done
    #: more to the desktop than they reported. ``None`` on a user turn.
    stop: str | None = None
    #: Assistant turns of a failed run: why it failed.
    error: str | None = None
    #: Assistant turns: how many actions the run took.
    steps: int | None = None
    #: Assistant turns of a funded run: what it cost, in USD.
    cost_usd: float | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, d: Any, where: str = "agent thread turn") -> AgentThreadTurn:
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        role = d.get("role")
        if not isinstance(role, str) or not role:
            raise MandalaError(f"{where}: no usable role")
        return cls(
            role=str.__str__(role),
            text=_text(d.get("text")),
            created_at=_text(d.get("created_at")),
            stop=_opt_text(d.get("stop")),
            error=_opt_text(d.get("error")),
            steps=_opt_count(d.get("steps")),
            cost_usd=_usd(d.get("cost_usd")),
            raw=dict(d),
        )


@dataclass(frozen=True)
class AgentThreadSummary:
    """One thread as ``client.agent_threads.list`` shows it: who it belongs to
    and how long it is, without its turns."""

    #: ``thr_`` and 24 hex characters: what ``thread_id=`` takes.
    id: str
    #: The computer its runs drove. A thread is continued only there.
    computer_id: str
    #: The first line of the first prompt kept in it, at most 80 characters.
    title: str
    #: How many turns it holds: a prompt and an answer per run, plus any
    #: ``messages`` it was started with. The platform keeps at most 100.
    turns: int
    created_at: str
    #: When a run last used it.
    updated_at: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def _fields(cls, d: Any, where: str) -> dict[str, Any]:
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        return {
            "id": _id(d, "id", where),
            "computer_id": _text(d.get("computer_id")),
            "title": _text(d.get("title")),
            "turns": _count(d.get("turns")),
            "created_at": _text(d.get("created_at")),
            "updated_at": _text(d.get("updated_at")),
            "raw": dict(d),
        }

    @classmethod
    def from_api(cls, d: Any, where: str = "agent thread") -> AgentThreadSummary:
        return cls(**cls._fields(d, where))


@dataclass(frozen=True)
class AgentThread(AgentThreadSummary):
    """One thread and every turn in it, from ``client.agent_threads.get``."""

    #: Every turn, oldest first: each ``user`` turn a prompt, each
    #: ``assistant`` turn how the run answered it.
    messages: tuple[AgentThreadTurn, ...] = field(default=(), kw_only=True)

    @classmethod
    def from_api(cls, d: Any, where: str = "agent thread") -> AgentThread:
        fields = cls._fields(d, where)
        turns = _rows(d, "messages", where)
        return cls(
            messages=tuple(
                AgentThreadTurn.from_api(turn, f"{where}: message {i}")
                for i, turn in enumerate(turns)
            ),
            **fields,
        )


@dataclass(frozen=True)
class AgentThreadPage:
    """One page of ``client.agent_threads.list``, most recently used first."""

    threads: builtins.list[AgentThreadSummary]
    #: Whether there are older threads past this page.
    has_more: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def next_before(self) -> str | None:
        """What to pass as ``before=`` for the next page — the last thread's
        id — or ``None`` on the last page."""
        return self.threads[-1].id if self.has_more and self.threads else None

    @classmethod
    def from_api(cls, d: Any, where: str = "GET agent/threads") -> AgentThreadPage:
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        rows = _rows(d, "threads", where)
        return cls(
            threads=[
                AgentThreadSummary.from_api(row, f"thread {i} from {where}")
                for i, row in enumerate(rows)
            ],
            has_more=_has_more(d, rows, where),
            raw=dict(d),
        )


# --- background runs -----------------------------------------------------------


@dataclass(frozen=True)
class AgentRunError:
    """Why a background run failed, or how it was interrupted."""

    #: What went wrong, in a sentence.
    error: str
    #: The status the agent route would have answered it with, or ``0`` where
    #: none was given. An ``interrupted`` run has 503 (the platform restarted
    #: while it was going) or 500 (it stopped without its ending recorded).
    status: int = 0
    #: The one-word classification, where there is one — read it the way
    #: :attr:`~mandala_computer.APIError.reason` is read anywhere else.
    reason: str | None = None
    #: Where to add AI credits, as an absolute ``http(s)`` link, when a funded
    #: run failed for want of them. The platform sends a path, resolved against
    #: the client's base URL; the value as sent is in :attr:`raw`.
    top_up_url: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(
        cls, d: Any, where: str = "agent run error", *, base_url: str | None = None
    ) -> AgentRunError:
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: error is neither null nor an object")
        return cls(
            error=_text(d.get("error")) or "the run failed",
            status=_count(d.get("status")),
            reason=_opt_text(d.get("reason")),
            top_up_url=top_up_link(d.get("top_up_url"), base_url),
            raw=dict(d),
        )


@dataclass(frozen=True)
class AgentRun:
    """One background run: what it was asked, where it has got to, and — once
    it has ended — how.

    From ``client.agent_runs`` and
    :meth:`~mandala_computer.Computer.start_agent_run`. Only :attr:`finished`
    means the task was done: ``completed`` with
    :attr:`~mandala_computer.AgentResult.stop` ``end_turn``. Every other ending
    leaves real work on the desktop — :attr:`steps_taken` says what — and
    ``interrupted`` and ``cancelled`` runs may have done more than they
    reported, so look at the screen before starting another.
    """

    #: ``run_`` and 24 hex characters.
    id: str
    computer_id: str
    #: ``running`` while it goes; ``completed``, ``failed``, ``cancelled`` or
    #: ``interrupted`` once it has ended (see :attr:`ended`). Kept as the
    #: platform sent it, so a status added later is a status, not a malformed
    #: answer.
    status: str
    #: The prompt, at most 32,000 characters of it.
    prompt: str
    #: The model the run was started with.
    model: str
    max_steps: int
    #: How many actions it has taken so far.
    steps: int
    created_at: str
    #: When it last took a step or changed status.
    updated_at: str
    #: The thread it is kept in: ``None`` until a new thread has something in
    #: it, and after the thread is deleted. Continue the conversation with
    #: ``thread_id=`` on the next run.
    thread_id: str | None = None
    #: When it ended; ``None`` while it is running.
    ended_at: str | None = None
    #: A funded run's charge so far, in USD. ``None`` on a run with your own
    #: ``model_key``.
    cost_usd: float | None = None
    #: How a ``completed`` run ended; ``None`` before then and on any other
    #: ending. Its ``thread_id`` is always ``None``: the run's own
    #: :attr:`thread_id` is the one to use.
    result: AgentResult | None = None
    #: Why a ``failed`` or ``interrupted`` run stopped; ``None`` otherwise.
    error: AgentRunError | None = None
    #: Every action so far, in order. Empty on a run from
    #: ``client.agent_runs.list``, which leaves them out: read the run for them.
    steps_taken: tuple[AgentStep, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def ended(self) -> bool:
        """Whether the run is over: ``completed``, ``failed``, ``cancelled`` or
        ``interrupted`` — or a status this version does not know, on a run the
        platform says has ended."""
        if self.status in AGENT_RUN_ENDED:
            return True
        return self.status != "running" and self.ended_at is not None

    @property
    def finished(self) -> bool:
        """True only for a ``completed`` run whose result is ``end_turn`` — the
        model deciding the task was done. The check most callers want, for the
        reason :attr:`~mandala_computer.AgentResult.finished` gives."""
        return self.status == "completed" and self.result is not None and self.result.finished

    @classmethod
    def from_api(cls, d: Any, where: str = "agent run", *, base_url: str | None = None) -> AgentRun:
        """Strict on the id a wait polls by and the status it ends on, and on
        the shape of the result, the error and the steps: a run whose ending was
        silently dropped would read as one that ended with nothing to say."""
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        status = d.get("status")
        if not isinstance(status, str) or not status:
            raise MandalaError(f"{where}: no usable status")
        result = d.get("result")
        if result is not None and not isinstance(result, Mapping):
            raise MandalaError(f"{where}: result is neither null nor an object")
        error = d.get("error")
        return cls(
            id=_id(d, "id", where),
            computer_id=_text(d.get("computer_id")),
            status=str.__str__(status),
            prompt=_text(d.get("prompt")),
            model=_text(d.get("model")),
            max_steps=_count(d.get("max_steps")),
            steps=_count(d.get("steps")),
            created_at=_text(d.get("created_at")),
            updated_at=_text(d.get("updated_at")),
            thread_id=_thread_ref(d, where),
            ended_at=_opt_text(d.get("ended_at")),
            cost_usd=_usd(d.get("cost_usd")),
            result=None if result is None else AgentResult.from_api(result),
            error=None
            if error is None
            else AgentRunError.from_api(error, f"{where}: error", base_url=base_url),
            steps_taken=_steps(d, where),
            raw=dict(d),
        )


@dataclass(frozen=True)
class AgentRunPage:
    """One page of ``client.agent_runs.list``, newest first."""

    #: Each run without its :attr:`~AgentRun.steps_taken`; read one for them.
    runs: builtins.list[AgentRun]
    #: Whether there are older runs past this page.
    has_more: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def next_before(self) -> str | None:
        """What to pass as ``before=`` for the next page — the last run's id —
        or ``None`` on the last page."""
        return self.runs[-1].id if self.has_more and self.runs else None

    @classmethod
    def from_api(
        cls, d: Any, where: str = "GET agent/runs", *, base_url: str | None = None
    ) -> AgentRunPage:
        if not isinstance(d, Mapping):
            raise MandalaError(f"{where}: not an object")
        rows = _rows(d, "runs", where)
        return cls(
            runs=[
                AgentRun.from_api(row, f"run {i} from {where}", base_url=base_url)
                for i, row in enumerate(rows)
            ],
            has_more=_has_more(d, rows, where),
            raw=dict(d),
        )
