"""Account webhooks (OPL-4302): the verifier, the resource, and the CLI's CRUD.

The verifier is the part a customer codes against and the platform cannot
change afterwards, so it is pinned to the §3.2 vectors of the platform's
account-webhooks plan — signed by two implementations there and recomputed by
a third, the published ``standardwebhooks`` package, before this file was
written — and to the three negatives the plan says every SDK's suite should
hold. The resource tests own the seam: what goes on the wire for each verb,
what a caller can and cannot express, and that the one shape carrying a secret
is the one answered by exactly two calls.

Both halves of the client, because both have the methods and
:mod:`tests.test_parity` only proves the signatures match.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _api, _cli
from mandala_computer._webhooks import REPLAY_WINDOW_S, SECRET_PREFIX

BASE = "https://api.test/api/v1"

# --- the §3.2 vectors, verbatim -----------------------------------------------

SECRET = "whsec_bWFuZGFsYS13ZWJob29rLXRlc3QtdmVjdG9yLWtleSE="
PREVIOUS_SECRET = "whsec_bWFuZGFsYS13ZWJob29rLXByZXZpb3VzLXNlY3JldDE="
ID = "whd-9f3c1a7e5b2d4c80"
TIMESTAMP = 1788264000  # 2026-09-01T12:00:00Z
BODY = (
    b'{"seq":41,"cursor":"mfc9z1k2x5ab.7:42","at":"2026-09-01T12:00:00.123456Z",'
    b'"type":"process.exited","computer":"vm-3f9a1c2b7d4e","source":"daemon",'
    b'"data":{"pid":4242,"exit_code":0}}'
)
SIGNATURE = "v1,PP7CJPCiIF9oXT07KaqThULfAcUn2NHnqw4RGHtpMpQ="
# The same delivery while a previous secret is inside its 24-hour grace: two
# signatures on the one header, new first.
ROTATION_SIGNATURE = (
    "v1,PP7CJPCiIF9oXT07KaqThULfAcUn2NHnqw4RGHtpMpQ= "
    "v1,v4aDLFaUcddhjKlS/A8H3yoTT/1JXDQahq4PtBhhq04="
)
# The negatives: what the primary secret signs when one thing changes.
SIGNATURE_ONE_SECOND_LATER = "v1,nkwCc4sFVT7w35kerNFmS9pxAIFHpB20av8iDbuTP3Y="
SIGNATURE_TRAILING_SPACE = "v1,8cpgEE8mQ0ngAOh7RdvX/dw74GipMz0nPdoXzLnoWx0="


def headers(signature: str = SIGNATURE, timestamp: int | str = TIMESTAMP) -> dict[str, str]:
    return {
        "webhook-id": ID,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": signature,
    }


def test_the_vector_is_the_one_the_plan_published() -> None:
    """The body is 179 bytes with the published digest, and the plan's own
    signing recipe over it yields the published signature. If this fails the
    vector was mistyped, and every test below is testing something else."""
    assert len(BODY) == 179
    assert (
        hashlib.sha256(BODY).hexdigest()
        == "455a358584a1d8c47bb2157ea07fd20f6a26595a6ecd991604564636b3ec95ee"
    )
    key = base64.b64decode(SECRET[len(SECRET_PREFIX) :])
    assert key == b"mandala-webhook-test-vector-key!"
    signed = ID.encode() + b"." + str(TIMESTAMP).encode() + b"." + BODY
    mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert "v1," + mac == SIGNATURE


def test_the_primary_vector_verifies() -> None:
    assert mc.verify(SECRET, headers(), BODY, now=TIMESTAMP) is True


def test_a_rotation_header_verifies_against_either_secret() -> None:
    """Two signatures, new first; a receiver on either secret passes throughout."""
    assert mc.verify(SECRET, headers(ROTATION_SIGNATURE), BODY, now=TIMESTAMP)
    assert mc.verify(PREVIOUS_SECRET, headers(ROTATION_SIGNATURE), BODY, now=TIMESTAMP)
    # And the previous secret alone does NOT verify the single-signature header.
    assert not mc.verify(PREVIOUS_SECRET, headers(), BODY, now=TIMESTAMP)


def test_negative_the_timestamp_is_signed() -> None:
    """The signature over 1788264001 differs, so presenting it with the
    original timestamp fails — and presenting it WITH its own timestamp passes,
    which is what proves the negative is a real signature and not a typo."""
    assert not mc.verify(SECRET, headers(SIGNATURE_ONE_SECOND_LATER), BODY, now=TIMESTAMP)
    assert mc.verify(
        SECRET, headers(SIGNATURE_ONE_SECOND_LATER, TIMESTAMP + 1), BODY, now=TIMESTAMP
    )


def test_negative_the_raw_bytes_are_signed() -> None:
    """One trailing space changes the signature. A verifier that re-serialised
    or stripped the body could not tell these two apart."""
    assert not mc.verify(SECRET, headers(SIGNATURE_TRAILING_SPACE), BODY, now=TIMESTAMP)
    assert mc.verify(SECRET, headers(SIGNATURE_TRAILING_SPACE), BODY + b" ", now=TIMESTAMP)
    assert not mc.verify(SECRET, headers(), BODY + b" ", now=TIMESTAMP)


def test_negative_outside_the_replay_window_is_refused_before_the_mac() -> None:
    """±300 s, judged by the receiver's clock. The boundary itself is inside."""
    assert REPLAY_WINDOW_S == 300
    assert mc.verify(SECRET, headers(), BODY, now=TIMESTAMP + 300)
    assert mc.verify(SECRET, headers(), BODY, now=TIMESTAMP - 300)
    assert not mc.verify(SECRET, headers(), BODY, now=TIMESTAMP + 301)
    assert not mc.verify(SECRET, headers(), BODY, now=TIMESTAMP - 301)
    # The window is a parameter, for a receiver that knows its clock is worse.
    assert mc.verify(SECRET, headers(), BODY, now=TIMESTAMP + 301, tolerance=600)
    assert not mc.verify(SECRET, headers(), BODY, now=TIMESTAMP + 1, tolerance=0)


