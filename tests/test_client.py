"""Client behaviour against a mocked API."""

from __future__ import annotations

import json
import shlex

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


@respx.mock
def test_screenshot_returns_bytes(client: mc.Client) -> None:
    respx.get(f"{BASE}/computers/vm-1/screenshot").mock(
        httpx.Response(200, content=b"\x89PNG\r\n", headers={"Content-Type": "image/png"})
    )
    assert mc.Computer(client._t, COMPUTER).screenshot().startswith(b"\x89PNG")


@respx.mock
def test_screenshot_width_becomes_query_param(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/computers/vm-1/screenshot").mock(httpx.Response(200, content=b"jpg"))
    mc.Computer(client._t, COMPUTER).screenshot(width=320)
    assert route.calls.last.request.url.params["w"] == "320"


@respx.mock
def test_exec_nonzero_exit_is_returned_not_raised(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        httpx.Response(200, json={"exit_code": 1, "stdout": "", "stderr": "boom", "timed_out": False})
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
def test_wait_for_guest_ignores_errors_while_booting(client: mc.Client) -> None:
    respx.post(f"{BASE}/computers/vm-1/exec").mock(
        side_effect=[
            httpx.Response(400, json={"error": "not running"}),
            httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False}),
        ]
    )
    mc.Computer(client._t, COMPUTER).wait_for_guest(timeout=5, poll=0)


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
                {"id": "s1", "computer_id": "vm-1", "created_at": "2026-07-31T04:00:00Z", "auto": True},
                {"id": "s2", "computer_id": "vm-1", "created_at": "2026-07-30T12:00:00Z", "auto": False},
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
    route = respx.delete(f"{BASE}/computers/vm-1/schedule").mock(
        httpx.Response(200, json=cleared)
    )
    put = respx.put(f"{BASE}/computers/vm-1/schedule").mock(httpx.Response(200, json={}))

    assert mc.Computer(client._t, COMPUTER).clear_schedule() == cleared
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
    """xdg-open and friends are installed and all exit 0 without launching
    anything, so naming the browser is the whole point of this method."""
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
    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "vnc": VNC})
    )
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

    respx.get(f"{BASE}/computers/vm-1").mock(
        httpx.Response(200, json={**COMPUTER, "vnc": VNC})
    )
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
