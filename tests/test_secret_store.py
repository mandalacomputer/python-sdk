"""The account's secret store: list, read, create, replace, delete (OPL-4984, OPL-5026).

Every wire case runs on both clients. Values are write-only: the platform never
returns one, so every answer here is a secret without a value, and the tests
check that the SDK never invents one either.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Mapping
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _api, _cli

BASE = "https://api.test/api/v1"
ID = "csec-0123456789abcdef"
REV = "csr-0123456789abcdef01234567"
REV2 = "csr-0123456789abcdef01234568"
SECRET = {
    "id": ID,
    "name": "OPENAI_API_KEY",
    "workspace_id": None,
    "revision_id": REV,
    "created_at": "2026-09-20T12:00:00Z",
    "updated_at": "2026-09-20T12:00:00Z",
    "last_used_at": None,
}
LIMITS = {
    "name_max_chars": 60,
    "value_max_bytes": 4096,
    "active_per_account": 100,
    "created_per_account": 1000,
}
LISTING = {"secrets": [SECRET], "delivery": True, "limits": LIMITS}


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


@pytest.fixture
def async_client() -> mc.AsyncClient:
    return mc.AsyncClient("gck_test", base_url=BASE)


def body(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


# --- reads --------------------------------------------------------------------


@respx.mock
def test_list_decodes_the_scope_and_the_limits(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    listed = client.secrets.list()
    assert "workspace_id" not in route.calls.last.request.url.params
    assert [s.name for s in listed.secrets] == ["OPENAI_API_KEY"]
    first = listed.secrets[0]
    assert first.id == ID and first.revision_id == REV
    assert first.workspace_id is None and first.last_used_at is None
    assert not hasattr(first, "value")
    assert listed.delivery is True
    assert listed.limits == mc.SecretLimits(60, 4096, 100, 1000)

    client.secrets.list(workspace_id="ws-1")
    assert route.calls.last.request.url.params["workspace_id"] == "ws-1"


@respx.mock
async def test_async_list_and_get(async_client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    one = respx.get(f"{BASE}/secrets/{ID}").mock(
        httpx.Response(200, json={**SECRET, "workspace_id": "ws-1"})
    )
    assert (await async_client.secrets.list()).secrets[0].id == ID
    got = await async_client.secrets.get(ID, workspace_id="ws-1")
    assert got.workspace_id == "ws-1"
    assert one.calls.last.request.url.params["workspace_id"] == "ws-1"


@pytest.mark.parametrize(
    "row",
    [
        {**SECRET, "id": ""},
        {**SECRET, "revision_id": None},
        {**SECRET, "revision_id": f" {REV}"},
        {k: v for k, v in SECRET.items() if k != "name"},
    ],
)
@respx.mock
def test_a_secret_without_a_usable_id_name_or_revision_is_refused(
    client: mc.Client, row: dict[str, Any]
) -> None:
    """A revision invented here would be sent back by a replace or a delete."""
    respx.get(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json=row))
    with pytest.raises(mc.MandalaError):
        client.secrets.get(ID)


@respx.mock
def test_a_listing_without_its_list_is_refused_not_read_as_empty(client: mc.Client) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={"delivery": True}))
    with pytest.raises(mc.MandalaError):
        client.secrets.list()


# --- writes -------------------------------------------------------------------


@respx.mock
def test_create_sends_the_name_and_value_and_answers_without_the_value(
    client: mc.Client,
) -> None:
    route = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
    made = client.secrets.create("OPENAI_API_KEY", "sk-test")
    assert body(route) == {"name": "OPENAI_API_KEY", "value": "sk-test"}
    assert made.id == ID and "sk-test" not in repr(made)
    client.secrets.create("OPENAI_API_KEY", b"sk-test", workspace_id="ws-1")  # type: ignore[arg-type]
    assert body(route) == {"name": "OPENAI_API_KEY", "value": "sk-test", "workspace_id": "ws-1"}


@respx.mock
async def test_async_replace_and_delete_carry_the_revision(async_client: mc.AsyncClient) -> None:
    put = respx.put(f"{BASE}/secrets/{ID}").mock(
        httpx.Response(200, json={**SECRET, "revision_id": REV2})
    )
    gone = respx.delete(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json={"ok": True}))
    replaced = await async_client.secrets.replace(ID, "sk-new", revision_id=REV)
    assert body(put) == {"value": "sk-new", "revision_id": REV}
    assert replaced.revision_id == REV2
    await async_client.secrets.delete(ID, revision_id=REV2, workspace_id="ws-1")
    params = gone.calls.last.request.url.params
    assert params["revision_id"] == REV2 and params["workspace_id"] == "ws-1"


@pytest.mark.parametrize(
    ("call", "why"),
    [
        (lambda c: c.secrets.create("", "v"), "name"),
        (lambda c: c.secrets.create("x" * 61, "v"), "name"),
        (lambda c: c.secrets.create("A\nB", "v"), "control"),
        (lambda c: c.secrets.create("A", ""), "value"),
        (lambda c: c.secrets.create("A", "x" * 4097), "value"),
        (lambda c: c.secrets.create("A", b"\xff"), "UTF-8"),
        (lambda c: c.secrets.create("A", "v", workspace_id=""), "workspace_id"),
        (lambda c: c.secrets.replace(ID, "v", revision_id=""), "revision_id"),
        (lambda c: c.secrets.replace(ID, "v", revision_id=None), "revision_id"),
        (lambda c: c.secrets.delete(ID, revision_id=None), "revision_id"),
        (lambda c: c.secrets.delete(ID, revision_id=f"{REV} "), "revision_id"),
    ],
)
@respx.mock
def test_refuses_what_the_platform_would_before_sending(
    client: mc.Client, call: Any, why: str
) -> None:
    """DELETE without a revision is a 400 upstream; here it never leaves."""
    with pytest.raises(ValueError, match=why):
        call(client)
    assert not respx.calls


def test_a_value_at_the_byte_limit_is_accepted_and_counted_in_bytes() -> None:
    assert _api.secret_value("x" * 4096) == "x" * 4096
    with pytest.raises(ValueError):
        _api.secret_value("é" * 2049)  # 4098 bytes in 2049 characters


@respx.mock
def test_a_stale_revision_is_a_conflict_that_changed_nothing(client: mc.Client) -> None:
    respx.put(f"{BASE}/secrets/{ID}").mock(
        httpx.Response(409, json={"error": "the secret has changed"})
    )
    with pytest.raises(mc.ConflictError):
        client.secrets.replace(ID, "sk-new", revision_id=REV)


@respx.mock
def test_a_503_on_a_create_is_not_called_safe_to_send_again(client: mc.Client) -> None:
    """It may have been stored: the platform says so of every change answered 503."""
    route = respx.post(f"{BASE}/secrets").mock(httpx.Response(503, json={"error": "off"}))
    retrying = mc.Client("gck_test", base_url=BASE, retries={"idempotent": 3})
    with pytest.raises(mc.UnavailableError) as caught:
        retrying.secrets.create("A", "v")
    assert not mc.is_transient(caught.value)
    assert route.call_count == 1


# --- create or replace --------------------------------------------------------


@respx.mock
def test_set_creates_a_name_that_is_not_there(client: mc.Client) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
    assert client.secrets.set("OPENAI_API_KEY", "sk").id == ID
    assert body(made) == {"name": "OPENAI_API_KEY", "value": "sk"}


@respx.mock
def test_set_replaces_a_name_that_is_there_at_the_revision_it_read(client: mc.Client) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    put = respx.put(f"{BASE}/secrets/{ID}").mock(
        httpx.Response(200, json={**SECRET, "revision_id": REV2})
    )
    assert client.secrets.set("OPENAI_API_KEY", "sk").revision_id == REV2
    assert body(put) == {"value": "sk", "revision_id": REV}


@respx.mock
async def test_async_set_reads_again_once_after_a_race(async_client: mc.AsyncClient) -> None:
    """Created by somebody else between the read and the create: read, replace."""
    respx.get(f"{BASE}/secrets").mock(
        side_effect=[
            httpx.Response(200, json={**LISTING, "secrets": []}),
            httpx.Response(200, json=LISTING),
        ]
    )
    respx.post(f"{BASE}/secrets").mock(httpx.Response(409, json={"error": "name taken"}))
    put = respx.put(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json=SECRET))
    await async_client.secrets.set("OPENAI_API_KEY", "sk")
    assert body(put)["revision_id"] == REV


@respx.mock
def test_set_gives_up_after_three_retries(client: mc.Client) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    put = respx.put(f"{BASE}/secrets/{ID}").mock(httpx.Response(409, json={"error": "moved"}))
    with pytest.raises(mc.ConflictError):
        client.secrets.set("OPENAI_API_KEY", "sk")
    assert put.call_count == 4


@respx.mock
def test_set_never_sends_a_503_again(client: mc.Client) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(503, json={"error": "away"}))
    with pytest.raises(mc.UnavailableError):
        client.secrets.set("A", "sk")
    assert made.call_count == 1


@respx.mock
def test_set_matches_a_name_ignoring_ascii_case_only(client: mc.Client) -> None:
    """The platform keeps names unique ignoring ASCII case, and only ASCII."""
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    put = respx.put(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json=SECRET))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
    client.secrets.set("openai_api_key", "sk")
    assert put.call_count == 1 and made.call_count == 0
    respx.get(f"{BASE}/secrets").mock(
        httpx.Response(200, json={**LISTING, "secrets": [{**SECRET, "name": "Straße"}]})
    )
    client.secrets.set("STRASSE", "sk")
    assert made.call_count == 1


# --- the CLI ------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


class _Stdin(io.TextIOWrapper):
    def isatty(self) -> bool:
        return False


def _stdin(data: bytes) -> _Stdin:
    return _Stdin(io.BytesIO(data), encoding="utf-8")


@respx.mock
def test_cli_list_prints_names_and_never_a_value(
    cli_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    assert _cli.main(["secrets", "list"]) == 0
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY" in out and ID in out and "account" in out


@respx.mock
def test_cli_set_reads_the_value_from_stdin_not_argv(
    cli_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
    monkeypatch.setattr(sys, "stdin", _stdin(b"sk-from-stdin\n"))
    assert _cli.main(["secrets", "set", "OPENAI_API_KEY", "--workspace", "ws-1"]) == 0
    assert body(made) == {
        "name": "OPENAI_API_KEY",
        "value": "sk-from-stdin",
        "workspace_id": "ws-1",
    }
    assert "sk-from-stdin" not in capsys.readouterr().out


@respx.mock
def test_cli_set_replaces_with_the_revision_and_can_keep_a_newline(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    put = respx.put(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json=SECRET))
    monkeypatch.setattr(sys, "stdin", _stdin(b"line\n"))
    assert _cli.main(["secrets", "set", "OPENAI_API_KEY", "--keep-newline"]) == 0
    assert body(put) == {"value": "line\n", "revision_id": REV}


def test_cli_set_takes_no_value_argument(cli_env: None) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["secrets", "set", "OPENAI_API_KEY", "sk-on-the-command-line"])


@respx.mock
def test_cli_set_prompts_without_echo_on_a_terminal(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
    monkeypatch.setattr(sys, "stdin", Tty())
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt: "typed")
    assert _cli.main(["secrets", "set", "A"]) == 0
    assert body(made)["value"] == "typed"


@respx.mock
def test_cli_set_refuses_an_empty_value(cli_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", _stdin(b"\n"))
    with pytest.raises(SystemExit):
        _cli.main(["secrets", "set", "A"])
    assert not respx.calls


@respx.mock
def test_cli_rm_deletes_by_name_at_the_revision_it_read(
    cli_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    gone = respx.delete(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json={"ok": True}))
    assert _cli.main(["secrets", "rm", "OPENAI_API_KEY"]) == 0
    assert gone.calls.last.request.url.params["revision_id"] == REV
    assert "deleted OPENAI_API_KEY" in capsys.readouterr().out


@respx.mock
def test_cli_rm_of_a_name_that_is_not_there_says_so(cli_env: None) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    with pytest.raises(SystemExit):
        _cli.main(["secrets", "rm", "MISSING"])


@respx.mock
def test_set_checks_the_value_before_any_request(client: mc.Client) -> None:
    with pytest.raises(ValueError):
        client.secrets.set("A", "x" * 4097)
    assert not respx.calls


@respx.mock
def test_cli_set_decodes_stdin_strictly_and_whole(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed bytes are refused before any request; a character read whole."""
    monkeypatch.setattr(sys, "stdin", _stdin(b"sk-\xff"))
    with pytest.raises(SystemExit):
        _cli.main(["secrets", "set", "A"])
    assert not respx.calls
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
    monkeypatch.setattr(sys, "stdin", _stdin("é東😀".encode()))
    assert _cli.main(["secrets", "set", "A"]) == 0
    assert body(made)["value"] == "é東😀"