def test_the_clock_defaults_to_now() -> None:
    """A day-old vector is refused on the real clock; a fresh signature passes."""
    assert not mc.verify(SECRET, headers(), BODY)
    import time

    now = int(time.time())
    key = base64.b64decode(SECRET[len(SECRET_PREFIX) :])
    signed = ID.encode() + b"." + str(now).encode() + b"." + BODY
    mac = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert mc.verify(SECRET, headers("v1," + mac, now), BODY)


def test_header_names_are_case_insensitive() -> None:
    """HTTP says so; a framework hands them over however it likes."""
    upper = {k.upper(): v for k, v in headers().items()}
    title = {"Webhook-Id": ID, "Webhook-Timestamp": str(TIMESTAMP), "Webhook-Signature": SIGNATURE}
    assert mc.verify(SECRET, upper, BODY, now=TIMESTAMP)
    assert mc.verify(SECRET, title, BODY, now=TIMESTAMP)
    # httpx's own case-insensitive mapping, which is what a test client gives.
    assert mc.verify(SECRET, httpx.Headers(title), BODY, now=TIMESTAMP)


@pytest.mark.parametrize("missing", ["webhook-id", "webhook-timestamp", "webhook-signature"])
def test_a_missing_header_is_false_not_an_error(missing: str) -> None:
    h = headers()
    del h[missing]
    assert mc.verify(SECRET, h, BODY, now=TIMESTAMP) is False


@pytest.mark.parametrize(
    "stamp",
    ["", "abc", "1788264000.5", "-1788264000", "+1788264000", "1_788_264_000", "١٧٨٨"],
)
def test_a_timestamp_that_is_not_decimal_seconds_is_false(stamp: str) -> None:
    """Digits only. ``int()`` takes several of these; a verifier must not."""
    assert mc.verify(SECRET, headers(timestamp=stamp), BODY, now=TIMESTAMP) is False


