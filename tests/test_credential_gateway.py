"""The credential gateway: a secret added to one computer's calls to one HTTPS
API, without the computer ever holding it. Listing, making and revoking
bindings on a handle and by the computer's id, on both clients; what is refused
before a request; and how strictly an answer is read."""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
BINDING = {
    "id": "cgw-0123456789abcdef01234567",
    "computer_id": "vm-9",
    "secret_id": "csec-0123456789abcdef",
    "origin": "https://api.example.com",
    "path_prefix": "/repos/example",
    "methods": ["GET", "POST"],
    "created_at": "2026-10-29T00:00:00.000Z",
    "base_url": "http://gateway.invalid/cgw-0123456789abcdef01234567",
    "placeholder": "placeholder-token",
}
ARGS: dict[str, Any] = {
    "secret_id": "csec-0123456789abcdef",
    "origin": "https://api.example.com",
    "path_prefix": "/repos/example",
    "methods": ["GET", "POST"],
}
COLLECTION = f"{BASE}/computers/vm-9/credential-gateway"


@pytest.fixture(params=["sync-handle", "sync-id", "async-handle", "async-id"])
async def gw(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Any]:
    """The three calls, through each of the four ways in, under one spelling."""
    monkeypatch.setenv("HOME", str(tmp_path))
    is_async = request.param.startswith("async")
    by_id = request.param.endswith("id")
    client = (mc.AsyncClient if is_async else mc.Client)(
        api_key="com_test", base_url=BASE, retries={"idempotent": 0}
    )
    handle = (mc.AsyncComputer if is_async else mc.Computer)(client._t, {"id": "vm-9"})

    async def invoke(op: str, *args: Any, **kwargs: Any) -> Any:
        if by_id:
            fn = getattr(client.credential_gateway, op)
            result = fn("vm-9", *args, **kwargs)
        else:
            name = {
                "list": "credential_gateway_bindings",
                "create": "create_credential_gateway_binding",
                "delete": "delete_credential_gateway_binding",
            }[op]
            result = getattr(handle, name)(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result

    try:
        yield invoke
    finally:
        if isinstance(client, mc.AsyncClient):
            await client.aclose()
        else:
            client.close()


async def test_list_create_and_revoke(gw: Any) -> None:
    with respx.mock as router:
        host = router.get(f"{BASE}/computers/vm-9").respond(503, json={"error": "down"})
        listed = router.get(COLLECTION).respond(200, json={"bindings": [BINDING]})
        created = router.post(COLLECTION).respond(201, json=BINDING)
        revoked = router.delete(f"{COLLECTION}/{BINDING['id']}").respond(200, json={"ok": True})

        bindings = await gw("list")
        assert bindings == [
            mc.CredentialGatewayBinding(
                id="cgw-0123456789abcdef01234567",
                computer_id="vm-9",
                secret_id="csec-0123456789abcdef",
                origin="https://api.example.com",
                path_prefix="/repos/example",
                methods=["GET", "POST"],
                created_at="2026-10-29T00:00:00.000Z",
                base_url="http://gateway.invalid/cgw-0123456789abcdef01234567",
                placeholder="placeholder-token",
            )
        ]
        made = await gw("create", **ARGS)
        assert made == bindings[0]
        assert made.raw == BINDING
        assert json.loads(created.calls.last.request.content) == ARGS
        assert await gw("delete", made.id) is None
        assert [r.call_count for r in (listed, created, revoked)] == [1, 1, 1]
        assert not host.called


async def test_an_empty_listing_reads_as_no_bindings(gw: Any) -> None:
    with respx.mock as router:
        router.get(COLLECTION).respond(200, json={"bindings": []})
        assert await gw("list") == []


async def test_a_binding_id_stays_one_path_segment(gw: Any) -> None:
    with respx.mock as router:
        revoked = router.delete(f"{COLLECTION}/cgw%2F..%2Fx").respond(200, json={"ok": True})
        await gw("delete", "cgw/../x")
        assert revoked.called


async def test_a_missing_binding_is_not_found(gw: Any) -> None:
    with respx.mock as router:
        router.delete(f"{COLLECTION}/cgw-000000000000000000000000").respond(
            404, json={"error": "binding not found"}
        )
        with pytest.raises(mc.NotFoundError):
            await gw("delete", "cgw-000000000000000000000000")


async def test_the_platforms_refusal_is_passed_on(gw: Any) -> None:
    with respx.mock as router:
        router.post(COLLECTION).respond(
            409,
            json={"error": "Computer scope or placement is changing.", "reason": "contention"},
        )
        with pytest.raises(mc.ConflictError):
            await gw("create", **ARGS)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"secret_id": ""}, "secret_id must not be empty"),
        ({"secret_id": "  "}, "secret_id must not be empty"),
        ({"secret_id": 7}, "secret_id must be a string"),
        ({"origin": "http://api.example.com"}, "exact HTTPS origin"),
        ({"origin": "https://api.example.com/zen"}, "exact HTTPS origin"),
        ({"origin": "https://api.example.com:8443"}, "exact HTTPS origin"),
        ({"origin": "https://api.example.com:443"}, "exact HTTPS origin"),
        ({"origin": "https://api.example.com:"}, "exact HTTPS origin"),
        ({"origin": "https://user:pw@api.example.com"}, "exact HTTPS origin"),
        ({"origin": "https://api.example.com?x=1"}, "exact HTTPS origin"),
        ({"origin": "https://api.example.com#x"}, "exact HTTPS origin"),
        ({"origin": "https://localhost"}, "exact HTTPS origin"),
        ({"origin": "https://[::1]"}, "exact HTTPS origin"),
        ({"origin": "https://10.0.0.1"}, "not an IP address"),
        ({"origin": "https://api.example .com"}, "exact HTTPS origin"),
        ({"origin": "api.example.com"}, "exact HTTPS origin"),
        ({"origin": "https://" + "a" * 250 + ".com"}, "exact HTTPS origin"),
        ({"path_prefix": "repos"}, "canonical absolute path"),
        ({"path_prefix": ""}, "canonical absolute path"),
        ({"path_prefix": "//repos"}, "canonical absolute path"),
        ({"path_prefix": "/repos//x"}, "canonical absolute path"),
        ({"path_prefix": "/repos?x=1"}, "canonical absolute path"),
        ({"path_prefix": "/repos#x"}, "canonical absolute path"),
        ({"path_prefix": "/repos/../admin"}, "canonical absolute path"),
        ({"path_prefix": "/repos/./x"}, "canonical absolute path"),
        ({"path_prefix": "/repos\\x"}, "canonical absolute path"),
        ({"path_prefix": "/rep os"}, "canonical absolute path"),
        ({"path_prefix": "/répos"}, "canonical absolute path"),
        ({"methods": "GET"}, "must be a list"),
        ({"methods": []}, "1 to 6 HTTP methods"),
        ({"methods": ["GET"] * 7}, "1 to 6 HTTP methods"),
        ({"methods": ["get"]}, "in capitals"),
        ({"methods": ["CONNECT"]}, "in capitals"),
        ({"methods": ["GET", "GET"]}, "must not repeat"),
        ({"methods": [1]}, "methods must be a string"),
        ({"auth_header": "X-API-Key"}, "auth_header must be"),
        ({"auth_header": "bearer"}, "auth_header must be"),
        ({"auth_header": 7}, "auth_header must be"),
    ],
)
async def test_what_the_platform_would_refuse_is_refused_first(
    gw: Any, change: dict[str, Any], message: str
) -> None:
    with respx.mock as router:
        route = router.post(COLLECTION).respond(201, json=BINDING)
        with pytest.raises(ValueError, match=message):
            await gw("create", **{**ARGS, **change})
        assert not route.called


