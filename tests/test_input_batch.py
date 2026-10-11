"""OpenAI input batches: ``POST computers/:id/input`` with a JSON array.

What an OpenAI ``computer_call`` hands a caller, forwarded as one request. The
contract that matters here is the split between the two ways a batch fails:

- refused before any action ran: the ordinary error for its status, and
  nothing happened
- stopped part-way: :class:`~mandala_computer.InputBatchError` carrying the
  batch as far as it went, never called worth sending again, because the
  actions before the failure ran

and that nothing in this SDK ever sends a batch twice. Both halves of the
client, because :mod:`tests.test_parity` proves only that the signatures match.
"""

from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
INPUT = f"{BASE}/computers/vm-1/input"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux"}
WINDOW = {"id": "0x2600003", "title": "T", "class": "Firefox"}

EVERY_ACTION: list[Any] = [
    {"type": "click", "x": 10, "y": 20, "button": "left", "keys": ["shift"]},
    {"type": "click", "x": 10.5, "y": 0, "button": "wheel", "keys": None},
    {"type": "double_click", "x": 10, "y": 20},
    {"type": "move", "x": 30, "y": 40, "keys": []},
    {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}], "keys": ["alt"]},
    {"type": "scroll", "x": 5, "y": 6, "scroll_x": 0, "scroll_y": -300},
    {"type": "keypress", "keys": ["ctrl", "c"]},
    {"type": "type", "text": "Café"},
    {"type": "wait"},
    {"type": "screenshot"},
]


def done(n: int, **extra: Any) -> dict[str, Any]:
    return {"ok": True, "completed": n, "results": [{"ok": True} for _ in range(n)], **extra}


def stopped(completed: int, at: int) -> dict[str, Any]:
    return {
        "ok": False,
        "completed": completed,
        "failed_index": at,
        "error": f"batch stopped at action {at}: guest refused; inspect the desktop",
        "results": [{"ok": True} for _ in range(completed)],
    }


@pytest.fixture
def computer() -> mc.Computer:
    return mc.Computer(mc.Client("gck_test", base_url=BASE)._t, COMPUTER)


# --- what goes out ------------------------------------------------------------


@respx.mock
def test_a_batch_goes_out_as_one_array_in_openais_own_words(computer: mc.Computer) -> None:
    route = respx.post(INPUT).mock(httpx.Response(200, json=done(len(EVERY_ACTION))))
    result = computer.input_batch(EVERY_ACTION)
    sent = json.loads(route.calls.last.request.content)
    assert sent == [
        {"type": "click", "x": 10, "y": 20, "button": "left", "keys": ["shift"]},
        # ``keys: None`` is OpenAI's "no keys", left off the wire.
        {"type": "click", "x": 10.5, "y": 0, "button": "wheel"},
        {"type": "double_click", "x": 10, "y": 20},
        {"type": "move", "x": 30, "y": 40, "keys": []},
        {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}], "keys": ["alt"]},
        {"type": "scroll", "x": 5, "y": 6, "scroll_x": 0, "scroll_y": -300},
        {"type": "keypress", "keys": ["ctrl", "c"]},
        {"type": "type", "text": "Café"},
        {"type": "wait"},
        {"type": "screenshot"},
    ]
    assert route.calls.last.request.url.params.multi_items() == []
    assert result.ok and result.completed == len(EVERY_ACTION)
    assert len(result.results) == len(EVERY_ACTION) and result.context is None


@respx.mock
def test_a_batch_gets_the_long_deadline_whatever_the_client_default(
    computer: mc.Computer,
) -> None:
    """The platform's proxy allows a batch 93 seconds; a 60-second client
    default would abandon a batch still running."""
    route = respx.post(INPUT).mock(httpx.Response(200, json=done(1)))
    computer.input_batch([{"type": "wait"}])
    assert route.calls.last.request.extensions["timeout"]["read"] >= 108