def test_an_unknown_signature_version_is_ignored_not_refused() -> None:
    """The spec's growth path: a ``v1a`` entry beside a good ``v1`` still passes,
    and an unknown version ALONE fails rather than passing by accident."""
    assert mc.verify(SECRET, headers("v1a,AAAA " + SIGNATURE), BODY, now=TIMESTAMP)
    assert not mc.verify(SECRET, headers("v1a,AAAA"), BODY, now=TIMESTAMP)
    assert not mc.verify(SECRET, headers("v2," + SIGNATURE[3:]), BODY, now=TIMESTAMP)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "v1",
        "v1,",
        "v1,not base64!",
        "v1,AAAA",  # decodes, wrong length
        "PP7CJPCiIF9oXT07KaqThULfAcUn2NHnqw4RGHtpMpQ=",  # no version
        "v1;PP7CJPCiIF9oXT07KaqThULfAcUn2NHnqw4RGHtpMpQ=",
    ],
)
def test_a_malformed_signature_entry_is_false(bad: str) -> None:
    assert mc.verify(SECRET, headers(bad), BODY, now=TIMESTAMP) is False


def test_a_malformed_entry_beside_a_good_one_does_not_spoil_it() -> None:
    assert mc.verify(SECRET, headers("v1,not-base64! " + SIGNATURE), BODY, now=TIMESTAMP)
    assert mc.verify(SECRET, headers(SIGNATURE + "  v1,AAAA"), BODY, now=TIMESTAMP)


def test_a_signature_with_the_wrong_key_is_false() -> None:
    other = SECRET_PREFIX + base64.b64encode(b"x" * 32).decode()
    assert mc.verify(other, headers(), BODY, now=TIMESTAMP) is False


@pytest.mark.parametrize(
    "secret",
    [
        "bWFuZGFsYS13ZWJob29rLXRlc3QtdmVjdG9yLWtleSE=",  # no prefix
        "whsec_",  # no key
        "whsec_not base64!",
        "whsec_bWFuZGFsYS13ZWJob29rLXRlc3QtdmVjdG9yLWtleSE",  # padding dropped
        "WHSEC_bWFuZGFsYS13ZWJob29rLXRlc3QtdmVjdG9yLWtleSE=",
    ],
)
def test_a_secret_of_the_wrong_shape_is_a_configuration_error(secret: str) -> None:
    """Raised, not False: nothing a forged request can cause, and a False would
    send the caller looking at the sender."""
    with pytest.raises(ValueError, match="secret"):
        mc.verify(secret, headers(), BODY, now=TIMESTAMP)


def test_a_secret_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(ValueError, match="secret must be a string"):
        mc.verify(SECRET.encode(), headers(), BODY, now=TIMESTAMP)  # type: ignore[arg-type]


def test_a_text_body_is_refused_before_anything_is_compared() -> None:
    """The obvious mistake — ``json.dumps(request.json)`` — is loud."""
    with pytest.raises(ValueError, match="bytes"):
        mc.verify(SECRET, headers(), BODY.decode(), now=TIMESTAMP)  # type: ignore[arg-type]


def test_bytes_like_bodies_are_taken_as_their_bytes() -> None:
    assert mc.verify(SECRET, headers(), bytearray(BODY), now=TIMESTAMP)
    assert mc.verify(SECRET, headers(), memoryview(BODY), now=TIMESTAMP)


def test_a_header_value_that_is_not_text_is_false() -> None:
    """A list of values, or bytes, is not a signature that can be checked."""
    h: dict[str, Any] = headers()
    h["webhook-signature"] = [SIGNATURE]
    assert mc.verify(SECRET, h, BODY, now=TIMESTAMP) is False
    h["webhook-signature"] = SIGNATURE.encode()
    assert mc.verify(SECRET, h, BODY, now=TIMESTAMP) is False


def test_the_replay_window_is_the_platforms_number() -> None:
    """Exported at the top level, and the same object the drift check reads."""
    assert mc.REPLAY_WINDOW_S is _api.WEBHOOK_REPLAY_WINDOW_S


# --- the body builder ----------------------------------------------------------


def test_create_needs_a_url_and_sends_only_what_was_named() -> None:
    assert _api.webhook_body(create=True, url="https://x.example/h") == {
        "url": "https://x.example/h"
    }
    with pytest.raises(ValueError, match="url is required"):
        _api.webhook_body(create=True)


