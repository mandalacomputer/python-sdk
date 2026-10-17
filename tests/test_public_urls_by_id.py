"""URL management must not need a Computer handle or a host-dependent read."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import respx
from tests.test_public_urls import BASE, LISTING, OFF, URL

import mandala_computer as mc


@pytest.fixture(params=[False, True], ids=["sync", "async"])
async def call(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    monkeypatch.setenv("HOME", str(tmp_path))
    client = (mc.AsyncClient if request.param else mc.Client)(
        api_key="com_test", base_url=BASE, retries={"idempotent": 0}
    )

    async def invoke(method: str, *args: Any, **kwargs: Any) -> Any:
        result = getattr(client.public_urls, method)(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    try:
        yield invoke
    finally:
        if isinstance(client, mc.AsyncClient):
            await client.aclose()
        else:
            client.close()


@pytest.mark.parametrize("listing", [LISTING, OFF])
async def test_all_four_calls_work_without_reading_the_computer(
    call: Any, listing: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as router:
        # Fetching a handle would fail. All four calls must go straight to the
        # URL routes, including list/delete when the computer has been lost.
        host = router.get(f"{BASE}/computers/vm-9").respond(503, json={"error": "host unavailable"})
        listed = router.get(f"{BASE}/computers/vm-9/public-urls").respond(200, json=listing)
        created = router.post(f"{BASE}/computers/vm-9/public-urls").respond(201, json=URL)
        updated = router.patch(f"{BASE}/computers/vm-9/public-urls/{URL['id']}").respond(
            200, json={**URL, "port": 8080}
        )
        deleted = router.delete(f"{BASE}/computers/vm-9/public-urls/{URL['id']}").respond(
            200, json={"ok": True}
        )

        result = await call("list", "vm-9")
        assert isinstance(result, mc.PublicUrls)
        assert result == mc.PublicUrls.from_api(listing)
        made = await call("create", "vm-9", 3000, label="demo", access="private")
        assert made == mc.PublicUrl.from_api(URL)
        assert json.loads(created.calls.last.request.content) == {
            "port": 3000,
            "label": "demo",
            "access": "private",
        }
        assert (await call("update", "vm-9", made.id, port=8080)).port == 8080
        assert json.loads(updated.calls.last.request.content) == {"port": 8080}
        assert await call("delete", "vm-9", made.id) is None
        assert not host.called
        assert [r.call_count for r in (listed, created, updated, deleted)] == [1, 1, 1, 1]
        assert len(router.calls) == 4


async def test_defaults_are_omitted_and_ids_remain_single_path_segments(call: Any) -> None:
    with respx.mock as router:
        collection = f"{BASE}/computers/vm%2Fa/public-urls"
        router.get(collection).respond(200, json=LISTING)
        created = router.post(collection).respond(201, json=URL)
        updated = router.patch(f"{collection}/pu%2Fb").respond(200, json=URL)
        router.delete(f"{collection}/pu%2Fb").respond(200, json={"ok": True})
        await call("list", "vm/a")
        await call("create", "vm/a", 3000)
        await call("update", "vm/a", "pu/b", access="private")
        await call("delete", "vm/a", "pu/b")
        assert json.loads(created.calls.last.request.content) == {"port": 3000}
        assert json.loads(updated.calls.last.request.content) == {"access": "private"}
        assert all(b"vm%2Fa" in c.request.url.raw_path for c in router.calls)
        assert b"pu%2Fb" in updated.calls.last.request.url.raw_path


@pytest.mark.parametrize(
    ("method", "args", "kwargs"),
    [
        ("list", [".."], {}),
        ("create", ["", 3000], {}),
        ("update", ["..", "pu-1"], {"port": 3000}),
        ("delete", ["", "pu-1"], {}),
        ("delete", ["vm-9", ".."], {}),
        ("update", ["vm-9", ""], {"port": 3000}),
        ("create", ["vm-9", 22], {}),
        ("create", ["vm-9", True], {}),
        ("create", ["vm-9", 3000], {"label": " "}),
        ("create", ["vm-9", 3000], {"access": "unknown"}),
        ("update", ["vm-9", "pu-1"], {}),
        ("update", ["vm-9", "pu-1"], {"port": 65536}),
        ("update", ["vm-9", "pu-1"], {"access": "unknown"}),
    ],
)
async def test_invalid_arguments_fail_before_network(
    call: Any, method: str, args: list[Any], kwargs: dict[str, Any]
) -> None:
    with respx.mock as router:
        with pytest.raises(ValueError):
            await call(method, *args, **kwargs)
        assert not router.calls


@pytest.mark.parametrize(
    ("method", "verb", "args", "kwargs", "suffix", "body"),
    [
        ("list", "GET", [], {}, "", {**LISTING, "limit": "two"}),
        ("create", "POST", [3000], {}, "", {**URL, "access": "unknown"}),
        ("update", "PATCH", ["pu-1"], {"port": 3000}, "/pu-1", {**URL, "port": None}),
        ("delete", "DELETE", ["pu-1"], {}, "/pu-1", []),
    ],
)
async def test_validation_and_api_error_codes_match_the_handle(
    call: Any,
    method: str,
    verb: str,
    args: list[Any],
    kwargs: dict[str, Any],
    suffix: str,
    body: Any,
) -> None:
    path = f"computers/vm-9/public-urls{suffix}"
    with respx.mock as router:
        route = router.route(method=verb, url=f"{BASE}/{path}").respond(200, json=body)
        with pytest.raises(mc.MandalaError, match=f"{verb} {path}"):
            await call(method, "vm-9", *args, **kwargs)
        route.respond(403, json={"error": "permission denied", "code": "PUBLIC_URLS_BLOCKED"})
        with pytest.raises(mc.PermissionDeniedError) as caught:
            await call(method, "vm-9", *args, **kwargs)
        assert caught.value.code == "PUBLIC_URLS_BLOCKED"
        assert route.call_count == 2
