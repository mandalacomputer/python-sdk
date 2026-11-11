"""What a subscription reports about itself (``last_error``, ``created_by``),
paging and filtering its deliveries, and what revoking the key that made it
says — sync, async and the CLI."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414

import mandala_computer as mc
from mandala_computer import _cli

HOOK_ID = "whk-2b7d4c809f3c1a7e"
LAST_ERROR = {
    "at": "2026-11-08T10:00:10.000Z",
    "status": None,
    "error": "timeout",
    "delivery": "whd-0123456789abcdef",
}
WEBHOOK: dict[str, Any] = {
    "id": HOOK_ID,
    "url": "https://ci.example.com/mandala",
    "description": "CI",
    "events": [],
    "computers": [],
    "enabled": False,
    "disabled_reason": "key_revoked",
    "disabled_at": "2026-11-08T11:00:00.000Z",
    "last_success_at": None,
    "last_failure_at": "2026-11-08T10:00:10.000Z",
    "last_status": None,
    "last_error": LAST_ERROR,
    "created_by": "apikey:key-a1b2c3d4e5f6",
    "created_at": "2026-11-08T09:00:00.000Z",
    "updated_at": "2026-11-08T11:00:00.000Z",
}
DELIVERY: dict[str, Any] = {
    "id": "whd-0123456789abcdef",
    "event_type": "process.exited",
    "computer": "vm-1",
    "cursor": "c:1",
    "state": "exhausted",
    "attempts": 8,
    "next_at": None,
    "attempted_at": "2026-11-08T10:00:00.000Z",
    "last_status": None,
    "last_error": "timeout",
    "delivered_at": None,
    "created_at": "2026-11-07T20:00:00.000Z",
}


def test_last_error_and_created_by_are_read_off_a_subscription() -> None:
    hook = mc.Webhook.from_api(WEBHOOK)
    assert hook.last_error == mc.WebhookLastError(
        "2026-11-08T10:00:10.000Z", None, "timeout", "whd-0123456789abcdef"
    )
    assert hook.created_by == "apikey:key-a1b2c3d4e5f6"
    assert hook.disabled_reason == "key_revoked"


def test_the_created_subscription_carries_them_too() -> None:
    hook = mc.WebhookCreated.from_api(
        {**WEBHOOK, "last_error": {**LAST_ERROR, "status": 503}, "secret": "whsec_x"}
    )
    assert hook.last_error is not None and hook.last_error.status == 503
    assert hook.created_by == "apikey:key-a1b2c3d4e5f6"


@pytest.mark.parametrize("value", [None, "broken", 3, []])
def test_an_absent_or_unreadable_last_error_is_none(value: Any) -> None:
    assert mc.Webhook.from_api({**WEBHOOK, "last_error": value}).last_error is None


def test_a_platform_without_the_fields_reads_none() -> None:
    old = {k: v for k, v in WEBHOOK.items() if k not in ("last_error", "created_by")}
    hook = mc.Webhook.from_api(old)
    assert hook.last_error is None and hook.created_by is None


@respx.mock
async def test_deliveries_send_paging_and_state_only_when_asked(client: Any) -> None:
    route = respx.get(f"{BASE}/webhooks/{HOOK_ID}/deliveries").mock(
        return_value=httpx.Response(200, json=[DELIVERY])
    )
    await resolved(client.webhooks.deliveries(HOOK_ID))
    assert route.calls.last.request.url.query == b""
    rows = await resolved(
        client.webhooks.deliveries(
            HOOK_ID, before="whd-00000000000000ff", limit=25, state="exhausted"
        )
    )
    assert dict(route.calls.last.request.url.params) == {
        "before": "whd-00000000000000ff",
        "limit": "25",
        "state": "exhausted",
    }
    assert rows[0].state == "exhausted"


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": 101}, {"limit": True}, {"state": "lost"}, {"before": ""}],
    ids=["limit-0", "limit-101", "limit-bool", "state", "before-empty"],
)
@respx.mock
async def test_a_bad_paging_argument_is_refused_before_anything_is_sent(
    client: Any, kwargs: dict[str, Any]
) -> None:
    route = respx.get(f"{BASE}/webhooks/{HOOK_ID}/deliveries")
    with pytest.raises(ValueError):
        await resolved(client.webhooks.deliveries(HOOK_ID, **kwargs))
    assert not route.called


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


@respx.mock
def test_cli_deliveries_pass_paging_and_state(cli_env: None) -> None:
    route = respx.get(f"{BASE}/webhooks/{HOOK_ID}/deliveries").mock(
        return_value=httpx.Response(200, json=[DELIVERY])
    )
    argv = ["webhooks", "deliveries", HOOK_ID, "--before", "whd-1", "--limit", "5"]
    assert _cli.main([*argv, "--state", "dropped", "--json"]) == 0
    assert dict(route.calls.last.request.url.params) == {
        "before": "whd-1",
        "limit": "5",
        "state": "dropped",
    }


# --- API key revoke ------------------------------------------------------------

KEY_ID = "key-a1b2c3d4e5f6"
LEFT_ON = [
    {"id": HOOK_ID, "url": "https://ci.example.com/mandala", "enabled": True},
    {"id": "whk-0000000000000002", "url": "https://other.example.com/", "enabled": False},
]


@respx.mock
async def test_revoke_answers_the_subscriptions_it_left_on(client: Any) -> None:
    respx.delete(f"{BASE}/api-keys/{KEY_ID}").mock(
        return_value=httpx.Response(200, json={"ok": True, "webhooks": LEFT_ON})
    )
    revoked = await resolved(client.api_keys.revoke(KEY_ID))
    assert revoked.webhooks == [
        mc.RevokedKeyWebhook(HOOK_ID, "https://ci.example.com/mandala", True),
        mc.RevokedKeyWebhook("whk-0000000000000002", "https://other.example.com/", False),
    ]


@respx.mock
async def test_revoke_with_none_made_through_the_key_answers_an_empty_list(client: Any) -> None:
    respx.delete(f"{BASE}/api-keys/{KEY_ID}").mock(
        return_value=httpx.Response(200, json={"ok": True, "webhooks": []})
    )
    assert (await resolved(client.api_keys.revoke(KEY_ID))).webhooks == []


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"ok": True}),
        httpx.Response(200, content=b""),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"ok": True, "webhooks": [{"id": HOOK_ID}]}),
        httpx.Response(200, json={"ok": True, "webhooks": "x"}),
    ],
    ids=["older-platform", "empty", "not-json", "row-without-enabled", "not-a-list"],
)
@respx.mock
async def test_a_revoke_answer_that_does_not_say_is_none_not_an_error(
    client: Any, answer: httpx.Response
) -> None:
    """The key is revoked by the time the answer is read, so an answer that
    cannot be read is not a failure, and is not "none" either."""
    respx.delete(f"{BASE}/api-keys/{KEY_ID}").mock(return_value=answer)
    assert (await resolved(client.api_keys.revoke(KEY_ID))).webhooks is None


@respx.mock
def test_cli_revoke_names_the_subscriptions_still_delivering(
    cli_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.delete(f"{BASE}/api-keys/{KEY_ID}").mock(
        return_value=httpx.Response(200, json={"ok": True, "webhooks": LEFT_ON})
    )
    assert _cli.main(["api-keys", "revoke", KEY_ID]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"revoked {KEY_ID}\n"
    assert HOOK_ID in captured.err and "still delivering" in captured.err
    assert "whk-0000000000000002" not in captured.err
    assert _cli.main(["api-keys", "revoke", KEY_ID, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "id": KEY_ID,
        "revoked": True,
        "webhooks": LEFT_ON,
    }