@respx.mock
@pytest.mark.parametrize(
    ("actions", "match"),
    [
        ("click", "must be a list"),
        ({"type": "wait"}, "must be a list"),
        ([], "1 to 32 actions"),
        ([{"type": "wait"}] * 33, "1 to 32 actions"),
        (["wait"], r"actions\[0\] must be an object"),
        ([{"type": "left_click", "x": 1, "y": 1}], r"actions\[0\].type must be one of"),
        ([{"x": 1}], r"actions\[0\].type must be one of"),
        ([{"type": "wait", "x": 0}], "OpenAI wait does not take 'x'"),
        ([{"type": "move", "x": 1, "y": 1, "button": "left"}], "does not take 'button'"),
        ([{"type": "keypress", "keys": ["a"], "x": None}], "does not take 'x'"),
        ([{"type": "move", "x": 1}], r"actions\[0\].y must be a number"),
        ([{"type": "move", "x": True, "y": 1}], r"actions\[0\].x must be a number"),
        ([{"type": "move", "x": math.nan, "y": 1}], "finite"),
        ([{"type": "move", "x": "1", "y": 1}], "must be a number"),
        ([{"type": "click", "x": 1, "y": 1}], "button must be one of"),
        ([{"type": "click", "x": 1, "y": 1, "button": "middle"}], "button must be one of"),
        ([{"type": "drag", "path": [{"x": 1, "y": 1}]}], "at least two"),
        ([{"type": "drag", "path": "ab"}], "at least two"),
        ([{"type": "drag", "path": [{"x": 1, "y": 1}, {"x": 2}]}], r"path\[1\] must be"),
        ([{"type": "drag", "path": [{"x": 1, "y": 1}, {"x": 2, "y": 2, "z": 0}]}], "path"),
        ([{"type": "scroll", "x": 1, "y": 1, "scroll_x": 0}], "scroll_y must be a number"),
        ([{"type": "type", "text": ""}], "nonempty string"),
        ([{"type": "type"}], "nonempty string"),
        ([{"type": "keypress"}], "keys must be a list"),
        ([{"type": "keypress", "keys": []}], "at least one key"),
        ([{"type": "keypress", "keys": "ctrl+c"}], "keys must be a list"),
        ([{"type": "move", "x": 1, "y": 1, "keys": [1]}], "keys must be a list"),
    ],
)
def test_a_malformed_batch_is_refused_before_anything_is_sent(
    computer: mc.Computer, actions: Any, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        computer.input_batch(actions)
    assert not respx.calls


@respx.mock
def test_context_is_asked_for_and_read(computer: mc.Computer) -> None:
    route = respx.post(INPUT).mock(
        httpx.Response(200, json=done(1, context={"windows": [WINDOW], "focused": WINDOW}))
    )
    result = computer.input_batch([{"type": "wait"}], context=True)
    assert route.calls.last.request.url.params["context"] == "1"
    assert result.context is not None and result.context.focused is not None
    assert result.context.focused.title == "T"


@respx.mock
def test_context_that_could_not_be_read_says_why(computer: mc.Computer) -> None:
    respx.post(INPUT).mock(httpx.Response(200, json=done(1, context_error="no desktop session")))
    result = computer.input_batch([{"type": "wait"}], context=True)
    assert result.context == mc.InputContext(windows=None, focused=None, error="no desktop session")


@respx.mock
def test_context_asked_for_and_not_answered_is_refused(computer: mc.Computer) -> None:
    respx.post(INPUT).mock(httpx.Response(200, json=done(1)))
    with pytest.raises(mc.MandalaError, match="neither context nor context_error"):
        computer.input_batch([{"type": "wait"}], context=True)


@respx.mock
def test_a_typed_action_reports_its_mechanism(computer: mc.Computer) -> None:
    respx.post(INPUT).mock(
        httpx.Response(
            200,
            json={"ok": True, "completed": 1, "results": [{"ok": True, "mechanism": "unicode"}]},
        )
    )
    result = computer.input_batch([{"type": "type", "text": "東京"}])
    assert result.results[0].mechanism == "unicode"


# --- the two ways a batch fails -----------------------------------------------


@respx.mock
@pytest.mark.parametrize("status", [400, 409, 502, 503])
def test_a_batch_that_stopped_part_way_is_an_input_batch_error(
    computer: mc.Computer, status: int
) -> None:
    """Whatever the status of what stopped it: a 409 or a 503's own class would
    call sending the batch again worth trying, and the actions before it ran."""
    respx.post(INPUT).mock(httpx.Response(status, json=stopped(2, 2)))
    with pytest.raises(mc.InputBatchError) as raised:
        computer.input_batch([{"type": "wait"}] * 4)
    error = raised.value
    assert error.status == status and error.method == "POST"
    assert not isinstance(error, (mc.ConflictError, mc.UnavailableError))
    assert error.result.ok is False
    assert error.result.completed == 2 and error.result.failed_index == 2
    assert error.result.error is not None and "inspect the desktop" in error.result.error
    assert len(error.result.results) == 2
    assert mc.is_transient(error) is False


@respx.mock
def test_a_stopped_batch_is_never_sent_again(computer: mc.Computer) -> None:
    """Not by the transport's read retries either, which never touch a POST."""
    client = mc.Client("gck_test", base_url=BASE, retries={"idempotent": 3})
    c = mc.Computer(client._t, COMPUTER)
    route = respx.post(INPUT).mock(httpx.Response(503, json=stopped(0, 0)))
    with pytest.raises(mc.InputBatchError):
        c.input_batch([{"type": "wait"}])
    assert route.call_count == 1


@respx.mock
@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (400, mc.APIError),
        (409, mc.ConflictError),
        (503, mc.UnavailableError),
        (404, mc.NotFoundError),
    ],
)
def test_a_batch_refused_before_any_action_ran_is_the_ordinary_error(
    computer: mc.Computer, status: int, cls: type[mc.APIError]
) -> None:
    """A static refusal is the ordinary error body, with no ``completed``."""
    respx.post(INPUT).mock(
        httpx.Response(status, json={"error": "computer is not running", "reason": "unavailable"})
    )
    with pytest.raises(cls) as raised:
        computer.input_batch([{"type": "wait"}])
    assert not isinstance(raised.value, mc.InputBatchError)
    assert raised.value.status == status


