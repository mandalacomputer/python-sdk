"""Which 409s are worth sending again (OPL-5974).

The platform's reference says a 409 without a ``reason`` is unclassified, and
that a client treating 409 as uniformly transient loops forever on half of
them. So :func:`mandala_computer.is_transient` answers ``True`` for a 409 only
when the platform says it clears: a ``reason`` of ``contention`` or
``starting``, or, on a body with no ``reason``, a ``code`` of
``idempotency_in_progress`` or ``WORKSPACE_MOVING``.

The table below is the one the TypeScript SDK and the MCP server classify by,
and the wire cases are the refusals the audit found answered ``True``: each
arrives through a real call on both clients, so the class the transport builds
is the class the predicate is asked about.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)
    monkeypatch.setenv("HOME", str(tmp_path))


#: (status, body, transient). The classification every client shares.
TABLE: list[tuple[int, dict[str, Any], bool]] = [
    # The platform's own words.
    (409, {"reason": "contention"}, True),
    (409, {"reason": "starting"}, True),
    (409, {"reason": "unavailable"}, False),
    (409, {"reason": "unsupported"}, False),
    (409, {"reason": "running"}, False),
    (409, {"reason": "exists"}, False),
    (409, {"reason": "name_taken"}, False),
    (409, {"reason": "stale_revision"}, False),
    (409, {"reason": "stale_capture"}, False),
    (409, {"reason": "revoked"}, False),
    # A word this version does not know is no classification at all.
    (409, {"reason": "quiesced"}, False),
    # The two codes documented as passing.
    (409, {"code": "idempotency_in_progress", "reason": "contention"}, True),
    (409, {"code": "idempotency_in_progress"}, True),
    (409, {"code": "WORKSPACE_MOVING"}, True),
    (409, {"code": "WORKSPACE_MOVING", "reason": "contention"}, True),
    # A move that is not settled and needs the dashboard: the word decides.
    (409, {"code": "WORKSPACE_MOVING", "reason": "unsupported"}, False),
    # A keyed call nobody heard end: the key answers the same forever.
    (409, {"code": "idempotency_outcome_unknown"}, False),
    # Public URLs.
    (409, {"code": "LABEL_TAKEN"}, False),
    (409, {"code": "PUBLIC_URL_LIMIT"}, False),
    (403, {"code": "PUBLIC_URLS_BLOCKED"}, False),
    (404, {"code": "PUBLIC_URLS_DISABLED"}, False),
    (429, {"code": "RATE_LIMITED"}, True),
    # Unclassified and codeless: the webhook cap, a disabled webhook's test, a
    # duplicate SSH key, the SSH key limit, a move already running.
    (409, {}, False),
]


@pytest.mark.parametrize(("status", "body", "transient"), TABLE)
def test_the_shared_classification(status: int, body: dict[str, Any], transient: bool) -> None:
    with respx.mock:
        respx.get(f"{BASE}/secrets").mock(
            return_value=httpx.Response(status, json={"error": "refused", **body})
        )
        with mc.Client() as client, pytest.raises(mc.APIError) as raised:
            client.secrets.list()
    assert mc.is_transient(raised.value) is transient


Call = Callable[[mc.Client], object]

#: The refusals the audit probed, each by the route that answers it.
WIRE: list[tuple[str, str, str, dict[str, Any], Call]] = [
    (
        "POST",
        "/computers/vm-9/public-urls",
        "public URL label taken",
        {"error": 'The label "cool-thing" is taken; choose another.', "code": "LABEL_TAKEN"},
        lambda c: mc.Computer(c._t, {"id": "vm-9"}).create_public_url(3000, label="cool-thing"),
    ),
    (
        "POST",
        "/computers/vm-9/public-urls",
        "public URL limit",
        {
            "error": "This computer has as many public URLs as its plan allows.",
            "code": "PUBLIC_URL_LIMIT",
        },
        lambda c: mc.Computer(c._t, {"id": "vm-9"}).create_public_url(3000),
    ),
    (
        "POST",
        "/webhooks",
        "webhook cap",
        {
            "error": "This account already has 10 of the 10 webhook subscriptions its plan allows; "
            "delete one first."
        },
        lambda c: c.webhooks.create("https://ci.example.com/mandala"),
    ),
    (
        "POST",
        "/webhooks/whk-2b7d4c809f3c1a7e/test",
        "disabled webhook test",
        {
            "error": 'This webhook is disabled. Enable it with PATCH {"enabled": true} before '
            "sending a test."
        },
        lambda c: c.webhooks.test("whk-2b7d4c809f3c1a7e"),
    ),
    (
        "POST",
        "/ssh-keys",
        "duplicate SSH key",
        {"error": "That key is already registered. A key can belong to one person only."},
        lambda c: c.ssh_keys.add("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample laptop"),
    ),
    (
        "POST",
        "/ssh-keys",
        "SSH key limit",
        {"error": "You already have 10 SSH keys, which is the limit. Remove one first."},
        lambda c: c.ssh_keys.add("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample laptop"),
    ),
]


@pytest.mark.parametrize(
    ("method", "path", "case", "body", "call"), WIRE, ids=[case for _, _, case, _, _ in WIRE]
)
def test_a_permanent_refusal_is_not_worth_sending_again(
    method: str, path: str, case: str, body: dict[str, Any], call: Call
) -> None:
    with respx.mock:
        route = respx.route(method=method, url=f"{BASE}{path}").mock(
            return_value=httpx.Response(409, json=body)
        )
        with mc.Client() as client, pytest.raises(mc.ConflictError) as raised:
            call(client)
    assert route.call_count == 1
    assert raised.value.reason is None
    assert mc.is_transient(raised.value) is False, case


async def test_the_async_client_classifies_them_the_same() -> None:
    with respx.mock:
        respx.post(f"{BASE}/computers/vm-9/public-urls").mock(
            side_effect=[
                httpx.Response(409, json={"error": "taken", "code": "LABEL_TAKEN"}),
                httpx.Response(409, json={"error": "full", "code": "PUBLIC_URL_LIMIT"}),
                httpx.Response(
                    409,
                    json={
                        "error": "This computer is being moved to another workspace.",
                        "code": "WORKSPACE_MOVING",
                        "reason": "contention",
                    },
                ),
                httpx.Response(
                    409,
                    json={
                        "error": "This computer's workspace change is not settled.",
                        "code": "WORKSPACE_MOVING",
                        "reason": "unsupported",
                    },
                ),
            ]
        )
        respx.post(f"{BASE}/ssh-keys").mock(
            return_value=httpx.Response(409, json={"error": "That key is already registered."})
        )
        respx.post(f"{BASE}/webhooks/whk-2b7d4c809f3c1a7e/test").mock(
            return_value=httpx.Response(409, json={"error": "This webhook is disabled."})
        )
        async with mc.AsyncClient() as client:
            computer = mc.AsyncComputer(client._t, {"id": "vm-9"})
            answers = []
            for _ in range(4):
                with pytest.raises(mc.ConflictError) as raised:
                    await computer.create_public_url(3000)
                answers.append((raised.value.code, mc.is_transient(raised.value)))
            with pytest.raises(mc.ConflictError) as key:
                await client.ssh_keys.add("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample")
            with pytest.raises(mc.ConflictError) as hook:
                await client.webhooks.test("whk-2b7d4c809f3c1a7e")
    assert answers == [
        ("LABEL_TAKEN", False),
        ("PUBLIC_URL_LIMIT", False),
        ("WORKSPACE_MOVING", True),
        ("WORKSPACE_MOVING", False),
    ]
    assert mc.is_transient(key.value) is False
    assert mc.is_transient(hook.value) is False