def test_update_must_name_something() -> None:
    with pytest.raises(ValueError, match="at least one field"):
        _api.webhook_body(create=False)


@pytest.mark.parametrize(
    "url", ["http://ci.example.com/h", "https://", "ci.example.com/h", "", " https://x"]
)
def test_an_endpoint_that_can_never_pass_is_refused_here(url: str) -> None:
    with pytest.raises(ValueError, match="https://"):
        _api.webhook_body(create=True, url=url)


def test_an_endpoint_the_platform_has_to_judge_is_sent() -> None:
    """Credentials in the URL and a private address are the platform's checks —
    they need a resolver — so the SDK does not pretend to make them."""
    body = _api.webhook_body(create=True, url="https://user:pw@10.0.0.1:8443/h")
    assert body["url"] == "https://user:pw@10.0.0.1:8443/h"


def test_an_empty_list_is_sent_because_it_clears_a_filter() -> None:
    body = _api.webhook_body(create=False, events=[], computers=[])
    assert body == {"events": [], "computers": []}


@pytest.mark.parametrize("value", ["process.exited", b"process.exited", 3, {"a": 1}, None])
def test_a_filter_that_is_not_a_list_is_refused(value: object) -> None:
    """A bare string most of all: it is a Sequence of its letters (OPL-4220)."""
    with pytest.raises(ValueError, match="list of strings"):
        _api.webhook_body(create=False, events=value)


def test_a_filter_entry_must_be_a_non_empty_string() -> None:
    with pytest.raises(ValueError, match="entry must be a string"):
        _api.webhook_body(create=False, events=["process.exited", 7])
    with pytest.raises(ValueError, match="empty string"):
        _api.webhook_body(create=False, computers=["vm-1", " "])


def test_the_computer_cap_is_the_platforms() -> None:
    assert _api.WEBHOOK_COMPUTERS_MAX == 64
    _api.webhook_body(create=False, computers=[f"vm-{i}" for i in range(64)])
    with pytest.raises(ValueError, match="at most 64"):
        _api.webhook_body(create=False, computers=[f"vm-{i}" for i in range(65)])


def test_the_description_cap_is_the_platforms() -> None:
    assert _api.WEBHOOK_DESCRIPTION_MAX == 200
    assert _api.webhook_body(create=False, description="x" * 200)["description"] == "x" * 200
    with pytest.raises(ValueError, match="at most 200"):
        _api.webhook_body(create=False, description="x" * 201)


def test_enabled_is_a_real_bool() -> None:
    """``"false"`` arms deliveries; the flag guard every other arming flag has."""
    with pytest.raises(ValueError, match="True or False"):
        _api.webhook_body(create=False, enabled="false")
    assert _api.webhook_body(create=False, enabled=False) == {"enabled": False}


def test_tuples_and_sets_are_lists_too() -> None:
    assert _api.webhook_body(create=False, events=("a", "b"))["events"] == ["a", "b"]
    assert _api.webhook_body(create=False, events=frozenset({"a"}))["events"] == ["a"]


def test_an_id_with_a_slash_cannot_repoint_the_route() -> None:
    assert _api.webhook("whk-1/rotate") == "webhooks/whk-1%2Frotate"
    with pytest.raises(ValueError):
        _api.webhook_action("..", "rotate")


# --- the models ---------------------------------------------------------------

WEBHOOK = {
    "id": "whk-2b7d4c809f3c1a7e",
    "url": "https://ci.example.com/mandala",
    "description": "CI",
    "events": ["process.exited", "computer.ready"],
    "computers": ["vm-1"],
    "enabled": False,
    "disabled_reason": "failing",
    "disabled_at": "2026-09-02T00:00:00.000Z",
    "last_success_at": "2026-09-01T12:00:00.000Z",
    "last_failure_at": "2026-09-01T23:59:00.000Z",
    "last_status": 503,
    "workspace_id": "wsp-1",
    "created_at": "2026-09-01T11:00:00.000Z",
    "updated_at": "2026-09-02T00:00:00.000Z",
}
CREATED = {**WEBHOOK, "enabled": True, "disabled_reason": None, "secret": SECRET}
DELIVERY = {
    "id": ID,
    "event_type": "process.exited",
    "computer": "vm-1",
    "cursor": "mfc9z1k2x5ab.7:42",
    "state": "exhausted",
    "attempts": 8,
    "next_at": None,
    "attempted_at": "2026-09-02T04:00:00.000Z",
    "last_status": None,
    "last_error": "timeout",
    "delivered_at": None,
    "created_at": "2026-09-01T12:00:00.000Z",
}