@pytest.mark.parametrize(
    "origin",
    ["https://api.example.com", "https://api.example.com/", "HTTPS://API.EXAMPLE.COM"],
)
@pytest.mark.parametrize("path_prefix", ["/", "/repos/example/", "/v1/%7Euser"])
async def test_valid_policies_are_sent_as_given(gw: Any, origin: str, path_prefix: str) -> None:
    with respx.mock as router:
        route = router.post(COLLECTION).respond(201, json=BINDING)
        await gw(
            "create",
            **{**ARGS, "origin": origin, "path_prefix": path_prefix, "methods": ("HEAD",)},
        )
        assert json.loads(route.calls.last.request.content) == {
            **ARGS,
            "origin": origin,
            "path_prefix": path_prefix,
            "methods": ["HEAD"],
        }


@pytest.mark.parametrize("auth_header", ["x-api-key", "authorization"])
async def test_auth_header_is_sent_when_given(gw: Any, auth_header: str) -> None:
    with respx.mock as router:
        route = router.post(COLLECTION).respond(201, json={**BINDING, "auth_header": auth_header})
        made = await gw("create", **ARGS, auth_header=auth_header)
        assert json.loads(route.calls.last.request.content) == {**ARGS, "auth_header": auth_header}
        assert made.auth_header == auth_header


