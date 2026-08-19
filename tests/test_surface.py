"""Pins the SDK to the platform's curated /api/v1 surface.

The platform allowlists routes server-side; anything else 404s. That check lives
there, but a client that calls a route the server does not expose fails at
runtime in a user's hands rather than here. This asserts every request the SDK
can issue lands on an allowlisted route.

Keep ALLOWED in step with V1_ROUTES in `web/lib/surface.ts` in the platform repo.
It mirrors that table in full, including the routes this SDK cannot yet call —
those are named in UNIMPLEMENTED, which is what keeps the distance between the
two visible rather than letting it grow quietly.

A mirror nobody compares is a comment. `scripts/check_surface.py` does the
comparison whenever the platform repo happens to be checked out, and its absence
is how three routes — background exec and the snapshot holdings — landed
upstream and stayed invisible here: the two tests below both stayed green,
because "every call lands on an allowlisted route" is trivially true of a
mirror that never learned the route exists. That script is run from this file,
rather than left as a command in the README, because a check somebody has to
remember is the same hole one step further back.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"

# (method, pattern) with ids replaced by ":id" — mirrors surface.ts V1_ROUTES.
ALLOWED = {
    ("GET", "templates"),
    ("GET", "sizes"),
    ("GET", "computers"),
    ("POST", "computers"),
    ("GET", "computers/:id"),
    ("PATCH", "computers/:id"),
    ("DELETE", "computers/:id"),
    ("POST", "computers/:id/start"),
    ("POST", "computers/:id/stop"),
    ("POST", "computers/:id/suspend"),
    ("POST", "computers/:id/restart"),
    ("POST", "computers/:id/clone"),
    ("GET", "computers/:id/screenshot"),
    ("POST", "computers/:id/input"),
    ("POST", "computers/:id/exec"),
    ("GET", "computers/:id/exec/:pid"),
    ("DELETE", "computers/:id/exec/:pid"),
    ("GET", "computers/:id/windows"),
    ("POST", "computers/:id/windows/:window"),
    ("GET", "snapshots"),
    ("GET", "computers/:id/snapshots"),
    ("POST", "computers/:id/snapshots"),
    ("POST", "snapshots/:id/restore"),
    ("POST", "snapshots/:id/clone"),
    ("DELETE", "snapshots/:id"),
    ("GET", "computers/:id/schedule"),
    ("PUT", "computers/:id/schedule"),
    ("DELETE", "computers/:id/schedule"),
    ("PUT", "computers/:id/files"),
    ("GET", "computers/:id/files"),
    # Reachable, and not yet reached from here — see UNIMPLEMENTED.
    ("POST", "computers/:id/agent"),
    ("POST", "chat/completions"),
}

# Routes the platform exposes that this SDK cannot yet call.
#
# ALLOWED mirrors the platform's table, so without this the two tests below
# would pass forever while the SDK fell further behind: "every call lands on an
# allowlisted route" stays true no matter how few calls there are. Pinning the
# difference makes the gap a number that has to be edited down deliberately, and
# makes a route added upstream show up here as a failing test rather than as a
# feature nobody noticed.
UNIMPLEMENTED = {
    # OPL-3567. One call that drives the computer until the task is done, and
    # the same engine behind an OpenAI-shaped door. Both need SSE.
    ("POST", "computers/:id/agent"),
    ("POST", "chat/completions"),
}

COMPUTER = {"id": "vm-1", "name": "d", "status": "running", "os": "linux", "cpu": 1}
SNAPSHOT = {"id": "snap-1", "computer_id": "vm-1", "name": "s", "kind": "disk", "state": "durable"}
HOLDINGS = {"count": 1, "size_bytes": 2, "fingerprint": "fp-abc"}
WINDOW = {"id": "0x2600003", "title": "T", "class": "Firefox"}
EXEC_STATUS = {"pid": 4242, "running": False, "exited": True, "exit_code": 0}


def pattern_for(path: str) -> str:
    """Reduce a concrete path to its route shape, as the server's proxy does.

    By position and by parent, never by a regex over the raw path, so an id can
    never be mistaken for a route segment — and the two ids that name something
    *inside* a computer get their own placeholders rather than a second ":id".
    Pinned to exactly position 3 for the same reason the platform pins it: a
    bare "parent is windows" rule makes the word a wildcard parent everywhere,
    and a later literal route like "computers/:id/windows/close-all" could then
    never match, being shadowed by the pattern it reduces to.
    """
    parts = [p for p in path.strip("/").split("/") if p]

    def one(i: int, seg: str) -> str:
        if i and parts[i - 1] in ("computers", "snapshots"):
            return ":id"
        if i == 3 and parts[0] == "computers" and parts[2] == "windows":
            return ":window"
        if i == 3 and parts[0] == "computers" and parts[2] == "exec":
            return ":pid"
        return seg

    return "/".join(one(i, seg) for i, seg in enumerate(parts))


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("gck_test", base_url=BASE)


@pytest.fixture
def async_client() -> mc.AsyncClient:
    return mc.AsyncClient("gck_test", base_url=BASE)


def api_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    get = request.method == "GET"
    if path.endswith("/screenshot"):
        return httpx.Response(200, content=b"png")
    if path.endswith("/files"):
        return httpx.Response(200, content=b"bytes")
    if path.endswith("/exec"):
        return httpx.Response(
            200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False, "pid": 4242}
        )
    if "/exec/" in path:
        return httpx.Response(200, json=EXEC_STATUS)
    if path.endswith("/windows"):
        return httpx.Response(200, json={"windows": [WINDOW]})
    if "/windows/" in path:
        return httpx.Response(200, json={"ok": True, "window": WINDOW, "gone": False})
    if path.endswith(("/templates", "/sizes")):
        return httpx.Response(200, json=[])
    # Collections list on GET and return a single object on POST — getting this
    # backwards is what made the first version of this test fail.
    if path.endswith("/snapshots"):
        # Three shapes on two routes: the account-wide GET lists, a computer's
        # GET answers holdings — a count, a size and a fingerprint, not a
        # listing — and the POST returns the one snapshot it took.
        if not get:
            return httpx.Response(200, json=SNAPSHOT)
        return httpx.Response(200, json=HOLDINGS if "/computers/" in path else [SNAPSHOT])
    if path.endswith("/computers"):
        return httpx.Response(200, json=[COMPUTER] if get else COMPUTER)
    return httpx.Response(200, json=COMPUTER)


def called_routes(calls: object) -> set[tuple[str, str]]:
    return {
        (call.request.method, pattern_for(call.request.url.path.replace("/api/v1", "", 1)))
        for call in calls  # type: ignore[attr-defined]
    }


def exercise_everything(client: mc.Client) -> None:
    """Call every method the SDK exposes that performs a request."""
    client.templates.list()
    client.sizes.list()
    client.computers.list()
    client.computers.get("vm-1")
    c = client.computers.create(template="base")
    c.refresh()
    c.start()
    c.stop()
    c.suspend()
    c.restart()
    c.clone(name="copy")
    c.rename("renamed")
    c.screenshot()
    c.screenshot(width=320)
    c.move(1, 2)
    c.click(1, 2)
    c.right_click(1, 2)
    c.middle_click(1, 2)
    c.double_click(1, 2)
    c.triple_click(1, 2)
    c.click(1, 2, "shift")
    c.click()
    c.drag(9, 9, from_x=1, from_y=2)
    c.mouse_down(1, 2)
    c.mouse_up()
    c.scroll(1, 2, direction="up")
    c.scroll(1, 2, direction="right", modifiers=("shift",))
    c.type("hi")
    c.key("ctrl", "c")
    c.hold_key("shift", seconds=1)
    c.wait(1)
    c.cursor_position()
    c.exec("true")
    c.exec("true", cwd="/tmp", env={"CI": "1"})
    job = c.start_exec("sleep 60")
    job.poll()
    job.kill()
    c.background_command(4242).poll()
    c.windows()
    c.windows(include_all=True)
    c.window_action("0x2600003", "focus")
    c.window_action("0x2600003", "move", x=10, y=20)
    c.resize(cpu=4, ram_mb=8192)
    c.set_idle_suspend(15)
    c.set_idle_suspend(None)
    c.read_file("/home/user/out.txt")
    c.write_file("/home/user/in.txt", b"hello")
    c.snapshot()
    c.snapshot(memory=True, name="before-upgrade")
    c.snapshots()
    c.snapshots(include_unfinished=True, allow_partial=True)
    c.snapshot_holdings()
    c.schedule()
    c.set_schedule(enabled=True, hour=4, tz="UTC")
    c.clear_schedule()
    client.computers.list(allow_partial=True)
    client.snapshots.list()
    client.snapshots.list(include_unfinished=True, allow_partial=True)
    client.snapshots.restore("snap-1")
    client.snapshots.clone("snap-1")
    client.snapshots.delete("snap-1")
    c.delete(purge_snapshots=True, expect="fp-abc")
    c.delete()


async def exercise_everything_async(client: mc.AsyncClient) -> None:
    """The async mirror of exercise_everything."""
    await client.templates.list()
    await client.sizes.list()
    await client.computers.list()
    await client.computers.get("vm-1")
    c = await client.computers.create(template="base")
    await c.refresh()
    await c.start()
    await c.stop()
    await c.suspend()
    await c.restart()
    await c.clone(name="copy")
    await c.rename("renamed")
    await c.screenshot()
    await c.screenshot(width=320)
    await c.move(1, 2)
    await c.click(1, 2)
    await c.right_click(1, 2)
    await c.middle_click(1, 2)
    await c.double_click(1, 2)
    await c.triple_click(1, 2)
    await c.click(1, 2, "shift")
    await c.click()
    await c.drag(9, 9, from_x=1, from_y=2)
    await c.mouse_down(1, 2)
    await c.mouse_up()
    await c.scroll(1, 2, direction="up")
    await c.scroll(1, 2, direction="right", modifiers=("shift",))
    await c.type("hi")
    await c.key("ctrl", "c")
    await c.hold_key("shift", seconds=1)
    await c.wait(1)
    await c.cursor_position()
    await c.exec("true")
    await c.exec("true", cwd="/tmp", env={"CI": "1"})
    job = await c.start_exec("sleep 60")
    await job.poll()
    await job.kill()
    await c.background_command(4242).poll()
    await c.windows()
    await c.windows(include_all=True)
    await c.window_action("0x2600003", "focus")
    await c.window_action("0x2600003", "move", x=10, y=20)
    await c.resize(cpu=4, ram_mb=8192)
    await c.set_idle_suspend(15)
    await c.set_idle_suspend(None)
    await c.read_file("/home/user/out.txt")
    await c.write_file("/home/user/in.txt", b"hello")
    await c.snapshot()
    await c.snapshot(memory=True, name="before-upgrade")
    await c.snapshots()
    await c.snapshots(include_unfinished=True, allow_partial=True)
    await c.snapshot_holdings()
    await c.schedule()
    await c.set_schedule(enabled=True, hour=4, tz="UTC")
    await c.clear_schedule()
    await client.computers.list(allow_partial=True)
    await client.snapshots.list()
    await client.snapshots.list(include_unfinished=True, allow_partial=True)
    await client.snapshots.restore("snap-1")
    await client.snapshots.clone("snap-1")
    await client.snapshots.delete("snap-1")
    await c.delete(purge_snapshots=True, expect="fp-abc")
    await c.delete()


@respx.mock
def test_every_call_lands_on_an_allowlisted_route(client: mc.Client) -> None:
    route = respx.route(host="api.test").mock(side_effect=api_handler)
    exercise_everything(client)

    called = called_routes(route.calls)
    assert called, "no requests were made — the exercise is not exercising anything"
    assert called <= ALLOWED, (
        f"SDK calls routes the server does not expose: {sorted(called - ALLOWED)}"
    )


@respx.mock
async def test_async_every_call_lands_on_an_allowlisted_route(
    async_client: mc.AsyncClient,
) -> None:
    route = respx.route(host="api.test").mock(side_effect=api_handler)
    await exercise_everything_async(async_client)

    called = called_routes(route.calls)
    assert called, "no requests were made — the exercise is not exercising anything"
    assert called <= ALLOWED, (
        f"SDK calls routes the server does not expose: {sorted(called - ALLOWED)}"
    )


@respx.mock
async def test_both_clients_hit_exactly_the_same_routes(
    client: mc.Client, async_client: mc.AsyncClient
) -> None:
    """Same routes, not merely each-inside-the-allowlist.

    Either client silently dropping a call would still satisfy the allowlist
    check above; only comparing them to each other catches that.
    """
    sync_route = respx.route(host="api.test").mock(side_effect=api_handler)
    exercise_everything(client)
    sync_called = called_routes(sync_route.calls)

    respx.reset()
    async_route = respx.route(host="api.test").mock(side_effect=api_handler)
    await exercise_everything_async(async_client)
    async_called = called_routes(async_route.calls)

    assert sync_called == async_called


@respx.mock
def test_the_unreached_part_of_the_surface_is_exactly_what_we_think(
    client: mc.Client,
) -> None:
    """The gap between the platform's surface and this SDK is the pinned one.

    Closing one of these means deleting its line from UNIMPLEMENTED, which is
    the point: the alternative is a set of routes nobody is tracking, on a
    surface whose whole design is that it is enumerable.
    """
    route = respx.route(host="api.test").mock(side_effect=api_handler)
    exercise_everything(client)

    assert ALLOWED - called_routes(route.calls) == UNIMPLEMENTED


def test_pattern_for_treats_ids_as_ids() -> None:
    assert pattern_for("/computers/vm-1/start") == "computers/:id/start"
    assert pattern_for("/snapshots/snap-1/clone") == "snapshots/:id/clone"
    assert pattern_for("/computers/vm-1/snapshots") == "computers/:id/snapshots"
    # A computer whose id looks like a route segment is still an id.
    assert pattern_for("/computers/audit") == "computers/:id"


def test_the_mirror_is_in_step_with_the_platform() -> None:
    """The drift check, run by the suite rather than only by hand.

    `scripts/check_surface.py` is what compares ALLOWED against the platform's
    own V1_ROUTES, and until this test existed nothing ran it — it was a line in
    the README, which is a thing somebody has to remember. That is the exact
    failure it was written for: the routes it would have caught went missing
    because no one thought to look.

    Skipped rather than failed where the platform is not checked out, which is
    the ordinary case on this repository and in its CI. Where it earns its keep
    is on a machine with both — which is every machine this file gets edited on.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import check_surface

    platform = check_surface.platform_repo()
    if platform is None:
        pytest.skip("platform repo not checked out; set MANDALA_PLATFORM_REPO to compare")

    # Compared as sets rather than through main(), so a drift prints as the
    # routes that differ instead of as a non-zero exit code.
    upstream = check_surface.table((platform / check_surface.SURFACE).read_text(), "V1_ROUTES")
    assert upstream == ALLOWED


def test_allowlist_excludes_the_daemons_internal_routes() -> None:
    """The ops and plan-owned endpoints are not tenant API and never should be.

    The previous test proves the SDK stays inside ALLOWED; this proves ALLOWED
    itself stays honest, so widening it later is a deliberate act rather than a
    quiet one. These routes are not owner-scoped in the daemon.
    """
    internal = {"audit", "host", "fleet", "retention"}
    reachable = {pattern.split("/")[0] for _, pattern in ALLOWED}
    assert not (reachable & internal)
