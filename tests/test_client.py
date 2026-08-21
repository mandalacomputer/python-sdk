"""Client behaviour against a mocked API."""

from __future__ import annotations

import builtins
import io
import json
import shlex
import time

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"

COMPUTER = {
    "id": "vm-1",
    "name": "dev",
    "status": "running",
    "os": "linux",
    "template": "base",
    "cpu": 2,
    "ram_mb": 2048,
    "disk_gb": 20,
    "created_at": "2026-07-31T00:00:00Z",
}


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


def test_api_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MANDALA_API_KEY", raising=False)
    with pytest.raises(mc.MandalaError, match="No API key"):
        mc.Client()


def test_api_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "gck_env")
    assert mc.Client(base_url=BASE).base_url == BASE


@respx.mock
def test_sends_bearer_token(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[COMPUTER]))
    client.computers.list()
    assert route.calls.last.request.headers["Authorization"] == "Bearer gck_test"


@respx.mock
def test_list_and_fields(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[COMPUTER]))
    (c,) = client.computers.list()
    assert (c.id, c.name, c.status, c.cpu, c.ram_mb) == ("vm-1", "dev", "running", 2, 2048)


@respx.mock
def test_create_omits_unset_fields(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    client.computers.create(name="dev", template="base")
    body = route.calls.last.request.content.decode()
    assert '"name"' in body and '"template"' in body
    # Unspecified sizing must not be sent as null — the server applies the
    # template's defaults only when the key is absent.
    assert "cpu" not in body and "ram_mb" not in body and "disk_gb" not in body


@respx.mock
def test_create_by_size_sends_the_word_alone(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    client.computers.create(name="dev", size="large")
    body = json.loads(route.calls.last.request.content)
    # The server expands the row into template and numbers; the SDK's half of
    # the contract is to send the name and nothing it stands in for.
    assert body["size"] == "large"
    assert "template" not in body and "cpu" not in body


def test_create_refuses_size_beside_explicit_sizing(client: mc.Client) -> None:
    # The server refuses this too, but the mistake is knowable without the
    # round trip — no request is mocked, so reaching the wire would fail loud.
    with pytest.raises(ValueError, match="size"):
        client.computers.create(size="large", cpu=4)


@pytest.mark.parametrize("name", ["", "   "])
def test_create_refuses_an_empty_name_before_the_request(client: mc.Client, name: str) -> None:
    with pytest.raises(ValueError, match="name must not be empty"):
        client.computers.create(name=name)


def test_clone_refuses_an_empty_optional_name_before_the_request(client: mc.Client) -> None:
    with pytest.raises(ValueError, match="name must not be empty"):
        client.snapshots.clone("snap-1", name=" \t")


def test_snapshot_refuses_an_empty_optional_name_before_the_request(client: mc.Client) -> None:
    with pytest.raises(ValueError, match="name must not be empty"):
        mc.Computer(client._t, COMPUTER).snapshot(name=" \t")


@respx.mock
def test_sizes_list(client: mc.Client) -> None:
    respx.get(f"{BASE}/sizes").mock(
        httpx.Response(
            200,
            json=[
                {
                    "id": "large",
                    "label": "Large",
                    "template": "base",
                    "cpu": 4,
                    "ram_mb": 8192,
                    "disk_gb": 40,
                    "allowed": True,
                    "cheapest_plan": "solo",
                }
            ],
        )
    )
    (s,) = client.sizes.list()
    assert (s.id, s.template, s.cpu, s.ram_mb, s.disk_gb) == ("large", "base", 4, 8192, 40)
    assert s.allowed is True and s.cheapest_plan == "solo"


@respx.mock
def test_unknown_response_fields_survive(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "future_field": 42})
    )
    assert client.computers.get("vm-1").raw["future_field"] == 42


# --- errors ---------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (401, mc.AuthenticationError),
        (402, mc.PlanLimitError),
        (403, mc.PermissionDeniedError),
        (404, mc.NotFoundError),
        (500, mc.APIError),
    ],
)
def test_status_maps_to_exception(client: mc.Client, status: int, exc: type) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(exc) as e:
        client.computers.list()
    assert e.value.status == status  # type: ignore[attr-defined]
    assert "nope" in str(e.value)


@respx.mock
def test_non_json_error_still_raises(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(502, text="bad gateway"))
    with pytest.raises(mc.APIError, match="bad gateway"):
        client.computers.list()


@respx.mock
def test_a_network_failure_is_still_a_mandala_error(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers").mock(side_effect=httpx.ConnectError("reset"))
    with pytest.raises(mc.MandalaError, match="ConnectError") as caught:
        client.computers.list()
    assert isinstance(caught.value.__cause__, httpx.ConnectError)


@respx.mock
@pytest.mark.parametrize("content", [b"", b"<html>captive portal</html>", b"{}"])
def test_a_listing_must_be_a_json_array_of_objects(client: mc.Client, content: bytes) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, content=content))
    with pytest.raises(mc.MandalaError, match="not a JSON array of objects"):
        client.computers.list()


@respx.mock
def test_exec_does_not_turn_an_empty_200_into_success(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200))
    with pytest.raises(mc.MandalaError, match="not a JSON object"):
        _computer(client).exec("true")


# --- control --------------------------------------------------------------


@respx.mock
def test_click_payload(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    mc.Computer(client._t, COMPUTER).click(10, 20)
    assert json.loads(route.calls.last.request.content) == {
        "action": "left_click",
        "x": 10,
        "y": 20,
    }


@respx.mock
def test_key_chord(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    mc.Computer(client._t, COMPUTER).key("ctrl", "c")
    assert json.loads(route.calls.last.request.content)["keys"] == ["ctrl", "c"]


def test_key_requires_at_least_one(client: mc.Client) -> None:
    with pytest.raises(ValueError):
        mc.Computer(client._t, COMPUTER).key()


def test_scroll_rejects_bad_direction(client: mc.Client) -> None:
    with pytest.raises(ValueError, match="up.*down"):
        mc.Computer(client._t, COMPUTER).scroll(direction="sideways")


def test_scroll_rejects_a_non_positive_amount(client: mc.Client) -> None:
    with pytest.raises(ValueError, match="amount must be positive"):
        mc.Computer(client._t, COMPUTER).scroll(amount=0)


@respx.mock
def test_screenshot_returns_bytes(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/screenshot").mock(
        httpx.Response(200, content=b"\x89PNG\r\n", headers={"Content-Type": "image/png"})
    )
    assert mc.Computer(client._t, COMPUTER).screenshot().startswith(b"\x89PNG")
    assert "image/png" in route.calls.last.request.headers["Accept"]


@respx.mock
def test_screenshot_refuses_a_json_success_body(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/screenshot").mock(
        httpx.Response(200, json={"error": "sign in again"})
    )
    with pytest.raises(mc.MandalaError, match="not binary content"):
        mc.Computer(client._t, COMPUTER).screenshot()


@respx.mock
def test_screenshot_width_becomes_query_param(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/screenshot").mock(httpx.Response(200, content=b"jpg"))
    mc.Computer(client._t, COMPUTER).screenshot(width=320)
    assert route.calls.last.request.url.params["w"] == "320"


@pytest.mark.parametrize("width", [0, -1])
def test_screenshot_refuses_a_non_positive_width(client: mc.Client, width: int) -> None:
    with pytest.raises(ValueError, match="width must be positive"):
        mc.Computer(client._t, COMPUTER).screenshot(width=width)


@respx.mock
def test_screenshot_fresh_becomes_query_param(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/screenshot").mock(httpx.Response(200, content=b"png"))
    mc.Computer(client._t, COMPUTER).screenshot(fresh=True)
    # `1`, not `true`: the platform documents the parameter as that one value.
    assert route.calls.last.request.url.params["fresh"] == "1"


@respx.mock
def test_screenshot_is_cached_unless_asked_otherwise(client: mc.Client) -> None:
    """The default sends neither parameter, and that is the cheap answer.

    Worth pinning rather than leaving implied: making `fresh` the default would
    turn every thumbnail into a capture, and dropping it would put a drive loop
    back on frames that predate its own clicks.
    """
    route = respx.get(f"{BASE}/computers/vm-1/screenshot").mock(httpx.Response(200, content=b"png"))
    mc.Computer(client._t, COMPUTER).screenshot()
    assert "fresh" not in route.calls.last.request.url.params
    assert "w" not in route.calls.last.request.url.params


@respx.mock
def test_stop_force_becomes_query_param(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/stop").mock(httpx.Response(200, json={"ok": True}))
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    mc.Computer(client._t, COMPUTER).stop(force=True)
    assert route.calls.last.request.url.params["force"] == "true"


@respx.mock
def test_stop_asks_the_guest_by_default(client: mc.Client) -> None:
    """No `force` unless it was asked for — this one can lose unwritten data."""
    route = respx.post(f"{BASE}/computers/vm-1/stop").mock(httpx.Response(200, json={"ok": True}))
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    mc.Computer(client._t, COMPUTER).stop()
    assert "force" not in route.calls.last.request.url.params


@respx.mock
def test_exec_nonzero_exit_is_returned_not_raised(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(
            200, json={"exit_code": 1, "stdout": "", "stderr": "boom", "timed_out": False}
        )
    )
    res = mc.Computer(client._t, COMPUTER).exec("false")
    assert res.exit_code == 1 and res.stderr == "boom" and not res.ok


@respx.mock
def test_exec_omits_session_unless_desktop_requested(client: mc.Client) -> None:
    """The server defaults to the system context; an empty session is not the same
    as an absent one, so the key stays off the wire until it is asked for."""
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
    )
    mc.Computer(client._t, COMPUTER).exec("whoami")
    assert "session" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_exec_desktop_sends_session_desktop(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
    )
    mc.Computer(client._t, COMPUTER).exec("whoami", desktop=True)
    assert json.loads(route.calls.last.request.content)["session"] == "desktop"


# --- waiting --------------------------------------------------------------


@respx.mock
def test_wait_until_running_polls_until_ready(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        side_effect=[
            httpx.Response(200, json={**COMPUTER, "status": "stopped"}),
            httpx.Response(200, json={**COMPUTER, "status": "running"}),
        ]
    )
    c = mc.Computer(client._t, {**COMPUTER, "status": "stopped"})
    assert c.wait_until_running(timeout=5, poll=0).status == "running"


@respx.mock
def test_wait_until_running_times_out(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "stopped"})
    )
    c = mc.Computer(client._t, COMPUTER)
    with pytest.raises(mc.TimeoutError):
        c.wait_until_running(timeout=0, poll=0)


@respx.mock
def test_wait_until_running_caps_refresh_to_its_remaining_budget(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "running"})
    )
    mc.Computer(client._t, COMPUTER).wait_until_running(timeout=2, poll=0)
    assert max(route.calls.last.request.extensions["timeout"].values()) <= 2


@respx.mock
def test_wait_for_guest_ignores_errors_while_booting(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        side_effect=[
            httpx.Response(400, json={"error": "not running"}),
            httpx.Response(
                200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}
            ),
        ]
    )
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    mc.Computer(client._t, COMPUTER).wait_for_guest(timeout=5, poll=0)


@respx.mock
def test_wait_for_guest_refreshes_a_stale_running_handle(client: mc.Client) -> None:
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(400, json={"error": "not running"})
    )
    refresh = respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "stopped"})
    )
    with pytest.raises(mc.MandalaError, match="stopped.+call start"):
        mc.Computer(client._t, COMPUTER).wait_for_guest(timeout=30, poll=0)
    assert probe.call_count == 1 and refresh.call_count == 1


@respx.mock
def test_wait_for_guest_reports_a_failed_start_without_probing(client: mc.Client) -> None:
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json={}))
    computer = mc.Computer(
        client._t,
        {**COMPUTER, "status": "stopped", "start_error": "no host had room"},
    )
    with pytest.raises(mc.MandalaError, match="did not start: no host had room"):
        computer.wait_for_guest(timeout=30, poll=0)
    assert not probe.called


@respx.mock
def test_wait_for_guest_reports_an_already_stopped_computer(client: mc.Client) -> None:
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json={}))
    with pytest.raises(mc.MandalaError, match=r"stopped.+call start\(\) first"):
        mc.Computer(client._t, {**COMPUTER, "status": "stopped"}).wait_for_guest()
    assert not probe.called


@respx.mock
def test_wait_for_guest_still_resumes_a_suspended_computer(client: mc.Client) -> None:
    probe = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    computer = mc.Computer(client._t, {**COMPUTER, "status": "suspended"})
    assert computer.wait_for_guest(timeout=5, poll=0) is computer
    assert probe.call_count == 1


# --- rename ---------------------------------------------------------------


