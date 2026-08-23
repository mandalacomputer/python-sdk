"""What the account has used, and the two caveats that come with it.

OPL-3765. The platform grew ``GET /usage`` because the dashboard could read these
figures and an API key could not — which is backwards for who needs them: a
script that launches computers in a loop is the caller that can run up a bill
without noticing.

What is pinned here is the binding rather than the arithmetic. The platform owns
the summing, and its own tests own whether the numbers are right. These own the
three things a client can get wrong about them:

- the window, because a timestamp with no zone is a silently shifted answer
  rather than an error, and the SDK is the layer that can refuse it before the
  round trip
- the caveats, because a total that is quietly short reads exactly like a total
  that is right, and only the flags say otherwise
- the withheld breakdown, because "no computers ran" and "this key may not see
  which did" arrive as the same empty list unless something separates them

Both halves of the client, because both have the method and
:mod:`tests.test_parity` only proves the signatures match — not that the async
one does the same thing with them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"

REPORT: dict[str, Any] = {
    "period": {
        "start": "2026-08-04T00:00:00.000Z",
        "end": "2026-09-04T00:00:00.000Z",
        "source": "subscription",
    },
    "from": "2026-08-04T00:00:00.000Z",
    "to": "2026-08-22T12:00:00.000Z",
    "usage": {
        "run_hours": 12.5,
        "vcpu_hours": 25,
        "ram_gb_hours": 50,
        "snapshot_gb_hours": 96,
        "snapshot_gb_months": 0.13,
        "disk_gb_hours": 480,
        "disk_gb_months": 0.66,
        "computers": [
            {
                "id": "vm-1",
                "name": "scratch",
                "run_hours": 12.5,
                "vcpu_hours": 25,
                "ram_gb_hours": 50,
            }
        ],
    },
    "degraded": False,
    "unmetered": False,
    "reported_through": "2026-08-20",
}


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


@pytest.fixture
def async_client() -> mc.AsyncClient:
    return mc.AsyncClient("com_test", base_url=BASE)


def answering(**over: Any) -> respx.Route:
    """The complete answer, with any field of it overridden."""
    return respx.get(f"{BASE}/usage").mock(httpx.Response(200, json={**REPORT, **over}))


class TestReadingUsage:
    @respx.mock
    def test_asks_for_the_billing_period_by_naming_no_window(self, client: mc.Client) -> None:
        route = answering()
        client.usage.read()
        # Not ``?from=&to=``, and not an empty pair either: the platform's
        # default IS the billing period, so the honest way to ask for it is to
        # say nothing.
        assert dict(route.calls.last.request.url.params) == {}

    @respx.mock
    def test_decodes_the_totals_and_the_breakdown(self, client: mc.Client) -> None:
        answering()
        report = client.usage.read()
        assert report.usage.vcpu_hours == 25
        # A float, not an int: 0.66 GB-months is a real charge, and truncating
        # it would round most small accounts' storage to nothing.
        assert report.usage.disk_gb_months == 0.66
        assert report.usage.computers == (
            mc.ComputerUsage(
                id="vm-1",
                name="scratch",
                run_hours=12.5,
                vcpu_hours=25.0,
                ram_gb_hours=50.0,
                gone=False,
            ),
        )
        assert report.reported_through == "2026-08-20"
        # The period is the ACCOUNT's; the window is what was measured.
        assert report.period.source == "subscription"
        assert report.from_ == "2026-08-04T00:00:00.000Z"

    @respx.mock
    def test_keeps_the_whole_payload_so_a_later_field_is_readable(self, client: mc.Client) -> None:
        answering(future_field=7)
        assert client.usage.read().raw["future_field"] == 7

    @respx.mock
    def test_marks_a_computer_that_is_no_longer_on_the_fleet(self, client: mc.Client) -> None:
        answering(
            usage={
                **REPORT["usage"],
                "computers": [{"id": "vm-gone", "run_hours": 1, "gone": True}],
            }
        )
        assert client.usage.read().usage.computers[0].gone is True


class TestTheWindow:
    @respx.mock
    def test_sends_an_aware_datetime_as_the_instant_it_carries(self, client: mc.Client) -> None:
        route = answering()
        client.usage.read(
            since=datetime(2026, 7, 1, tzinfo=timezone.utc),
            until=datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=1))),
        )
        params = dict(route.calls.last.request.url.params)
        # The offset travels rather than being normalised: it IS part of the
        # instant, and rewriting it would be this SDK deciding what was meant.
        assert params == {"from": "2026-07-01T00:00:00+00:00", "to": "2026-08-01T00:00:00+01:00"}

    @respx.mock
    def test_takes_a_string_that_already_carries_a_zone(self, client: mc.Client) -> None:
        route = answering()
        client.usage.read(since="2026-07-01T00:00:00Z")
        assert dict(route.calls.last.request.url.params) == {"from": "2026-07-01T00:00:00Z"}

    @respx.mock
    def test_refuses_a_naive_datetime_before_the_round_trip(self, client: mc.Client) -> None:
        # The failure this check exists for does not look like a failure. A
        # naive datetime has no zone, so rendering it means guessing one — and
        # a window shifted by a few hours is the worst possible outcome on the
        # one call whose output somebody checks against an invoice.
        route = answering()
        with pytest.raises(ValueError, match="aware datetime"):
            # The naive datetime is the subject, so the lint that exists to stop
            # one being written by accident is suppressed rather than obeyed.
            client.usage.read(since=datetime(2026, 7, 1))  # noqa: DTZ001
        assert not route.called

    @respx.mock
    def test_refuses_a_string_with_no_zone(self, client: mc.Client) -> None:
        route = answering()
        with pytest.raises(ValueError, match="time zone"):
            client.usage.read(since="2026-08-01T00:00:00")
        with pytest.raises(ValueError, match="^until must be"):
            client.usage.read(until="2026-08-01")
        assert not route.called

    @respx.mock
    def test_reports_the_window_that_was_measured(self, client: mc.Client) -> None:
        # A ``until`` in the future is answered as now. The response carries the
        # instant used, so two reads are comparable as windows rather than as
        # requests.
        answering()
        report = client.usage.read(until="2026-12-01T00:00:00Z")
        assert report.to == "2026-08-22T12:00:00.000Z"


class TestTheShortfalls:
    @respx.mock
    def test_reads_false_on_a_complete_answer(self, client: mc.Client) -> None:
        answering()
        report = client.usage.read()
        assert (report.degraded, report.unmetered) == (False, False)

    @respx.mock
    def test_carries_an_unreachable_hypervisor_as_a_caveat_not_an_error(
        self, client: mc.Client
    ) -> None:
        # 200 with a flag, deliberately, and the SDK must not turn it into a
        # raise: the numbers that ARE known are still worth having, and the
        # caller is the one who knows whether a short total matters for what
        # they are doing.
        answering(degraded=True)
        report = client.usage.read()
        assert report.degraded is True
        assert report.usage.vcpu_hours == 25

    @respx.mock
    def test_keeps_the_shortfall_that_never_clears_apart(self, client: mc.Client) -> None:
        answering(unmetered=True)
        report = client.usage.read()
        assert (report.degraded, report.unmetered) == (False, True)

    @respx.mock
    def test_says_nothing_has_settled_when_the_platform_says_nothing_has(
        self, client: mc.Client
    ) -> None:
        answering(reported_through=None)
        assert client.usage.read().reported_through is None


class TestAWorkspaceScopedKey:
    @respx.mock
    def test_gets_the_totals_with_the_breakdown_withheld(self, client: mc.Client) -> None:
        withheld = {k: v for k, v in REPORT["usage"].items() if k != "computers"}
        answering(usage=withheld)
        report = client.usage.read()
        assert report.usage.vcpu_hours == 25
        # Empty rather than None, so iterating needs no check first...
        assert report.usage.computers == ()
        # ...and the flag is what separates "may not see" from "nothing ran".
        assert report.breakdown is False

    @respx.mock
    def test_reads_an_account_that_ran_nothing_as_a_real_empty_breakdown(
        self, client: mc.Client
    ) -> None:
        answering(usage={**REPORT["usage"], "computers": []})
        report = client.usage.read()
        assert report.usage.computers == ()
        assert report.breakdown is True


class TestTheAsyncHalf:
    @respx.mock
    async def test_reads_the_same_report(self, async_client: mc.AsyncClient) -> None:
        answering()
        async with async_client as c:
            report = await c.usage.read()
        assert report.usage.vcpu_hours == 25
        assert report.usage.computers[0].id == "vm-1"

    @respx.mock
    async def test_refuses_the_same_naive_datetime(self, async_client: mc.AsyncClient) -> None:
        route = answering()
        async with async_client as c:
            with pytest.raises(ValueError, match="aware datetime"):
                await c.usage.read(until=datetime(2026, 7, 1))  # noqa: DTZ001
        assert not route.called