def test_cli_set_refuses_a_prompt_answer_that_is_not_utf8(
    cli_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    import getpass

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "bad\udcff")
    with respx.mock:
        with pytest.raises(SystemExit):
            _cli.main(["secrets", "set", "A"])
        assert not respx.calls


@respx.mock
def test_cli_rm_prefers_an_exact_name_and_refuses_ambiguity(cli_env: None) -> None:
    other = {**SECRET, "id": "csec-00000000000000aa", "name": ID.upper()}
    named_like_an_id = {**SECRET, "id": "csec-00000000000000bb", "name": "csec-00000000000000aa"}
    gone = respx.delete(url__regex=rf"{BASE}/secrets/.*").mock(
        httpx.Response(200, json={"ok": True})
    )
    # An exact name wins over an id spelled the same.
    respx.get(f"{BASE}/secrets").mock(
        httpx.Response(200, json={**LISTING, "secrets": [other, named_like_an_id]})
    )
    assert _cli.main(["secrets", "rm", "csec-00000000000000aa"]) == 0
    assert gone.calls.last.request.url.path.endswith("csec-00000000000000bb")
    # A case-folded name and a different secret's id: refused, nothing deleted.
    respx.get(f"{BASE}/secrets").mock(
        httpx.Response(200, json={**LISTING, "secrets": [SECRET, other]})
    )
    calls = gone.call_count
    with pytest.raises(SystemExit):
        _cli.main(["secrets", "rm", ID])
    assert gone.call_count == calls


