"""The derivation itself, on sources written to break it.

``surface_inventory`` is what makes the completeness check in ``test_surface.py``
mean "every public method is exercised" rather than "every route is reached"
(OPL-3900). A derivation nobody tests is the same unfalsifiable guarantee one
layer down: it would be perfectly quiet about a method it simply failed to see,
and the suite it feeds would go green for the wrong reason.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from tests.surface_inventory import (
    half,
    inventory,
    names,
    record_named_calls,
    requesting_methods,
)

import mandala_computer as mc

# A second public method on a route another method already reaches — the exact
# shape OPL-3900 is about, written the way the SDK writes it. `paste` is not the
# one that touches the transport; `_input` is, and `paste` is only reachable as
# request-making through it.
TWO_ON_ONE_ROUTE = """
class Computer:
    def _input(self, body):
        return self._t.json_object("PUT", "computers/x/clipboard", json=body)

    def set_clipboard(self, text):
        return self._input({"text": text})

    def paste(self, text):
        return self._input({"text": text, "paste": True})
"""


def test_a_second_method_on_an_existing_route_is_in_the_inventory() -> None:
    """The failure the route check cannot see, seen.

    Both methods land on ``PUT computers/:id/clipboard``, so ``called_routes``
    is identical whether or not ``paste`` is ever called. The inventory names
    them separately, which is the whole difference.
    """
    assert requesting_methods(TWO_ON_ONE_ROUTE) == {
        "Computer": frozenset({"set_clipboard", "paste"})
    }


def test_a_helper_chain_is_followed_to_the_end() -> None:
    """Two hops, not one.

    ``Computer.click`` reaches the wire through ``_input``, and ``open`` through
    ``exec`` through ``_exec``. A rule that only looked for a direct
    ``self._t`` touch would call most of the input surface non-requesting and
    then be satisfied by an exercise that skipped all of it.
    """
    source = """
class Computer:
    def _send(self, body):
        return self._t.request("POST", "computers/x/input", json=body)

    def _input(self, body):
        return self._send(body)

    def click(self, x, y):
        self._input({"x": x, "y": y})
"""
    assert requesting_methods(source) == {"Computer": frozenset({"click"})}


def test_private_methods_bridge_but_are_not_surface() -> None:
    """A private helper is how a public method reaches the wire, not a caller's
    entry point — so it counts on the way through and not in the answer."""
    source = """
class Computer:
    def _refresh(self):
        return self._t.json_object("GET", "computers/x")

    def refresh(self):
        return self._refresh()
"""
    assert requesting_methods(source) == {"Computer": frozenset({"refresh"})}


def test_a_class_that_never_reaches_the_wire_is_left_out() -> None:
    """Models, exceptions and decoders are not surface with nothing on it."""
    source = """
class Move:
    @classmethod
    def from_api(cls, d):
        return cls(state=d.get("state"))

    def summary(self):
        return self.state
"""
    assert requesting_methods(source) == {}


def test_mutually_recursive_helpers_settle() -> None:
    """The closure widens until nothing new is reached, and a cycle is a cycle.

    Two helpers that call each other would spin a naive fixed point forever;
    this pins that they do not, and that the public method above them is still
    found.
    """
    source = """
class Builds:
    def _a(self, n):
        return self._b(n) if n else self._t.json("GET", "builds")

    def _b(self, n):
        return self._a(n - 1)

    def wait(self):
        return self._a(3)
"""
    assert requesting_methods(source) == {"Builds": frozenset({"wait"})}


def test_a_transport_verb_that_is_not_the_transport_is_not_a_request() -> None:
    """``self._t`` and a verb, both — not either.

    A model with a ``json`` attribute, or a helper calling ``json.loads``, is
    not a request, and reading one as such would put a method in the inventory
    that no exercise can ever make a call for.
    """
    source = """
class Report:
    def render(self):
        return json.dumps(self._body.json)

    def totals(self):
        return self._cache.listing
"""
    assert requesting_methods(source) == {}


def test_an_unrecognized_transport_member_fails_instead_of_disappearing() -> None:
    """A new wire wrapper must first be classified by the inventory.

    Without this failure, ``self._t.post`` looks like no request at all and the
    public method silently falls out of every completeness assertion this
    inventory feeds — the exact blind spot the derivation exists to close.
    """
    source = """
class Computers:
    def create(self, body):
        return self._t.post("computers", json=body)
"""
    with pytest.raises(
        ValueError,
        match=r"unclassified transport member in Computers\.create: self\._t\.post",
    ):
        requesting_methods(source)


def test_known_non_request_transport_members_stay_out_of_the_inventory() -> None:
    """Lifecycle, configuration and retry-budget reads are classified too."""
    source = """
class Client:
    def url(self):
        return self._t.base_url

    def close(self):
        self._t.close()

    async def aclose(self):
        await self._t.aclose()

    def retry_budget(self, err):
        return self._t.phase_ceiling(err)
"""
    assert requesting_methods(source) == {}


def test_the_derived_inventory_matches_the_shipped_classes() -> None:
    """Every name derived from source is really there on the class.

    The derivation reads text; the recorder patches objects. If those two ever
    disagree — a method behind a decorator that renames it, say — the recorder
    would raise rather than silently record nothing, and this is the cheaper
    place to find out.
    """
    found = inventory()
    assert found, "no request-making classes found at all"
    for cls, methods in found.items():
        for method in methods:
            assert callable(vars(cls)[method]), f"{cls.__name__}.{method}"


@respx.mock
def test_the_recorder_counts_only_calls_the_caller_makes_itself() -> None:
    """Borrowed coverage is not coverage.

    ``Computer.agent`` is ``agent_stream`` waited out — it drives the public
    method underneath it. A recorder that counted calls from anywhere would mark
    ``agent_stream`` exercised because its neighbour ran, which is the same
    borrowed guarantee as sharing a route, one level down: the streaming entry
    point would keep its own signature, its own defaults and no coverage.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent"):
            return httpx.Response(
                200,
                content=b'event: done\ndata: {"steps": 1, "stop": "end_turn", "text": "done"}\n\n',
                headers={"Content-Type": "text/event-stream"},
            )
        return httpx.Response(200, json={"id": "vm-1", "status": "running"})

    respx.route(host="api.test").mock(side_effect=handler)
    surface = half(inventory(), asynchronous=False)

    def drives_the_stream(c: mc.Computer) -> None:
        c.agent("do the thing", model_key="sk-ant-test")

    with mc.Client("gck_test", base_url="https://api.test/api/v1") as client:
        computer = client.computers.get("vm-1")
        with record_named_calls(surface, [drives_the_stream]) as named:
            drives_the_stream(computer)

    assert named == {"Computer.agent"}


def test_the_recorder_puts_the_classes_back() -> None:
    """Including when the exercise raises, since a leaked wrapper would follow
    every later test in the session."""
    surface = half(inventory(), asynchronous=False)
    before = {(cls, method): vars(cls)[method] for cls, ms in surface.items() for method in ms}

    def boom() -> None:
        raise RuntimeError("the exercise failed")

    with pytest.raises(RuntimeError), record_named_calls(surface, [boom]):
        boom()

    assert {(cls, m): vars(cls)[m] for cls, ms in surface.items() for m in ms} == before


def test_both_halves_are_non_empty_and_disjoint() -> None:
    """The split is by module, so a renamed class cannot quietly empty a half."""
    found = inventory()
    sync, asynchronous = half(found, asynchronous=False), half(found, asynchronous=True)
    assert names(sync) and names(asynchronous)
    assert not (set(sync) & set(asynchronous))