@respx.mock
def test_rename_updates_the_handle_in_place(client: mc.Client) -> None:
    """The handle a caller is already holding is the one that must be right.

    Renaming through a stale handle and then reading .name off it is the
    obvious next line of anyone's script.
    """
    route = respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "name": "build box"})
    )
    c = mc.Computer(client._t, COMPUTER)
    assert c.rename("build box") is c
    assert c.name == "build box"
    assert json.loads(route.calls[0].request.content) == {"name": "build box"}


@respx.mock
def test_rename_reports_the_name_the_server_settled_on(client: mc.Client) -> None:
    """The server trims and caps, so the stored name may not be the one sent."""
    respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "name": "two lines tabbed"})
    )
    c = mc.Computer(client._t, COMPUTER)
    assert c.rename("  two\nlines\ttabbed  ").name == "two lines tabbed"


def test_rename_refuses_an_empty_name_without_asking(client: mc.Client) -> None:
    """The server refuses it too; there is no reason to spend a round trip."""
    c = mc.Computer(client._t, COMPUTER)
    for name in ("", "   ", "\t\n"):
        with pytest.raises(ValueError, match="must not be empty"):
            c.rename(name)
    assert c.name == "dev", "a refused rename must not touch the handle"


# --- schedule -------------------------------------------------------------


@respx.mock
def test_schedule_is_the_window_only(client: mc.Client) -> None:
    """The schedule carries no last_run, by design.

    It was the scheduler's own bookkeeping and lied in both directions — as a
    zero time it reported the last backup two millennia ago, and as a creation
    stamp it reported one that never happened. Snapshot capture times are the
    honest source; pinned here so it cannot quietly come back.
    """
    respx.get(f"{BASE}/computers/vm-1/schedule").mock(
        httpx.Response(200, json={"enabled": False, "hour": 0, "minute": 0, "tz": "UTC"})
    )
    sched = mc.Computer(client._t, COMPUTER).schedule()
    assert "last_run" not in sched
    # Empty string would mean "UTC" to the daemon but is rejected by every
    # timezone library, so the surface must name the zone.
    assert sched["tz"] == "UTC"


@respx.mock
def test_scheduled_snapshots_are_distinguishable(client: mc.Client) -> None:
    """`auto` is what makes snapshot times usable as backup history.

    It also marks the only snapshots retention will ever age out.
    """
    respx.get(f"{BASE}/snapshots").mock(
        httpx.Response(
            200,
            json=[
                {
                    "id": "s1",
                    "computer_id": "vm-1",
                    "created_at": "2026-07-31T04:00:00Z",
                    "auto": True,
                },
                {
                    "id": "s2",
                    "computer_id": "vm-1",
                    "created_at": "2026-07-30T12:00:00Z",
                    "auto": False,
                },
            ],
        )
    )
    snaps = client.snapshots.list()
    assert [s.auto for s in snaps] == [True, False]
    assert [s.is_scheduled for s in snaps] == [True, False]
    last_backup = max((s.created_at for s in snaps if s.auto), default=None)
    assert last_backup == "2026-07-31T04:00:00Z"


@respx.mock
def test_clear_schedule_is_a_delete_not_a_disable(client: mc.Client) -> None:
    """Disabling keeps the time and the bookkeeping; clearing removes both."""
    cleared = {"enabled": False, "hour": 0, "minute": 0, "tz": "UTC"}
    route = respx.delete(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json=cleared))
    put = respx.put(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json={}))

    c = mc.Computer(client._t, {**COMPUTER, "snapshot_schedule": {"enabled": True}})
    assert c.clear_schedule() == cleared
    assert c.snapshot_schedule is None
    assert route.called
    assert not put.called, "clearing must not go through the set path"


@respx.mock
def test_set_schedule_validates_before_sending(client: mc.Client) -> None:
    route = respx.put(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json={}))
    c = mc.Computer(client._t, COMPUTER)
    with pytest.raises(ValueError, match="hour"):
        c.set_schedule(enabled=True, hour=24)
    with pytest.raises(ValueError, match="minute"):
        c.set_schedule(enabled=True, minute=60)
    assert not route.called, "invalid input must not reach the API"


# --- ephemeral ------------------------------------------------------------


@respx.mock
def test_ephemeral_deletes_on_exit(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    delete = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    with client.computers.ephemeral(template="base") as c:
        assert c.id == "vm-1"
    assert delete.called


@respx.mock
def test_ephemeral_deletes_even_when_block_raises(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    delete = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    with pytest.raises(RuntimeError), client.computers.ephemeral(template="base"):
        raise RuntimeError("boom")
    assert delete.called, "a leaked computer bills until someone notices"


@respx.mock
def test_create_does_not_delete(client: mc.Client) -> None:
    """create() is not scoped to a block — only ephemeral() may destroy a disk."""
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    delete = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200))
    client.computers.create(template="base")
    assert not delete.called


# --- open() ---------------------------------------------------------------


@respx.mock
def test_open_runs_in_the_desktop_session(client: mc.Client) -> None:
    """A browser with no DISPLAY is the bug open() exists to stop anyone hitting."""
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
    )
    mc.Computer(client._t, COMPUTER).open("https://example.com")
    body = json.loads(route.calls.last.request.content)
    assert body["session"] == "desktop"


@respx.mock
def test_open_detaches_the_launch(client: mc.Client) -> None:
    """A foreground browser blocks until the timeout kills it, then reports a
    failure it did not have — having opened the window anyway."""
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
    )
    mc.Computer(client._t, COMPUTER).open("https://example.com")
    command = json.loads(route.calls.last.request.content)["command"]
    assert command.endswith("&")
    assert "nohup" in command


@respx.mock
def test_open_does_not_ask_for_the_default_handler(client: mc.Client) -> None:
    """Naming the browser is the whole point of this method: the choice lives
    here rather than in whatever the guest's default handler resolves to."""
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
    )
    mc.Computer(client._t, COMPUTER).open("https://example.com")
    command = json.loads(route.calls.last.request.content)["command"]
    for handler in ("xdg-open", "exo-open", "sensible-browser", "x-www-browser"):
        assert handler not in command


def test_open_url_reaches_the_shell_as_one_argument() -> None:
    """The URL is interpolated into a shell command, so a URL that is also shell
    syntax must not become shell syntax. Parsed the way the shell would parse
    it, the whole URL has to come back as a single word — a substring check
    would pass on quoting that does not actually hold."""
    url = "https://x/?a=b; touch /tmp/pwned"
    assert url in shlex.split(mc._api.open_url_command(url))


def test_open_refuses_a_url_a_browser_would_read_as_a_flag() -> None:
    """Quoting stops the shell seeing it; nothing stops the browser seeing it.
    No URL starts with a dash, so this is refused rather than escaped."""
    with pytest.raises(ValueError, match="must not start with"):
        mc._api.open_url_command("--profile=/tmp/evil")


def test_open_refuses_an_empty_url() -> None:
    for blank in ("", "   "):
        with pytest.raises(ValueError, match="must not be empty"):
            mc._api.open_url_command(blank)


# --- the computer-use verb set (OPL-3567) ---------------------------------
#
# Every action Anthropic's computer tool can emit has a method here. The point
# is not coverage for its own sake: a caller wiring the standard tool definition
# to this SDK has to stub whatever is missing, and drag, triple click, held
# buttons, held keys and wait are exactly the actions a model reaches for when a
# click has not worked.


def _computer(client: mc.Client) -> mc.Computer:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    return client.computers.get("vm-1")


@respx.mock
def test_a_click_with_no_coordinate_clicks_where_the_pointer_is(client: mc.Client) -> None:
    # Absent and (0, 0) are different requests. A model emits a bare click after
    # a move; sending zeros would click the corner of the screen instead.
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    c = _computer(client)
    c.click()
    assert json.loads(route.calls[0].request.content) == {"action": "left_click"}
    c.click(10, 20)
    assert json.loads(route.calls[1].request.content) == {"action": "left_click", "x": 10, "y": 20}


@respx.mock
def test_modifiers_are_held_for_the_click(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    _computer(client).click(10, 20, "ctrl", "shift")
    assert json.loads(route.calls[0].request.content)["text"] == "ctrl+shift"


@respx.mock
def test_a_drag_sends_both_ends(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    c = _computer(client)
    c.drag(90, 80, from_x=10, from_y=20)
    assert json.loads(route.calls[0].request.content) == {
        "action": "left_click_drag",
        "coordinate": [90, 80],
        "start_coordinate": [10, 20],
    }
    # Without an origin the platform is asked to start from wherever the pointer
    # is — it refuses if nothing has put it anywhere, rather than guessing.
    c.drag(90, 80)
    assert "start_coordinate" not in json.loads(route.calls[1].request.content)


@respx.mock
def test_the_cursor_position_is_none_until_something_places_it(client: mc.Client) -> None:
    # The virtual pointing device takes coordinates and reports none back, so an
    # untouched pointer has no position anybody knows. Zeros here would be
    # indistinguishable from the top-left corner, which is the exact wrong thing
    # to hand a caller about to move relative to it.
    respx.post(f"{BASE}/computers/vm-1/input").mock(
        httpx.Response(200, json={"ok": True, "x": 0, "y": 0, "known": False})
    )
    assert _computer(client).cursor_position() is None


@respx.mock
def test_the_cursor_position_is_returned_once_it_is_known(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/input").mock(
        httpx.Response(200, json={"ok": True, "x": 640, "y": 400, "known": True})
    )
    assert _computer(client).cursor_position() == (640, 400)


@respx.mock
def test_scroll_takes_all_four_directions_and_refuses_the_rest(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    c = _computer(client)
    for direction in ("up", "down", "left", "right"):
        c.scroll(1, 2, direction=direction)
    with pytest.raises(ValueError):
        c.scroll(1, 2, direction="sideways")


@respx.mock
def test_hold_key_and_wait_refuse_a_non_positive_duration(client: mc.Client) -> None:
    c = _computer(client)
    with pytest.raises(ValueError):
        c.hold_key("shift", seconds=0)
    with pytest.raises(ValueError):
        c.wait(-1)
    with pytest.raises(ValueError):
        c.hold_key(seconds=1)


@respx.mock
def test_long_input_actions_widen_the_request_budget(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    c = _computer(client)
    c.wait(120)
    assert _budget(route)["read"] == 120 + mc._client.DEADLINE_SLACK
    c.hold_key("shift", seconds=90)
    assert _budget(route)["read"] == 90 + mc._client.DEADLINE_SLACK


# --- resolution (OPL-3567) -------------------------------------------------


@respx.mock
def test_resolution_is_sent_on_create_and_read_back(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers").mock(
        httpx.Response(200, json={**COMPUTER, "id": "vm-2", "resolution": "1920x1080x24"})
    )
    c = client.computers.create(template="base", resolution="1920x1080")
    assert json.loads(route.calls[0].request.content)["resolution"] == "1920x1080"
    assert c.resolution == "1920x1080x24"
    assert c.screen == (1920, 1080)


@respx.mock
def test_a_computer_that_reports_no_resolution_reads_as_the_default(client: mc.Client) -> None:
    # A server old enough not to report one has computers that render at
    # 1280x800x24, so that is the answer rather than an empty string — a caller
    # sizing a coordinate space needs a number.
    c = _computer(client)
    assert c.resolution == "1280x800x24"
    assert c.screen == (1280, 800)


def test_screen_accepts_uppercase_resolution_separators() -> None:
    c = mc.Computer(None, {**COMPUTER, "resolution": "1920X1080x24"})  # type: ignore[arg-type]
    assert c.screen == (1920, 1080)


def test_screen_rejects_a_reported_malformed_resolution() -> None:
    c = mc.Computer(None, {**COMPUTER, "resolution": "1920xwide"})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid computer resolution"):
        _ = c.screen


@respx.mock
def test_a_defaulted_scroll_does_not_move_the_pointer_to_the_corner(client: mc.Client) -> None:
    # scroll() used to send x=0,y=0 always, and the server reads a flat
    # coordinate as a position — so a defaulted scroll moved to the top-left
    # first and scrolled whatever was there rather than what the caller was
    # looking at. Omitted means "under the pointer", which is what it has
    # always meant.
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    c = _computer(client)
    c.scroll(direction="down")
    body = json.loads(route.calls[0].request.content)
    assert "x" not in body and "y" not in body
    # And a point the caller did give still travels, including the corner —
    # sent as `coordinate`, which is the one spelling the platform cannot
    # confuse with a defaulted scroll. Asserting on `x` here passed while the
    # end-to-end operation scrolled under the pointer instead.
    c.scroll(0, 0, direction="down")
    sent = json.loads(route.calls[1].request.content)
    assert sent["coordinate"] == [0, 0]
    assert "x" not in sent


@respx.mock
def test_half_a_drag_origin_is_refused_rather_than_dropped(client: mc.Client) -> None:
    # Dropping it produces a drag that succeeds while selecting a different
    # region — the worst shape a mistake can take, because nothing reports it.
    c = _computer(client)
    with pytest.raises(ValueError):
        c.drag(90, 80, from_x=10)
    with pytest.raises(ValueError):
        c.drag(90, 80, from_y=20)


# --- the create envelope ----------------------------------------------------


VNC = {
    "url": "wss://api.test/api/v1/computers/vm-1/vnc?token=abc",
    "view_url": "wss://api.test/api/v1/computers/vm-1/vnc?token=view-def",
    "token": "abc",
    "view_token": "view-def",
    "embed_url": "https://api.test/embed/desktop#computer=vm-1&token=view-def",
}


@respx.mock
def test_a_create_that_could_not_boot_still_hands_back_the_computer(
    client: mc.Client,
) -> None:
    """The platform answers 201 {computer, start_error} rather than an error.

    Read as an ordinary computer that envelope has no id — and the id is the
    whole reason it is sent, because the machine exists and is being paid for.
    """
    respx.post(f"{BASE}/computers").mock(
        httpx.Response(
            201,
            json={"computer": COMPUTER, "start_error": "no host had room to start it"},
        )
    )
    c = client.computers.create(template="base")
    assert c.id == "vm-1"
    assert c.cpu == 2
    assert c.start_error == "no host had room to start it"


@respx.mock
def test_an_ordinary_create_reports_no_start_error(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(201, json=COMPUTER))
    assert client.computers.create(template="base").start_error == ""


@respx.mock
def test_a_refresh_clears_the_start_error(client: mc.Client) -> None:
    # It describes one start attempt, not the machine.
    respx.post(f"{BASE}/computers").mock(
        httpx.Response(201, json={"computer": COMPUTER, "start_error": "boom"})
    )
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    c = client.computers.create(template="base")
    assert c.refresh().start_error == ""


# --- suspend ----------------------------------------------------------------


@respx.mock
def test_suspend_posts_and_reads_the_state_back(client: mc.Client) -> None:
    suspended = {**COMPUTER, "status": "suspended", "suspended": {"at": "2026-08-14T09:00:00Z"}}
    route = respx.post(f"{BASE}/computers/vm-1/suspend").mock(
        httpx.Response(200, json={"ok": True})
    )
    # Running when it is fetched, suspended when it is read back afterwards.
    respx.get(f"{BASE}/computers/vm-1").mock(
        side_effect=[
            httpx.Response(200, json=COMPUTER),
            httpx.Response(200, json=suspended),
        ]
    )
    c = client.computers.get("vm-1").suspend()
    assert route.called
    assert c.is_suspended
    assert c.suspended_at == "2026-08-14T09:00:00Z"


@respx.mock
def test_a_running_computer_is_not_suspended(client: mc.Client) -> None:
    c = _computer(client)
    assert not c.is_suspended
    assert c.suspended_at == ""


@respx.mock
def test_waiting_for_a_suspended_computer_says_so_rather_than_timing_out(
    client: mc.Client,
) -> None:
    # It will not become "running" on its own, and a timeout is the least
    # informative answer available about the one case a caller fixes in a line.
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "status": "suspended"})
    )
    c = client.computers.get("vm-1")
    with pytest.raises(mc.MandalaError, match="suspended"):
        c.wait_until_running(timeout=30)


# --- the connect surface ----------------------------------------------------


@respx.mock
def test_a_single_computer_carries_the_desktop_credentials(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={**COMPUTER, "vnc": VNC}))
    vnc = client.computers.get("vm-1").vnc
    assert vnc is not None
    assert vnc.token == "abc"
    assert vnc.view_token == "view-def"
    assert vnc.embed_url.endswith("token=view-def")


