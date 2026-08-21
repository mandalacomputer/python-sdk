"""Pins the SDK to the platform's curated /api/v1 surface.

The platform allowlists routes server-side; anything else 404s. That check lives
there, but a client that calls a route the server does not expose fails at
runtime in a user's hands rather than here. This asserts every request the SDK
can issue lands on an allowlisted route.

Keep ALLOWED in step with V1_ROUTES in `web/lib/surface.ts` in the platform repo.
It mirrors that table in full, including the routes this SDK does not call —
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

PARAMETERS mirrors `web/lib/apidoc.ts` the same way, and exists because the
route table was not enough. `Range` on `GET computers/:id/files` is what lets a
file larger than one request moves come off a computer at all, and it arrived on
a route ALLOWED already listed — so every check here stayed green through a
whole feature going missing. A parameter is not a smaller kind of surface: the
call lands in the right place either way, and what is absent is the argument
that made it worth making.
"""

from __future__ import annotations

import io
import json
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
    ("POST", "computers/:id/agent"),
    # Reachable, and not reached from here — see UNIMPLEMENTED.
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
    # The OpenAI-shaped door onto the agent loop, which `POST
    # computers/:id/agent` is the front of and this SDK does drive.
    #
    # Deliberately not wrapped, and not a gap waiting to be closed: a caller who
    # wants this route already has an OpenAI client and points its base_url
    # here, which is the entire reason the platform put the door there. A
    # second, worse OpenAI client inside this SDK would be a maintenance
    # obligation with no user. The TypeScript SDK leaves it out for the same
    # reason, in the same set.
    ("POST", "chat/completions"),
}

# Every query, header and body field the platform documents, by route —
# mirroring the `DOCS` table in `web/lib/apidoc.ts` as ALLOWED mirrors
# V1_ROUTES.
#
# Its own table because the route table could not see the thing that made it
# necessary. `Range` on `GET computers/:id/files` is what lets a file larger
# than one request moves come off a computer at all, and it arrived on a route
# this mirror already listed — so every check here stayed green while the whole
# feature was missing. A parameter is not a smaller kind of surface: `force` on
# a stop, `fresh` on a screenshot and `env` on an exec are each the difference
# between a call that works and a call that works wrongly and says nothing.
PARAMETERS: dict[str, set[str]] = {
    "GET templates": set(),
    "GET sizes": set(),
    "GET computers": {"query:allow_partial"},
    "POST computers": {
        "body:name",
        "body:size",
        "body:template",
        "body:cpu",
        "body:ram_mb",
        "body:disk_gb",
        "body:resolution",
        "body:start",
    },
    "GET computers/:id": set(),
    "PATCH computers/:id": {
        "body:name",
        "body:cpu",
        "body:ram_mb",
        "body:disk_gb",
        "body:idle_suspend_min",
    },
    "DELETE computers/:id": {"query:snapshots", "query:expect"},
    "POST computers/:id/start": set(),
    "POST computers/:id/stop": {"query:force"},
    "POST computers/:id/suspend": set(),
    "POST computers/:id/restart": set(),
    "POST computers/:id/clone": {"body:name"},
    # Computer use.
    "GET computers/:id/screenshot": {"query:w", "query:fresh"},
    "POST computers/:id/input": {
        "body:action",
        "body:x",
        "body:y",
        "body:coordinate",
        "body:start_coordinate",
        "body:text",
        "body:key",
        "body:keys",
        "body:button",
        "body:scroll_direction",
        "body:amount",
        "body:scroll_amount",
        "body:duration",
    },
    "POST computers/:id/exec": {
        "body:command",
        "body:session",
        "body:timeout_s",
        "body:background",
        "body:cwd",
        "body:env",
    },
    "GET computers/:id/exec/:pid": set(),
    "DELETE computers/:id/exec/:pid": set(),
    "GET computers/:id/windows": {"query:include"},
    "POST computers/:id/windows/:window": {
        "body:action",
        "body:x",
        "body:y",
        "body:width",
        "body:height",
    },
    "POST computers/:id/agent": {
        "header:X-Model-Key",
        "body:prompt",
        "body:system",
        "body:max_steps",
        "body:model",
        "body:stream",
    },
    "POST chat/completions": {
        "header:X-Model-Key",
        "body:computer_id",
        "body:messages",
        "body:model",
        "body:max_steps",
        "body:stream",
    },
    # An upload's body is the file itself, raw — there are no named fields to
    # mirror. A download's `Range` is the one header a *caller* sets that
    # reaches the daemon; see `Computer.read_file_part`.
    "PUT computers/:id/files": {"query:path"},
    "GET computers/:id/files": {"query:path", "header:Range"},
    "GET snapshots": {"query:allow_partial", "query:include"},
    "GET computers/:id/snapshots": set(),
    "POST computers/:id/snapshots": {"body:name", "body:memory"},
    "POST snapshots/:id/restore": set(),
    "POST snapshots/:id/clone": {"body:name"},
    "DELETE snapshots/:id": set(),
    "GET computers/:id/schedule": set(),
    "PUT computers/:id/schedule": {"body:enabled", "body:hour", "body:minute", "body:tz"},
    "DELETE computers/:id/schedule": set(),
}