# --- review findings (OPL-5026) -------------------------------------------------

VALUE = "sk-live-DO-NOT-LEAK-0123456789"
LONE = "\ud800"


def _leaks(err: BaseException) -> bool:
    """Whether the submitted value is anywhere an exception can show it."""
    seen: list[BaseException] = []
    cur: BaseException | None = err
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    text = " ".join(f"{e!r} {e!s} {e.args!r} {vars(e)!r}" for e in seen)
    return VALUE in text


SYNC_CALLS = [
    lambda c: c.secrets.create(f"A{LONE}", VALUE),
    lambda c: c.secrets.create("A", VALUE, workspace_id=f"ws{LONE}"),
    lambda c: c.secrets.create("A", f"{VALUE}{LONE}"),
    lambda c: c.secrets.replace(ID, VALUE, revision_id=f"{REV}{LONE}"),
    lambda c: c.secrets.replace(ID, VALUE, revision_id=REV, workspace_id=f"ws{LONE}"),
    lambda c: c.secrets.replace(f"csec-{LONE}", VALUE, revision_id=REV),
    lambda c: c.secrets.set(f"A{LONE}", VALUE),
    lambda c: c.secrets.set("A", VALUE, workspace_id=f"ws{LONE}"),
    lambda c: c.secrets.create("A", VALUE + "x" * 4096),
]