@respx.mock
def test_a_listed_computer_has_no_credentials_until_it_is_refreshed(
    client: mc.Client,
) -> None:
    # The platform keeps them off the list deliberately: a credential in every
    # list response is a credential in every log line that captured one.
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[COMPUTER]))
    (c,) = client.computers.list()
    assert c.vnc is None

    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={**COMPUTER, "vnc": VNC}))
    assert c.refresh().vnc is not None


@respx.mock
def test_half_a_connect_surface_is_no_connect_surface(client: mc.Client) -> None:
    # A URL built over a missing credential is indistinguishable from a working
    # one and answers 401 forever, so anything short of both is None.
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "vnc": {**VNC, "view_token": ""}})
    )
    assert client.computers.get("vm-1").vnc is None


# --- truncated guest output -------------------------------------------------


@respx.mock
def test_exec_reports_output_the_guest_agent_stopped_capturing(client: mc.Client) -> None:
    """16 MiB in, qemu-ga stops capturing and says so.

    Dropped, a command whose output passed the cap comes back short and looks
    complete — a failure with no symptom.
    """
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(
            200,
            json={
                "exit_code": 0,
                "stdout": "half a file",
                "stderr": "",
                "timed_out": False,
                "out_truncated": True,
            },
        )
    )
    res = _computer(client).exec("cat /var/log/big")
    assert res.truncated
    assert res.out_truncated and not res.err_truncated
    # Still a command that succeeded — whether a short answer is acceptable is
    # the caller's call, so `ok` deliberately does not fold this in.
    assert res.ok


@respx.mock
def test_an_ordinary_exec_is_not_truncated(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(
            200,
            json={"exit_code": 0, "stdout": "ok", "stderr": "", "timed_out": False},
        )
    )
    assert not _computer(client).exec("echo ok").truncated


# --- partial listings -------------------------------------------------------


@respx.mock
def test_a_complete_listing_says_it_is_complete(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[COMPUTER]))
    computers = client.computers.list()
    assert computers.is_complete
    assert computers.incomplete is None


@respx.mock
def test_allow_partial_is_opt_in_and_absent_otherwise(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers").mock(httpx.Response(200, json=[COMPUTER]))
    client.computers.list()
    assert "allow_partial" not in route.calls.last.request.url.params
    client.computers.list(allow_partial=True)
    assert route.calls.last.request.url.params["allow_partial"] == "1"


@respx.mock
def test_a_short_listing_says_so_rather_than_reading_as_a_smaller_fleet(
    client: mc.Client,
) -> None:
    """The header is the only thing that distinguishes short from deleted."""
    respx.get(f"{BASE}/computers").mock(
        httpx.Response(200, json=[COMPUTER], headers={"X-GC-Incomplete": "3"})
    )
    computers = client.computers.list(allow_partial=True)
    assert len(computers) == 1
    assert not computers.is_complete
    assert computers.incomplete == 3


def test_listing_transformations_preserve_partial_state() -> None:
    partial = mc.Listing.of([1, 2, 3], incomplete=4)

    for transformed in (partial.copy(), partial[1:], partial + [4]):
        assert isinstance(transformed, mc.Listing)
        assert transformed.incomplete == 4
        assert not transformed.is_complete

    combined = mc.Listing.of([0]) + partial
    assert combined.incomplete == 4
    assert not combined.is_complete

    for transformed in ([0] + partial, partial * 2, 2 * partial, partial * 0):
        assert isinstance(transformed, mc.Listing)
        assert not transformed.is_complete
    assert (partial * 2).incomplete == 8


def test_listing_in_place_growth_merges_partial_state() -> None:
    partial = mc.Listing.of([1, 2], incomplete=3)

    extended = mc.Listing.of([0])
    extended.extend(partial)
    assert extended == [0, 1, 2] and extended.incomplete == 3

    combined = mc.Listing.of([0])
    original = combined
    combined += partial
    assert combined is original and combined.incomplete == 3

    partial.extend(partial)
    assert partial == [1, 2, 1, 2] and partial.incomplete == 6


@respx.mock
def test_a_short_listing_that_cannot_say_by_how_much_is_still_short(
    client: mc.Client,
) -> None:
    """Zero is a real answer and is not the same as complete.

    A computer created during the outage was never cached against the host now
    holding it, so the count is 0 while the list is genuinely short. Branching
    on the number rather than on `is_complete` reads that as "nothing missing".
    """
    respx.get(f"{BASE}/computers").mock(
        httpx.Response(200, json=[], headers={"X-GC-Incomplete": "0"})
    )
    computers = client.computers.list(allow_partial=True)
    assert computers.incomplete == 0
    assert not computers.is_complete


@respx.mock
def test_a_listing_that_would_be_short_is_refused_by_default(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers").mock(
        httpx.Response(503, json={"error": "a hypervisor cannot be reached"})
    )
    with pytest.raises(mc.UnavailableError) as e:
        client.computers.list()
    assert e.value.status == 503
    # Its own class, and not a conflict: nothing here clears by retrying the
    # same request differently, but passing allow_partial does change it.
    assert not isinstance(e.value, mc.ConflictError)


@respx.mock
def test_unreachable_rows_are_marked_rather_than_believed(client: mc.Client) -> None:
    """A cached row carries an id and nothing else — including no status."""
    respx.get(f"{BASE}/computers").mock(
        httpx.Response(
            200,
            json=[COMPUTER, {"id": "vm-2", "unreachable": True}],
            headers={"X-GC-Incomplete": "1"},
        )
    )
    listed, missing = client.computers.list(allow_partial=True)
    assert not listed.unreachable
    assert missing.unreachable
    assert missing.status == ""


@respx.mock
def test_snapshot_listing_carries_include_and_partial(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/snapshots").mock(httpx.Response(200, json=[]))
    client.snapshots.list()
    assert not route.calls.last.request.url.params
    client.snapshots.list(include_unfinished=True, allow_partial=True)
    params = route.calls.last.request.url.params
    assert params["include"] == "unfinished" and params["allow_partial"] == "1"


@respx.mock
def test_an_orphaned_snapshot_says_which_operation_still_works(client: mc.Client) -> None:
    respx.get(f"{BASE}/snapshots").mock(
        httpx.Response(
            200,
            json=[
                {
                    "id": "snap-1",
                    "computer_id": "vm-gone",
                    "orphaned": True,
                    "computer_name": "was-dev",
                },
            ],
        )
    )
    (s,) = client.snapshots.list()
    assert s.orphaned
    assert s.computer_name == "was-dev"


@respx.mock
def test_a_snapshot_carries_the_shape_it_would_clone_back_as(client: mc.Client) -> None:
    """The sizing on a snapshot is the capture's, not the source computer's now.

    Which is the whole reason to read it before cloning: a computer resized
    after the capture clones back to what it was.
    """
    respx.get(f"{BASE}/snapshots").mock(
        httpx.Response(
            200,
            json=[
                {
                    "id": "snap-1",
                    "computer_id": "vm-1",
                    "os": "linux",
                    "template": "base",
                    "cpu": 2,
                    "ram_mb": 4096,
                    "disk_gb": 40,
                    "resolution": "1920x1080x24",
                },
            ],
        )
    )
    (snap,) = client.snapshots.list()
    assert (snap.os, snap.template) == ("linux", "base")
    assert (snap.cpu, snap.ram_mb, snap.disk_gb) == (2, 4096, 40)
    assert snap.resolution == "1920x1080x24"


@respx.mock
def test_a_snapshot_without_a_shape_reads_empty_rather_than_raising(client: mc.Client) -> None:
    """An unreachable stub carries an id and nothing else, this included."""
    respx.get(f"{BASE}/snapshots").mock(
        httpx.Response(200, json=[{"id": "snap-1", "unreachable": True}])
    )
    (snap,) = client.snapshots.list()
    assert snap.unreachable
    assert (snap.os, snap.cpu, snap.resolution) == ("", 0, "")


@respx.mock
def test_a_computer_carries_its_workspace_and_its_schedule(client: mc.Client) -> None:
    """Both readable off a computer already in hand, without a second call."""
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(
            200,
            json={
                **COMPUTER,
                "workspace_id": "ws-7",
                "snapshot_schedule": {"enabled": True, "hour": 3, "minute": 30, "tz": "UTC"},
            },
        )
    )
    c = client.computers.get("vm-1")
    assert c.workspace_id == "ws-7"
    assert c.snapshot_schedule == {"enabled": True, "hour": 3, "minute": 30, "tz": "UTC"}


@respx.mock
def test_a_computer_with_neither_says_so_rather_than_inventing_one(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    c = client.computers.get("vm-1")
    assert c.workspace_id == ""
    # None, not an empty mapping: "no schedule" and "a schedule that is off"
    # are different answers, and set_schedule(enabled=False) is the second.
    assert c.snapshot_schedule is None


@respx.mock
def test_a_null_workspace_is_the_documented_empty_string(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "workspace_id": None})
    )
    assert client.computers.get("vm-1").workspace_id == ""