@respx.mock
def test_a_success_status_that_says_the_batch_stopped_is_still_an_error(
    computer: mc.Computer,
) -> None:
    respx.post(INPUT).mock(httpx.Response(200, json=stopped(1, 1)))
    with pytest.raises(mc.InputBatchError) as raised:
        computer.input_batch([{"type": "wait"}] * 3)
    assert raised.value.status == 200
    assert raised.value.result.failed_index == 1


@respx.mock
def test_a_success_that_ran_fewer_actions_than_sent_is_an_error(computer: mc.Computer) -> None:
    """So a returned result always ran in full."""
    respx.post(INPUT).mock(httpx.Response(200, json=done(2)))
    with pytest.raises(mc.InputBatchError, match="stopped after 2 of 3") as raised:
        computer.input_batch([{"type": "wait"}] * 3)
    assert raised.value.result.ok is False and raised.value.result.completed == 2


@respx.mock
@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"completed": 1, "results": [{"ok": True}]}, "ok must be"),
        ({"ok": True, "completed": "1", "results": [{"ok": True}]}, "completed must be"),
        ({"ok": True, "completed": -1, "results": []}, "completed must be"),
        ({"ok": True, "completed": 1, "results": {}}, "results must be an array"),
        ({"ok": True, "completed": 1, "results": []}, "one result per completed"),
        ({"ok": True, "completed": 1, "results": [{}]}, r"results\[0\].ok"),
        (
            {"ok": True, "completed": 1, "results": [{"ok": True, "mechanism": 3}]},
            "mechanism",
        ),
        ({**stopped(0, 0), "failed_index": None}, "present on a batch that stopped"),
        ({**stopped(0, 0), "failed_index": -1}, "failed_index must be"),
        ({**stopped(0, 0), "error": 5}, "error must be"),
    ],
)
def test_an_answer_that_is_not_a_batch_result_is_refused(
    computer: mc.Computer, body: dict[str, Any], match: str
) -> None:
    respx.post(INPUT).mock(httpx.Response(200, json=body))
    with pytest.raises(mc.MandalaError, match=match):
        computer.input_batch([{"type": "wait"}])


