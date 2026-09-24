"""Public HTTP diagnostics, constructor compatibility, and stream isolation."""

import json

import httpx
import pytest

import mandala_computer as mc
from mandala_computer import _client
from mandala_computer._agent import to_agent_event
from mandala_computer._client import error_for_status

BASE = "https://api.test/api/v1"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux", "cpu": 2}
HEADERS = {"x-ReQuEsT-Id": "header-id", "Allow": "GET, HEAD, OPTIONS", "WWW-Authenticate": "Bearer"}
META = {"request_id": "header-id", "allow": "GET, HEAD, OPTIONS", "www_authenticate": "Bearer"}
AUTH_CASES = [
    ({"error": "missing", "reason": "missing"}, "missing", "Bearer", "missing"),
    (
        {"error": "invalid", "reason": "invalid"},
        "invalid",
        'Bearer error="invalid_token"',
        "invalid",
    ),
    (
        {"error": "revoked", "reason": "revoked"},
        "revoked",
        'Bearer error="invalid_token"',
        "revoked",
    ),
    (
        {"error": {"message": "run revoked", "reason": "revoked", "code": 503}},
        "revoked",
        'Bearer error="invalid_token"',
        "run revoked",
    ),
    ({"error": {"message": "provider refused", "code": 503}}, None, None, "provider refused"),
    ({"error": "future", "reason": "new-reason"}, "new-reason", None, "future"),
    ({"error": {"message": "nested", "reason": "revoked"}, "reason": 7}, "revoked", None, "nested"),
    (
        {"error": {"message": "top wins", "reason": "revoked"}, "reason": "top-reason"},
        "top-reason",
        None,
        "top wins",
    ),
    ({"error": {"message": "malformed reason", "reason": 7}}, None, None, "malformed reason"),
]
BRANCHES = [
    (400, {}, mc.APIError),
    (405, {}, mc.MethodNotAllowedError),
    (429, {}, mc.RateLimitError),
    (409, {"move": {"required": True, "possible": True}}, mc.MoveRequiredError),
    (409, {"move": {"required": True, "possible": False}}, mc.MoveRequiredError),
    (409, {"code": "template_image_preparing"}, mc.ConflictError),
    (409, {"reason": "exists"}, mc.FileExistsError),
    (416, {}, mc.RangeNotSatisfiableError),
    (504, {}, mc.GatewayTimeoutError),
    (520, {}, mc.OriginResponseError),
    (521, {}, mc.OriginUnreachableError),
    (525, {}, mc.OriginTLSError),
]
CORRELATION = [
    ("header", "body", "header"),
    ("", "body", "body"),
    ("  ", "body", "body"),
    (None, " opaque body ", " opaque body "),
    (None, 123, None),
    (None, {}, None),
    (None, "", None),
    (None, "  ", None),
    (None, None, None),
]


def assert_metadata(error, expected=META):
    for key, value in expected.items():
        assert getattr(error, key) == value


def stream_response(body):
    return httpx.Response(
        200,
        content=f"event: error\ndata: {json.dumps(body)}\n\n",
        headers={**HEADERS, "Content-Type": "text/event-stream", "Retry-After": "99"},
    )


class Broken(httpx.SyncByteStream):
    def __init__(self):
        self.closed = False

    def __iter__(self):
        yield b'{"error":'
        raise httpx.ReadError("response interrupted")

    def close(self):
        self.closed = True


@pytest.mark.parametrize("allow", ["GET, HEAD, POST, OPTIONS", "GET, HEAD, OPTIONS", None])
def test_405_is_public_and_preserves_path_specific_allow(allow):
    body = {
        "error": "method not allowed",
        "request_id": "body-id",
        "allow": "ignored",
        "www_authenticate": "ignored",
    }
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            405,
            json=body,
            headers={"X-Request-ID": "method-id", **({"Allow": allow} if allow else {})},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with (
            mc.Client(
                "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
            ) as client,
            pytest.raises(mc.MethodNotAllowedError) as caught,
        ):
            client._t.json("PUT", "/computers")
        assert not http.is_closed
    error = caught.value
    assert str(error) == body["error"]
    assert error.status == 405 and error.body == body
    assert error.request_id == "method-id" and error.allow == allow
    assert error.www_authenticate is None and not mc.is_transient(error)
    assert len(calls) == 1


@pytest.mark.parametrize("payload,reason,challenge,message", AUTH_CASES)
def test_http_auth_matrix(payload, reason, challenge, message):
    body = {
        **payload,
        "request_id": "body-auth",
        "usage": {"input_tokens": 17},
        "steps": [{"action": "click"}],
        "extra": "retained",
    }
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            401,
            json=body,
            headers={
                "X-Request-ID": "header-auth",
                **({"WWW-Authenticate": challenge} if challenge else {}),
            },
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}) as client,
        pytest.raises(mc.AuthenticationError) as caught,
    ):
        client.computers.list()
    error = caught.value
    assert str(error) == message and error.status == 401 and error.body == body
    assert error.request_id == "header-auth" and error.reason == reason
    assert error.www_authenticate == challenge and not mc.is_transient(error)
    assert len(calls) == 1


