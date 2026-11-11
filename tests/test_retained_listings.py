"""Listing a computer's retained results and artifacts, with quota usage."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, computer, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414
from tests.test_artifacts import ARTIFACT_ID
from tests.test_retained_results import EXECUTION_ID, RESULT_ID

import mandala_computer as mc

STARTED = "2026-09-16T12:00:00.123Z"
EXPIRES = "2026-09-17T12:00:00.123Z"
USAGE = {
    "computer": {"objects": 2, "bytes": 300, "max_objects": 200, "max_bytes": 1_000_000},
    "account": {"objects": 9, "bytes": 900, "max_objects": 2000, "max_bytes": 9_000_000},
}
RESULT_LISTING: dict[str, Any] = {
    "results": [
        {
            "result_id": RESULT_ID,
            "kind": "background-output",
            "bytes": 120,
            "workspace_id": None,
            "execution_id": EXECUTION_ID,
            "started_at": STARTED,
            "expires_at": EXPIRES,
        },
        {
            "result_id": "res_" + "c" * 32,
            "kind": "synchronous-output",
            "bytes": 180,
            "workspace_id": "wsp-0123456789ab",
            "execution_id": None,
            "started_at": STARTED,
            "expires_at": EXPIRES,
        },
    ],
    "usage": USAGE,
}
ARTIFACT_LISTING: dict[str, Any] = {
    "artifacts": [
        {
            "artifact_id": ARTIFACT_ID,
            "size": 3,
            "workspace_id": None,
            "execution_id": None,
            "started_at": STARTED,
            "expires_at": EXPIRES,
        }
    ],
    "usage": USAGE,
}


@respx.mock
async def test_results_lists_every_row_and_both_quotas(client: Any) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/results").mock(
        return_value=httpx.Response(200, json=RESULT_LISTING)
    )
    listing = await resolved(computer(client).results())
    assert route.called and route.calls.last.request.url.query == b""
    assert [r.result_id for r in listing.results] == [RESULT_ID, "res_" + "c" * 32]
    first, second = listing.results
    assert (first.kind, first.bytes, first.execution_id) == (
        "background-output",
        120,
        EXECUTION_ID,
    )
    assert (second.workspace_id, second.execution_id) == ("wsp-0123456789ab", None)
    assert listing.usage.computer == mc.RetainedQuota(2, 300, 200, 1_000_000)
    assert listing.usage.account == mc.RetainedQuota(9, 900, 2000, 9_000_000)


@respx.mock
async def test_artifacts_lists_every_row_and_both_quotas(client: Any) -> None:
    respx.get(f"{BASE}/computers/vm-1/artifacts").mock(
        return_value=httpx.Response(200, json=ARTIFACT_LISTING)
    )
    listing = await resolved(computer(client).artifacts())
    assert listing.artifacts == (
        mc.RetainedArtifactEntry(ARTIFACT_ID, 3, None, None, STARTED, EXPIRES),
    )
    assert listing.usage.account.max_objects == 2000


def _broken(listing: dict[str, Any], key: str, field: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(listing)
    if field in ("usage",):
        out["usage"] = value
    else:
        out[key][0][field] = value
    return out


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("result_id", "res_short"),
        ("kind", "other"),
        ("bytes", -1),
        ("execution_id", "exec_x"),
        ("started_at", "yesterday"),
        ("expires_at", "2026-09-15T00:00:00Z"),
        ("usage", {"computer": USAGE["computer"]}),
    ],
)
@respx.mock
async def test_a_result_listing_with_one_unreadable_part_is_refused_whole(
    client: Any, field: str, value: Any
) -> None:
    """A listing one row shorter would hide the object a caller lists in order
    to delete it, so one unreadable row refuses the whole answer."""
    respx.get(f"{BASE}/computers/vm-1/results").mock(
        return_value=httpx.Response(200, json=_broken(RESULT_LISTING, "results", field, value))
    )
    with pytest.raises(mc.MandalaError, match="retained response"):
        await resolved(computer(client).results())


@respx.mock
async def test_an_artifact_listing_row_without_its_workspace_is_refused(client: Any) -> None:
    broken = copy.deepcopy(ARTIFACT_LISTING)
    del broken["artifacts"][0]["workspace_id"]
    respx.get(f"{BASE}/computers/vm-1/artifacts").mock(
        return_value=httpx.Response(200, json=broken)
    )
    with pytest.raises(mc.MandalaError, match="workspace_id"):
        await resolved(computer(client).artifacts())