@pytest.mark.parametrize("call", SYNC_CALLS)
@respx.mock
def test_no_error_from_a_secret_write_carries_the_value(client: mc.Client, call: Any) -> None:
    """A lone surrogate beside the value used to fail inside the HTTP client's
    body encoder, whose exception held the whole body — value included."""
    with pytest.raises(ValueError) as caught:
        call(client)
    assert not _leaks(caught.value)
    assert not respx.calls


@pytest.mark.parametrize("call", SYNC_CALLS)
@respx.mock
async def test_async_no_error_from_a_secret_write_carries_the_value(
    async_client: mc.AsyncClient, call: Any
) -> None:
    with pytest.raises(ValueError) as caught:
        await call(async_client)
    assert not _leaks(caught.value)
    assert not respx.calls


def test_the_backstop_replaces_an_encoder_failure_with_no_context() -> None:
    """Should validation ever miss one, the error raised is fixed and unchained."""

    def encode() -> None:
        raise UnicodeEncodeError("utf-8", f'{{"value": "{VALUE}"}}', 0, 1, "surrogates")

    with pytest.raises(ValueError) as caught:
        _api.sealed(encode)
    assert str(caught.value) == _api.SECRET_UNENCODABLE
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not _leaks(caught.value)


async def test_the_async_backstop_is_unchained_too() -> None:
    async def encode() -> None:
        raise TypeError(VALUE)

    with pytest.raises(ValueError) as caught:
        await _api.asealed(encode)
    assert caught.value.__context__ is None and not _leaks(caught.value)


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("  TOKEN  ", "TOKEN"),
        ("\tTOKEN\n", "TOKEN"),
        (" TOKEN﻿", "TOKEN"),  # JavaScript's trim, not str.strip()
        ("　TOKEN ", "TOKEN"),
    ],
)
def test_a_name_is_trimmed_as_the_platform_trims_it(given: str, stored: str) -> None:
    assert _api.secret_name(given) == stored