@respx.mock
def test_a_computers_snapshots_keep_the_rows_nobody_could_read(client: mc.Client) -> None:
    """Filtering by computer must not delete the markers saying it is short.

    An unreachable stub has no computer_id — there was no daemon to say which
    computer it belongs to — so an equality filter drops precisely the rows that
    say the answer is incomplete, and then reports a confident count.
    """
    respx.get(f"{BASE}/snapshots").mock(
        httpx.Response(
            200,
            json=[
                {"id": "snap-1", "computer_id": "vm-1"},
                {"id": "snap-2", "computer_id": "vm-other"},
                {"id": "snap-3", "unreachable": True},
            ],
            headers={"X-GC-Incomplete": "1"},
        )
    )
    snapshots = _computer(client).snapshots(allow_partial=True)
    assert [s.id for s in snapshots] == ["snap-1", "snap-3"]
    assert not snapshots.is_complete


# --- snapshot holdings and the purge interlock ------------------------------


@respx.mock
def test_holdings_is_a_summary_and_not_a_listing(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/snapshots").mock(
        httpx.Response(200, json={"count": 2, "size_bytes": 6_100_000_000, "fingerprint": "fp-abc"})
    )
    held = _computer(client).snapshot_holdings()
    assert (held.count, held.size_bytes, held.fingerprint) == (2, 6_100_000_000, "fp-abc")


@respx.mock
def test_an_ordinary_delete_sends_no_purge_query(client: mc.Client) -> None:
    route = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    _computer(client).delete()
    assert not route.calls.last.request.url.params


@respx.mock
def test_a_purge_is_bound_to_the_fingerprint_it_was_given(client: mc.Client) -> None:
    route = respx.delete(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={"ok": True, "snapshots_deleted": 2})
    )
    deleted = _computer(client).delete(purge_snapshots=True, expect="fp-abc")
    params = route.calls.last.request.url.params
    assert params["snapshots"] == "delete"
    assert params["expect"] == "fp-abc"
    assert deleted == 2


@respx.mock
def test_a_purge_without_a_fingerprint_deletes_nothing(client: mc.Client) -> None:
    """Refused here, before the round trip. The platform would accept it.

    An unguarded purge is bound to whatever the set is at the moment it fires
    rather than to what anybody agreed to destroy — the race being that a
    capture finishes between the decision and the call.
    """
    route = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    c = _computer(client)
    with pytest.raises(ValueError, match="snapshot_holdings"):
        c.delete(purge_snapshots=True)
    assert not route.called


@respx.mock
def test_a_stale_fingerprint_is_not_smuggled_onto_an_ordinary_delete(
    client: mc.Client,
) -> None:
    """It would refuse the delete for a reason that has nothing to do with it."""
    route = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    _computer(client).delete(expect="fp-stale")
    assert "expect" not in route.calls.last.request.url.params


@respx.mock
def test_a_quiet_server_is_not_reported_as_nothing_destroyed(client: mc.Client) -> None:
    """None, not 0. This is the one irreversible call on the object."""
    respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"ok": True}))
    assert _computer(client).delete(purge_snapshots=True, expect="fp-abc") is None


@respx.mock
def test_a_malformed_delete_count_is_an_sdk_error(client: mc.Client) -> None:
    respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={"snapshots_deleted": []}))
    with pytest.raises(mc.MandalaError, match="invalid snapshots_deleted"):
        _computer(client).delete()


# --- background exec --------------------------------------------------------


@respx.mock
def test_a_background_exec_returns_a_handle_and_no_deadline(client: mc.Client) -> None:
    """The wire field timeout_s is omitted rather than sent and ignored.

    The server does ignore it — not waiting is the whole request — but a payload
    carrying a deadline that means nothing is one somebody later reads as a
    promise the platform never made.
    """
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"pid": 4242, "command": "make", "running": True})
    )
    job = _computer(client).start_exec("make", cwd="/src")
    body = json.loads(route.calls.last.request.content)
    assert body == {"command": "make", "background": True, "cwd": "/src"}
    assert (job.pid, job.command) == (4242, "make")


@respx.mock
def test_a_background_exec_requires_a_positive_pid(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json={}))
    with pytest.raises(mc.MandalaError, match="positive pid"):
        _computer(client).start_exec("make")


@respx.mock
def test_a_foreground_exec_still_carries_its_deadline(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    _computer(client).exec("true", timeout=5, env={"CI": "1"})
    body = json.loads(route.calls.last.request.content)
    assert body == {"command": "true", "timeout_s": 5, "env": {"CI": "1"}}


def test_a_foreground_exec_needs_a_positive_deadline(client: mc.Client) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        mc.Computer(client._t, COMPUTER).exec("true", timeout=0)


def test_a_relative_cwd_is_refused_before_the_request() -> None:
    with pytest.raises(ValueError, match="cwd must be absolute"):
        mc._api.exec_body("make", 30, cwd="src")
    with pytest.raises(ValueError, match="cwd must be absolute"):
        mc._api.exec_body("make", 30, cwd=r"..\build")


def test_a_windows_guest_path_is_absolute_too() -> None:
    r"""The check does not know the guest's OS, so it cannot insist on a slash.

    A leading `/` is drive-relative on Windows, which the daemon refuses; the
    forms it accepts there are `C:\...` and a `\\` UNC share. Insisting on a
    slash here would leave `cwd` and the file transfers unusable on a Windows
    guest, refusing locally exactly the values the server wants.
    """
    assert mc._api.exec_body("dir", 30, cwd=r"C:\build")["cwd"] == r"C:\build"
    assert mc._api.exec_body("dir", 30, cwd="C:/build")["cwd"] == "C:/build"
    assert mc._api.files_params(r"\\share\out.zip") == {"path": r"\\share\out.zip"}
    # One backslash is drive-relative, not a UNC share, and the daemon refuses
    # it with the rest of the relative paths.
    with pytest.raises(ValueError, match="must be absolute"):
        mc._api.files_params(r"\share\out.zip")
    with pytest.raises(ValueError, match="must be absolute"):
        mc._api.files_params("C:")


@respx.mock
def test_polling_reads_the_new_output_and_whether_more_is_waiting(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json={"pid": 4242}))
    respx.get(f"{BASE}/computers/vm-1/exec/4242").mock(
        httpx.Response(
            200,
            json={
                "pid": 4242,
                "running": True,
                "stdout": "compiling\n",
                "stdout_offset": 10,
                "more": True,
            },
        )
    )
    status = _computer(client).start_exec("make").poll()
    assert status.stdout == "compiling\n"
    assert status.more and not status.done
    # Absent rather than 0 until it has exited: 0 is the one value that reads as
    # success to anything not checking `done` first.
    assert status.exit_code is None


@respx.mock
def test_a_finished_command_reports_its_exit_code(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json={"pid": 4242}))
    respx.get(f"{BASE}/computers/vm-1/exec/4242").mock(
        httpx.Response(200, json={"pid": 4242, "running": False, "exited": True, "exit_code": 2})
    )
    status = _computer(client).start_exec("make").poll()
    assert status.done and status.exit_code == 2


@respx.mock
def test_killing_answers_with_the_tail_nobody_had_read(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(200, json={"pid": 4242}))
    route = respx.delete(f"{BASE}/computers/vm-1/exec/4242").mock(
        httpx.Response(200, json={"pid": 4242, "killed": True, "stdout": "last line\n"})
    )
    status = _computer(client).start_exec("tail -f /var/log/syslog").kill()
    assert route.called
    assert status.killed and status.stdout == "last line\n"


@respx.mock
def test_a_pid_from_a_previous_session_needs_no_request_to_rebuild(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/exec/99").mock(
        httpx.Response(200, json={"pid": 99, "running": True})
    )
    job = _computer(client).background_command(99)
    assert not route.called  # nothing is verified until it is polled
    assert job.poll().pid == 99


# --- windows ----------------------------------------------------------------


@respx.mock
def test_listing_windows_reads_the_named_array(client: mc.Client) -> None:
    """`{"windows": []}`, not a bare array — an empty desktop says so."""
    respx.get(f"{BASE}/computers/vm-1/windows").mock(
        httpx.Response(
            200,
            json={
                "windows": [
                    {
                        "id": "0x2600003",
                        "title": "Example — Mozilla Firefox",
                        "class": "Navigator",
                        "type": "normal",
                        "x": 10,
                        "y": 20,
                        "width": 1200,
                        "height": 700,
                        "focused": True,
                    }
                ]
            },
        )
    )
    (w,) = _computer(client).windows()
    # The class is the application and is stable; the title is whatever page it
    # happens to be showing, which is why matching goes on the class.
    assert w.wm_class == "Navigator"
    assert w.title.endswith("Firefox")
    assert (w.x, w.y, w.width, w.height) == (10, 20, 1200, 700)
    assert w.focused


@respx.mock
def test_the_desktops_own_furniture_is_left_out_unless_asked_for(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/windows").mock(
        httpx.Response(200, json={"windows": []})
    )
    c = _computer(client)
    c.windows()
    assert "include" not in route.calls.last.request.url.params
    c.windows(include_all=True)
    assert route.calls.last.request.url.params["include"] == "all"


@respx.mock
@pytest.mark.parametrize("rows", [{}, ["not a window"]])
def test_windows_refuses_non_object_rows(client: mc.Client, rows: object) -> None:
    respx.get(f"{BASE}/computers/vm-1/windows").mock(httpx.Response(200, json={"windows": rows}))
    with pytest.raises(mc.MandalaError, match="array of objects"):
        _computer(client).windows()


@respx.mock
def test_a_window_action_answers_with_the_window_as_it_now_is(client: mc.Client) -> None:
    """The window manager places the frame; a move to 300,200 lands where it lands."""
    respx.post(f"{BASE}/computers/vm-1/windows/0x2600003").mock(
        httpx.Response(
            200,
            json={"ok": True, "window": {"id": "0x2600003", "x": 305, "y": 229}, "gone": False},
        )
    )
    res = _computer(client).window_action("0x2600003", "move", x=300, y=200)
    assert res.window is not None
    assert (res.window.x, res.window.y) == (305, 229)
    assert not res.gone


@respx.mock
def test_a_closed_window_is_gone_rather_than_undescribable(client: mc.Client) -> None:
    """Both outcomes have no window; `gone` is what separates them."""
    respx.post(f"{BASE}/computers/vm-1/windows/0x2600003").mock(
        httpx.Response(200, json={"ok": True, "window": None, "gone": True})
    )
    res = _computer(client).window_action("0x2600003", "close")
    assert res.window is None
    assert res.gone


def test_a_window_id_cannot_add_a_path_segment() -> None:
    """Unescaped, a "/" in a window id would land the request on another route.

    Unlike a computer or a snapshot id, this one is minted by the guest's window
    manager rather than by the platform, so it is not from a known alphabet.
    """
    assert mc._api.window("vm-1", "0x2600003") == "computers/vm-1/windows/0x2600003"
    assert mc._api.window("vm-1", "a/b") == "computers/vm-1/windows/a%2Fb"


def test_a_typo_in_a_window_action_names_the_set() -> None:
    with pytest.raises(ValueError, match="action must be one of"):
        mc._api.window_body("focuss")


def test_half_a_window_geometry_is_refused_rather_than_zeroed() -> None:
    """The same reasoning as half a drag origin: it succeeds in the wrong place."""
    with pytest.raises(ValueError, match="both x and y"):
        mc._api.window_body("move", x=300)
    with pytest.raises(ValueError, match="both width and height"):
        mc._api.window_body("resize", width=800)


def test_window_actions_require_only_the_geometry_they_use() -> None:
    with pytest.raises(ValueError, match="move needs"):
        mc._api.window_body("move")
    with pytest.raises(ValueError, match="resize needs"):
        mc._api.window_body("resize")
    with pytest.raises(ValueError, match="only valid for move"):
        mc._api.window_body("focus", x=1, y=2)
    with pytest.raises(ValueError, match="only valid for resize"):
        mc._api.window_body("close", width=10, height=10)


# --- resize and the idle window ---------------------------------------------


@respx.mock
def test_a_resize_sends_only_the_sizing_group(client: mc.Client) -> None:
    """The platform refuses a resize combined with a rename, and is right to.

    A resize needs the computer stopped and a rename does not, so one request
    cannot honour both without applying half of it.
    """
    route = respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "cpu": 4, "ram_mb": 8192})
    )
    c = _computer(client).resize(cpu=4, ram_mb=8192)
    assert json.loads(route.calls.last.request.content) == {"cpu": 4, "ram_mb": 8192}
    assert (c.cpu, c.ram_mb) == (4, 8192)


