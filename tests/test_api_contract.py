"""Catalogue metadata and foreground deadlines through both real transports."""

from __future__ import annotations

import inspect
import json
import math
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _api

BASE = "https://api.test/api/v1"
TEMPLATE = {
    "name": "base",
    "label": "Base",
    "os": "linux",
    "cpu": 2,
    "ram_mb": 2048,
    "disk_gb": 20,
    "ref": "system/base@1.0.0",
}
RESULT = {"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}


def test_foreground_timeout_enforcement_uses_mirrored_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_api, "MAX_EXEC_TIMEOUT_SECONDS", 2)
    assert _api.exec_body("true", 2)["timeout_s"] == 2
    with pytest.raises(ValueError, match="from 1 to 2"):
        _api.exec_body("true", 3)
    assert "timeout_s" not in _api.exec_body("true", 3, background=True)


@pytest.fixture(params=[False, True], ids=["sync", "async"])
async def client(request: pytest.FixtureRequest) -> Any:
    if request.param:
        async with mc.AsyncClient("gck_test", base_url=BASE) as client:
            yield client
    else:
        with mc.Client("gck_test", base_url=BASE) as client:
            yield client


async def resolved(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def computer(client: Any) -> Any:
    cls = mc.AsyncComputer if isinstance(client, mc.AsyncClient) else mc.Computer
    return cls(client._t, {"id": "vm-1", "os": "linux"})


@pytest.mark.parametrize("header", [None, "0", "3"])
@pytest.mark.parametrize("rows", [[], [TEMPLATE]])
@respx.mock
async def test_catalogue_completeness(client: Any, header: str | None, rows: list) -> None:
    headers = {} if header is None else {"X-GC-Incomplete": header}
    route = respx.get(f"{BASE}/templates").mock(httpx.Response(200, json=rows, headers=headers))
    listing = await resolved(client.templates.list())
    assert isinstance(listing, list)
    assert isinstance(listing, mc.Listing)
    assert listing.is_complete is (header is None)
    assert listing.incomplete == (None if header is None else int(header))
    assert [item.raw for item in listing] == rows
    assert not route.calls.last.request.url.params


@pytest.mark.parametrize("body", [{}, ["bad"], [None], [TEMPLATE, 1]])
@respx.mock
async def test_catalogue_rejects_malformed_rows(client: Any, body: Any) -> None:
    respx.get(f"{BASE}/templates").mock(
        httpx.Response(200, json=body, headers={"X-GC-Incomplete": "0"})
    )
    with pytest.raises(mc.MandalaError, match="array"):
        await resolved(client.templates.list())


@pytest.mark.parametrize("operation", ["list", "validate", "publish", "get"])
@pytest.mark.parametrize(
    "fields, expected",
    [
        ({}, (None, None)),
        ({"desktop": "wayland", "icon": "penguin"}, ("wayland", "penguin")),
        ({"desktop": None, "icon": None}, (None, None)),
        ({"desktop": 7, "icon": False}, ("7", "False")),
    ],
)
@respx.mock
async def test_template_display_fields(
    client: Any, operation: str, fields: dict, expected: tuple
) -> None:
    row = {**TEMPLATE, **fields}
    if operation == "list":
        respx.get(f"{BASE}/templates").mock(httpx.Response(200, json=[row]))
        template = (await resolved(client.templates.list()))[0]
    else:
        method = "GET" if operation == "get" else "POST"
        suffix = {"validate": "/validate", "publish": "", "get": "/system/base"}[operation]
        respx.route(method=method, url=f"{BASE}/templates{suffix}").mock(
            httpx.Response(200, json={"template": row, "valid": True})
        )
        args = ("system", "base") if operation == "get" else ("kind: Template",)
        template = (await resolved(getattr(client.templates, operation)(*args))).template
    assert (template.desktop, template.icon) == expected
    assert template.ref == TEMPLATE["ref"]
    assert template.raw == row


def test_template_preserves_all_positional_slots() -> None:
    raw = {"custom": "value"}
    template = mc.Template("base", "Base", "linux", 2, 2048, 20, raw)
    assert template.raw is raw
    assert (template.ref, template.desktop, template.icon) == (None, None, None)
    enriched = mc.Template(
        "base",
        "Base",
        "linux",
        2,
        2048,
        20,
        raw,
        ref="system/base@1.0.0",
        desktop="x11",
        icon="linux",
    )
    assert (enriched.desktop, enriched.icon) == ("x11", "linux")
    for name in ("ref", "desktop", "icon"):
        assert (
            inspect.signature(mc.Template).parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        )


@pytest.mark.parametrize("timeout", [1, 300, 301, 600, 1.0, 600.0])
@respx.mock
async def test_foreground_timeout_integer_wire(client: Any, timeout: Any) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json=RESULT))
    result = await resolved(computer(client).exec("true", timeout=timeout))
    assert result.ok
    body = json.loads(route.calls.last.request.content)
    assert body["timeout_s"] == timeout
    assert type(body["timeout_s"]) is int


@pytest.mark.parametrize(
    "timeout", [0, -1, 0.5, 1.5, 600.5, 601, True, False, math.inf, -math.inf, math.nan, "1", None]
)
@respx.mock
async def test_foreground_timeout_rejected_before_request(client: Any, timeout: Any) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json=RESULT))
    with pytest.raises(ValueError, match="timeout"):
        await resolved(computer(client).exec("true", timeout=timeout))
    assert not route.called


@respx.mock
async def test_background_and_fractional_input_waits(client: Any) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"pid": 123, "command": "work", "running": True})
    )
    await resolved(computer(client).start_exec("work"))
    assert json.loads(route.calls.last.request.content) == {"command": "work", "background": True}
    assert _api.exec_body("work", 601, background=True) == {"command": "work", "background": True}
    assert _api.wait_body(0.25)["duration"] == 0.25
    assert _api.hold_key_body(("A",), 0.25)["duration"] == 0.25


@respx.mock
async def test_internal_exec_keeps_fractional_http_cap(client: Any) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json=RESULT))
    await resolved(computer(client)._exec("true", 1, timeout_cap=0.25))
    assert json.loads(route.calls.last.request.content)["timeout_s"] == 1
    assert route.calls.last.request.extensions["timeout"]["read"] == 0.25