# --- the async half -----------------------------------------------------------


@respx.mock
async def test_the_async_half_sends_the_same_batch_and_reads_the_same_answer() -> None:
    route = respx.post(INPUT).mock(
        httpx.Response(
            200,
            json=done(len(EVERY_ACTION), context={"windows": [WINDOW], "focused": None}),
        )
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        result = await c.input_batch(EVERY_ACTION, context=True)
    assert len(json.loads(route.calls.last.request.content)) == len(EVERY_ACTION)
    assert route.calls.last.request.extensions["timeout"]["read"] >= 108
    assert result.completed == len(EVERY_ACTION)
    assert result.context is not None and result.context.focused is None


@respx.mock
async def test_the_async_half_raises_a_stopped_batch_the_same_way() -> None:
    respx.post(INPUT).mock(
        side_effect=[
            httpx.Response(409, json=stopped(1, 1)),
            httpx.Response(409, json={"error": "busy", "reason": "contention"}),
        ]
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.InputBatchError) as stopped_error:
            await c.input_batch([{"type": "wait"}] * 2)
        with pytest.raises(mc.ConflictError) as refused:
            await c.input_batch([{"type": "wait"}])
    assert stopped_error.value.result.failed_index == 1
    assert mc.is_transient(stopped_error.value) is False
    assert not isinstance(refused.value, mc.InputBatchError)
    assert mc.is_transient(refused.value) is True


async def test_the_async_half_refuses_a_malformed_batch_before_sending() -> None:
    with respx.mock:
        async with mc.AsyncClient("gck_test", base_url=BASE) as client:
            c = mc.AsyncComputer(client._t, COMPUTER)
            with pytest.raises(ValueError, match="1 to 32 actions"):
                await c.input_batch([])
        assert not respx.calls


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(
            409, content=b'{"ok": false, "compl', headers={"Content-Type": "application/json"}
        ),
        httpx.Response(409, content=b""),
        httpx.Response(
            409, content=b"<html>conflict</html>", headers={"Content-Type": "text/html"}
        ),
        httpx.Response(409, json={"reason": "contention"}),
        httpx.Response(503, content=b"upstream unavailable"),
        httpx.Response(400, json=["not", "an", "error"]),
        httpx.Response(
            409,
            json={
                "ok": False,
                "failed_index": 1,
                "error": "batch stopped at action 1",
                "results": [{"ok": True}],
            },
        ),
        httpx.Response(409, json={"error": "stopped", "results": []}),
    ],
    ids=[
        "truncated",
        "empty",
        "proxy-page",
        "no-sentence",
        "503-text",
        "400-array",
        "result-without-completed",
        "results-beside-an-error",
    ],
)
def test_a_batch_answer_that_cannot_be_read_is_not_called_safe_to_send_again(
    computer: mc.Computer, response: httpx.Response
) -> None:
    """At a status an execution failure arrives with, a body that is neither the
    ordinary error nor the batch's result cannot say whether actions ran. Its
    class is kept; the replay answer is not."""
    respx.post(INPUT).mock(response)
    with pytest.raises(mc.APIError) as raised:
        computer.input_batch([{"type": "wait"}] * 2)
    assert not isinstance(raised.value, mc.InputBatchError)
    assert raised.value.status == response.status_code
    assert mc.is_transient(raised.value) is False