def test_a_webhook_decodes_every_field_and_keeps_null_as_none() -> None:
    w = mc.Webhook.from_api(WEBHOOK)
    assert w.id == "whk-2b7d4c809f3c1a7e"
    assert w.events == ["process.exited", "computer.ready"]
    assert w.computers == ["vm-1"]
    assert w.enabled is False
    assert w.disabled_reason == "failing"
    assert w.is_failing
    assert w.last_status == 503
    assert w.workspace_id == "wsp-1"
    assert w.raw == WEBHOOK
    healthy = mc.Webhook.from_api({**WEBHOOK, "enabled": True, "disabled_reason": None})
    assert healthy.disabled_reason is None
    assert not healthy.is_failing
    # Disabled by the customer is not failing.
    mine = mc.Webhook.from_api({**WEBHOOK, "disabled_reason": "customer"})
    assert not mine.enabled and not mine.is_failing


def test_a_webhook_never_carries_a_secret() -> None:
    """The read shape has no such attribute, so a caller cannot even ask."""
    w = mc.Webhook.from_api(WEBHOOK)
    assert not hasattr(w, "secret")


def test_nulls_and_absences_decode_to_the_documented_defaults() -> None:
    w = mc.Webhook.from_api({"id": "whk-1", "url": "https://x/h"})
    assert w.enabled is True  # the platform's default, and the safe reading
    assert w.events == [] and w.computers == []
    assert w.workspace_id == ""  # omitted on an account-wide subscription
    assert w.last_status is None
    assert w.disabled_reason is None
    assert w.last_success_at is None
    # A last_status that is not a whole number is not a status.
    assert mc.Webhook.from_api({**WEBHOOK, "last_status": 50.3}).last_status is None
    assert mc.Webhook.from_api({**WEBHOOK, "last_status": "503"}).last_status == 503


def test_created_is_a_webhook_with_its_secret() -> None:
    c = mc.WebhookCreated.from_api(CREATED)
    assert isinstance(c, mc.Webhook)
    assert c.secret == SECRET
    assert c.enabled is True
    assert c.raw == CREATED
    # And the verifier takes it as it comes.
    assert mc.verify(c.secret, headers(), BODY, now=TIMESTAMP)


def test_a_delivery_decodes_and_knows_when_it_is_finished() -> None:
    d = mc.WebhookDelivery.from_api(DELIVERY)
    assert d.id == ID
    assert d.state == "exhausted"
    assert d.attempts == 8
    assert d.last_status is None
    assert d.last_error == "timeout"
    assert d.next_at is None
    assert d.is_finished and not d.is_delivered
    pending = mc.WebhookDelivery.from_api({**DELIVERY, "state": "pending", "attempts": 0})
    assert not pending.is_finished
    done = mc.WebhookDelivery.from_api({**DELIVERY, "state": "delivered", "last_status": 200})
    assert done.is_finished and done.is_delivered and done.last_status == 200
    dropped = mc.WebhookDelivery.from_api({**DELIVERY, "state": "dropped"})
    assert dropped.is_finished


# --- the resource, both halves --------------------------------------------------


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


@pytest.fixture
def async_client() -> mc.AsyncClient:
    return mc.AsyncClient("com_test", base_url=BASE)


def sent(route: respx.Route) -> Mapping[str, Any]:
    return json.loads(route.calls.last.request.content)