def test_a_resize_that_changes_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one of cpu"):
        mc._api.resize_body(cpu=None, ram_mb=None, disk_gb=None)


@respx.mock
def test_the_idle_window_is_read_back_and_cleared_with_null(client: mc.Client) -> None:
    """None is SENT, not omitted — dropped it would mean "change nothing"."""
    route = respx.patch(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "idle_suspend_min": 15})
    )
    c = _computer(client).set_idle_suspend(15)
    assert json.loads(route.calls.last.request.content) == {"idle_suspend_min": 15}
    assert c.idle_suspend_min == 15

    route.mock(httpx.Response(200, json=COMPUTER))
    c.set_idle_suspend(None)
    assert json.loads(route.calls.last.request.content) == {"idle_suspend_min": None}
    # Absent, not zero: the host's own sweep is a property of the host and is
    # deliberately not reported in its place.
    assert c.idle_suspend_min is None


def test_zero_pins_a_computer_against_the_idle_sweep() -> None:
    """0 is the platform's "never suspend", not a malformed duration.

    It is what a long job started inside the guest needs — nothing reaches the
    host from outside while it runs, so it is idle by every measure the host can
    take. Refusing 0 here would leave no way to say it, and the obvious
    substitute, None, asks for the opposite: follow the host's own window.
    """
    assert mc._api.idle_suspend_body(0) == {"idle_suspend_min": 0}
    with pytest.raises(ValueError, match="cannot be negative"):
        mc._api.idle_suspend_body(-1)


# --- ids are not trusted to be shaped like ids -----------------------------


def test_ids_are_percent_encoded_into_the_path() -> None:
    """A path segment stays one segment, whatever the caller passed.

    Real ids are platform-minted hex, but `get()` takes any string, and the
    damage from one that is not is not a 404: `..` re-points the request at a
    route nobody meant, with the account's bearer token on it, and `?` puts
    query keys on a request whose own parameters carry interlocks.
    """
    assert mc._api.computer("../../admin") == "computers/..%2F..%2Fadmin"
    assert mc._api.snapshot("../computers") == "snapshots/..%2Fcomputers"
    assert mc._api.computer_action("a/b", "start") == "computers/a%2Fb/start"
    assert mc._api.snapshot_action("a?x=1", "restore") == "snapshots/a%3Fx%3D1/restore"
    assert mc._api.files("a b") == "computers/a%20b/files"
    assert mc._api.exec_handle("a/b", 7) == "computers/a%2Fb/exec/7"
    assert mc._api.window("a/b", "0x1/2") == "computers/a%2Fb/windows/0x1%2F2"
    # The ordinary case is untouched, which is why this was invisible.
    assert mc._api.computer("vm-1a2b3c4d5e6f") == "computers/vm-1a2b3c4d5e6f"


def test_a_dot_only_id_is_refused_rather_than_encoded() -> None:
    """Encoding cannot save `..` — only refusing it can.

    `quote` leaves a dot alone whatever `safe` says, and the dot-segment
    removal in RFC 3986 is applied by the client to the assembled URL. So
    `get("..")` addressed /api/v1 itself, and a computer whose id was `..`
    turned that computer's /snapshots into the account's whole snapshot list —
    which is a bad thing to hand a purge loop. An empty id does the same to the
    collection route, and then builds a Computer over a list payload.
    """
    for bad in ("", ".", "..", "...."):
        with pytest.raises(ValueError, match="empty or all dots"):
            mc._api.computer(bad)
        with pytest.raises(ValueError, match="empty or all dots"):
            mc._api.snapshot(bad)
    # A dot inside a real id is an ordinary character and stays one.
    assert mc._api.computer("vm-1.2") == "computers/vm-1.2"


@respx.mock
def test_a_dot_only_id_never_reaches_the_wire(client: mc.Client) -> None:
    route = respx.get(url__startswith=BASE).mock(httpx.Response(200, json=COMPUTER))
    for bad in ("..", ""):
        with pytest.raises(ValueError):
            client.computers.get(bad)
    assert not route.called, "the request that must not happen is the whole point"


@respx.mock
def test_a_hostile_id_cannot_escape_the_api_prefix(client: mc.Client) -> None:
    route = respx.get(url__startswith=BASE).mock(httpx.Response(200, json=COMPUTER))
    client.computers.get("../../admin")
    # raw_path, not path: `path` is the decoded view, and what matters is the
    # bytes on the wire. Unencoded, httpx resolves the dot segments away and
    # this lands on /api/admin — off the versioned surface, bearer token on it.
    assert route.calls.last.request.url.raw_path == b"/api/v1/computers/..%2F..%2Fadmin"


# --- half a coordinate is a mistake, not a default -------------------------


def test_half_a_coordinate_is_refused_rather_than_zero_filled() -> None:
    """`click(100)` used to click (100, 0) and report success.

    Both coordinates or neither: neither means "wherever the pointer is", which
    is a real request. One of the two is a caller who meant to name a point, and
    zero-filling the other half acts somewhere else entirely with nothing said.
    It is the case `drag_body` already refuses for an origin.
    """
    for x, y in ((5, None), (None, 5), (0, None), (None, 0)):
        with pytest.raises(ValueError, match="both x and y"):
            mc._api.click_body("left_click", x, y, ())
        with pytest.raises(ValueError, match="both x and y"):
            mc._api.button_body("left_mouse_down", x, y)
        with pytest.raises(ValueError, match="both x and y"):
            mc._api.scroll_body(x, y, "down", 3)

    # Neither and both still work, and 0 is still a real coordinate.
    assert mc._api.click_body("left_click", None, None, ()) == {"action": "left_click"}
    assert mc._api.click_body("left_click", 0, 0, ())["x"] == 0
    assert mc._api.scroll_body(0, 0, "down", 3)["coordinate"] == [0, 0]


@respx.mock
def test_a_half_coordinate_click_never_reaches_the_wire(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    with pytest.raises(ValueError, match="both x and y"):
        _computer(client).click(100)
    assert not route.calls


@respx.mock
def test_strings_and_booleans_are_not_pointer_coordinates(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/input").mock(httpx.Response(200, json={"ok": True}))
    computer = _computer(client)
    with pytest.raises(TypeError, match="y must be an integer coordinate"):
        computer.click(100, "shift")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="y must be an integer coordinate"):
        computer.scroll(1, "down")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="x must be an integer coordinate"):
        computer.move(True, 20)
    with pytest.raises(TypeError, match="to_x must be an integer coordinate"):
        computer.drag(False, 20)
    assert not route.calls


# --- credentials stay out of the repr --------------------------------------


def test_the_vnc_repr_does_not_carry_the_credentials() -> None:
    """`token` is root-equivalent on that machine and does not expire.

    It lasts until the computer restarts or somebody rotates it, so one log line
    or traceback rendering this object hands over the desktop for as long as the
    machine runs. What a repr is for survives: which URLs are set, and where.
    """
    vnc = mc.VncConnect.from_api(
        {
            "url": "wss://h/vnc?token=SECRET",
            "view_url": "wss://h/vnc?token=VIEWONLY",
            "token": "SECRET",
            "view_token": "VIEWONLY",
            "embed_url": "https://h/embed#token=SECRET",
            "terminal_url": "wss://h/term?token=SECRET",
        }
    )
    assert vnc is not None
    text = repr(vnc)
    assert "SECRET" not in text
    assert "VIEWONLY" not in text
    assert "wss://h/vnc?<redacted>" in text
    assert "https://h/embed?<redacted>" in text
    # The values themselves are untouched — only the repr is lossy.
    assert vnc.token == "SECRET"
    assert vnc.raw["token"] == "SECRET"


def test_the_vnc_repr_survives_an_empty_terminal_url() -> None:
    vnc = mc.VncConnect.from_api({"url": "wss://h/v", "token": "a", "view_token": "b"})
    assert vnc is not None
    assert "terminal_url=''" in repr(vnc)


# --- exec results carry what they were built from --------------------------


def test_exec_result_keeps_the_raw_payload() -> None:
    """The forward-compatibility promise the other models keep.

    A server that starts returning more should not need a new SDK for the
    caller to reach it, and `exec` is the most-used route on the surface.
    """
    result = mc.ExecResult.from_api(
        {"exit_code": 0, "stdout": "hi", "stderr": "", "duration_ms": 12}
    )
    assert result.raw["duration_ms"] == 12
    assert result.stdout == "hi"


def test_exec_result_preserves_a_null_exit_code() -> None:
    result = mc.ExecResult.from_api(
        {"exit_code": None, "stdout": "partial", "stderr": "", "timed_out": True}
    )
    assert result.exit_code is None
    assert not result.ok


def test_null_model_fields_are_normalized_without_losing_the_row() -> None:
    template = mc.Template.from_api(
        {"name": None, "label": None, "os": None, "cpu": None, "ram_mb": None, "disk_gb": None}
    )
    size = mc.Size.from_api(
        {"id": None, "label": None, "template": None, "cpu": None, "ram_mb": None, "disk_gb": None}
    )
    snapshot = mc.Snapshot.from_api(
        {
            "id": None,
            "computer_id": None,
            "name": None,
            "state": None,
            "size_bytes": None,
            "created_at": None,
            "cpu": None,
            "ram_mb": None,
            "disk_gb": None,
        }
    )
    holdings = mc.SnapshotHoldings.from_api(
        {"count": None, "size_bytes": None, "fingerprint": None}
    )
    window = mc.Window.from_api(
        {"id": None, "title": None, "class": None, "type": None, "x": None, "y": None}
    )
    status = mc.ExecStatus.from_api(
        {"pid": None, "command": None, "stdout_offset": None, "stderr_offset": None}
    )

    assert (template.name, template.cpu, template.ram_mb, template.disk_gb) == ("", 0, 0, 0)
    assert (size.id, size.label, size.cpu, size.ram_mb, size.disk_gb) == ("", "", 0, 0, 0)
    assert (snapshot.id, snapshot.state, snapshot.size_bytes, snapshot.cpu) == ("", "", 0, 0)
    assert (holdings.count, holdings.size_bytes, holdings.fingerprint) == (0, 0, "")
    assert (window.id, window.title, window.x, window.y) == ("", "", 0, 0)
    assert (status.pid, status.command, status.stdout_offset, status.stderr_offset) == (0, "", 0, 0)


def test_null_computer_string_fields_are_empty() -> None:
    computer = mc.Computer(
        None,
        {"id": None, "name": None, "status": None, "os": None, "template": None},
    )  # type: ignore[arg-type]
    assert (computer.id, computer.name, computer.status, computer.os, computer.template) == (
        "",
        "",
        "",
        "",
        "",
    )


@pytest.mark.parametrize("model", [mc.ExecResult, mc.ExecStatus])
def test_a_boolean_exit_code_is_rejected(model: type[mc.ExecResult | mc.ExecStatus]) -> None:
    with pytest.raises(TypeError, match="exit_code.+boolean"):
        model.from_api({"exit_code": False})


# --- 429 is its own answer -------------------------------------------------


@respx.mock
def test_rate_limit_is_its_own_error_carrying_the_wait(client: mc.Client) -> None:
    """Every route on this surface is metered, so 429 is reachable everywhere.

    It is the one refusal that says exactly how long to wait, which is no use to
    a caller who cannot tell it apart from any other failure.
    """
    respx.get(f"{BASE}/computers").mock(
        httpx.Response(429, headers={"Retry-After": "30"}, json={"error": "slow down"})
    )
    with pytest.raises(mc.RateLimitError) as caught:
        client.computers.list()
    assert caught.value.retry_after == 30.0
    assert caught.value.status == 429
    assert str(caught.value) == "slow down"


@respx.mock
def test_a_rate_limit_without_a_usable_header_still_classifies(client: mc.Client) -> None:
    # The HTTP-date form is legal and this surface does not send it; guessing at
    # it against a clock that may disagree is worse than saying nothing.
    respx.get(f"{BASE}/computers").mock(
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    )
    with pytest.raises(mc.RateLimitError) as caught:
        client.computers.list()
    assert caught.value.retry_after is None


# --- waiting for a guest waits only on things that can change --------------


@respx.mock
def test_wait_for_guest_does_not_wait_out_a_revoked_key(client: mc.Client) -> None:
    """401 will not clear by waiting.

    It used to cost the full 180-second timeout and then report "the guest did
    not respond", which is both wrong and the least useful thing this method
    could say about a revoked key.
    """
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(401, json={"error": "revoked"}))
    with pytest.raises(mc.AuthenticationError):
        _computer(client).wait_for_guest(timeout=30, poll=0.01)