def test_a_name_is_not_trimmed_of_what_the_platform_keeps() -> None:
    """U+001C is whitespace to Python and a control character to the platform."""
    with pytest.raises(ValueError, match="control"):
        _api.secret_name("\x1cTOKEN")
    with pytest.raises(ValueError):
        _api.secret_name("   ")
    assert _api.secret_name(" " + "x" * 60 + " ") == "x" * 60


def _store_that_trims() -> tuple[respx.Route, respx.Route]:
    """A store the way the platform keeps one: the trimmed name, once."""
    held: list[dict[str, Any]] = []

    def listing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**LISTING, "secrets": held})

    def create(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        if any(s["name"].lower() == sent["name"].strip().lower() for s in held):
            return httpx.Response(409, json={"error": "name taken"})
        held.append({**SECRET, "name": sent["name"].strip()})
        return httpx.Response(201, json=held[-1])

    respx.get(f"{BASE}/secrets").mock(side_effect=listing)
    made = respx.post(f"{BASE}/secrets").mock(side_effect=create)
    put = respx.put(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json=SECRET))
    return made, put


@respx.mock
def test_set_twice_with_a_padded_name_creates_then_replaces(client: mc.Client) -> None:
    made, put = _store_that_trims()
    client.secrets.set("  TOKEN ", "one")
    client.secrets.set("  TOKEN ", "two")
    assert made.call_count == 1 and put.call_count == 1
    assert json.loads(made.calls.last.request.content)["name"] == "TOKEN"


@respx.mock
async def test_async_set_twice_with_a_padded_name_creates_then_replaces(
    async_client: mc.AsyncClient,
) -> None:
    made, put = _store_that_trims()
    await async_client.secrets.set("\tTOKEN\n", "one")
    await async_client.secrets.set(" token ", "two")
    assert made.call_count == 1 and put.call_count == 1


@pytest.mark.parametrize(
    ("stdin", "sent"),
    [
        (b"v\n", "v"),
        (b"v\r\n", "v"),
        (b"v\r", "v\r"),  # a lone CR is part of the value, as in the TypeScript CLI
        (b"v\n\n", "v\n"),
        (b"v", "v"),
    ],
)
@respx.mock
def test_cli_set_drops_only_one_lf_or_crlf(
    cli_env: None, monkeypatch: pytest.MonkeyPatch, stdin: bytes, sent: str
) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
    monkeypatch.setattr(sys, "stdin", _stdin(stdin))
    assert _cli.main(["secrets", "set", "A"]) == 0
    assert body(made)["value"] == sent


@respx.mock
def test_cli_rm_finds_a_padded_name(cli_env: None) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    gone = respx.delete(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json={"ok": True}))
    assert _cli.main(["secrets", "rm", "  OPENAI_API_KEY "]) == 0
    assert gone.call_count == 1


# --- transport failures on a secret write (OPL-5026 re-review) -------------------

TRANSPORT_FAILURES = [
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
]


def _holds_value(obj: object, seen: set[int] | None = None, depth: int = 0) -> bool:
    """Whether ``obj``, or anything reachable from it, carries the value.

    Walks exceptions through ``__cause__`` and ``__context__``, every attribute,
    and an httpx request's or response's CONTENT — which a repr omits, and which
    is where the leak this guards against lived.
    """
    seen = set() if seen is None else seen
    if id(obj) in seen or depth > 6:
        return False
    seen.add(id(obj))
    if isinstance(obj, (bytes, bytearray)):
        return VALUE.encode() in obj
    if isinstance(obj, str):
        return VALUE in obj
    if isinstance(obj, httpx.Request):
        try:
            content = obj.content
        except httpx.RequestNotRead:
            content = b""
        return VALUE.encode() in content or _holds_value(str(obj.url), seen, depth + 1)
    if isinstance(obj, httpx.Response):
        return _holds_value(obj.content, seen, depth + 1) or _holds_value(
            obj.request, seen, depth + 1
        )
    if isinstance(obj, BaseException):
        parts: list[object] = [
            repr(obj),
            str(obj),
            *obj.args,
            obj.__cause__,
            obj.__context__,
            *vars(obj).values(),
        ]
        for name in ("request", "response"):
            try:
                parts.append(getattr(obj, name))
            except (RuntimeError, AttributeError):
                pass
        return any(_holds_value(p, seen, depth + 1) for p in parts if p is not None)
    if isinstance(obj, Mapping):
        return any(_holds_value(v, seen, depth + 1) for v in (*obj.keys(), *obj.values()))
    if isinstance(obj, (list, tuple, set)):
        return any(_holds_value(v, seen, depth + 1) for v in obj)
    return VALUE in repr(obj)