@pytest.mark.parametrize("header,body_id,expected", CORRELATION)
def test_header_first_top_level_correlation(header, body_id, expected):
    body = {"error": {"message": "refused", "request_id": "nested-id"}, "request_id": body_id}
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    401, json=body, headers={} if header is None else {"X-Request-ID": header}
                )
            )
        ) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.AuthenticationError) as caught,
    ):
        client.computers.list()
    assert caught.value.request_id == expected


@pytest.mark.parametrize(
    "nested", [None, [], 42, {"message": {}}, {"message": []}, {"message": "  "}]
)
def test_malformed_nested_message_is_not_coerced(nested):
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(400, json={"error": nested})
            )
        ) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.APIError) as caught,
    ):
        client.computers.list()
    assert str(caught.value) == "HTTP 400"


@pytest.mark.parametrize(
    "method,content",
    [("HEAD", b""), ("GET", b""), ("GET", b"<html>refused</html>"), ("GET", b'{"error":')],
)
def test_empty_head_and_unreadable_bodies_keep_headers(method, content):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(405, content=content, headers=HEADERS)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.MethodNotAllowedError) as caught,
    ):
        client._t.json(method, "/computers")
    assert_metadata(caught.value)
    assert calls[0].method == method and "x-request-id" not in calls[0].headers


@pytest.mark.parametrize("status", [401, 405, 429])
def test_known_refusal_keeps_headers_after_body_reset_without_retry(status):
    body = Broken()
    response = httpx.Response(status, stream=body, headers={**HEADERS, "Retry-After": "7"})
    calls = []

    def handler(request):
        calls.append(request)
        return response

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}) as client,
        pytest.raises(mc.APIError) as caught,
    ):
        client.computers.list()
    assert_metadata(caught.value)
    assert caught.value.status == status and caught.value.retry_after == 7
    assert len(calls) == 1 and response.is_closed and body.closed


@pytest.mark.parametrize("status,extra,cls", BRANCHES)
def test_every_finite_constructor_branch(status, extra, cls):
    body = {**extra, "request_id": "body-branch"}
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    status,
                    json=body,
                    headers={**HEADERS, "Retry-After": "13", "Content-Range": "bytes */12345"},
                )
            )
        ) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(cls) as caught,
    ):
        client.computers.list()
    error = caught.value
    assert_metadata(error)
    assert error.retry_after == 13 and error.body == body and error.status == status
    if isinstance(error, mc.RangeNotSatisfiableError):
        assert error.size == 12345
    if isinstance(error, mc.MoveRequiredError):
        assert error.move_possible is extra["move"]["possible"]


def test_direct_constructor_compatibility_and_body_fallback():
    body = {"request_id": "body-direct"}
    error = mc.APIError("bad", status=400, body=body, retry_after=17)
    assert error.retry_after == 17 and error.request_id == "body-direct"
    for error in [
        mc.APIError("bad", status=400, body=body, retry_after=19, **META),
        mc.RateLimitError("rate", status=429, body=body, retry_after=19, **META),
        mc.RangeNotSatisfiableError(
            "range", status=416, body=body, size=37, retry_after=19, **META
        ),
        mc.MoveRequiredError(
            "move", status=409, body=body, move_possible=False, retry_after=19, **META
        ),
    ]:
        assert_metadata(error)
        assert error.retry_after == 19 and error.body is body
    assert mc.RangeNotSatisfiableError("range", status=416, size=37, retry_after=29).size == 37


@pytest.mark.parametrize("status", [401, 402, 403, 404, 405])
def test_clearing_reasons_do_not_make_terminal_statuses_replayable(status):
    for body in [{"reason": "starting"}, {"error": {"reason": "contention"}}]:
        calls = []

        def handler(request, calls=calls, body=body):
            calls.append(request)
            return httpx.Response(status, json=body)

        with (
            httpx.Client(transport=httpx.MockTransport(handler)) as http,
            mc.Client(
                "com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}
            ) as client,
            pytest.raises(mc.APIError) as caught,
        ):
            client.computers.list()
        assert not mc.is_transient(caught.value) and len(calls) == 1


@pytest.mark.parametrize("status", [400, 409])
def test_nested_clearing_reason_is_diagnostic_only(status):
    cls = mc.ConflictError if status == 409 else mc.APIError
    assert mc.is_transient(cls("flat", status=status, body={"reason": "starting"}))
    error = cls("nested", status=status, body={"error": {"reason": "starting"}})
    assert error.reason == "starting" and not mc.is_transient(error)