@respx.mock
def test_wait_for_guest_still_waits_through_a_booting_agent(client: mc.Client) -> None:
    """409 is what the agent answers with in the first seconds of a start."""
    route = respx.post(f"{BASE}/computers/vm-1/exec")
    route.side_effect = [
        httpx.Response(409, json={"error": "guest agent not ready"}),
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""}),
    ]
    c = _computer(client)
    assert c.wait_for_guest(timeout=30, poll=0.01) is c
    assert len(route.calls) == 2


@respx.mock
def test_wait_for_guest_preserves_a_rate_limit(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(429, headers={"Retry-After": "12"}, json={"error": "slow down"})
    )
    with pytest.raises(mc.RateLimitError) as caught:
        _computer(client).wait_for_guest(timeout=30, poll=0)
    assert caught.value.retry_after == 12
    assert route.call_count == 1


@respx.mock
def test_wait_for_guest_caps_the_probe_to_its_remaining_budget(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    _computer(client).wait_for_guest(timeout=2, poll=0)
    assert json.loads(route.calls.last.request.content)["timeout_s"] == 2
    assert max(route.calls.last.request.extensions["timeout"].values()) <= 2


# --- ephemeral cleanup does not displace the caller's exception ------------


@respx.mock
def test_a_failing_ephemeral_cleanup_keeps_the_original_error(client: mc.Client) -> None:
    """The block's exception is the news; a failed delete is a footnote.

    It used to be the other way round: a 409 from the delete replaced whatever
    the user's code raised, so `except MyError:` around the block stopped
    firing.
    """
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    respx.delete(f"{BASE}/computers/vm-1").mock(
        httpx.Response(409, json={"error": "a snapshot is being taken"})
    )

    def caller_raises() -> None:
        with client.computers.ephemeral():
            raise ZeroDivisionError("the caller's own bug")

    with (
        pytest.warns(UserWarning, match="still billable") as warning,
        pytest.raises(ZeroDivisionError),
    ):
        caller_raises()
    assert warning[0].filename == __file__


@respx.mock
def test_ephemeral_still_deletes_on_the_ordinary_path(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    route = respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json={}))
    with client.computers.ephemeral() as c:
        assert c.id == "vm-1"
    assert route.called


@respx.mock
def test_a_failing_ephemeral_cleanup_still_raises_on_a_clean_exit(client: mc.Client) -> None:
    """With nothing else propagating, the delete's own error is the news."""
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    respx.delete(f"{BASE}/computers/vm-1").mock(httpx.Response(409, json={"error": "in flight"}))
    with pytest.raises(mc.ConflictError), client.computers.ephemeral():
        pass


@respx.mock
def test_a_cleanup_that_fails_at_the_transport_keeps_the_original_error(
    client: mc.Client,
) -> None:
    """A network blip during teardown must not displace the caller's exception."""
    respx.post(f"{BASE}/computers").mock(httpx.Response(200, json=COMPUTER))
    respx.delete(f"{BASE}/computers/vm-1").mock(side_effect=httpx.ConnectError("reset"))

    def caller_raises() -> None:
        with client.computers.ephemeral():
            raise ZeroDivisionError("the caller's own bug")

    with pytest.warns(UserWarning, match="still billable"), pytest.raises(ZeroDivisionError):
        caller_raises()


def test_exec_result_equality_ignores_the_raw_payload() -> None:
    """An ExecResult is a value: callers assert on one and put them in sets.

    `raw` carries whatever else the server sent, so comparing it made a result
    unequal to the result a caller built by hand, and comparing a dict at all
    made the frozen dataclass unhashable.
    """
    got = mc.ExecResult.from_api(
        {"exit_code": 0, "stdout": "hi", "stderr": "", "timed_out": False, "unknown": 1}
    )
    assert got == mc.ExecResult(0, "hi", "", False)
    assert len({got, mc.ExecResult(0, "hi", "", False)}) == 1
    assert got.raw["unknown"] == 1


# --- request deadlines ------------------------------------------------------


def _budget(route: respx.Route) -> dict[str, float | None]:
    """The timeout httpx was actually handed for the last call on this route."""
    return dict(route.calls.last.request.extensions["timeout"])


def test_widening_a_mixed_timeout_keeps_infinite_halves_and_widens_finite_ones() -> None:
    read_unbounded = httpx.Timeout(connect=1, read=None, write=2, pool=1)
    widened = mc._client._BaseTransport._budget(read_unbounded, 10)
    assert widened.read is None and widened.write == 10

    write_unbounded = httpx.Timeout(connect=1, read=2, write=None, pool=1)
    widened = mc._client._BaseTransport._budget(write_unbounded, 10)
    assert widened.read == 10 and widened.write is None


@respx.mock
def test_a_long_exec_waits_as_long_as_it_asked_to(client: mc.Client) -> None:
    """The deadline the caller names is the one the transport honours.

    The platform stretches its own deadline to match, so a fixed 60-second
    client abandoned a 300-second command at 60 — while the command carried on
    running in the guest with its output and its exit code going nowhere.

    What this does NOT establish is that the command survives that long. A proxy
    in front of the platform gives up at about two minutes whatever was asked
    for; see test_a_proxy_giving_up_is_not_reported_as_a_bare_status.
    """
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    c = _computer(client)

    c.exec("make", timeout=300)
    assert _budget(route)["read"] == 300 + mc._client.DEADLINE_SLACK

    # Under the client's own default it changes nothing. This only ever widens.
    c.exec("true", timeout=5)
    assert _budget(route)["read"] == mc._client.DEFAULT_TIMEOUT


@respx.mock
def test_the_file_routes_get_a_budget_of_their_own(client: mc.Client) -> None:
    """A transfer of up to 64 MiB is not an ordinary control-plane request."""
    get = respx.get(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200, content=b"hi"))
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200, json={}))
    c = _computer(client)

    c.read_file("/tmp/a")
    c.write_file("/tmp/a", b"hi")
    assert _budget(get)["read"] == mc._client.FILE_TIMEOUT
    assert _budget(put)["write"] == mc._client.FILE_TIMEOUT
    assert get.calls.last.request.headers["Accept"] == "application/octet-stream"


@respx.mock
def test_write_file_refuses_an_oversized_body_before_the_request(
    client: mc.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    put = respx.put(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200))
    monkeypatch.setattr(mc._computer, "FILE_SIZE_LIMIT", 2)
    with pytest.raises(ValueError, match="may not exceed"):
        _computer(client).write_file("/tmp/a", "€")
    assert not put.called


def test_the_sdk_timeout_is_also_the_builtin_timeout() -> None:
    error = mc.TimeoutError("gave up")
    assert isinstance(error, mc.MandalaError)
    assert isinstance(error, builtins.TimeoutError)


@respx.mock
def test_read_file_refuses_an_html_success_body(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(httpx.Response(200, html="<html>sign in</html>"))
    with pytest.raises(mc.MandalaError, match="not binary content"):
        _computer(client).read_file("/tmp/a")


# --- ranged reads ----------------------------------------------------------
#
# The platform serves a window of a guest file (OPL-3727), and that is the only
# way anything larger than the 64 MiB one request moves comes off a computer at
# all. What these pin is the half of that a client owns: asking for the right
# window, and believing the answer about which one arrived rather than the
# question that was asked.


def _window(data: bytes, first: int, total: int) -> httpx.Response:
    """A 206, spelled the way the daemon spells one."""
    return httpx.Response(
        206,
        content=data,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Range": f"bytes {first}-{first + len(data) - 1}/{total}",
            "Accept-Ranges": "bytes",
        },
    )


@respx.mock
def test_read_file_part_asks_for_a_window_and_says_where_it_landed(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"abcd", 8, 100))

    part = _computer(client).read_file_part("/tmp/a", offset=8, length=4)

    assert route.calls.last.request.headers["Range"] == "bytes=8-11"
    assert (part.data, part.offset, part.total, part.partial) == (b"abcd", 8, 100, True)
    assert (part.end, part.remaining, part.at_end) == (12, 88, False)


@respx.mock
def test_a_window_with_no_length_asks_for_the_rest_of_the_file(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"xyz", 7, 10))

    part = _computer(client).read_file_part("/tmp/a", offset=7)

    assert route.calls.last.request.headers["Range"] == "bytes=7-"
    assert part.at_end and part.remaining == 0


@respx.mock
def test_a_tail_is_asked_for_by_counting_back_from_the_end(client: mc.Client) -> None:
    """`bytes=-N`, which is the form the platform keeps anchored at the END.

    Worth having its own spelling rather than making a caller work out
    ``size - n``: they would need the size first, and the whole point of the tail
    form is that it reads the last of something without knowing how long it is.
    """
    route = respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"tail", 96, 100))

    part = _computer(client).read_file_part("/var/log/x.log", offset=-4)

    assert route.calls.last.request.headers["Range"] == "bytes=-4"
    assert (part.offset, part.at_end) == (96, True)


def test_a_tail_takes_no_length() -> None:
    """Refused here rather than sent, because the header cannot spell it.

    ``bytes=-N`` is anchored at both ends already. Silently dropping one of the
    two numbers would answer a question nobody asked, and the trim rule makes
    which one you dropped matter.
    """
    with pytest.raises(ValueError, match="tail is spelled by its offset alone"):
        mc._api.files_range(-4096, 512)


def test_a_zero_length_window_is_refused_before_the_request() -> None:
    with pytest.raises(ValueError, match="at least 1 byte"):
        mc._api.files_range(0, 0)


@pytest.mark.parametrize(
    ("offset", "length", "message"),
    [
        (0.5, None, "offset must be an integer"),
        (0, 1.5, "length must be an integer"),
        (False, None, "offset must be an integer"),
        (0, True, "length must be an integer"),
    ],
)
def test_fractional_and_boolean_file_ranges_are_refused(
    offset: int, length: int | None, message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        mc._api.files_range(offset, length)


@respx.mock
def test_a_window_larger_than_one_request_moves_is_sent_anyway(client: mc.Client) -> None:
    """Asking for more than the ceiling is not an error, and must not become one.

    The caller cannot know the ceiling before they ask — that is the premise of
    the whole feature — so the platform trims and says so. A client that refused
    the ask locally would put the unknowable number back in the caller's hands.
    """
    over = mc._client.FILE_SIZE_LIMIT * 4
    route = respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"a" * 64, 0, over))

    part = _computer(client).read_file_part("/tmp/big", offset=0, length=over)

    assert route.calls.last.request.headers["Range"] == f"bytes=0-{over - 1}"
    # Fewer bytes than asked for, on a success. The answer is what to believe.
    assert len(part.data) == 64 and not part.at_end and part.end == 64


@respx.mock
def test_a_file_the_guest_cannot_measure_ignores_the_range(client: mc.Client) -> None:
    """A ``/proc`` entry: no length to report, so no positions and no total.

    The platform answers 200 with the whole thing and ``Accept-Ranges: none``,
    and the status is how a client tells. Reading that as a window at offset zero
    would invent a total the platform declined to promise.
    """
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(
            200,
            content=b"cpu0",
            headers={"Content-Type": "application/octet-stream", "Accept-Ranges": "none"},
        )
    )

    part = _computer(client).read_file_part("/proc/stat", offset=0, length=1024)

    assert (part.data, part.offset, part.total, part.partial) == (b"cpu0", 0, None, False)
    # Everything there was, arrived. There is nothing to page through.
    assert part.at_end and part.remaining is None


@respx.mock
def test_a_partial_answer_with_no_content_range_is_refused(client: mc.Client) -> None:
    """The header is the only thing that says which bytes these are.

    A proxy that drops it leaves a 206 that cannot be read, and every mistake
    available without it is silent: taken for the start of the file, a window
    writes the middle of a download over its beginning and reports success.
    """
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(206, content=b"abc", headers={"Content-Type": "application/octet-stream"})
    )
    with pytest.raises(mc.MandalaError, match="no readable Content-Range"):
        _computer(client).read_file_part("/tmp/a", offset=0, length=3)


@respx.mock
def test_a_window_whose_body_contradicts_its_content_range_is_refused(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(
            206,
            content=b"ab",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-9/100",
            },
        )
    )
    with pytest.raises(mc.MandalaError, match="cannot both be true"):
        _computer(client).read_file_part("/tmp/a", offset=0, length=10)


@respx.mock
def test_a_partial_answer_wider_than_the_requested_range_is_refused(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"x" * 40, 0, 100))

    with pytest.raises(mc.MandalaError, match="outside the requested Range bytes=0-9"):
        _computer(client).read_file_part("/tmp/a", offset=0, length=10)