@respx.mock
def test_create_posts_the_body_and_answers_the_secret(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/webhooks").mock(return_value=httpx.Response(201, json=CREATED))
    created = client.webhooks.create(
        "https://ci.example.com/mandala",
        description="CI",
        events=["process.exited"],
        computers=["vm-1"],
        enabled=False,
    )
    assert sent(route) == {
        "url": "https://ci.example.com/mandala",
        "description": "CI",
        "events": ["process.exited"],
        "computers": ["vm-1"],
        "enabled": False,
    }
    assert isinstance(created, mc.WebhookCreated)
    assert created.secret == SECRET


@respx.mock
def test_create_with_only_a_url_sends_only_a_url(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/webhooks").mock(return_value=httpx.Response(201, json=CREATED))
    client.webhooks.create("https://ci.example.com/mandala")
    assert sent(route) == {"url": "https://ci.example.com/mandala"}


@respx.mock
def test_create_refuses_a_bad_url_before_the_wire(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/webhooks")
    with pytest.raises(ValueError, match="https://"):
        client.webhooks.create("http://ci.example.com/mandala")
    with pytest.raises(ValueError, match="list of strings"):
        client.webhooks.create("https://ci.example.com/mandala", events="process.exited")
    assert not route.called


@respx.mock
def test_the_eleventh_subscription_is_a_conflict(client: mc.Client) -> None:
    respx.post(f"{BASE}/webhooks").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": "this account already has 10 of the 10 webhook subscriptions its "
                "plan allows; delete one first"
            },
        )
    )
    with pytest.raises(mc.ConflictError, match="10 of the 10"):
        client.webhooks.create("https://ci.example.com/mandala")


@respx.mock
def test_list_and_get_answer_webhooks_without_secrets(client: mc.Client) -> None:
    respx.get(f"{BASE}/webhooks").mock(return_value=httpx.Response(200, json=[WEBHOOK]))
    respx.get(f"{BASE}/webhooks/whk-2b7d4c809f3c1a7e").mock(
        return_value=httpx.Response(200, json=WEBHOOK)
    )
    [listed] = client.webhooks.list()
    got = client.webhooks.get("whk-2b7d4c809f3c1a7e")
    assert type(listed) is mc.Webhook and type(got) is mc.Webhook
    assert listed == got


@respx.mock
def test_update_patches_only_what_was_named(client: mc.Client) -> None:
    route = respx.patch(f"{BASE}/webhooks/whk-2b7d4c809f3c1a7e").mock(
        return_value=httpx.Response(200, json=WEBHOOK)
    )
    client.webhooks.update("whk-2b7d4c809f3c1a7e", enabled=True)
    assert sent(route) == {"enabled": True}
    client.webhooks.update("whk-2b7d4c809f3c1a7e", events=[], description="")
    assert sent(route) == {"events": [], "description": ""}


@respx.mock
def test_update_naming_nothing_never_reaches_the_wire(client: mc.Client) -> None:
    route = respx.patch(f"{BASE}/webhooks/whk-2b7d4c809f3c1a7e")
    with pytest.raises(ValueError, match="at least one field"):
        client.webhooks.update("whk-2b7d4c809f3c1a7e")
    assert not route.called


@respx.mock
def test_delete_rotate_test_and_deliveries_hit_their_routes(client: mc.Client) -> None:
    delete = respx.delete(f"{BASE}/webhooks/whk-1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    rotate = respx.post(f"{BASE}/webhooks/whk-1/rotate").mock(
        return_value=httpx.Response(200, json=CREATED)
    )
    test = respx.post(f"{BASE}/webhooks/whk-1/test").mock(
        return_value=httpx.Response(202, json={**DELIVERY, "state": "pending"})
    )
    deliveries = respx.get(f"{BASE}/webhooks/whk-1/deliveries").mock(
        return_value=httpx.Response(200, json=[DELIVERY])
    )
    assert client.webhooks.delete("whk-1") is None
    rotated = client.webhooks.rotate("whk-1")
    assert isinstance(rotated, mc.WebhookCreated) and rotated.secret == SECRET
    queued = client.webhooks.test("whk-1")
    assert isinstance(queued, mc.WebhookDelivery) and not queued.is_finished
    [d] = client.webhooks.deliveries("whk-1")
    assert d.state == "exhausted"
    assert all(r.called for r in (delete, rotate, test, deliveries))
    # The two bodyless POSTs send no body at all.
    assert rotate.calls.last.request.content == b""
    assert test.calls.last.request.content == b""