def _secret_writes(failure: type[Exception]) -> None:
    respx.post(f"{BASE}/secrets").mock(side_effect=failure)
    respx.put(f"{BASE}/secrets/{ID}").mock(side_effect=failure)
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))


WRITES = [
    lambda c: c.secrets.create("A", VALUE),
    lambda c: c.secrets.replace(ID, VALUE, revision_id=REV),
    lambda c: c.secrets.set("OPENAI_API_KEY", VALUE),
]


@pytest.mark.parametrize("failure", TRANSPORT_FAILURES)
@pytest.mark.parametrize("write", WRITES)
@respx.mock
def test_a_transport_failure_on_a_secret_write_carries_no_value(
    failure: type[Exception], write: Any
) -> None:
    client = mc.Client("gck_test", base_url=BASE, retries={"idempotent": 2})
    _secret_writes(failure)
    with pytest.raises(mc.MandalaError) as caught:
        write(client)
    err = caught.value
    # The type and meaning are kept: a connect-phase failure is still one.
    assert isinstance(err, (mc.ConnectionError, mc.TimeoutError))
    assert err.__cause__ is None and err.__context__ is None
    assert not _holds_value(err)


@pytest.mark.parametrize("failure", TRANSPORT_FAILURES)
@pytest.mark.parametrize("write", WRITES)
@respx.mock
async def test_async_a_transport_failure_on_a_secret_write_carries_no_value(
    failure: type[Exception], write: Any
) -> None:
    client = mc.AsyncClient("gck_test", base_url=BASE, retries={"idempotent": 2})
    _secret_writes(failure)
    with pytest.raises(mc.MandalaError) as caught:
        await write(client)
    err = caught.value
    assert isinstance(err, (mc.ConnectionError, mc.TimeoutError))
    assert err.__cause__ is None and err.__context__ is None
    assert not _holds_value(err)


@respx.mock
def test_a_scrubbed_error_keeps_its_class_and_meaning(client: mc.Client) -> None:
    respx.post(f"{BASE}/secrets").mock(side_effect=httpx.ConnectError)
    with pytest.raises(mc.ConnectionError) as caught:
        client.secrets.create("A", VALUE)
    assert type(caught.value) is mc.ConnectionError and mc.is_transient(caught.value)
    respx.post(f"{BASE}/secrets").mock(side_effect=httpx.ReadError)
    with pytest.raises(mc.ConnectionInterruptedError) as lost:
        client.secrets.create("A", VALUE)
    assert not mc.is_transient(lost.value)


@respx.mock
def test_an_http_refusal_on_a_secret_write_carries_no_value(client: mc.Client) -> None:
    respx.put(f"{BASE}/secrets/{ID}").mock(httpx.Response(409, json={"error": "moved"}))
    with pytest.raises(mc.ConflictError) as caught:
        client.secrets.replace(ID, VALUE, revision_id=REV)
    assert caught.value.status == 409 and caught.value.method == "PUT"
    assert not _holds_value(caught.value)


# --- a caller's own httpx client, whose hooks raise (OPL-5026 re-review 2) ------


class HeldRequest(Exception):
    """A caller's exception that keeps the request — and so the body."""

    def __init__(self, request: object) -> None:
        super().__init__("hook refused")
        self.request = request


def _raise_for_status(response: httpx.Response) -> None:
    response.raise_for_status()


async def _araise_for_status(response: httpx.Response) -> None:
    response.raise_for_status()


def _hold(response: httpx.Response) -> None:
    # Only on the write: the read `set` makes first carries no value.
    if response.request.method != "GET":
        raise HeldRequest(response.request)


async def _ahold(response: httpx.Response) -> None:
    # Only on the write: the read `set` makes first carries no value.
    if response.request.method != "GET":
        raise HeldRequest(response.request)


HOOK_CASES = [
    (409, "raise_for_status", mc.ConflictError),
    (500, "raise_for_status", mc.APIError),
    (409, "hold", mc.MandalaError),
]


def _hooked_store(status: int) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    for route in (respx.post(f"{BASE}/secrets"), respx.put(f"{BASE}/secrets/{ID}")):
        route.mock(httpx.Response(status, json={"error": "refused", "reason": "contention"}))