@pytest.mark.parametrize(
    "status,message", [(404, "no such file in the guest"), (400, "permission denied")]
)
def test_actual_guest_file_resource_keeps_status_and_metadata(status, message):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": message}, headers=HEADERS)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.APIError) as caught,
    ):
        mc.Computer(client._t, COMPUTER).read_file("/tmp/missing")
    assert type(caught.value) is (mc.NotFoundError if status == 404 else mc.APIError)
    assert str(caught.value) == message
    assert_metadata(caught.value)
    assert calls[0].url.path == "/api/v1/computers/vm-1/files"


def test_generic_resource_404_retains_its_own_message():
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    404, json={"error": "no such computer"}, headers=HEADERS
                )
            )
        ) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.NotFoundError, match="no such computer") as caught,
    ):
        client.computers.get("missing")
    assert_metadata(caught.value)


@pytest.mark.parametrize("status", [0, 400, 401, 429, 504, 520])
def test_stream_frame_partial_work_and_metadata_are_preserved_without_retry_advice(status):
    body = {
        "error": "run stopped",
        "status": status,
        "reason": "starting",
        "request_id": "frame-id",
        "usage": {"input_tokens": 31},
        "steps": [{"action": "click"}],
    }
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: stream_response(body))) as http,
        mc.Client("com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}) as client,
    ):
        computer = mc.Computer(client._t, COMPUTER)
        (event,) = list(computer.agent_stream("task", model_key="sk-test"))
        assert isinstance(event, mc.AgentFailed) and event.raw == body
        with pytest.raises(mc.MandalaError) as caught:
            computer.agent("task", model_key="sk-test")
    error = caught.value
    assert (
        error.agent.raw == body
        and error.agent.usage.input_tokens == 31
        and len(error.agent.steps) == 1
    )
    if status:
        assert_metadata(error, {"request_id": "frame-id", "allow": None, "www_authenticate": None})
        assert error.reason is None and error.retry_after is None
    if status in (504, 520):
        assert type(error) is mc.APIError
    if status == 0:
        assert type(error) is mc.MandalaError
    nested = error_for_status(
        status, "nested", {"error": {"reason": "starting"}, "request_id": "nested-frame"}
    )
    assert nested.reason is None and nested.request_id == "nested-frame"
    if status == 400:
        assert not mc.is_transient(nested)


def test_agent_failed_positional_equality_and_raw_frame_compatibility():
    previous = mc.AgentFailed("stopped", 401, mc.AgentUsage(), ())
    with_raw = mc.AgentFailed("stopped", 401, mc.AgentUsage(), (), {"request_id": "frame-id"})
    assert previous == with_raw and repr(previous) == repr(with_raw)
    assert previous.raw == {}
    frame = {"error": "stopped", "status": 401, "reason": "revoked", "request_id": "id"}
    assert to_agent_event("error", frame, 0).raw is frame


def test_final_attempt_correlation_and_response_cleanup(monkeypatch):
    monkeypatch.setattr(_client, "_retry_sleep", lambda delay: None)
    responses = []

    def handler(request):
        number = len(responses) + 1
        response = httpx.Response(
            503,
            json={"error": "unavailable", "request_id": f"body-{number}"},
            headers={"X-Request-ID": f"attempt-{number}"},
        )
        responses.append(response)
        return response

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http, retries={"idempotent": 2}) as client,
        pytest.raises(mc.APIError) as caught,
    ):
        client.computers.list()
    assert caught.value.request_id == "attempt-3" and caught.value.body["request_id"] == "body-3"
    assert len(responses) == 3 and all(response.is_closed for response in responses)


def test_previous_id_is_not_reused_for_a_missing_id_or_connection_error():
    def handler(request):
        if request.url.path.endswith("/sizes"):
            return httpx.Response(
                401, json={"error": "refused"}, headers={"X-Request-ID": "sizes-id"}
            )
        if request.url.path.endswith("/computers"):
            return httpx.Response(401, json={"error": "refused"})
        raise httpx.ConnectError("connection refused", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        for path, expected in [("/sizes", "sizes-id"), ("/computers", None)]:
            with pytest.raises(mc.APIError) as caught:
                client._t.json("GET", path)
            assert caught.value.request_id == expected
        with pytest.raises(mc.ConnectionError) as connection:
            client._t.json("GET", "/builds")
        assert not hasattr(connection.value, "request_id")


def test_raw_openai_stream_envelope_and_terminator_are_unchanged():
    body = {"error": {"message": "revoked", "reason": "revoked"}, "request_id": "chat-frame"}
    response = httpx.Response(
        200,
        content=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n",
        headers={"Content-Type": "text/event-stream"},
    )
    with (
        httpx.Client(transport=httpx.MockTransport(lambda request: response)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        frames = list(client._t.sse("POST", "/chat/completions"))
    assert [frame.data for frame in frames] == [body, "[DONE]"]


@pytest.mark.parametrize(
    "mode", ["json", "binary", "listing", "bounded_json_object", "bounded_binary", "sse"]
)
def test_all_response_readers_preserve_headers(mode):
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "refused"}, headers=HEADERS)
            )
        ) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.AuthenticationError) as caught,
    ):
        if mode == "listing":
            client._t.listing("/computers")
        elif mode == "sse":
            next(client._t.sse("POST", "/computers/vm-1/agent"))
        elif mode == "binary":
            client._t.binary(
                "GET",
                "/computers",
                accept="application/octet-stream",
                content_types=("application/octet-stream",),
            )
        else:
            getattr(client._t, mode)(
                "GET", "/computers", **({"max_bytes": 4096} if mode.startswith("bounded") else {})
            )
    assert_metadata(caught.value)