@respx.mock
def test_an_unreadable_answer_at_a_status_no_batch_failure_has_keeps_its_answer(
    computer: mc.Computer,
) -> None:
    """A 429 is refused before anything runs; an edge's page for it stays a
    rate limit worth waiting out."""
    respx.post(INPUT).mock(httpx.Response(429, content=b"<html>slow down</html>"))
    with pytest.raises(mc.RateLimitError) as raised:
        computer.input_batch([{"type": "wait"}])
    assert mc.is_transient(raised.value) is True


@respx.mock
async def test_the_async_half_reads_a_partial_result_without_completed_the_same_way() -> None:
    respx.post(INPUT).mock(
        httpx.Response(409, json={"ok": False, "failed_index": 0, "error": "stopped"})
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.ConflictError) as raised:
            await c.input_batch([{"type": "wait"}])
    assert mc.is_transient(raised.value) is False


@respx.mock
async def test_the_async_half_reads_an_unreadable_answer_the_same_way() -> None:
    respx.post(INPUT).mock(
        httpx.Response(
            409, content=b'{"ok": false, "compl', headers={"Content-Type": "application/json"}
        )
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.ConflictError) as raised:
            await c.input_batch([{"type": "wait"}])
    assert mc.is_transient(raised.value) is False


# --- a success that says otherwise ---------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    "body",
    [
        {"ok": True, "completed": 1, "results": [{"ok": False}]},
        {**done(1), "failed_index": 0},
        {**done(1), "error": "batch stopped at action 0"},
    ],
    ids=["an-action-not-ok", "a-failed-index", "an-error"],
)
def test_a_success_beside_evidence_of_a_failure_is_raised(
    computer: mc.Computer, body: dict[str, Any]
) -> None:
    """``ok: true`` with an action that did not run, a failure index or an
    error sentence: a caller handed back a success would carry on as if every
    action had run."""
    respx.post(INPUT).mock(httpx.Response(200, json=body))
    with pytest.raises(mc.InputBatchError) as raised:
        computer.input_batch([{"type": "wait"}])
    assert raised.value.status == 200 and raised.value.result.ok is False
    assert mc.is_transient(raised.value) is False


@respx.mock
async def test_the_async_half_raises_a_contradictory_success_too() -> None:
    respx.post(INPUT).mock(
        httpx.Response(200, json={"ok": True, "completed": 1, "results": [{"ok": False}]})
    )
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        c = mc.AsyncComputer(client._t, COMPUTER)
        with pytest.raises(mc.InputBatchError):
            await c.input_batch([{"type": "wait"}])


# --- the openai package's own action objects ----------------------------------


class _Action:
    """Stands in for an ``openai`` action model: fields, and pydantic's
    ``model_dump``, unset optional fields ``None`` as the package leaves them."""

    def __init__(self, **fields: Any) -> None:
        self._fields = fields

    def model_dump(self, *, exclude_none: bool = False) -> dict[str, Any]:
        return {k: v for k, v in self._fields.items() if not (exclude_none and v is None)}


@respx.mock
def test_the_openai_packages_action_objects_are_taken_as_they_are(computer: mc.Computer) -> None:
    route = respx.post(INPUT).mock(httpx.Response(200, json=done(3)))
    computer.input_batch(
        [
            _Action(type="click", x=1, y=2, button="left", keys=None),
            _Action(type="drag", path=[{"x": 1, "y": 2}, {"x": 3, "y": 4}], keys=None),
            {"type": "wait"},
        ]
    )
    assert json.loads(route.calls.last.request.content) == [
        {"type": "click", "x": 1, "y": 2, "button": "left"},
        {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]},
        {"type": "wait"},
    ]


@respx.mock
@pytest.mark.parametrize(
    ("action", "match"),
    [
        (_Action(type="click", x=1, y=2, button="left", pending_safety_checks=[]), "does not take"),
        (_Action(type="drag", path=[{"x": 1, "y": 2}, {"x": 3, "y": 4, "z": 1}]), "path"),
        (object(), "must be an object"),
    ],
)
def test_an_action_object_is_checked_as_a_mapping_is(
    computer: mc.Computer, action: object, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        computer.input_batch([action])  # type: ignore[list-item]
    assert not respx.calls