@pytest.mark.parametrize(("status", "hook", "cls"), HOOK_CASES)
@pytest.mark.parametrize("write", WRITES)
@respx.mock
def test_a_response_hook_that_raises_leaks_no_value(
    status: int, hook: str, cls: type[Exception], write: Any
) -> None:
    fn = _raise_for_status if hook == "raise_for_status" else _hold
    http = httpx.Client(event_hooks={"response": [fn]})
    client = mc.Client("gck_test", base_url=BASE, http_client=http)
    _hooked_store(status)
    with pytest.raises(Exception) as caught:
        write(client)
    err = caught.value
    assert isinstance(err, cls)
    assert err.__cause__ is None and err.__context__ is None
    assert not _holds_value(err)
    if isinstance(err, mc.APIError):
        assert err.status == status and err.method in ("POST", "PUT")


@pytest.mark.parametrize(("status", "hook", "cls"), HOOK_CASES)
@pytest.mark.parametrize("write", WRITES)
@respx.mock
async def test_async_a_response_hook_that_raises_leaks_no_value(
    status: int, hook: str, cls: type[Exception], write: Any
) -> None:
    fn = _araise_for_status if hook == "raise_for_status" else _ahold
    http = httpx.AsyncClient(event_hooks={"response": [fn]})
    client = mc.AsyncClient("gck_test", base_url=BASE, http_client=http)
    _hooked_store(status)
    with pytest.raises(Exception) as caught:
        await write(client)
    err = caught.value
    assert isinstance(err, cls)
    assert err.__cause__ is None and err.__context__ is None
    assert not _holds_value(err)


def test_control_flow_is_not_sanitized() -> None:
    def interrupted() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _api.sealed(interrupted)


def test_an_unbuildable_exception_becomes_a_mandala_error_naming_it() -> None:
    class NeedsTwo(Exception):
        def __init__(self, a: object, b: object) -> None:
            super().__init__(a, b)

    def fail() -> None:
        raise NeedsTwo(VALUE, VALUE)

    with pytest.raises(mc.MandalaError, match="NeedsTwo") as caught:
        _api.sealed(fail)
    assert not _holds_value(caught.value)


# --- a sanitizer that cannot fail (OPL-5026 re-review 3) -------------------------


def _read_then_raise(response: httpx.Response) -> None:
    response.read()
    response.raise_for_status()


async def _aread_then_raise(response: httpx.Response) -> None:
    await response.aread()
    response.raise_for_status()


def _gzipped_store(status: int) -> None:
    import gzip

    packed = gzip.compress(json.dumps({"error": "refused", "reason": "contention"}).encode())
    answer = httpx.Response(
        status,
        content=packed,
        headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
    )
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    respx.post(f"{BASE}/secrets").mock(answer)
    respx.put(f"{BASE}/secrets/{ID}").mock(answer)


@pytest.mark.parametrize(("status", "cls"), [(409, mc.ConflictError), (500, mc.APIError)])
@pytest.mark.parametrize("write", WRITES)
@respx.mock
def test_a_gzipped_refusal_read_by_a_hook_leaks_no_value(
    status: int, cls: type[Exception], write: Any
) -> None:
    """The body is already decoded; decoding it again used to fail mid-sanitize."""
    http = httpx.Client(event_hooks={"response": [_read_then_raise]})
    client = mc.Client("gck_test", base_url=BASE, http_client=http)
    _gzipped_store(status)
    with pytest.raises(cls) as caught:
        write(client)
    err = caught.value
    assert err.__cause__ is None and err.__context__ is None
    assert not _holds_value(err)
    assert isinstance(err, mc.APIError) and err.status == status


@pytest.mark.parametrize(("status", "cls"), [(409, mc.ConflictError), (500, mc.APIError)])
@pytest.mark.parametrize("write", WRITES)
@respx.mock
async def test_async_a_gzipped_refusal_read_by_a_hook_leaks_no_value(
    status: int, cls: type[Exception], write: Any
) -> None:
    http = httpx.AsyncClient(event_hooks={"response": [_aread_then_raise]})
    client = mc.AsyncClient("gck_test", base_url=BASE, http_client=http)
    _gzipped_store(status)
    with pytest.raises(cls) as caught:
        await write(client)
    err = caught.value
    assert err.__cause__ is None and err.__context__ is None
    assert not _holds_value(err)