@respx.mock
def test_a_refused_window_carries_the_files_real_length(client: mc.Client) -> None:
    """416, and the number that makes the next ask possible without guessing."""
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(
            416,
            json={"error": "range not satisfiable"},
            headers={"Content-Range": "bytes */4096"},
        )
    )
    with pytest.raises(mc.RangeNotSatisfiableError) as caught:
        _computer(client).read_file_part("/tmp/a", offset=9999)

    assert caught.value.size == 4096
    assert caught.value.status == 416
    assert isinstance(caught.value, mc.APIError)


@respx.mock
def test_a_refusal_that_names_no_length_says_so_rather_than_zero(client: mc.Client) -> None:
    """``None``, not ``0`` — a zero here would read as "the file is empty"."""
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(416, json={"error": "range not satisfiable"})
    )
    with pytest.raises(mc.RangeNotSatisfiableError) as caught:
        _computer(client).read_file_part("/tmp/a", offset=9999)
    assert caught.value.size is None


@respx.mock
def test_a_whole_file_past_the_ceiling_is_its_own_error(client: mc.Client) -> None:
    """413 has a next move attached, which a bare APIError could not carry."""
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(413, json={"error": "that file is 2000000000 bytes"})
    )
    with pytest.raises(mc.FileTooLargeError) as caught:
        _computer(client).read_file("/tmp/big")
    assert caught.value.status == 413
    assert isinstance(caught.value, mc.APIError)


# --- paging ----------------------------------------------------------------


def _paging(*parts: bytes) -> object:
    """A file served one window at a time, answering from the Range it is sent.

    Written to serve the *requested* offset rather than a fixed script, so a
    download that asks for the wrong window fails these tests instead of walking
    off the end of a list.
    """
    body = b"".join(parts)
    sizes = [len(p) for p in parts]

    def handler(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Range"].removeprefix("bytes=").split("-")[0])
        at = 0
        for n in sizes:
            if at == first:
                return _window(body[first : first + n], first, len(body))
            at += n
        return httpx.Response(
            416, json={"error": "no"}, headers={"Content-Range": f"bytes */{len(body)}"}
        )

    return handler


@respx.mock
def test_download_file_pages_until_the_file_ends(client: mc.Client, tmp_path) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/files").mock(
        side_effect=_paging(b"one", b"two", b"thr", b"ee")
    )
    dst = tmp_path / "out.bin"

    written = _computer(client).download_file("/home/user/out.bin", dst, part_size=3)

    assert written == 11
    assert dst.read_bytes() == b"onetwothree"
    assert [call.request.headers["Range"] for call in route.calls] == [
        "bytes=0-2",
        "bytes=3-5",
        "bytes=6-8",
        "bytes=9-11",
    ]


@respx.mock
def test_download_file_resumes_from_where_the_answer_ended(client: mc.Client, tmp_path) -> None:
    """Not from where the ask would have ended, which is the trimmed case.

    A window past the ceiling comes back short on a 200-shaped success, so a
    loop that advanced by ``part_size`` would skip everything it did not
    receive — a file with holes in it and nothing raised.
    """
    route = respx.get(f"{BASE}/computers/vm-1/files").mock(side_effect=_paging(b"aa", b"bbbb"))
    dst = tmp_path / "out.bin"

    assert _computer(client).download_file("/x", dst, part_size=4) == 6
    assert dst.read_bytes() == b"aabbbb"
    # Asked for four, given two, and asked again from two rather than four.
    assert [call.request.headers["Range"] for call in route.calls] == ["bytes=0-3", "bytes=2-5"]


@respx.mock
def test_an_empty_file_downloads_as_nothing_rather_than_as_a_failure(
    client: mc.Client, tmp_path
) -> None:
    """An empty file has no byte at any position, so it refuses every window."""
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(416, json={"error": "no"}, headers={"Content-Range": "bytes */0"})
    )
    dst = tmp_path / "empty"

    assert _computer(client).download_file("/tmp/empty", dst) == 0
    assert dst.read_bytes() == b""


@respx.mock
def test_a_file_that_shrinks_mid_download_is_not_reported_as_finished(
    client: mc.Client, tmp_path
) -> None:
    """The alternative is a short file on disk and an exit code saying it is fine.

    Only the FIRST window reads a refusal as an empty file. One anywhere else is
    the file having changed underneath, which is news.
    """
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        side_effect=[
            # Ten bytes, says the first answer, so the loop asks for more.
            _window(b"aa", 0, 10),
            # Two, says the file, by the time it is asked again.
            httpx.Response(416, json={"error": "no"}, headers={"Content-Range": "bytes */2"}),
        ]
    )
    dst = tmp_path / "out.bin"

    with pytest.raises(mc.RangeNotSatisfiableError):
        _computer(client).download_file("/x", dst, part_size=2)
    # What did arrive is still there; what is refused is calling it the file.
    assert dst.read_bytes() == b"aa"


@respx.mock
def test_a_refused_download_leaves_the_local_file_alone(client: mc.Client, tmp_path) -> None:
    """The first window is asked for before anything local is opened.

    ``read_file`` never touched the destination when it failed, and a paging
    download that truncated one on the way to a 404 would be a regression worn
    as an improvement.
    """
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(404, json={"error": "no such file"})
    )
    dst = tmp_path / "out.bin"
    dst.write_bytes(b"do not touch")

    with pytest.raises(mc.NotFoundError):
        _computer(client).download_file("/gone", dst)
    assert dst.read_bytes() == b"do not touch"


@respx.mock
def test_a_first_window_from_the_wrong_offset_is_caught_before_the_file_is_opened(
    client: mc.Client, tmp_path
) -> None:
    """Going first does not make a misplaced window any less misplaced.

    And the check has to happen before the destination is opened, not inside it.
    Opening for write is destructive on its own, so a refusal that had already
    truncated somebody's file would be keeping the letter of "nothing was
    written" and none of the point — which a BytesIO destination cannot see.
    """
    respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"ab", 40, 100))
    dst = tmp_path / "out.bin"
    dst.write_bytes(b"do not touch")

    with pytest.raises(mc.MandalaError, match="wrong bytes at the wrong place"):
        _computer(client).download_file("/x", dst, part_size=2)
    assert dst.read_bytes() == b"do not touch"


@respx.mock
def test_a_window_that_ends_before_it_starts_is_refused(client: mc.Client) -> None:
    """`bytes 5-4/100` with an empty body, which the length check alone lets by.

    A window of no bytes ends exactly where it began, so the paging loop asks for
    it again, receives it again, and never stops — and every check it passes on
    the way is satisfied: the offset is the one that was asked for, and zero
    bytes is what a zero-length window should carry. Refusing it in the parse is
    what makes the loop's advance a property of the parse.
    """
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(
            206,
            content=b"",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 5-4/100",
            },
        )
    )
    with pytest.raises(mc.MandalaError, match="ends before it starts"):
        _computer(client).read_file_part("/x", offset=5, length=8)


@respx.mock
def test_a_window_that_extends_past_its_total_is_refused(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/files").mock(
        httpx.Response(
            206,
            content=b"abcdefgh",
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-7/4",
            },
        )
    )
    with pytest.raises(mc.MandalaError, match="does not fit inside a 4-byte file"):
        _computer(client).read_file_part("/x", offset=0, length=8)


@respx.mock
def test_a_file_that_shrinks_inside_the_window_is_caught_by_its_length(
    client: mc.Client,
) -> None:
    """The shrink a 416 cannot catch, because the next window still lands inside.

    The refusal at the end of the file is the loud version. This is the quiet
    one: a file rewritten shorter, whose next window is perfectly satisfiable, so
    every request succeeds and `at_end` arrives on schedule. What comes back is
    two files spliced at whatever offset the change landed on, under a byte count
    that looks entirely reasonable.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Range"].removeprefix("bytes=").split("-")[0])
        if first == 0:
            return _window(b"OLDOLD", 0, 100)
        return _window(b"new", 6, 9)  # rewritten to nine bytes in between

    respx.get(f"{BASE}/computers/vm-1/files").mock(side_effect=handler)
    sink = io.BytesIO()

    with pytest.raises(mc.MandalaError, match="was 100 bytes and is 9"):
        _computer(client).download_file("/x", sink, part_size=6)


@respx.mock
def test_a_file_that_grows_while_it_is_read_is_followed(client: mc.Client) -> None:
    """The other direction, and not an error.

    Appending leaves every window already read exactly where it was, so following
    the new end is still one file. Only shrinking invalidates what has been
    written, which is why the two are not treated alike.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        first = int(request.headers["Range"].removeprefix("bytes=").split("-")[0])
        if first == 0:
            return _window(b"aa", 0, 4)
        return _window(b"bb", first, 6)  # two more bytes arrived in between

    respx.get(f"{BASE}/computers/vm-1/files").mock(side_effect=handler)
    sink = io.BytesIO()

    assert _computer(client).download_file("/x", sink, part_size=2) == 6
    assert sink.getvalue() == b"aabbbb"


@respx.mock
def test_download_file_writes_into_a_handle_and_leaves_it_open(client: mc.Client) -> None:
    """A handle is the caller's; closing it would end a transfer they own."""
    respx.get(f"{BASE}/computers/vm-1/files").mock(side_effect=_paging(b"ab", b"cd"))
    sink = io.BytesIO()

    assert _computer(client).download_file("/x", sink, part_size=2) == 4
    assert not sink.closed
    assert sink.getvalue() == b"abcd"


@respx.mock
def test_download_file_drains_short_writes_before_advancing(client: mc.Client) -> None:
    class ShortSink(io.BytesIO):
        def write(self, data: bytes) -> int:
            return super().write(data[:2])

    route = respx.get(f"{BASE}/computers/vm-1/files").mock(
        side_effect=_paging(b"abcd", b"efgh")
    )
    sink = ShortSink()

    assert _computer(client).download_file("/x", sink, part_size=4) == 8
    assert sink.getvalue() == b"abcdefgh"
    assert [call.request.headers["Range"] for call in route.calls] == ["bytes=0-3", "bytes=4-7"]


@respx.mock
def test_a_window_from_the_wrong_offset_stops_the_download(client: mc.Client) -> None:
    """A hop that ignores the Range serves the same window forever.

    Two failures in one: the bytes would be appended at a position they do not
    belong to, and the loop would never reach an end to stop at. The offset asked
    for is what the answer is held against — nothing inside a single response
    could tell.
    """
    respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"ab", 0, 100))
    sink = io.BytesIO()

    with pytest.raises(mc.MandalaError, match="wrong bytes at the wrong place"):
        _computer(client).download_file("/x", sink, part_size=2)
    # The first window was legitimate and is kept; the repeat is what stopped it.
    assert sink.getvalue() == b"ab"


def test_download_file_refuses_a_part_size_of_nothing(client: mc.Client) -> None:
    with pytest.raises(ValueError, match="at least 1 byte"):
        mc.Computer(client._t, {"id": "vm-1"}).download_file("/x", io.BytesIO(), part_size=0)


@respx.mock
def test_download_file_refuses_a_fractional_part_size_before_request(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"ab", 0, 2))

    with pytest.raises(TypeError, match="length must be an integer"):
        _computer(client).download_file("/x", io.BytesIO(), part_size=1.5)
    assert not route.called


@respx.mock
def test_a_ranged_read_gets_the_file_routes_budget(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/files").mock(_window(b"ab", 0, 2))
    _computer(client).read_file_part("/tmp/a", offset=0, length=2)
    assert _budget(route)["read"] == mc._client.FILE_TIMEOUT
    assert route.calls.last.request.headers["Accept"] == "application/octet-stream"


@respx.mock
def test_a_client_of_your_own_is_never_shortened() -> None:
    """Widening is the only thing this does, so a patient client stays patient."""
    route = respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    patient = mc.Client("gck_test", base_url=BASE, http_client=httpx.Client(timeout=600.0))
    mc.Computer(patient._t, COMPUTER).exec("true", timeout=30)
    assert _budget(route)["read"] == 600.0


@respx.mock
def test_a_transport_timeout_arrives_as_a_mandala_error(client: mc.Client) -> None:
    """``except MandalaError`` is the one handler the README tells callers to write.

    A raw httpx.ReadTimeout walked straight through it, so a call that ran long
    failed in a way nothing in the SDK's own error table described.
    """
    respx.post(f"{BASE}/computers/vm-1/exec").mock(side_effect=httpx.ReadTimeout("too slow"))
    with pytest.raises(mc.TimeoutError, match="did not answer") as caught:
        mc.Computer(client._t, COMPUTER).exec("sleep 999", timeout=100)
    assert isinstance(caught.value, mc.MandalaError)


@respx.mock
@pytest.mark.parametrize(
    ("header", "expected"),
    [("inf", None), ("-inf", None), ("nan", None), ("-5", 0.0), ("2.5", 2.5)],
)
def test_retry_after_survives_only_as_a_usable_delay(
    client: mc.Client, header: str, expected: float | None
) -> None:
    """The value is handed to time.sleep, where inf blocks forever and nan raises.

    Both parse as floats, so guarding on ValueError alone let them through. A
    negative delay is one that has already passed, which is zero.
    """
    respx.get(f"{BASE}/computers").mock(
        httpx.Response(429, headers={"Retry-After": header}, json={"error": "slow down"})
    )
    with pytest.raises(mc.RateLimitError) as caught:
        client.computers.list()
    assert caught.value.retry_after == expected


@respx.mock
def test_set_schedule_reads_its_own_answer(client: mc.Client) -> None:
    """The PUT already returns the schedule as stored.

    Re-reading it with a GET cost a second metered round trip to report a value
    a concurrent change could have altered in between — which would then come
    back looking like what this call stored.
    """
    stored = {"enabled": True, "hour": 4, "minute": 0, "tz": "UTC"}
    put = respx.put(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json=stored))
    get = respx.get(f"{BASE}/computers/vm-1/schedule").mock(
        httpx.Response(200, json={"enabled": False})
    )

    c = mc.Computer(client._t, COMPUTER)
    assert c.set_schedule(enabled=True) == stored
    assert c.snapshot_schedule == stored
    assert (put.call_count, get.call_count) == (1, 0)