@respx.mock
def test_a_disabled_subscription_cannot_be_tested(client: mc.Client) -> None:
    respx.post(f"{BASE}/webhooks/whk-1/test").mock(
        return_value=httpx.Response(409, json={"error": "webhook whk-1 is disabled"})
    )
    with pytest.raises(mc.ConflictError, match="disabled"):
        client.webhooks.test("whk-1")


@respx.mock
async def test_the_async_half_does_the_same_things(async_client: mc.AsyncClient) -> None:
    create = respx.post(f"{BASE}/webhooks").mock(return_value=httpx.Response(201, json=CREATED))
    respx.get(f"{BASE}/webhooks").mock(return_value=httpx.Response(200, json=[WEBHOOK]))
    respx.get(f"{BASE}/webhooks/whk-1").mock(return_value=httpx.Response(200, json=WEBHOOK))
    patch = respx.patch(f"{BASE}/webhooks/whk-1").mock(
        return_value=httpx.Response(200, json=WEBHOOK)
    )
    respx.delete(f"{BASE}/webhooks/whk-1").mock(return_value=httpx.Response(200, json={"ok": 1}))
    respx.post(f"{BASE}/webhooks/whk-1/rotate").mock(return_value=httpx.Response(200, json=CREATED))
    respx.post(f"{BASE}/webhooks/whk-1/test").mock(return_value=httpx.Response(202, json=DELIVERY))
    respx.get(f"{BASE}/webhooks/whk-1/deliveries").mock(
        return_value=httpx.Response(200, json=[DELIVERY])
    )
    async with async_client as client:
        created = await client.webhooks.create(
            "https://ci.example.com/mandala", events=["process.exited"], enabled=False
        )
        assert sent(create) == {
            "url": "https://ci.example.com/mandala",
            "events": ["process.exited"],
            "enabled": False,
        }
        assert created.secret == SECRET
        assert (await client.webhooks.list())[0] == await client.webhooks.get("whk-1")
        await client.webhooks.update("whk-1", computers=[])
        assert sent(patch) == {"computers": []}
        with pytest.raises(ValueError, match="at least one field"):
            await client.webhooks.update("whk-1")
        with pytest.raises(ValueError, match="list of strings"):
            await client.webhooks.update("whk-1", events="process.exited")
        await client.webhooks.delete("whk-1")
        assert (await client.webhooks.rotate("whk-1")).secret == SECRET
        assert not (await client.webhooks.test("whk-1")).is_delivered
        assert (await client.webhooks.deliveries("whk-1"))[0].last_error == "timeout"


# --- the CLI: the CRUD only ------------------------------------------------------


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