# Parameters the platform documents that this SDK deliberately does not send.
#
# UNIMPLEMENTED's counterpart, and the same argument: PARAMETERS mirrors the
# platform, so without this the difference between "documented" and "sent" would
# have nowhere to be written down and no test could tell a parameter nobody got
# round to from one nobody wants.
#
# Three of these are the flat vocabulary's second name for something already
# sent, and one is a whole route.
UNIMPLEMENTED_PARAMETERS = {
    # `keys: ["ctrl", "c"]` is sent instead. The chord-as-one-string form cannot
    # express a key whose own name contains the separator.
    "POST computers/:id/input  body:key",
    # `scroll_direction` is sent instead — `button` is the flat vocabulary's
    # name for it, and on a route that also takes a real mouse button that is a
    # word worth not overloading.
    "POST computers/:id/input  body:button",
    # `amount` is sent instead. Same value, two names.
    "POST computers/:id/input  body:scroll_amount",
    # The OpenAI-shaped door, whole. See UNIMPLEMENTED for why it stays shut.
    "POST chat/completions  header:X-Model-Key",
    "POST chat/completions  body:computer_id",
    "POST chat/completions  body:messages",
    "POST chat/completions  body:model",
    "POST chat/completions  body:max_steps",
    "POST chat/completions  body:stream",
}

#: Header names the platform documents anywhere — the ones worth looking for on
#: a recorded request. Every call also carries an Authorization and an Accept
#: this SDK wrote itself, and neither is a parameter a caller chose.
DOCUMENTED_HEADERS = {
    name.removeprefix("header:")
    for names in PARAMETERS.values()
    for name in names
    if name.startswith("header:")
}

COMPUTER = {"id": "vm-1", "name": "d", "status": "running", "os": "linux", "cpu": 1}
SNAPSHOT = {"id": "snap-1", "computer_id": "vm-1", "name": "s", "kind": "disk", "state": "durable"}
HOLDINGS = {"count": 1, "size_bytes": 2, "fingerprint": "fp-abc"}
WINDOW = {"id": "0x2600003", "title": "T", "class": "Firefox"}
EXEC_STATUS = {"pid": 4242, "running": False, "exited": True, "exit_code": 0}
# One agent run, over in a frame. The route answers a stream whether or not
# `stream` was set — a non-streaming run is the same body without the framing —
# so the handler below serves this to both, and `agent_once` reads it as JSON.
AGENT_DONE = b'event: done\ndata: {"steps": 1, "stop": "end_turn", "text": "done"}\n\n'
AGENT_RESULT = {"steps": 1, "stop": "end_turn", "text": "done"}


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
    if path.endswith("/agent"):
        # The streaming and non-streaming forms are one route and one handler
        # here, told apart by what the caller asked for — which is the same
        # thing the platform does with `stream`.
        if json.loads(request.content or b"{}").get("stream"):
            return httpx.Response(
                200, content=AGENT_DONE, headers={"Content-Type": "text/event-stream"}
            )
        return httpx.Response(200, json=AGENT_RESULT)
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


