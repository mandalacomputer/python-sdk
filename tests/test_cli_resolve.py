"""A computer operand shaped like a computer id is that id (OPL-5980).

The platform lets a computer be named like another's id. Matching an id the
listing does not hold against names sent a command meant for a deleted
computer, a destructive one included, to the live computer named after it.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mandala_computer import _cli

BASE = "https://api.test/api/v1"

DELETED = "vm-0123456789ab"
NAMESAKE = {"id": "vm-bbbbbbbbbbbb", "name": DELETED, "status": "running", "os": "linux"}
GONE = httpx.Response(404, json={"error": "computer not found"})


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


def _changes() -> respx.Route:
    """Every request that could change something, recorded rather than answered."""
    return respx.route(method__in=["POST", "PUT", "PATCH", "DELETE"]).mock(
        return_value=httpx.Response(200, json={**NAMESAKE, "egress_proxy": None})
    )


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["egress-proxy", "clear", DELETED], id="egress-proxy-clear"),
        pytest.param(["browser-proxy", "clear", DELETED], id="browser-proxy-clear"),
        pytest.param(["move", DELETED, "--workspace", "unassigned"], id="move-workspace"),
    ],
)
@respx.mock
def test_a_deleted_id_never_reaches_the_computer_named_after_it(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[NAMESAKE]))
    by_id = respx.get(f"{BASE}/computers/{DELETED}").mock(return_value=GONE)
    changes = _changes()
    code = _cli.main([*args, "--json"])
    assert [str(c.request.url) for c in changes.calls] == []
    assert by_id.called
    assert code == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"


@respx.mock
def test_an_incomplete_listing_does_not_hide_the_ids_own_answer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(200, json=[NAMESAKE], headers={"X-GC-Incomplete": "1"})
    )
    respx.get(f"{BASE}/computers/{DELETED}").mock(return_value=GONE)
    changes = _changes()
    code = _cli.main(["egress-proxy", "clear", DELETED, "--json"])
    assert not changes.called
    assert code == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_found"


@respx.mock
def test_an_id_the_listing_omits_is_read_from_its_own_route() -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[NAMESAKE]))
    real = {"id": DELETED, "name": "archived", "status": "stopped", "os": "linux"}
    respx.get(f"{BASE}/computers/{DELETED}").mock(return_value=httpx.Response(200, json=real))
    with _cli._client() as client:
        assert _cli._resolve(client, DELETED).id == DELETED


@respx.mock
def test_a_listed_id_needs_no_read() -> None:
    real = {"id": DELETED, "name": "archived", "status": "stopped", "os": "linux"}
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[NAMESAKE, real]))
    by_id = respx.get(f"{BASE}/computers/{DELETED}")
    with _cli._client() as client:
        assert _cli._resolve(client, DELETED).id == DELETED
    assert not by_id.called


@respx.mock
def test_refuses_a_different_computer_from_the_ids_route() -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{BASE}/computers/{DELETED}").mock(return_value=httpx.Response(200, json=NAMESAKE))
    with _cli._client() as client, pytest.raises(_cli._Failure) as caught:
        _cli._resolve(client, DELETED)
    assert caught.value.reason == "unexpected_computer"


@respx.mock
def test_a_name_still_resolves_without_a_read() -> None:
    # Not id-shaped: the id is lowercase hex, so this is a name like any other.
    shouted = {**NAMESAKE, "id": "vm-cccccccccccc", "name": DELETED.upper()}
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[NAMESAKE, shouted]))
    by_id = respx.get(url__startswith=f"{BASE}/computers/")
    with _cli._client() as client:
        assert _cli._resolve(client, DELETED.upper()).id == "vm-cccccccccccc"
    assert not by_id.called