@respx.mock
def test_cli_list_is_a_table_and_json_on_request(
    env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/webhooks").mock(return_value=httpx.Response(200, json=[WEBHOOK]))
    assert _cli.main(["webhooks", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["ID", "ENABLED", "LAST", "EVENTS", "URL"]
    assert "whk-2b7d4c809f3c1a7e" in out
    assert "off (failing)" in out
    assert "process.exited,computer.ready" in out
    assert "503" in out
    assert _cli.main(["webhooks", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [WEBHOOK]


@respx.mock
def test_cli_list_with_nothing_says_so_on_stderr(
    env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/webhooks").mock(return_value=httpx.Response(200, json=[]))
    assert _cli.main(["webhooks", "list"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no webhooks" in captured.err


@respx.mock
def test_cli_create_prints_the_secret_once_and_says_so(
    env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    route = respx.post(f"{BASE}/webhooks").mock(return_value=httpx.Response(201, json=CREATED))
    assert (
        _cli.main(
            [
                "webhooks",
                "create",
                "https://ci.example.com/mandala",
                "--description",
                "CI",
                "--event",
                "process.exited",
                "--event",
                "computer.ready",
                "--computer",
                "vm-1",
                "--disabled",
            ]
        )
        == 0
    )
    assert sent(route) == {
        "url": "https://ci.example.com/mandala",
        "description": "CI",
        "events": ["process.exited", "computer.ready"],
        "computers": ["vm-1"],
        "enabled": False,
    }
    captured = capsys.readouterr()
    assert json.loads(captured.out)["secret"] == SECRET
    assert "shown once" in captured.err


@respx.mock
def test_cli_create_with_a_url_alone_sends_a_url_alone(env: None) -> None:
    route = respx.post(f"{BASE}/webhooks").mock(return_value=httpx.Response(201, json=CREATED))
    assert _cli.main(["webhooks", "create", "https://ci.example.com/mandala"]) == 0
    assert sent(route) == {"url": "https://ci.example.com/mandala"}


@respx.mock
def test_cli_update_clears_a_filter_with_a_word_for_it(env: None) -> None:
    route = respx.patch(f"{BASE}/webhooks/whk-1").mock(
        return_value=httpx.Response(200, json=WEBHOOK)
    )
    assert _cli.main(["webhooks", "update", "whk-1", "--all-events", "--enable"]) == 0
    assert sent(route) == {"events": [], "enabled": True}
    assert (
        _cli.main(["webhooks", "update", "whk-1", "--computer", "vm-1", "--computer", "vm-2"]) == 0
    )
    assert sent(route) == {"computers": ["vm-1", "vm-2"]}
    assert (
        _cli.main(["webhooks", "update", "whk-1", "--disable", "--url", "https://x.example/h"]) == 0
    )
    assert sent(route) == {"enabled": False, "url": "https://x.example/h"}


def test_cli_update_refuses_a_filter_and_its_clearing_together(env: None) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["webhooks", "update", "whk-1", "--event", "a", "--all-events"])
    with pytest.raises(SystemExit):
        _cli.main(["webhooks", "update", "whk-1", "--enable", "--disable"])


@respx.mock
def test_cli_update_naming_nothing_is_an_error_message(
    env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    route = respx.patch(f"{BASE}/webhooks/whk-1")
    assert _cli.main(["webhooks", "update", "whk-1"]) == 1
    assert "at least one field" in capsys.readouterr().err
    assert not route.called


@respx.mock
def test_cli_get_delete_rotate_test_deliveries(
    env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/webhooks/whk-1").mock(return_value=httpx.Response(200, json=WEBHOOK))
    respx.delete(f"{BASE}/webhooks/whk-1").mock(return_value=httpx.Response(200, json={"ok": 1}))
    respx.post(f"{BASE}/webhooks/whk-1/rotate").mock(return_value=httpx.Response(200, json=CREATED))
    respx.post(f"{BASE}/webhooks/whk-1/test").mock(
        return_value=httpx.Response(202, json={**DELIVERY, "state": "pending", "attempts": 0})
    )
    respx.get(f"{BASE}/webhooks/whk-1/deliveries").mock(
        return_value=httpx.Response(200, json=[DELIVERY])
    )
    assert _cli.main(["webhooks", "get", "whk-1"]) == 0
    assert json.loads(capsys.readouterr().out) == WEBHOOK
    assert _cli.main(["webhooks", "delete", "whk-1"]) == 0
    assert capsys.readouterr().out.strip() == "deleted whk-1"
    assert _cli.main(["webhooks", "rotate", "whk-1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["secret"] == SECRET
    assert "24 hours" in captured.err
    assert _cli.main(["webhooks", "test", "whk-1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["state"] == "pending"
    assert "webhooks deliveries whk-1" in captured.err
    assert _cli.main(["webhooks", "deliveries", "whk-1"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["ID", "STATE", "TRIES", "EVENT", "COMPUTER", "OUTCOME"]
    assert "exhausted" in out and "timeout" in out and "vm-1" in out
    assert _cli.main(["webhooks", "deliveries", "whk-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [DELIVERY]


@respx.mock
def test_cli_api_errors_are_messages(env: None, capsys: pytest.CaptureFixture[str]) -> None:
    respx.post(f"{BASE}/webhooks").mock(
        return_value=httpx.Response(400, json={"error": "url must resolve to a public address"})
    )
    assert _cli.main(["webhooks", "create", "https://localhost/h"]) == 1
    assert "public address" in capsys.readouterr().err


def test_cli_a_bad_url_never_reaches_the_wire(
    env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main(["webhooks", "create", "http://ci.example.com/h"]) == 1
    assert "https://" in capsys.readouterr().err