async def test_no_auth_header_sends_none(gw: Any) -> None:
    # Left out, a platform that predates the field still takes the binding.
    with respx.mock as router:
        route = router.post(COLLECTION).respond(201, json=BINDING)
        await gw("create", **ARGS, auth_header=None)
        assert "auth_header" not in json.loads(route.calls.last.request.content)


@pytest.mark.parametrize(
    ("sent", "read"),
    [
        ("x-api-key", "x-api-key"),
        (None, "authorization"),
        ("", "authorization"),
        (7, "authorization"),
        ("x-other-key", "x-other-key"),
    ],
)
async def test_auth_header_is_read_with_authorization_as_its_default(
    gw: Any, sent: Any, read: str
) -> None:
    with respx.mock as router:
        router.get(COLLECTION).respond(200, json={"bindings": [{**BINDING, "auth_header": sent}]})
        [binding] = await gw("list")
        assert binding.auth_header == read


@pytest.mark.parametrize(
    "row",
    [
        {k: v for k, v in BINDING.items() if k != "id"},
        {**BINDING, "id": ""},
        {**BINDING, "origin": None},
        {**BINDING, "path_prefix": 7},
        {**BINDING, "methods": []},
        {**BINDING, "methods": "GET"},
        {**BINDING, "methods": ["CONNECT"]},
        {k: v for k, v in BINDING.items() if k != "base_url"},
        {**BINDING, "placeholder": ""},
    ],
)
async def test_an_unreadable_binding_refuses_the_answer(gw: Any, row: dict[str, Any]) -> None:
    with respx.mock as router:
        router.get(COLLECTION).respond(200, json={"bindings": [BINDING, row]})
        router.post(COLLECTION).respond(201, json=row)
        with pytest.raises(mc.MandalaError, match="credential gateway binding"):
            await gw("list")
        with pytest.raises(mc.MandalaError, match="credential gateway binding"):
            await gw("create", **ARGS)


@pytest.mark.parametrize("body", [{}, {"bindings": None}, {"bindings": {}}, {"bindings": [7]}])
async def test_a_listing_that_is_not_one_is_refused(gw: Any, body: dict[str, Any]) -> None:
    with respx.mock as router:
        router.get(COLLECTION).respond(200, json=body)
        with pytest.raises(mc.MandalaError, match="credential gateway binding"):
            await gw("list")


@respx.mock
def test_the_body_omits_nothing_and_adds_nothing() -> None:
    route = respx.post(COLLECTION).mock(return_value=httpx.Response(201, json=BINDING))
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        client.credential_gateway.create("vm-9", **ARGS)
    assert sorted(json.loads(route.calls.last.request.content)) == [
        "methods",
        "origin",
        "path_prefix",
        "secret_id",
    ]
