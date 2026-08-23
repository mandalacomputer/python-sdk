"""How long automatic snapshots are kept, and why this is a read and nothing else.

OPL-3767 on the platform, OPL-3783 here. ``set_schedule`` says, correctly, that
the plan's retention decides how long automatic snapshots live — and for a long
time no route said what it was. A caller either hardcoded a number per plan tier
or inferred one by watching ``auto`` snapshots vanish.

The arithmetic is the platform's and its own tests own it. What is pinned here is
the binding: that this reaches the account-scoped route rather than anything
computer-shaped, that the three numbers arrive as numbers, and that an all-zero
window is passed through as itself rather than helpfully reinterpreted — zero
means "this tier is off", and the SDK is not the layer that gets to decide what
that implies about existing snapshots.
"""

from __future__ import annotations

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"


def client() -> mc.Client:
    return mc.Client(api_key="com_test", base_url=BASE)


@respx.mock
def test_asks_the_account_scoped_route_with_nothing_on_it() -> None:
    route = respx.get(f"{BASE}/retention").mock(
        return_value=httpx.Response(200, json={"daily": 7, "weekly": 4, "monthly": 12})
    )
    with client() as c:
        r = c.snapshots.retention()
    # No id, no query. The window belongs to the account, so a per-computer path
    # would be asking a question this API does not have an answer for.
    assert route.called
    assert route.calls.last.request.url.query == b""
    assert (r.daily, r.weekly, r.monthly) == (7, 4, 12)


@respx.mock
def test_passes_an_all_zero_window_through_as_itself() -> None:
    # What an account with no active subscription reads. Zero means the tier is
    # off, and this deliberately does not translate that into a claim about what
    # happens to existing snapshots: on the platform the same three zeroes mean
    # "your plan grants no retained history" as an entitlement and "never reap"
    # as a daemon policy, and picking one here would be inventing an answer the
    # wire did not carry.
    respx.get(f"{BASE}/retention").mock(
        return_value=httpx.Response(200, json={"daily": 0, "weekly": 0, "monthly": 0})
    )
    with client() as c:
        r = c.snapshots.retention()
    assert (r.daily, r.weekly, r.monthly) == (0, 0, 0)


@respx.mock
def test_keeps_a_field_it_does_not_model_and_does_not_invent_one_it_lacks() -> None:
    # ``raw`` is the convention every model here follows: a platform that grows a
    # field should not need an SDK release before a caller can see it. And a
    # response missing a tier reads as 0, which is what an absent tier means.
    respx.get(f"{BASE}/retention").mock(
        return_value=httpx.Response(200, json={"daily": 7, "yearly": 3})
    )
    with client() as c:
        r = c.snapshots.retention()
    assert (r.weekly, r.monthly) == (0, 0)
    assert r.raw["yearly"] == 3


@pytest.mark.asyncio
@respx.mock
async def test_the_async_half_answers_the_same() -> None:
    # Parity is asserted structurally in test_parity.py; this is the behaviour
    # behind it, since a method can match a signature and still call nothing.
    respx.get(f"{BASE}/retention").mock(
        return_value=httpx.Response(200, json={"daily": 7, "weekly": 0, "monthly": 0})
    )
    async with mc.AsyncClient(api_key="com_test", base_url=BASE) as c:
        r = await c.snapshots.retention()
    assert (r.daily, r.weekly, r.monthly) == (7, 0, 0)
