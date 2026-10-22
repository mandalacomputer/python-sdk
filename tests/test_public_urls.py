"""Public URLs (platform OPL-3753): a port inside a computer served at
``https://<label>.<base_domain>``. The four methods on both computers, what they
send, what they refuse before a request, and how strictly an answer is read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
URL = {
    "id": "pu-0123456789abcdef",
    "url": "https://cool-thing.mandala.dev",
    "hostname": "cool-thing.mandala.dev",
    "label": "cool-thing",
    "port": 3000,
    "access": "private",
    "disabled": False,
    "created_at": "2026-10-12T00:00:00.000Z",
    "updated_at": "2026-10-12T00:05:00.000Z",
}
LISTING = {"enabled": True, "base_domain": "mandala.dev", "limit": 2, "public_urls": [URL]}
OFF = {
    "enabled": False,
    "reason": "Public URLs are not enabled here.",
    "base_domain": "mandala.dev",
    "limit": 0,
    "public_urls": [],
}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)
    monkeypatch.setenv("HOME", str(tmp_path))


@respx.mock
def test_list_create_update_and_delete() -> None:
    respx.get(f"{BASE}/computers/vm-9/public-urls").mock(
        return_value=httpx.Response(200, json=LISTING)
    )
    create = respx.post(f"{BASE}/computers/vm-9/public-urls").mock(
        return_value=httpx.Response(201, json=URL)
    )
    update = respx.patch(f"{BASE}/computers/vm-9/public-urls/pu-0123456789abcdef").mock(
        return_value=httpx.Response(200, json={**URL, "port": 8000, "access": "public"})
    )
    delete = respx.delete(f"{BASE}/computers/vm-9/public-urls/pu-0123456789abcdef").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with mc.Client() as client:
        computer = mc.Computer(client._t, {"id": "vm-9"})
        listing = computer.public_urls()
        assert listing.enabled is True
        assert listing.reason is None
        assert listing.base_domain == "mandala.dev"
        assert listing.limit == 2
        assert listing.public_urls == [
            mc.PublicUrl(
                id="pu-0123456789abcdef",
                url="https://cool-thing.mandala.dev",
                hostname="cool-thing.mandala.dev",
                label="cool-thing",
                port=3000,
                access="private",
                disabled=False,
                created_at="2026-10-12T00:00:00.000Z",
                updated_at="2026-10-12T00:05:00.000Z",
            )
        ]
        assert listing.public_urls[0].is_private is True

        made = computer.create_public_url(3000)
        assert json.loads(create.calls.last.request.content) == {"port": 3000}
        assert made.url == "https://cool-thing.mandala.dev"
        computer.create_public_url(3000, label="cool-thing", access="private")
        assert json.loads(create.calls.last.request.content) == {
            "port": 3000,
            "label": "cool-thing",
            "access": "private",
        }

        changed = computer.update_public_url(made.id, port=8000, access="public")
        assert json.loads(update.calls.last.request.content) == {"port": 8000, "access": "public"}
        assert (changed.port, changed.access, changed.is_private) == (8000, "public", False)
        computer.update_public_url(made.id, access="private")
        assert json.loads(update.calls.last.request.content) == {"access": "private"}

        assert computer.delete_public_url(made.id) is None
        assert delete.called


@respx.mock
def test_a_listing_where_public_urls_are_off_still_reads() -> None:
    respx.get(f"{BASE}/computers/vm-9/public-urls").mock(return_value=httpx.Response(200, json=OFF))
    with mc.Client() as client:
        listing = mc.Computer(client._t, {"id": "vm-9"}).public_urls()
    assert (listing.enabled, listing.reason, listing.limit, listing.public_urls) == (
        False,
        "Public URLs are not enabled here.",
        0,
        [],
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"port": None}, "port is required"),
        ({"port": True}, "whole number from 1 to 65535"),
        ({"port": "3000"}, "whole number from 1 to 65535"),
        ({"port": 3000.0}, "whole number from 1 to 65535"),
        ({"port": 0}, "whole number from 1 to 65535"),
        ({"port": 65536}, "whole number from 1 to 65535"),
        ({"port": 22}, "port 22"),
        ({"port": 3000, "label": ""}, "label must not be empty"),
        ({"port": 3000, "label": "   "}, "label must not be empty"),
        ({"port": 3000, "label": 7}, "label must be a string"),
        ({"port": 3000, "access": "secret"}, 'access must be "public" or "private"'),
        ({"port": 3000, "access": True}, "access must be a string"),
    ],
)
def test_a_create_the_platform_would_refuse_is_refused_first(
    kwargs: dict[str, Any], message: str
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE}/computers/vm-9/public-urls")
        with mc.Client() as client:
            computer = mc.Computer(client._t, {"id": "vm-9"})
            port = kwargs.pop("port")
            with pytest.raises(ValueError, match=message):
                computer.create_public_url(port, **kwargs)
        assert not route.called


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "must name a port, an access, or both"),
        ({"port": None, "access": None}, "must name a port, an access, or both"),
        ({"port": 22}, "port 22"),
        ({"access": "everyone"}, 'access must be "public" or "private"'),
    ],
)
def test_an_update_the_platform_would_refuse_is_refused_first(
    kwargs: dict[str, Any], message: str
) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.patch(url__regex=rf"{BASE}/computers/vm-9/public-urls/.*")
        with mc.Client() as client:
            computer = mc.Computer(client._t, {"id": "vm-9"})
            with pytest.raises(ValueError, match=message):
                computer.update_public_url("pu-0123456789abcdef", **kwargs)
        assert not route.called


def test_the_label_never_changes() -> None:
    # The label IS the URL: there is no keyword for it on an update, and the
    # payload builder refuses one if handed it, rather than send what the
    # platform would ignore.
    from mandala_computer import _api

    with pytest.raises(ValueError, match="label cannot change"):
        _api.public_url_body(label="other", create=False)


def test_a_url_id_cannot_climb_out_of_its_route() -> None:
    with mc.Client() as client:
        computer = mc.Computer(client._t, {"id": "vm-9"})
        with pytest.raises(ValueError, match="empty or all dots"):
            computer.delete_public_url("..")
        with pytest.raises(ValueError, match="empty or all dots"):
            computer.update_public_url("", port=8000)


@respx.mock
def test_an_id_with_a_slash_stays_one_segment() -> None:
    route = respx.delete(f"{BASE}/computers/vm-9/public-urls/a%2Fb").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with mc.Client() as client:
        mc.Computer(client._t, {"id": "vm-9"}).delete_public_url("a/b")
    assert route.called


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"port": None}, "no usable port"),
        ({"port": "3000"}, "no usable port"),
        ({"port": True}, "no usable port"),
        ({"port": 0}, "no usable port"),
        ({"id": None}, "no usable id"),
        ({"url": ""}, "no usable url"),
        ({"hostname": 7}, "no usable hostname"),
        ({"label": None}, "no usable label"),
        ({"access": None}, 'neither "public" nor "private"'),
        ({"access": "restricted"}, 'neither "public" nor "private"'),
        ({"access": " private"}, 'neither "public" nor "private"'),
        ({"access": ""}, 'neither "public" nor "private"'),
        ({"disabled": None}, "whether it is disabled"),
        ({"disabled": "maybe"}, "whether it is disabled"),
        ({"created_at": None}, "no usable created_at"),
        ({"updated_at": None}, "no usable updated_at"),
    ],
)
def test_a_url_read_wrong_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(mc.MandalaError, match=message):
        mc.PublicUrl.from_api({**URL, **change})
    missing = {k: v for k, v in URL.items() if k not in change}
    with pytest.raises(mc.MandalaError, match=message):
        mc.PublicUrl.from_api(missing)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"public_urls": None}, "expected public URLs"),
        ({"public_urls": {"0": URL}}, "expected public URLs"),
        ({"public_urls": ["pu-1"]}, "not an object"),
        ({"public_urls": [{**URL, "port": None}]}, "no usable port"),
        ({"enabled": None}, "whether public URLs are enabled"),
        ({"base_domain": ""}, "no base domain"),
        ({"limit": -1}, "non-negative whole number"),
        ({"limit": True}, "non-negative whole number"),
        ({"limit": None}, "non-negative whole number"),
        ({"reason": 7}, "reason must be text"),
    ],
)
def test_a_listing_read_wrong_is_refused(change: dict[str, Any], message: str) -> None:
    with pytest.raises(mc.MandalaError, match=message):
        mc.PublicUrls.from_api({**LISTING, **change})


@respx.mock
def test_a_malformed_answer_from_the_platform_raises_with_the_route() -> None:
    respx.get(f"{BASE}/computers/vm-9/public-urls").mock(
        return_value=httpx.Response(200, json={**LISTING, "limit": "two"})
    )
    with mc.Client() as client:
        computer = mc.Computer(client._t, {"id": "vm-9"})
        with pytest.raises(mc.MandalaError, match="GET computers/vm-9/public-urls"):
            computer.public_urls()


@respx.mock
def test_refusals_keep_the_platforms_code() -> None:
    respx.post(f"{BASE}/computers/vm-9/public-urls").mock(
        return_value=httpx.Response(
            409, json={"error": "that label is taken", "code": "LABEL_TAKEN"}
        )
    )
    with mc.Client() as client:
        computer = mc.Computer(client._t, {"id": "vm-9"})
        with pytest.raises(mc.ConflictError, match="taken") as raised:
            computer.create_public_url(3000, label="cool-thing")
    assert raised.value.code == "LABEL_TAKEN"


@respx.mock
def test_trial_public_refusal_preserves_plan_error_and_code() -> None:
    respx.post(f"{BASE}/computers/vm-9/public-urls").mock(
        return_value=httpx.Response(
            402,
            json={
                "error": "Trial accounts can only use private URLs.",
                "code": "PUBLIC_ACCESS_REQUIRES_PLAN",
            },
        )
    )
    with mc.Client() as client, pytest.raises(mc.PlanLimitError) as raised:
        client.public_urls.create("vm-9", 3000, access="public")
    assert raised.value.code == "PUBLIC_ACCESS_REQUIRES_PLAN"


@respx.mock
async def test_async_methods() -> None:
    respx.get(f"{BASE}/computers/vm-9/public-urls").mock(
        return_value=httpx.Response(200, json=LISTING)
    )
    create = respx.post(f"{BASE}/computers/vm-9/public-urls").mock(
        return_value=httpx.Response(201, json=URL)
    )
    update = respx.patch(f"{BASE}/computers/vm-9/public-urls/pu-0123456789abcdef").mock(
        return_value=httpx.Response(200, json={**URL, "port": 8000})
    )
    delete = respx.delete(f"{BASE}/computers/vm-9/public-urls/pu-0123456789abcdef").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with mc.AsyncClient() as client:
        computer = mc.AsyncComputer(client._t, {"id": "vm-9"})
        assert [u.label for u in (await computer.public_urls()).public_urls] == ["cool-thing"]
        made = await computer.create_public_url(3000, access="public")
        assert json.loads(create.calls.last.request.content) == {"port": 3000, "access": "public"}
        assert (await computer.update_public_url(made.id, port=8000)).port == 8000
        assert json.loads(update.calls.last.request.content) == {"port": 8000}
        await computer.delete_public_url(made.id)
        assert delete.called
        with pytest.raises(ValueError, match="port 22"):
            await computer.create_public_url(22)


@respx.mock
async def test_an_unknown_access_mode_is_refused_not_read_as_public() -> None:
    respx.post(f"{BASE}/computers/vm-9/public-urls").mock(
        return_value=httpx.Response(201, json={**URL, "access": "restricted"})
    )
    respx.patch(f"{BASE}/computers/vm-9/public-urls/pu-0123456789abcdef").mock(
        return_value=httpx.Response(200, json={**URL, "access": "restricted"})
    )
    with (
        mc.Client() as client,
        pytest.raises(mc.MandalaError, match="POST computers/vm-9/public-urls"),
    ):
        mc.Computer(client._t, {"id": "vm-9"}).create_public_url(3000)
    async with mc.AsyncClient() as client:
        with pytest.raises(mc.MandalaError, match="neither"):
            await mc.AsyncComputer(client._t, {"id": "vm-9"}).update_public_url(
                "pu-0123456789abcdef", port=8000
            )