@pytest.mark.parametrize("status", [504, 520])
def test_named_nested_message_keeps_actual_http_status(status):
    body = {
        "error": {"message": "specific downstream refusal", "code": 401, "reason": "new-reason"},
        "request_id": "body-id",
    }
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json=body, headers=HEADERS)
            )
        ) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.APIError) as caught,
    ):
        client._t.json("POST", "/chat/completions")
    assert str(caught.value) == body["error"]["message"] and caught.value.status == status
    assert caught.value.reason == "new-reason" and caught.value.body == body
    assert_metadata(caught.value)


def test_ordinary_string_message_remains_verbatim():
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(400, json={"error": "  "}))
        ) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
        pytest.raises(mc.APIError) as caught,
    ):
        client.computers.list()
    assert str(caught.value) == "  "


# --- a create-only upload's unreadable 409 (OPL-4994, Codex review) ----------

_CREATE_ONLY_COMPUTER = {"id": "vm-1", "name": "dev", "status": "running"}


def _upload_answering(response, *, overwrite):
    def handler(request):
        if request.method == "PUT":
            return response()
        return httpx.Response(200, json=_CREATE_ONLY_COMPUTER)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        computer = mc.Computer(client._t, _CREATE_ONLY_COMPUTER)
        with pytest.raises(mc.ConflictError) as caught:
            computer.write_file("/tmp/a", b"hi", overwrite=overwrite)
    return caught.value


@pytest.mark.parametrize(
    "response",
    [
        lambda: httpx.Response(409, stream=Broken()),
        lambda: httpx.Response(409, content=b""),
        lambda: httpx.Response(409, content=b"<html>conflict</html>"),
    ],
    ids=["interrupted", "empty", "not-json"],
)
def test_a_create_only_409_that_cannot_be_read_is_never_transient(response):
    """The lost body is the one thing that said ``exists``; the request still knows."""
    error = _upload_answering(response, overwrite=False)
    assert isinstance(error, mc.FileExistsError)
    assert error.status == 409 and error.reason is None
    assert "refused as a conflict, reason unknown" in str(error)
    assert not mc.is_transient(error)


_REASONLESS_JSON = [
    {"error": "conflict"},
    {"reason": 5},
    {"error": "conflict", "reason": None},
    {"reason": ""},
    {"reason": "   "},
    {},
]
_REASONLESS_IDS = ["missing", "numeric", "null", "empty-string", "blank-string", "empty-object"]


@pytest.mark.parametrize("body", _REASONLESS_JSON, ids=_REASONLESS_IDS)
def test_a_create_only_409_with_no_usable_reason_is_never_transient(body):
    """JSON that arrived but carries no string word is as unclassified as a lost body."""
    error = _upload_answering(lambda: httpx.Response(409, json=body), overwrite=False)
    assert isinstance(error, mc.FileExistsError)
    assert error.status == 409 and not (error.reason or "").strip()
    assert "refused as a conflict, reason unknown" in str(error)
    assert "already exists" not in str(error)
    assert not mc.is_transient(error)


def test_a_reasonless_json_409_on_an_ordinary_upload_is_left_as_it_was():
    error = _upload_answering(
        lambda: httpx.Response(409, json={"error": "conflict"}), overwrite=True
    )
    assert type(error) is mc.ConflictError


def test_an_unreadable_409_on_an_ordinary_upload_is_left_as_it_was():
    error = _upload_answering(lambda: httpx.Response(409, content=b""), overwrite=True)
    assert type(error) is mc.ConflictError


def test_a_readable_create_only_409_keeps_the_platforms_word():
    error = _upload_answering(
        lambda: httpx.Response(409, json={"error": "busy", "reason": "contention"}),
        overwrite=False,
    )
    assert type(error) is mc.ConflictError and error.reason == "contention"