@respx.mock
def test_a_proxy_giving_up_is_not_reported_as_a_bare_status(client: mc.Client) -> None:
    """524 is a hop in front of the platform, and says so.

    Measured against app.mandala.computer on 2026-08-20: `sleep 110` with
    timeout=230 returned normally at 110.6s, while `sleep 130` died at 125.2s
    with timeout=300 and at 125.3s with timeout=3600 — a 12x difference in what
    was asked for, and 0.1s in where it ended. Cloudflare content-negotiates that
    error page, so a client asking for JSON (which every request here does) gets
    an EMPTY body, which left `str(e)` reading "HTTP 524" and named nothing a
    caller could act on.
    """
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(524, content=b""))
    with pytest.raises(mc.GatewayTimeoutError) as e:
        _computer(client).exec("sleep 130", timeout=300)
    assert e.value.status == 524
    assert isinstance(e.value, mc.APIError)
    # The three things the bare status did not say: whose ceiling it is, that
    # the command outlived the request, and what to use instead.
    assert "proxy" in str(e.value)
    assert "outlived the request" in str(e.value)
    assert "start_exec()" in str(e.value)


@respx.mock
def test_the_proxys_own_error_page_is_never_shown(client: mc.Client) -> None:
    """An HTML body is discarded rather than truncated into the message.

    Without the override this read as 500 characters of Cloudflare boilerplate —
    the failure mode the empty-body case only looks better than. The override is
    conditional on the platform not having named the failure itself, and an HTML
    page never does, so this is the branch that still substitutes.
    """
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(
            524,
            headers={"content-type": "text/html; charset=UTF-8"},
            text="<!DOCTYPE html><html><body>error code: 524</body></html>",
        )
    )
    with pytest.raises(mc.GatewayTimeoutError) as e:
        _computer(client).exec("sleep 130", timeout=300)
    assert "DOCTYPE" not in str(e.value)


@respx.mock
def test_an_ordinary_gateway_timeout_lands_in_the_same_place(client: mc.Client) -> None:
    """504 and 524 differ only in which hop gave up first."""
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(504, content=b""))
    with pytest.raises(mc.GatewayTimeoutError) as e:
        client.computers.get("vm-1")
    assert e.value.status == 504


@respx.mock
def test_a_platform_that_named_the_failure_keeps_its_own_words(client: mc.Client) -> None:
    """The substitution is for a body that said nothing, not for every 504.

    A gateway status can also come from a hop that speaks this surface's JSON,
    and "upstream unavailable before dispatch" is a more specific true thing
    than the generic message. Overwriting it would be the client replacing the
    platform with a guess.
    """
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(504, json={"error": "upstream unavailable before dispatch"})
    )
    with pytest.raises(mc.GatewayTimeoutError) as e:
        client.computers.get("vm-1")
    assert str(e.value) == "upstream unavailable before dispatch"
    # Still the class that says what a gateway timeout is; only the words differ.
    assert e.value.status == 504


@respx.mock
def test_the_generic_message_does_not_promise_a_command_to_a_read(client: mc.Client) -> None:
    """A GET started no command, so the message must not claim one is running.

    The wording is shared by every route, and the exec advice is hedged
    precisely so that a listing or a read is not told a confident falsehood
    about work it never began.
    """
    respx.get(f"{BASE}/computers").mock(httpx.Response(524, content=b""))
    with pytest.raises(mc.GatewayTimeoutError) as e:
        client.computers.list()
    said = str(e.value)
    assert "Nothing was cancelled" in said
    assert "Most often" in said
    # The sentence that would be false here is not stated unconditionally.
    assert "whatever this request started is still running" not in said


def test_an_in_band_failure_is_not_an_edge_that_gave_up() -> None:
    """A status delivered ON a stream is proof no proxy abandoned it.

    The agent loop reports its own failures as events inside a 200. A 504 from
    there is the platform relaying a downstream timeout, not an edge that
    stopped waiting — and a caller branching on the class to decide whether its
    work survived would be answered wrongly.
    """
    err = mc._client.error_for_status(504, "the agent run failed: model provider timed out")
    assert isinstance(err, mc.APIError)
    assert not isinstance(err, mc.GatewayTimeoutError)
    assert err.status == 504
    # The statuses that do travel this way are unaffected.
    assert isinstance(mc._client.error_for_status(401, "revoked"), mc.AuthenticationError)
    assert isinstance(mc._client.error_for_status(409, "busy"), mc.ConflictError)


@respx.mock
def test_an_origin_never_reached_is_not_one_that_stopped_answering(client: mc.Client) -> None:
    """Opposite implications, which is why they are two classes.

    A 524 means the request arrived and its work carries on; a 522 means it
    never arrived, so nothing was started and nothing outlives anything.
    """
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(522, content=b""))
    with pytest.raises(mc.OriginUnreachableError) as e:
        client.computers.get("vm-1")
    assert e.value.status == 522
    assert not isinstance(e.value, mc.GatewayTimeoutError)
    assert "never sent" in str(e.value)
    assert "clears on its own" in str(e.value)


@respx.mock
def test_a_certificate_that_will_not_agree_is_not_told_to_wait(client: mc.Client) -> None:
    """525 and 526 fail identically on every retry, and get their own class.

    Sharing one with the unreachable statuses is what let a ``wait_*`` helper
    retry a certificate failure: the fatal tuple names classes, so there was no
    way to say "this one, but not its neighbours".
    """
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(526, content=b""))
    with pytest.raises(mc.OriginTLSError) as e:
        client.computers.get("vm-1")
    assert not isinstance(e.value, mc.OriginUnreachableError)
    assert "TLS handshake" in str(e.value)
    assert "report it rather than waiting it out" in str(e.value)


@respx.mock
def test_a_bad_certificate_ends_a_wait_instead_of_running_it_out(client: mc.Client) -> None:
    """The bug the split exists to fix, and it contradicted its own message.

    ``_FATAL_WHILE_WAITING`` names classes. While the TLS statuses shared a
    class with an origin that is merely down, wait_for_guest could not tell them
    apart: it caught every 526 from the probe and from the refresh behind it,
    discarded both, and spent its full 180 seconds before reporting "the guest
    did not respond" — the wrong cause and the wrong class, about a certificate
    whose own error said to report it rather than wait it out.
    """
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(526, content=b""))
    respx.post(f"{BASE}/computers/vm-1/exec").mock(httpx.Response(526, content=b""))
    started = time.monotonic()
    with pytest.raises(mc.OriginTLSError):
        _computer(client).wait_for_guest(timeout=30)
    # Raised on the first probe, not waited out.
    assert time.monotonic() - started < 5


@respx.mock
def test_an_unreachable_origin_says_so_even_when_something_sent_a_body(
    client: mc.Client,
) -> None:
    """No "did the platform name it" guard here, unlike the gateway pair.

    The status itself says the platform was never reached, so a body cannot be
    its account of what happened — there is nothing to defer to.
    """
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(521, json={"error": "web server is down"})
    )
    with pytest.raises(mc.OriginUnreachableError) as e:
        client.computers.get("vm-1")
    assert "never sent" in str(e.value)


def test_an_unreachable_origin_cannot_arrive_on_a_stream() -> None:
    """The in-band split, one step past the gateway case.

    A status delivered on a response that was a 200 contradicts the claim that
    the request never reached the platform, rather than merely failing to
    support it.
    """
    err = mc._client.error_for_status(522, "the agent run failed: upstream gone")
    assert isinstance(err, mc.APIError)
    assert not isinstance(err, mc.OriginUnreachableError)
    assert err.status == 522


@respx.mock
def test_a_520_does_not_claim_the_work_never_happened(client: mc.Client) -> None:
    """520 is the trap in the 52x range, and the costly one to get backwards.

    Cloudflare returns it when the origin DID receive the request and answered
    with something empty, unknown or oversized. Filed with the unreachable
    statuses it inherited their message — "the request never arrived, so nothing
    was started" — about a create that may well have created something.
    """
    respx.post(f"{BASE}/computers").mock(httpx.Response(520, content=b""))
    with pytest.raises(mc.OriginResponseError) as e:
        client.computers.create(template="base")
    assert e.value.status == 520
    # Neither of its neighbours: the answers they give about surviving work are
    # both wrong here, in opposite directions.
    assert not isinstance(e.value, mc.OriginUnreachableError)
    assert not isinstance(e.value, mc.GatewayTimeoutError)
    said = str(e.value)
    assert "never arrived" not in said
    assert "did arrive" in said
    assert "creates something" in said


@respx.mock
def test_a_520_keeps_a_message_the_platform_may_have_written(client: mc.Client) -> None:
    """Guarded where 521-526 are not, and the difference is who could have spoken.

    A 520 is the platform's own answer arriving mangled, so a body that parsed
    as this surface's JSON plausibly is its account. On the unreachable statuses
    it provably cannot be.
    """
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(520, json={"error": "the hypervisor closed the connection"})
    )
    with pytest.raises(mc.OriginResponseError) as e:
        client.computers.get("vm-1")
    assert str(e.value) == "the hypervisor closed the connection"


def test_a_520_cannot_arrive_on_a_stream_either() -> None:
    err = mc._client.error_for_status(520, "the agent run failed: upstream gone")
    assert isinstance(err, mc.APIError)
    assert not isinstance(err, mc.OriginResponseError)


@respx.mock
def test_a_substituted_message_still_says_which_status_it_stands_in_for(
    client: mc.Client,
) -> None:
    """Four classes, eight statuses, and three of them share one sentence.

    Explaining a failure in prose took the number out of ``str(e)``, which the
    bare ``HTTP 522`` at least had: an operator reading a log that captured only
    the message could no longer tell 521 from 523, and support asks for the
    status before anything else.
    """
    for status, cls in (
        (504, mc.GatewayTimeoutError),
        (520, mc.OriginResponseError),
        (522, mc.OriginUnreachableError),
        (526, mc.OriginTLSError),
    ):
        respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(status, content=b""))
        with pytest.raises(cls) as e:
            client.computers.get("vm-1")
        assert str(e.value).endswith(f"(HTTP {status})")


@respx.mock
def test_an_edge_error_page_survives_on_the_exception(client: mc.Client) -> None:
    """Not shown, not discarded.

    A proxy's HTML is the wrong thing to put in front of a caller and the right
    thing to still have when one asks support about it — the Cloudflare Ray ID
    is in that page and nowhere else, and substituting the message dropped it.
    """
    page = "<html><body>error code: 522 Ray ID: 8f2a1c</body></html>"
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(522, headers={"content-type": "text/html"}, text=page)
    )
    with pytest.raises(mc.OriginUnreachableError) as e:
        client.computers.get("vm-1")
    assert "Ray ID" not in str(e.value)
    assert "8f2a1c" in str(e.value.body)


@respx.mock
def test_a_message_the_platform_wrote_is_not_stamped_with_a_status(
    client: mc.Client,
) -> None:
    """The stamp is for our wording, not the platform's.

    Its message is its own sentence and ``e.status`` already carries the number;
    appending one would be this client editing the platform's words.
    """
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(504, json={"error": "upstream unavailable before dispatch"})
    )
    with pytest.raises(mc.GatewayTimeoutError) as e:
        client.computers.get("vm-1")
    assert str(e.value) == "upstream unavailable before dispatch"