def sent_parameters(calls: object) -> dict[str, set[str]]:
    """Every documented parameter the recorded calls actually carried, by route.

    Read off the requests rather than off the source, because what the SDK sends
    is the only thing a user experiences. Headers are matched against
    DOCUMENTED_HEADERS alone: every request also carries an ``Authorization`` and
    an ``Accept`` this SDK wrote for itself, and neither is a parameter anyone
    chose.
    """
    found: dict[str, set[str]] = {}
    for call in calls:  # type: ignore[attr-defined]
        request = call.request
        route = f"{request.method} {pattern_for(request.url.path.replace('/api/v1', '', 1))}"
        names = found.setdefault(route, set())
        names.update(f"query:{key}" for key, _ in request.url.params.multi_items())
        names.update(f"header:{h}" for h in DOCUMENTED_HEADERS if h in request.headers)
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"null")
            if isinstance(body, dict):
                names.update(f"body:{key}" for key in body)
    return found


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
    # Everything a create can name, in the two shapes that are allowed: a size
    # stands in for a template and a shape together, so it cannot be combined
    # with the four it replaces.
    client.computers.create(
        name="dev",
        template="base",
        cpu=2,
        ram_mb=4096,
        disk_gb=40,
        resolution="1920x1080",
        start=False,
    )
    client.computers.create(size="small")
    c.refresh()
    c.start()
    c.stop()
    c.stop(force=True)
    c.suspend()
    c.restart()
    c.clone(name="copy")
    c.rename("renamed")
    c.screenshot()
    c.screenshot(width=320)
    c.screenshot(fresh=True)
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
    # `desktop` is the wire's `session`, and the only value it takes.
    c.exec("true", desktop=True)
    job = c.start_exec("sleep 60")
    job.poll()
    job.kill()
    c.background_command(4242).poll()
    c.windows()
    c.windows(include_all=True)
    c.window_action("0x2600003", "focus")
    c.window_action("0x2600003", "move", x=10, y=20)
    c.window_action("0x2600003", "resize", width=800, height=600)
    c.resize(cpu=4, ram_mb=8192)
    c.resize(disk_gb=64)
    c.set_idle_suspend(15)
    c.set_idle_suspend(None)
    c.read_file("/home/user/out.txt")
    c.read_file_part("/home/user/out.txt", offset=0, length=1024)
    c.read_file_part("/var/log/build.log", offset=-4096)
    c.download_file("/home/user/out.txt", io.BytesIO())
    c.write_file("/home/user/in.txt", b"hello")
    c.snapshot()
    c.snapshot(memory=True, name="before-upgrade")
    c.snapshots()
    c.snapshots(include_unfinished=True, allow_partial=True)
    c.snapshot_holdings()
    c.schedule()
    c.set_schedule(enabled=True, hour=4, tz="UTC")
    c.clear_schedule()
    c.agent("do the thing", model_key="sk-ant-test")
    c.agent("do the thing", model_key="sk-ant-test", system="be brief", max_steps=5, model="m")
    c.agent_once("do the thing", model_key="sk-ant-test")
    client.computers.list(allow_partial=True)
    client.snapshots.list()
    client.snapshots.list(include_unfinished=True, allow_partial=True)
    client.snapshots.restore("snap-1")
    client.snapshots.clone("snap-1", name="from-snap")
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
    await client.computers.create(
        name="dev",
        template="base",
        cpu=2,
        ram_mb=4096,
        disk_gb=40,
        resolution="1920x1080",
        start=False,
    )
    await client.computers.create(size="small")
    await c.refresh()
    await c.start()
    await c.stop()
    await c.stop(force=True)
    await c.suspend()
    await c.restart()
    await c.clone(name="copy")
    await c.rename("renamed")
    await c.screenshot()
    await c.screenshot(width=320)
    await c.screenshot(fresh=True)
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
    await c.exec("true", desktop=True)
    job = await c.start_exec("sleep 60")
    await job.poll()
    await job.kill()
    await c.background_command(4242).poll()
    await c.windows()
    await c.windows(include_all=True)
    await c.window_action("0x2600003", "focus")
    await c.window_action("0x2600003", "move", x=10, y=20)
    await c.window_action("0x2600003", "resize", width=800, height=600)
    await c.resize(cpu=4, ram_mb=8192)
    await c.resize(disk_gb=64)
    await c.set_idle_suspend(15)
    await c.set_idle_suspend(None)
    await c.read_file("/home/user/out.txt")
    await c.read_file_part("/home/user/out.txt", offset=0, length=1024)
    await c.read_file_part("/var/log/build.log", offset=-4096)
    await c.download_file("/home/user/out.txt", io.BytesIO())
    await c.write_file("/home/user/in.txt", b"hello")
    await c.snapshot()
    await c.snapshot(memory=True, name="before-upgrade")
    await c.snapshots()
    await c.snapshots(include_unfinished=True, allow_partial=True)
    await c.snapshot_holdings()
    await c.schedule()
    await c.set_schedule(enabled=True, hour=4, tz="UTC")
    await c.clear_schedule()
    await c.agent("do the thing", model_key="sk-ant-test")
    await c.agent(
        "do the thing", model_key="sk-ant-test", system="be brief", max_steps=5, model="m"
    )
    await c.agent_once("do the thing", model_key="sk-ant-test")
    await client.computers.list(allow_partial=True)
    await client.snapshots.list()
    await client.snapshots.list(include_unfinished=True, allow_partial=True)
    await client.snapshots.restore("snap-1")
    await client.snapshots.clone("snap-1", name="from-snap")
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
def test_every_documented_parameter_is_sent_or_pinned_as_unsent(client: mc.Client) -> None:
    """The SDK sends every parameter the platform documents, or says why not.

    The route half of this file cannot see any of this: a call lands on the right
    route whether or not it carried the argument that made it worth making. The
    four the TypeScript SDK found missing this way — a forced stop, a fresh
    screenshot, an exec's environment and a snapshot's name — were each a call
    that worked and worked wrongly, which is the failure that does not announce
    itself.
    """
    route = respx.route(host="api.test").mock(side_effect=api_handler)
    exercise_everything(client)
    sent = sent_parameters(route.calls)

    unsent = {
        f"{name} on {where}"
        for where, names in PARAMETERS.items()
        for name in names - sent.get(where, set())
        if f"{where}  {name}" not in UNIMPLEMENTED_PARAMETERS
    }
    assert not unsent, (
        f"documented parameters this SDK never sends: {sorted(unsent)}. "
        "Send them, or pin them in UNIMPLEMENTED_PARAMETERS with the reason."
    )


