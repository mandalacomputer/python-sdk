"""Preparation refusals require an explicit continuation of the original create."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from tests.test_client import BASE, COMPUTER

import mandala_computer as mc

ORIGINAL = {
    "name": "dev",
    "template": "example/tool@1.0.0",
    "cpu": 2,
    "ram_mb": 4096,
    "disk_gb": 40,
    "start": False,
    "resolution": "1920x1080",
}
TOKEN = " opaque-token "


def refusal(state: str = "preparing") -> dict:
    return {
        "error": "Template image is not ready",
        "code": "template_image_preparing",
        "template_transfer": TOKEN,
        "preparation": {
            "state": state,
            "error": "Image preparation failed" if state == "failed" else None,
        },
    }


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"template_transfer": "token"},
        *({"template": value, "template_transfer": "token"} for value in [None, 1, "", " \t"]),
        *(
            {"template": "base", "template_transfer": value}
            for value in [1, False, [], {}, "", " \t"]
        ),
        {"size": "small", "template_transfer": "token"},
        {"size": "small", "template": "base", "template_transfer": "token"},
    ],
)
async def test_invalid_continuation_never_sends_a_request(asynchronous, arguments) -> None:
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=COMPUTER)

    transport = httpx.MockTransport(respond)
    if asynchronous:
        async with (
            httpx.AsyncClient(transport=transport) as http,
            mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
        ):
            with pytest.raises(ValueError):
                await client.computers.create(**arguments)
    else:
        with (
            httpx.Client(transport=transport) as http,
            mc.Client("gck_test", base_url=BASE, http_client=http) as client,
            pytest.raises(ValueError),
        ):
            client.computers.create(**arguments)
    assert calls == []


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_explicit_continuation_preserves_original_body(asynchronous) -> None:
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(409, json=refusal(), headers={"Retry-After": "5"})
        return httpx.Response(200, json=COMPUTER)

    transport = httpx.MockTransport(respond)
    if asynchronous:
        async with (
            httpx.AsyncClient(transport=transport) as http,
            mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
        ):
            with pytest.raises(mc.ConflictError) as caught:
                await client.computers.create(**ORIGINAL)
            assert len(bodies) == 1
            c = await client.computers.create(
                **ORIGINAL, template_transfer=caught.value.body["template_transfer"]
            )
    else:
        with (
            httpx.Client(transport=transport) as http,
            mc.Client("gck_test", base_url=BASE, http_client=http) as client,
        ):
            with pytest.raises(mc.ConflictError) as caught:
                client.computers.create(**ORIGINAL)
            assert len(bodies) == 1
            c = client.computers.create(
                **ORIGINAL, template_transfer=caught.value.body["template_transfer"]
            )
    assert c.id == COMPUTER["id"]
    assert caught.value.retry_after == 5.0
    assert bodies == [ORIGINAL, {**ORIGINAL, "template_transfer": TOKEN}]


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("state", ["preparing", "failed"])
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("5", 5.0),
        ("2.5", 2.5),
        ("-5", 0.0),
        ("Wed, 09 Sep 2026 12:00:05 GMT", 5.0),
        ("Wednesday, 09-Sep-26 12:00:05 GMT", 5.0),
        ("Wed Sep  9 12:00:05 2026", 5.0),
        ("Wed, 09 Sep 2026 11:59:55 GMT", 0.0),
        (None, None),
        ("", None),
        ("nonsense", None),
        ("nan", None),
        ("inf", None),
        ("Wed, 99 Sep 2026 12:00:05 GMT", None),
    ],
)
async def test_preparation_error_retains_body_and_delay(
    asynchronous, state, header, expected, monkeypatch
) -> None:
    import mandala_computer._client as implementation

    monkeypatch.setattr(
        implementation, "time", SimpleNamespace(time=lambda: 1788955200), raising=False
    )
    body = refusal(state)
    headers = {} if header is None else {"Retry-After": header}
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json=body, headers=headers))
    if asynchronous:
        async with (
            httpx.AsyncClient(transport=transport) as http,
            mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
        ):
            with pytest.raises(mc.ConflictError) as caught:
                await client.computers.create(**ORIGINAL)
    else:
        with (
            httpx.Client(transport=transport) as http,
            mc.Client("gck_test", base_url=BASE, http_client=http) as client,
            pytest.raises(mc.ConflictError) as caught,
        ):
            client.computers.create(**ORIGINAL)
    assert caught.value.status == 409
    assert caught.value.body == body
    assert caught.value.retry_after == expected


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("state", ["preparing", "copying", "ready", "failed"])
@pytest.mark.parametrize("reason", [None, "starting"])
async def test_retry_helper_does_not_blindly_replay_preparation(
    asynchronous, state, reason
) -> None:
    calls = []
    body = refusal(state)
    if reason is not None:
        body["reason"] = reason

    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(409, json=body, headers={"Retry-After": "5"})
        return httpx.Response(200, json=COMPUTER)

    async def async_retry(client):
        for _ in range(2):
            try:
                return await client.computers.create(**ORIGINAL)
            except mc.APIError as err:
                if not mc.is_transient(err):
                    raise

    def sync_retry(client):
        for _ in range(2):
            try:
                return client.computers.create(**ORIGINAL)
            except mc.APIError as err:
                if not mc.is_transient(err):
                    raise

    transport = httpx.MockTransport(respond)
    if asynchronous:
        async with (
            httpx.AsyncClient(transport=transport) as http,
            mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
        ):
            with pytest.raises(mc.ConflictError):
                await async_retry(client)
    else:
        with (
            httpx.Client(transport=transport) as http,
            mc.Client("gck_test", base_url=BASE, http_client=http) as client,
            pytest.raises(mc.ConflictError),
        ):
            sync_retry(client)
    assert len(calls) == 1


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize(
    ("status", "body", "expected_type"),
    [
        (400, {"error": "Invalid request"}, mc.APIError),
        (409, {"error": "Busy"}, mc.ConflictError),
        (
            409,
            {"error": "Move required", "move": {"required": True, "possible": True}},
            mc.MoveRequiredError,
        ),
        (416, {"error": "Range unavailable"}, mc.RangeNotSatisfiableError),
        (429, {"error": "Slow down"}, mc.RateLimitError),
        (503, {"error": "Unavailable"}, mc.UnavailableError),
    ],
)
async def test_retry_after_survives_other_api_error_mappings(
    asynchronous, status, body, expected_type
) -> None:
    headers = {"Retry-After": "7", "Content-Range": "bytes */12"}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json=body, headers=headers)
    )
    if asynchronous:
        async with (
            httpx.AsyncClient(transport=transport) as http,
            mc.AsyncClient("gck_test", base_url=BASE, http_client=http) as client,
        ):
            with pytest.raises(expected_type) as caught:
                await client.computers.create(**ORIGINAL)
    else:
        with (
            httpx.Client(transport=transport) as http,
            mc.Client("gck_test", base_url=BASE, http_client=http) as client,
            pytest.raises(expected_type) as caught,
        ):
            client.computers.create(**ORIGINAL)
    assert caught.value.retry_after == 7.0
    assert caught.value.body == body
    if isinstance(caught.value, mc.MoveRequiredError):
        assert caught.value.move_possible is True
    if isinstance(caught.value, mc.RangeNotSatisfiableError):
        assert caught.value.size == 12
