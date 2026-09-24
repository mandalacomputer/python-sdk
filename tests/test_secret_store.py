"""The account's secret store: list, read, create, replace, delete (OPL-4984, OPL-5026).

Every wire case runs on both clients. Values are write-only: the platform never
returns one, so every answer here is a secret without a value, and the tests
check that the SDK never invents one either.
"""

from __future__ import annotations

import io
import json
import sys
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