@respx.mock
def test_no_call_sends_a_parameter_the_platform_does_not_document(client: mc.Client) -> None:
    """The other direction, which is the one that fails silently in a user's hands.

    A query key or body field the platform has never heard of is ignored, and a
    request that is ignored in part looks exactly like one that worked. So a
    rename upstream shows up here as a parameter with no documentation rather
    than as a feature that quietly stopped happening.
    """
    route = respx.route(host="api.test").mock(side_effect=api_handler)
    exercise_everything(client)

    undocumented = {
        f"{name} on {where}"
        for where, names in sent_parameters(route.calls).items()
        for name in names - PARAMETERS.get(where, set())
    }
    assert not undocumented, (
        f"SDK sends parameters the platform does not take: {sorted(undocumented)}"
    )


@respx.mock
async def test_both_clients_send_exactly_the_same_parameters(
    client: mc.Client, async_client: mc.AsyncClient
) -> None:
    """Parity down to the arguments, not just the routes.

    The route comparison next door is satisfied by an async half that reaches the
    same endpoints having dropped a keyword on the way — which is the drift that
    actually happens, because a parameter is added to one half by the person who
    needed it.
    """
    sync_route = respx.route(host="api.test").mock(side_effect=api_handler)
    exercise_everything(client)
    sync_sent = sent_parameters(sync_route.calls)

    respx.reset()
    async_route = respx.route(host="api.test").mock(side_effect=api_handler)
    await exercise_everything_async(async_client)

    assert sync_sent == sent_parameters(async_route.calls)


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
    # And the same for what each of them takes. `Range` on the download is the
    # reason this half exists: a whole feature, on a route the line above was
    # already satisfied by.
    assert check_surface.parameter_drift(check_surface.parameters(platform), PARAMETERS) == []


def test_allowlist_excludes_the_daemons_internal_routes() -> None:
    """The ops and plan-owned endpoints are not tenant API and never should be.

    The previous test proves the SDK stays inside ALLOWED; this proves ALLOWED
    itself stays honest, so widening it later is a deliberate act rather than a
    quiet one. These routes are not owner-scoped in the daemon.
    """
    internal = {"audit", "host", "fleet", "retention"}
    reachable = {pattern.split("/")[0] for _, pattern in ALLOWED}
    assert not (reachable & internal)
