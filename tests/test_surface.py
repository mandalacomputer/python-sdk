"""Pins the SDK to the platform's curated /api/v1 surface.

The platform allowlists routes server-side; anything else 404s. That check lives
there, but a client that calls a route the server does not expose fails at
runtime in a user's hands rather than here. This asserts every request the SDK
can issue lands on an allowlisted route.

Keep ALLOWED in step with `web/lib/v1.ts` in the platform repo.
"""

from __future__ import annotations

import httpx
import pytest
import respx

import gorillacloud as gc

BASE = "https://api.test/api/v1"

# (method, pattern) with ids replaced by ":id" — mirrors lib/v1.ts ROUTES.
ALLOWED = {
    ("GET", "templates"),
    ("GET", "computers"),
    ("POST", "computers"),
    ("GET", "computers/:id"),
    ("DELETE", "computers/:id"),
    ("POST", "computers/:id/start"),
    ("POST", "computers/:id/stop"),
    ("POST", "computers/:id/restart"),
    ("POST", "computers/:id/clone"),
    ("GET", "computers/:id/screenshot"),
    ("POST", "computers/:id/input"),
    ("POST", "computers/:id/exec"),
    ("GET", "snapshots"),
    ("POST", "computers/:id/snapshots"),
    ("POST", "snapshots/:id/restore"),
    ("POST", "snapshots/:id/clone"),
    ("DELETE", "snapshots/:id"),
    ("GET", "computers/:id/schedule"),
    ("PUT", "computers/:id/schedule"),
}

COMPUTER = {"id": "vm-1", "name": "d", "status": "running", "os": "linux", "cpu": 1}
SNAPSHOT = {"id": "snap-1", "computer_id": "vm-1", "name": "s", "kind": "disk", "state": "durable"}


def pattern_for(path: str) -> str:
    """Reduce a concrete path to its route shape, as the server's proxy does."""
    parts = [p for p in path.strip("/").split("/") if p]
    return "/".join(
        ":id" if i and parts[i - 1] in ("computers", "snapshots") else seg
        for i, seg in enumerate(parts)
    )


@pytest.fixture
def client() -> gc.Client:
    return gc.Client("gck_test", base_url=BASE)


def exercise_everything(client: gc.Client) -> None:
    """Call every method the SDK exposes that performs a request."""
    client.templates.list()
    client.computers.list()
    client.computers.get("vm-1")
    c = client.computers.create(template="base")
    c.refresh()
    c.start()
    c.stop()
    c.restart()
    c.clone(name="copy")
    c.screenshot()
    c.screenshot(width=320)
    c.move(1, 2)
    c.click(1, 2)
    c.right_click(1, 2)
    c.middle_click(1, 2)
    c.double_click(1, 2)
    c.scroll(1, 2, direction="up")
    c.type("hi")
    c.key("ctrl", "c")
    c.exec("true")
    c.snapshot()
    c.snapshot(memory=True)
    c.snapshots()
    c.schedule()
    c.set_schedule(enabled=True, hour=4, tz="UTC")
    client.snapshots.list()
    client.snapshots.restore("snap-1")
    client.snapshots.clone("snap-1")
    client.snapshots.delete("snap-1")
    c.delete()


@respx.mock
def test_every_call_lands_on_an_allowlisted_route(client: gc.Client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        get = request.method == "GET"
        if path.endswith("/screenshot"):
            return httpx.Response(200, content=b"png")
        if path.endswith("/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False})
        if path.endswith("/templates"):
            return httpx.Response(200, json=[])
        # Collections list on GET and return a single object on POST — getting
        # this backwards is what made the first version of this test fail.
        if path.endswith("/snapshots"):
            return httpx.Response(200, json=[SNAPSHOT] if get else SNAPSHOT)
        if path.endswith("/computers"):
            return httpx.Response(200, json=[COMPUTER] if get else COMPUTER)
        return httpx.Response(200, json=COMPUTER)

    route = respx.route(host="api.test").mock(side_effect=handler)
    exercise_everything(client)

    called = {
        (call.request.method, pattern_for(call.request.url.path.replace("/api/v1", "", 1)))
        for call in route.calls
    }
    assert called, "no requests were made — the exercise is not exercising anything"
    assert called <= ALLOWED, f"SDK calls routes the server does not expose: {sorted(called - ALLOWED)}"


def test_pattern_for_treats_ids_as_ids() -> None:
    assert pattern_for("/computers/vm-1/start") == "computers/:id/start"
    assert pattern_for("/snapshots/snap-1/clone") == "snapshots/:id/clone"
    assert pattern_for("/computers/vm-1/snapshots") == "computers/:id/snapshots"
    # A computer whose id looks like a route segment is still an id.
    assert pattern_for("/computers/audit") == "computers/:id"


def test_allowlist_excludes_the_daemons_internal_routes() -> None:
    """The ops and plan-owned endpoints are not tenant API and never should be.

    The previous test proves the SDK stays inside ALLOWED; this proves ALLOWED
    itself stays honest, so widening it later is a deliberate act rather than a
    quiet one. These routes are not owner-scoped in the daemon.
    """
    internal = {"audit", "host", "fleet", "retention"}
    reachable = {pattern.split("/")[0] for _, pattern in ALLOWED}
    assert not (reachable & internal)
