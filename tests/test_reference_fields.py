"""Fields and routes the API reference documents, decoded as it says (OPL-5026).

One file for the audit's decoding findings, each case written against the
reference's own wording: a computer's secret state, a snapshot's restore
availability, the holdings' presence and counts, ``type``'s mechanism, the
paste action, the guest directory listing, passive signals, API activity,
``no_wake`` on a transfer, the whole answer of a delete, and the refusal words.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _api
from mandala_computer._computer import UNICODE_TYPE_TIMEOUT

BASE = "https://api.test/api/v1"
A = "csec-0123456789abcdef"
REV = "csr-0123456789abcdef01234567"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux"}


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


@pytest.fixture
def async_client() -> mc.AsyncClient:
    return mc.AsyncClient("gck_test", base_url=BASE)


def computer(client: mc.Client, **fields: Any) -> mc.Computer:
    return mc.Computer(client._t, {**COMPUTER, **fields})


# --- a computer's secret state -----------------------------------------------


def test_secrets_pending_is_three_answers(client: mc.Client) -> None:
    """``null`` is "could not be checked", and must not read as either answer."""
    assert computer(client, secrets_pending=True).secrets_pending is True
    assert computer(client, secrets_pending=False).secrets_pending is False
    assert computer(client, secrets_pending=None).secrets_pending is None
    # Absent: a computer with no secrets, which is sent the field only while true.
    assert computer(client).secrets_pending is False
    assert computer(client, secrets_pending="maybe").secrets_pending is None


def test_secret_bindings_decode_env_and_file_rows(client: mc.Client) -> None:
    c = computer(
        client,
        secrets=[
            {"secret_id": A, "revision_id": REV, "env": "API_TOKEN"},
            {"secret_id": "csec-0123456789abcde0", "revision_id": REV, "file": "kubeconfig"},
        ],
        secrets_generation=4,
        secrets_applied={
            "generation": 4,
            "applied_at": "2026-09-20T12:00:00Z",
            "revisions": {A: REV},
        },
    )
    env, file = c.secret_bindings
    assert (env.env, env.file, env.revision_id) == ("API_TOKEN", None, REV)
    assert (file.env, file.file) == (None, "kubeconfig")
    assert c.secrets_generation == 4
    assert c.secrets_applied == mc.SecretsReceipt(4, "2026-09-20T12:00:00Z", {A: REV})
    assert c.secrets_error is None
    assert computer(client, secrets_error="delivery failed").secrets_error == "delivery failed"


def test_a_missing_secrets_pending_beside_bindings_is_unknown(client: mc.Client) -> None:
    """The platform always sends it beside bindings; its absence is no answer."""
    bound = [{"secret_id": A, "revision_id": REV, "env": "X"}]
    assert computer(client, secrets=bound).secrets_pending is None


def test_no_secrets_reads_as_none_bound(client: mc.Client) -> None:
    c = computer(client)
    assert c.secret_bindings == [] and c.secrets_generation is None and c.secrets_applied is None


@pytest.mark.parametrize(
    "rows",
    [
        "not a list",
        [{"secret_id": A, "env": "X"}],
        [{"secret_id": A, "revision_id": REV}],
        [{"secret_id": A, "revision_id": REV, "env": "X", "file": "x"}],
    ],
)
def test_a_binding_row_it_cannot_read_is_refused(client: mc.Client, rows: object) -> None:
    """Strict, like ``Computer.secrets()``: a short list would look complete."""
    with pytest.raises(mc.MandalaError):
        computer(client, secrets=rows).secret_bindings  # noqa: B018


def test_desktop_and_running_ram(client: mc.Client) -> None:
    c = computer(client, desktop="wayland", running_ram_mb=4096)
    assert c.desktop == "wayland" and c.running_ram_mb == 4096
    bare = computer(client)
    assert bare.desktop == "" and bare.running_ram_mb is None
    assert computer(client, running_ram_mb=0).running_ram_mb == 0


def test_memory_dropped_reason_is_an_open_set(client: mc.Client) -> None:
    c = computer(client)
    c._note_clone_answer({"memory_dropped": True, "memory_dropped_reason": "capture unrecorded"})
    assert c.memory_dropped and c.memory_dropped_reason == "capture unrecorded"


# --- snapshots -----------------------------------------------------------------


def test_restore_available_and_computer_unreachable() -> None:
    row = {"id": "snap-1", "computer_id": "vm-1", "state": "durable"}
    assert mc.Snapshot.from_api(row).restore_available is None
    assert mc.Snapshot.from_api({**row, "restore_available": False}).restore_available is False
    assert mc.Snapshot.from_api({**row, "restore_available": True}).restore_available is True
    stub = mc.Snapshot.from_api({**row, "computer_unreachable": True})
    assert stub.computer_unreachable and not stub.orphaned


def test_holdings_carry_presence_and_the_in_flight_counts() -> None:
    held = mc.SnapshotHoldings.from_api(
        {
            "count": 3,
            "size_bytes": 9,
            "fingerprint": "fp",
            "computer_present": False,
            "capturing": 1,
            "deleting": 2,
        }
    )
    assert (held.computer_present, held.capturing, held.deleting) == (False, 1, 2)
    assert mc.SnapshotHoldings.from_api({"fingerprint": "fp"}).computer_present is None


# --- input -------------------------------------------------------------------


@respx.mock
def test_type_returns_the_mechanism_and_waits_long_enough_for_unicode(
    client: mc.Client,
) -> None:
    seen: list[float | None] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        text = json.loads(request.content)["text"]
        return httpx.Response(
            200, json={"ok": True, "mechanism": "physical" if text.isascii() else "mixed"}
        )

    respx.post(f"{BASE}/computers/vm-1/input").mock(side_effect=answer)
    c = computer(client)
    assert c.type("hi") == "physical"
    assert c.type("Café") == "mixed"
    assert seen[1] is not None and seen[1] >= UNICODE_TYPE_TIMEOUT
    assert seen[0] is not None and seen[0] < UNICODE_TYPE_TIMEOUT


@respx.mock
async def test_async_type_answers_none_from_a_platform_without_a_mechanism(
    async_client: mc.AsyncClient,
) -> None:
    respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    c = mc.AsyncComputer(async_client._t, COMPUTER)
    assert await c.type("hi") is None


def test_paste_body() -> None:
    assert _api.paste_body("Café — 東京", False) == {"action": "paste", "text": "Café — 東京"}
    assert _api.paste_body("ls", True)["keys"] == ["ctrl", "shift", "v"]
    for bad in ("", "a\x00b", "x" * 8193):
        with pytest.raises(ValueError):
            _api.paste_body(bad, False)
    with pytest.raises(ValueError):
        _api.paste_body("x", "false")


# --- files -------------------------------------------------------------------


@respx.mock
def test_no_wake_is_sent_only_when_asked(client: mc.Client) -> None:
    get = respx.get(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200, content=b"x"))
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200, json={}))
    c = computer(client)
    c.read_file("/tmp/a")
    assert "no_wake" not in get.calls.last.request.url.params
    c.read_file("/tmp/a", no_wake=True)
    assert get.calls.last.request.url.params["no_wake"] == "1"
    c.write_file("/tmp/a", b"x", no_wake=True, overwrite=False)
    params = put.calls.last.request.url.params
    assert params["no_wake"] == "1" and params["overwrite"] == "false"
    with pytest.raises(ValueError):
        _api.files_params("/tmp/a", "yes")  # type: ignore[arg-type]


@respx.mock
def test_list_dir_decodes_a_bounded_listing(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/files/list").mock(
        httpx.Response(
            200,
            json={
                "path": "/home/user",
                "entries": [
                    {"name": "a b.txt", "type": "file", "size_bytes": 5},
                    {"name": "docs", "type": "directory"},
                ],
                "truncated": True,
                "skipped": 2,
            },
        )
    )
    listed = computer(client).list_directory("/home/user")
    assert route.calls.last.request.url.params["path"] == "/home/user"
    assert [e.name for e in listed.entries] == ["a b.txt", "docs"]
    assert listed.entries[0].size_bytes == 5 and listed.entries[1].size_bytes is None
    assert listed.truncated and listed.skipped == 2
    with pytest.raises(ValueError):
        computer(client).list_directory("relative")


def test_a_listing_with_no_entries_list_is_refused() -> None:
    with pytest.raises(mc.MandalaError):
        mc.GuestDirectory.from_api({"path": "/", "truncated": False, "skipped": 0})


# --- passive history ---------------------------------------------------------


@respx.mock
def test_signals_baseline_then_replay(client: mc.Client) -> None:
    page = {
        "computer": "vm-1",
        "from": "h-0",
        "cursor": "h-1",
        "events": [{"type": "computer.idle", "seq": 1, "data": {"idle_seconds": 60}}],
        "more": True,
        "baseline": False,
        "supported": ["computer.idle"],
        "retention": "ephemeral",
    }
    route = respx.get(f"{BASE}/computers/vm-1/signals").mock(httpx.Response(200, json=page))
    got = computer(client).signals("h-0", limit=10)
    assert route.calls.last.request.url.params == httpx.QueryParams({"since": "h-0", "limit": "10"})
    assert got.cursor == "h-1" and got.from_cursor == "h-0" and got.more and got.gap is None
    assert got.events[0]["type"] == "computer.idle"
    computer(client).signals()
    assert not route.calls.last.request.url.params
    for bad in (0, 101, True):
        with pytest.raises((ValueError, TypeError)):
            _api.signals_params(None, bad)


def test_a_signal_page_without_a_cursor_is_refused_not_read_as_empty() -> None:
    with pytest.raises(mc.MandalaError):
        mc.SignalPage.from_api({"events": []})


@respx.mock
async def test_async_activities_pages_and_changes(async_client: mc.AsyncClient) -> None:
    item = {
        "activity_id": "act_1",
        "account_id": "acc",
        "computer_id": "vm-1",
        "workspace_id": None,
        "channel": "api",
        "route": "exec",
        "action": "exec",
        "state": "completed",
        "received_at": "t0",
        "observed_at": "t1",
        "http_status": 200,
        "exit_code": 0,
        "has_results": True,
    }
    page = {"items": [item], "next_cursor": "old-1", "changes_cursor": "chg-1", "gap": False}
    route = respx.get(f"{BASE}/computers/vm-1/activities").mock(httpx.Response(200, json=page))
    c = mc.AsyncComputer(async_client._t, COMPUTER)
    got = await c.activities()
    assert got.next_cursor == "old-1" and got.changes_cursor == "chg-1" and got.health is None
    assert got.items[0].exit_code == 0 and got.items[0].has_results
    await c.activities("chg-1", changes=True)
    assert route.calls.last.request.url.params == httpx.QueryParams(
        {"cursor": "chg-1", "changes": "1"}
    )
    with pytest.raises(ValueError):
        await c.activities(changes=True)


# --- delete ------------------------------------------------------------------


@respx.mock
def test_a_detailed_delete_keeps_the_whole_answer(client: mc.Client) -> None:
    answer = {
        "ok": False,
        "snapshots_deleted": 1,
        "computer_deleted": None,
        "error": "queued",
        "purge": {
            "selected": 2,
            "confirmed": 1,
            "queued": 1,
            "failed": 0,
            "unknown": 0,
            "remaining": None,
            "unselected": 0,
            "complete": False,
        },
    }
    respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(202, json=answer))
    c = computer(client)
    got = c.delete(purge_snapshots=True, expect="fp", detailed=True)
    assert got.ok is False and got.computer_deleted is None and got.error == "queued"
    assert got.purge is not None and got.purge.queued == 1 and got.purge.remaining is None
    assert c.delete(purge_snapshots=True, expect="fp") == 1


# --- refusals ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason", "transient"),
    [
        (409, "starting", True),
        (409, "contention", True),
        (409, "unavailable", False),
        (409, "unsupported", False),
        (409, "exists", False),
        (409, "some-new-word", True),  # unknown: the type's answer stands
        (400, "some-new-word", False),
        (502, None, False),  # the guest agent silent past its boot window
    ],
)
@respx.mock
def test_refusal_words_on_the_wire(
    client: mc.Client, status: int, reason: str | None, transient: bool
) -> None:
    body: dict[str, Any] = {"error": "refused"}
    if reason is not None:
        body["reason"] = reason
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(status, json=body))
    with pytest.raises(mc.APIError) as caught:
        computer(client).exec("true")
    assert caught.value.reason == reason
    assert mc.is_transient(caught.value) is transient


@pytest.mark.parametrize(
    ("answer", "cls"),
    [
        ({"error": "not running"}, mc.ComputerNotRunningError),
        ({"error": "not running", "reason": "unavailable"}, mc.ComputerNotRunningError),
        ({"error": "busy", "reason": "contention"}, mc.ConflictError),
    ],
)
@respx.mock
def test_a_no_wake_refusal_is_final(
    client: mc.Client, answer: dict[str, Any], cls: type[Exception]
) -> None:
    """Documented as a 409 with no reason; only a start changes it."""
    respx.get(f"{BASE}/computers/vm-1/files").mock(httpx.Response(409, json=answer))
    respx.put(f"{BASE}/computers/vm-1/files").mock(httpx.Response(409, json=answer))
    c = computer(client)
    for call in (
        lambda: c.read_file("/tmp/a", no_wake=True),
        lambda: c.read_file_part("/tmp/a", length=4, no_wake=True),
        lambda: c.write_file("/tmp/a", b"x", no_wake=True),
    ):
        with pytest.raises(mc.ConflictError) as caught:
            call()
        assert type(caught.value) is cls
        assert mc.is_transient(caught.value) is (cls is mc.ConflictError)


@respx.mock
async def test_async_a_no_wake_refusal_is_final(async_client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(httpx.Response(409, json={"error": "stopped"}))
    c = mc.AsyncComputer(async_client._t, COMPUTER)
    with pytest.raises(mc.ComputerNotRunningError):
        await c.read_file("/tmp/a", no_wake=True)


@respx.mock
def test_without_no_wake_a_reasonless_409_stays_a_conflict(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(httpx.Response(409, json={"error": "busy"}))
    with pytest.raises(mc.ConflictError) as caught:
        computer(client).read_file("/tmp/a")
    assert type(caught.value) is mc.ConflictError


@respx.mock
def test_a_create_only_no_wake_upload_keeps_the_create_only_reading(client: mc.Client) -> None:
    """Both asked, no word: the refusal that claims nothing about path or computer."""
    respx.put(f"{BASE}/computers/vm-1/files").mock(httpx.Response(409, json={"error": "?"}))
    with pytest.raises(mc.CreateOnlyConflictError):
        computer(client).write_file("/tmp/a", b"x", overwrite=False, no_wake=True)


# --- positional compatibility (OPL-5026 review) --------------------------------


def test_snapshot_keeps_its_earlier_positional_signature() -> None:
    """New fields were appended, so a pre-change positional call means the same."""
    raw = {"id": "snap-1"}
    snap = mc.Snapshot(
        "snap-1",
        "vm-1",
        "n",
        "disk",
        "durable",
        5,
        "t",
        False,
        True,  # through auto
        "name",
        True,
        True,  # computer_name, orphaned, unreachable
        "linux",
        "base",
        2,
        4096,
        40,
        "1280x800x24",
        raw,
    )
    assert snap.unreachable is True and snap.orphaned is True
    assert (snap.os, snap.template, snap.cpu, snap.resolution) == (
        "linux",
        "base",
        2,
        "1280x800x24",
    )
    assert snap.raw == raw
    assert snap.computer_unreachable is False and snap.restore_available is None


def test_holdings_keep_their_earlier_positional_signature() -> None:
    raw = {"count": 1}
    held = mc.SnapshotHoldings(1, 2, "fp", raw)
    assert held.raw == raw and held.computer_present is None
    assert (held.capturing, held.deleting) == (0, 0)


def test_computer_keeps_its_constructor(client: mc.Client) -> None:
    c = mc.Computer(client._t, COMPUTER)
    assert c.id == "vm-1" and c.secrets_pending is False and c.secret_bindings == []


def test_api_errors_keep_their_constructors() -> None:
    err = mc.UnavailableError("away", status=503, body={"reason": "x"}, retry_after=1.0)
    assert err.method is None and err.reason == "x"
    moved = mc.MoveRequiredError("move", status=409, move_possible=True)
    assert moved.method is None