@respx.mock
def test_a_sanitizer_that_throws_falls_back_to_a_fixed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(err: Exception) -> Exception:
        raise RuntimeError(f"sanitizer broke on {VALUE}")

    monkeypatch.setattr(_api, "_from_status", broken)
    monkeypatch.setattr(_api, "_scrubbed", broken)
    http = httpx.Client(event_hooks={"response": [_raise_for_status]})
    client = mc.Client("gck_test", base_url=BASE, http_client=http)
    respx.post(f"{BASE}/secrets").mock(httpx.Response(409, json={"error": "taken"}))
    with pytest.raises(mc.APIError) as caught:
        client.secrets.create("A", VALUE)
    err = caught.value
    assert str(err) == _api.SECRET_WRITE_FAILED
    assert (err.status, err.method) == (409, "POST")
    assert err.__cause__ is None and err.__context__ is None
    assert not _holds_value(err)


async def test_async_a_sanitizer_that_throws_falls_back_to_a_fixed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(err: Exception) -> Exception:
        raise RuntimeError(VALUE)

    monkeypatch.setattr(_api, "_sanitized", broken)

    async def fail() -> None:
        raise HeldRequest(VALUE)

    with pytest.raises(mc.MandalaError) as caught:
        await _api.asealed(fail)
    assert str(caught.value) == _api.SECRET_WRITE_FAILED
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not _holds_value(caught.value)


# --- non-ASCII headers on a hooked refusal (OPL-5026 re-review 4) ----------------

#: A header value no ASCII codec will round-trip: Latin-1 bytes on the wire.
ODD_HEADER = ("X-Note", "caf\xe9".encode("latin-1"))


def _odd(status: int, **extra: str) -> httpx.Response:
    headers = [ODD_HEADER, (b"Content-Type", b"application/json")]
    headers += [(k.encode(), v.encode()) for k, v in extra.items()]
    return httpx.Response(status, headers=headers, content=b'{"error": "refused"}')


@respx.mock
def test_set_recovers_from_a_conflict_with_a_non_ascii_header() -> None:
    http = httpx.Client(event_hooks={"response": [_raise_for_status]})
    client = mc.Client("gck_test", base_url=BASE, http_client=http)
    respx.get(f"{BASE}/secrets").mock(
        side_effect=[
            httpx.Response(200, json={**LISTING, "secrets": []}),
            httpx.Response(200, json=LISTING),
        ]
    )
    respx.post(f"{BASE}/secrets").mock(_odd(409))
    put = respx.put(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json=SECRET))
    client.secrets.set("OPENAI_API_KEY", VALUE)
    assert put.call_count == 1


@respx.mock
async def test_async_set_recovers_from_a_conflict_with_a_non_ascii_header() -> None:
    http = httpx.AsyncClient(event_hooks={"response": [_araise_for_status]})
    client = mc.AsyncClient("gck_test", base_url=BASE, http_client=http)
    respx.get(f"{BASE}/secrets").mock(
        side_effect=[
            httpx.Response(200, json={**LISTING, "secrets": []}),
            httpx.Response(200, json=LISTING),
        ]
    )
    respx.post(f"{BASE}/secrets").mock(_odd(409))
    put = respx.put(f"{BASE}/secrets/{ID}").mock(httpx.Response(200, json=SECRET))
    await client.secrets.set("OPENAI_API_KEY", VALUE)
    assert put.call_count == 1


@respx.mock
def test_a_rate_limit_with_a_non_ascii_header_stays_a_rate_limit() -> None:
    http = httpx.Client(event_hooks={"response": [_raise_for_status]})
    client = mc.Client("gck_test", base_url=BASE, http_client=http)
    respx.post(f"{BASE}/secrets").mock(_odd(429, **{"Retry-After": "7"}))
    with pytest.raises(mc.RateLimitError) as caught:
        client.secrets.create("A", VALUE)
    assert caught.value.retry_after == 7.0
    assert caught.value.__context__ is None and not _holds_value(caught.value)


@respx.mock
async def test_async_a_rate_limit_with_a_non_ascii_header_stays_a_rate_limit() -> None:
    http = httpx.AsyncClient(event_hooks={"response": [_araise_for_status]})
    client = mc.AsyncClient("gck_test", base_url=BASE, http_client=http)
    respx.put(f"{BASE}/secrets/{ID}").mock(_odd(429, **{"Retry-After": "7"}))
    with pytest.raises(mc.RateLimitError) as caught:
        await client.secrets.replace(ID, VALUE, revision_id=REV)
    assert caught.value.retry_after == 7.0
    assert caught.value.__context__ is None and not _holds_value(caught.value)
