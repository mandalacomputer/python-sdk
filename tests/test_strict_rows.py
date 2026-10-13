"""Rows a listing cannot be read without, and the window geometry the platform
takes (OPL-5974).

A webhook, a delivery or an SSH key without its ``id`` is one no later call can
name, so it is refused rather than handed back as ``""``. A webhook whose
``enabled`` cannot be read is refused rather than reported as delivering. The
TypeScript SDK reads the same rows the same way.

A window move takes whole x and y from -32768 to 32767 and a resize whole width
and height from 1 to 32767, the signed 16-bit range X11 holds; anything else is
refused before a request, on both clients.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _api

BASE = "https://api.test/api/v1"
WEBHOOK = {
    "id": "whk-2b7d4c809f3c1a7e",
    "url": "https://ci.example.com/mandala",
    "description": "",
    "events": ["process.exited"],
    "computers": [],
    "enabled": True,
    "disabled_reason": None,
    "disabled_at": None,
    "last_success_at": None,
    "last_failure_at": None,
    "last_status": None,
    "created_at": "2026-09-01T12:00:00.000Z",
    "updated_at": "2026-09-01T12:00:00.000Z",
}
DELIVERY = {
    "id": "whd-0123456789abcdef",
    "event_id": "evt-1",
    "event_type": "process.exited",
    "state": "delivered",
    "attempts": 1,
    "created_at": "2026-09-01T12:00:00.000Z",
}
KEY = {
    "id": "sshk-0123456789abcdef",
    "name": "laptop",
    "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample laptop",
    "fingerprint": "SHA256:abc",
    "key_type": "ssh-ed25519",
    "created_at": "2026-09-01T12:00:00.000Z",
    "last_used_at": None,
    "reach": "this_account",
}

Read = Callable[[mc.Client], object]

#: (route, a whole listing, a broken one, what the refusal names, the call).
LISTINGS: list[tuple[str, list[Any], list[Any], str, Read]] = [
    (
        "/webhooks",
        [WEBHOOK],
        [WEBHOOK, {**WEBHOOK, "id": ""}],
        "webhook 1",
        lambda c: c.webhooks.list(),
    ),
    (
        "/webhooks",
        [WEBHOOK],
        [{k: v for k, v in WEBHOOK.items() if k != "id"}],
        "webhook 0",
        lambda c: c.webhooks.list(),
    ),
    (
        "/webhooks",
        [WEBHOOK],
        [{**WEBHOOK, "enabled": "on"}],
        "enabled",
        lambda c: c.webhooks.list(),
    ),
    (
        "/webhooks",
        [WEBHOOK],
        [{k: v for k, v in WEBHOOK.items() if k != "enabled"}],
        "enabled",
        lambda c: c.webhooks.list(),
    ),
    ("/webhooks", [WEBHOOK], [WEBHOOK, "not a row"], "array", lambda c: c.webhooks.list()),
    (
        f"/webhooks/{WEBHOOK['id']}/deliveries",
        [DELIVERY],
        [DELIVERY, {**DELIVERY, "id": None}],
        "delivery 1",
        lambda c: c.webhooks.deliveries(str(WEBHOOK["id"])),
    ),
    ("/ssh-keys", [KEY], [{**KEY, "id": 7}], "key 0", lambda c: c.ssh_keys.list()),
    ("/ssh-keys", [KEY], [KEY, None], "array", lambda c: c.ssh_keys.list()),
]


@pytest.mark.parametrize(("path", "whole", "broken", "names", "read"), LISTINGS)
@respx.mock
def test_a_listing_row_that_cannot_be_used_is_refused(
    path: str, whole: list[Any], broken: list[Any], names: str, read: Read
) -> None:
    route = respx.get(f"{BASE}{path}").mock(httpx.Response(200, json=whole))
    with mc.Client("gck_test", base_url=BASE) as client:
        assert len(read(client)) == 1  # type: ignore[arg-type]
        route.mock(httpx.Response(200, json=broken))
        with pytest.raises(mc.MandalaError, match=names):
            read(client)


@respx.mock
async def test_the_async_listings_refuse_the_same_rows() -> None:
    respx.get(f"{BASE}/webhooks").mock(httpx.Response(200, json=[{**WEBHOOK, "enabled": None}]))
    respx.get(f"{BASE}/webhooks/{WEBHOOK['id']}/deliveries").mock(
        httpx.Response(200, json=[{k: v for k, v in DELIVERY.items() if k != "id"}])
    )
    respx.get(f"{BASE}/ssh-keys").mock(httpx.Response(200, json=[{**KEY, "id": ""}]))
    async with mc.AsyncClient("gck_test", base_url=BASE) as client:
        with pytest.raises(mc.MandalaError, match="webhook 0: .*enabled"):
            await client.webhooks.list()
        with pytest.raises(mc.MandalaError, match="delivery 0: .*id"):
            await client.webhooks.deliveries(str(WEBHOOK["id"]))
        with pytest.raises(mc.MandalaError, match="key 0: .*id"):
            await client.ssh_keys.list()


@respx.mock
def test_a_single_webhook_read_is_as_strict() -> None:
    respx.get(f"{BASE}/webhooks/{WEBHOOK['id']}").mock(
        httpx.Response(200, json={**WEBHOOK, "enabled": "maybe"})
    )
    with (
        mc.Client("gck_test", base_url=BASE) as client,
        pytest.raises(mc.MandalaError, match="enabled"),
    ):
        client.webhooks.get(str(WEBHOOK["id"]))


def test_whole_rows_read_as_sent() -> None:
    assert mc.Webhook.from_api({**WEBHOOK, "enabled": False}).enabled is False
    assert mc.WebhookDelivery.from_api(DELIVERY).id == DELIVERY["id"]
    assert mc.SshKey.from_api(KEY).id == KEY["id"]


# --- window geometry ---------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "names"),
    [
        ({"x": -32769, "y": 0}, "x must be from -32768 to 32767"),
        ({"x": 0, "y": 32768}, "y must be from -32768 to 32767"),
        ({"x": 1.5, "y": 0}, "x must be an integer"),
    ],
)
def test_a_window_move_outside_sixteen_bits_is_refused(kwargs: dict[str, Any], names: str) -> None:
    with pytest.raises(ValueError, match=names):
        _api.window_body("move", **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "names"),
    [
        ({"width": 0, "height": 10}, "width must be from 1 to 32767"),
        ({"width": 10, "height": -5}, "height must be from 1 to 32767"),
        ({"width": 32768, "height": 10}, "width must be from 1 to 32767"),
        ({"width": 10.0, "height": 10}, "width must be a whole number"),
    ],
)
def test_a_window_resize_outside_its_range_is_refused(kwargs: dict[str, Any], names: str) -> None:
    with pytest.raises(ValueError, match=names):
        _api.window_body("resize", **kwargs)


def test_the_edges_of_the_range_are_sent() -> None:
    assert _api.window_body("move", x=-32768, y=32767) == {
        "action": "move",
        "x": -32768,
        "y": 32767,
    }
    assert _api.window_body("resize", width=1, height=32767) == {
        "action": "resize",
        "width": 1,
        "height": 32767,
    }


@respx.mock
async def test_both_clients_refuse_before_any_request() -> None:
    route = respx.post(url__regex=rf"{BASE}/computers/vm-1/windows/.*").mock(
        httpx.Response(200, json={"window": None})
    )
    with mc.Client("gck_test", base_url=BASE) as client:
        c = mc.Computer(client._t, {"id": "vm-1", "status": "running", "os": "linux"})
        with pytest.raises(ValueError, match="from 1 to 32767"):
            c.window_action("0x1", "resize", width=0, height=10)
        with pytest.raises(ValueError, match="from -32768 to 32767"):
            c.window_action("0x1", "move", x=40000, y=0)
    async with mc.AsyncClient("gck_test", base_url=BASE) as aclient:
        a = mc.AsyncComputer(aclient._t, {"id": "vm-1", "status": "running", "os": "linux"})
        with pytest.raises(ValueError, match="from 1 to 32767"):
            await a.window_action("0x1", "resize", width=10, height=99999)
        with pytest.raises(ValueError, match="from -32768 to 32767"):
            await a.window_action("0x1", "move", x=0, y=-40000)
    assert not route.called
