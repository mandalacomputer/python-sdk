"""Pins the SDK to the platform's curated /api/v1 surface.

The platform allowlists routes server-side; anything else 404s. That check lives
there, but a client that calls a route the server does not expose fails at
runtime in a user's hands rather than here. This asserts every request the SDK
can issue lands on an allowlisted route.

Keep ALLOWED in step with V1_ROUTES in `web/lib/surface.ts` in the platform repo.
It mirrors that table in full, including the routes this SDK does not call —
those are named in UNIMPLEMENTED, which is what keeps the distance between the
two visible rather than letting it grow quietly. The tables themselves live in
``surface_tables.py`` so the drift-check script can import them without pytest.

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

Both of those compare this SDK to the platform. `surface_inventory` is the third
dimension and points inward: every public method of this SDK is named in
`exercise_everything`, not merely every route reached by one. They are different
claims wherever two methods share a route, which here is most of them — `exec`,
`open` and the guest wait are all `POST computers/:id/exec` — so a method added
beside an existing one and left out of the exercise shipped with no coverage at
all, on a suite whose whole design is that the surface is enumerable (OPL-3900).
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import aclosing, closing
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx
from tests.surface_inventory import half, inventory, names, record_named_calls
from tests.surface_tables import ALLOWED, PARAMETERS
from tests.test_account import account_report
from tests.test_artifacts import ARTIFACT_ID, artifact_manifest, download
from tests.test_retained_results import EXECUTION_ID, RESULT_ID, page, result_manifest

import mandala_computer as mc

BASE = "https://api.test/api/v1"

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
    # The account's workspaces, read only (OPL-5057). Listed to stay in step
    # with the surface; no SDK method yet.
    ("GET", "workspaces"),
    ("GET", "workspaces/:id"),
    ("GET", "workspaces/:id/members"),
}

# Parameters the SDK does not yet send or deliberately omits.
#
# UNIMPLEMENTED's counterpart, and the same argument: PARAMETERS mirrors the
# platform, so without this the difference between "documented" and "sent" would
# have nowhere to be written down and no test could tell a parameter nobody got
# round to from one nobody wants.
UNIMPLEMENTED_PARAMETERS = {
    # OPL-5051: `format` (png or jpeg), `quality`, `region` (x,y,w,h) and
    # `scale` shape the screenshot. Listed to stay in step with the surface;
    # not yet sent.
    "GET computers/:id/screenshot  query:format",
    "GET computers/:id/screenshot  query:quality",
    "GET computers/:id/screenshot  query:region",
    "GET computers/:id/screenshot  query:scale",
    # `manage_keys: true` is refused from every API key (403): the permission
    # is granted only from a dashboard session, and false is the default. There
    # is nothing for this SDK to send.
    "POST api-keys  body:manage_keys",
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
RETENTION = {"daily": 7, "weekly": 4, "monthly": 12}
# One subscription (platform OPL-4300), and the same one as a create answers
# it: the secret is on exactly that shape and on no read, so the handler below
# serves WEBHOOK to the reads and WEBHOOK_CREATED to the create and the rotate.
# One API key as the listing shows it, and the mint's answer with the raw key
# once (OPL-5053); whoami for an account-wide key that manages keys.
API_KEY = {
    "id": "key-a1b2c3d4e5f6",
    "name": "ci",
    "prefix": "com_1a2b3c4d…",
    "created_at": "2026-09-20T08:00:00.000Z",
    "last_used_at": None,
    "workspace_id": None,
    "workspace_name": None,
    "manage_keys": False,
}
API_KEY_CREATED = {**API_KEY, "raw": "com_" + "ab" * 24}
WHOAMI = {
    "user": {"id": "usr-1", "email": "dana@example.com", "name": "Dana"},
    "account": {"id": "acc-1", "name": "Acme", "plan": "team", "status": "active"},
    "role": "owner",
    "workspace": None,
    "key": {**API_KEY, "manage_keys": True},
}
SSH_KEY = {
    "id": "sshk-74025eba1b658b99",
    "name": "laptop",
    "public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGqsBlqrbipXh/7n81gKS46IyjJY7nVv8mGtIAE+v76w",
    "fingerprint": "SHA256:7OR2azJrv1nm44ploDfzY03D/74wXZGn8Qj4fabJDXs",
    "key_type": "ssh-ed25519",
    "created_at": "2026-09-16T12:00:00Z",
    "last_used_at": None,
}
SSH_ACCESS = {
    "computer": "vm-1",
    "enabled": True,
    "available": True,
    "pending": False,
    "key_count": 1,
    "keys_pushed": 1,
    "error": None,
}
# A computer's secret bindings, as `GET computers/:id/secrets` answers them.
SECRET_BINDINGS = {
    "secrets": [
        {
            "secret_id": "csec-0123456789abcdef",
            "revision_id": "csr-0123456789abcdef01234567",
            "env": "API_TOKEN",
        },
        {
            "secret_id": "csec-0123456789abcde0",
            "revision_id": "csr-0123456789abcdef01234568",
            "file": "kubeconfig",
        },
    ],
    "version": 3,
}
# The account's secret store (OPL-4984): one secret, never with a value, and the
# listing that carries it with the store's limits.
SECRET = {
    "id": "csec-0123456789abcdef",
    "name": "OPENAI_API_KEY",
    "workspace_id": None,
    "revision_id": "csr-0123456789abcdef01234567",
    "created_at": "2026-09-20T12:00:00Z",
    "updated_at": "2026-09-20T12:00:00Z",
    "last_used_at": None,
}
SECRET_LIST = {
    "secrets": [SECRET],
    "delivery": True,
    "limits": {
        "name_max_chars": 60,
        "value_max_bytes": 4096,
        "active_per_account": 100,
        "created_per_account": 1000,
    },
}
GUEST_DIRECTORY = {
    "path": "/home/user",
    "entries": [{"name": "notes.txt", "type": "file", "size_bytes": 5}],
    "truncated": False,
    "skipped": 0,
}
SIGNAL_PAGE = {
    "computer": "vm-1",
    "from": "c-0",
    "cursor": "c-1",
    "events": [],
    "more": False,
    "baseline": True,
    "supported": ["computer.started"],
    "retention": "ephemeral",
}
ACTIVITY_ID = "act_0123456789abcdef0123456789abcdef"
ACTIVITY = {
    "activity_id": ACTIVITY_ID,
    "account_id": "acc-1",
    "computer_id": "vm-1",
    "workspace_id": None,
    "channel": "api",
    "route": "exec",
    "action": "exec",
    "state": "completed",
    "received_at": "2026-09-20T12:00:00Z",
    "observed_at": "2026-09-20T12:00:01Z",
    "revision": 2,
    "has_results": True,
}
ACTIVITY_PAGE = {
    "items": [ACTIVITY],
    "next_cursor": None,
    "changes_cursor": "chg-1",
    "gap": False,
    "health": {
        "recording_started_at": "2026-09-01T00:00:00Z",
        "earliest_retained_at": None,
        "count_truncated": False,
        "age_truncated": False,
        "capture": "available",
        "completeness": "best-effort",
        "gap_at": None,
        "recovered_at": None,
    },
}
ACTIVITY_RESULTS = {"activity_id": ACTIVITY_ID, "revision": 2, "more": False, "items": []}
WEBHOOK = {
    "id": "whk-2b7d4c809f3c1a7e",
    "url": "https://ci.example.com/mandala",
    "description": "",
    "events": ["process.exited"],
    "computers": [],
    "enabled": True,
    "disabled_reason": None,
    "disabled_at": None,
    "last_success_at": None,
    "last_failure_at": None,
    "last_status": None,
    "created_at": "2026-09-01T12:00:00.000Z",
    "updated_at": "2026-09-01T12:00:00.000Z",
}
WEBHOOK_CREATED = {**WEBHOOK, "secret": "whsec_bWFuZGFsYS13ZWJob29rLXRlc3QtdmVjdG9yLWtleSE="}
DELIVERY = {
    "id": "whd-9f3c1a7e5b2d4c80",
    "event_type": "webhook.test",
    "computer": "",
    "cursor": "",
    "state": "pending",
    "attempts": 0,
    "next_at": "2026-09-01T12:00:01.000Z",
    "attempted_at": None,
    "last_status": None,
    "last_error": None,
    "delivered_at": None,
    "created_at": "2026-09-01T12:00:00.000Z",
}
# One finished move, in the account-wide envelope the route actually answers.
#
# Two rows, and only one of them this computer's: `Computer.wait_for_move`
# filters `GET /moves` by `computer_id`, and a fixture with a single row would
# be satisfied by a filter that took the first one — which is the bug the
# filter exists to prevent, since a finished move for a different computer stays
# listed for a day. `live: false` is what lets the wait return rather than poll.
MOVES = {
    "moves": [
        {
            "id": "mv-0",
            "computer_id": "vm-other",
            "state": "moving",
            "live": True,
            "started_at": "2026-08-26T11:00:00.000Z",
        },
        {
            "id": "mv-1",
            "computer_id": "vm-1",
            "state": "moved",
            "live": False,
            "started_at": "2026-08-26T12:00:00.000Z",
            "finished_at": "2026-08-26T12:05:00.000Z",
        },
    ]
}
WINDOW = {"id": "0x2600003", "title": "T", "class": "Firefox"}
EXEC_STATUS = {"pid": 4242, "running": False, "exited": True, "exit_code": 0}
USAGE = {
    "period": {
        "start": "2026-08-04T00:00:00Z",
        "end": "2026-09-04T00:00:00Z",
        "source": "subscription",
    },
    "from": "2026-08-04T00:00:00Z",
    "to": "2026-08-22T12:00:00Z",
    "usage": {"run_hours": 1.5, "vcpu_hours": 3, "computers": []},
    "degraded": False,
    "unmetered": False,
    "reported_through": None,
}
# One agent run, over in a frame. The route answers a stream whether or not
# `stream` was set — a non-streaming run is the same body without the framing —
# so the handler below serves this to both, and `agent_once` reads it as JSON.
AGENT_DONE = b'event: done\ndata: {"steps": 1, "stop": "end_turn", "text": "done"}\n\n'
AGENT_RESULT = {"steps": 1, "stop": "end_turn", "text": "done"}

# One published template, in the platform's own spelling (platform OPL-3789).
#
# `document` as an OBJECT, not the canonical string the store keeps: the platform
# parses it back on the way out so a caller reading a template gets JSON it can
# address. A fixture holding the string would let a decoder that forgot to expect
# an object pass.
PUBLISHED_TEMPLATE = {
    "ref": "acc-1/devbox@1.0.0",
    "doc_digest": "sha256:aaaa",
    "document": {"apiVersion": "mandala/v1", "kind": "Template"},
    "template": {
        "name": "devbox",
        "ref": "acc-1/devbox@1.0.0",
        "label": "My desktop",
        "os": "linux",
        "cpu": 2,
        "ram_mb": 4096,
        "disk_gb": 30,
    },
    "versions": ["1.0.0"],
    "published_at": "2026-08-26T12:00:00.000Z",
}

# What a retire took away (platform OPL-3830). `templates` and `refs_claimed`
# deliberately differ: a retired ref still counts, and a fixture where the two
# agreed would let a decoder that read one field for both pass.
RETIRED_TEMPLATES = {
    "retired": ["acc-1/devbox@1.0.0"],
    "retired_at": "2026-08-26T13:00:00.000Z",
    "versions": [],
    "templates": 0,
    "refs_claimed": 1,
}

TEMPLATE_CHECK = {
    "valid": True,
    "ref": "acc-1/devbox@1.0.0",
    "doc_digest": "sha256:aaaa",
    "build_digest": "sha256:bbbb",
}

TEMPLATE_BUILD = {
    "id": "bld-1",
    "ref": "acc-1/devbox@1.0.0",
    "status": "running",
    "started_at": "2026-08-26T12:00:00.000Z",
}

BUILD_PROGRESS = {
    "id": "bld-1",
    "status": "succeeded",
    "done": True,
    "phase": "published",
    "step": 2,
    "of": 2,
    "steps": [
        {"n": 1, "kind": "apt", "label": "ripgrep", "status": "done"},
        {"n": 2, "kind": "finish", "label": "cleanup", "status": "done"},
    ],
    "note": "",
    "error": "",
    "updated_at": "2026-08-26T12:15:00.000Z",
}

BUILD_EVENTS = (
    b"event: progress\ndata: "
    + json.dumps({**BUILD_PROGRESS, "done": False, "status": "running"}).encode()
    + b"\n\nevent: done\ndata: "
    + json.dumps(BUILD_PROGRESS).encode()
    + b"\n\n"
)


#: Every public callable in this SDK that can put a request on the wire, by the
#: class that defines it — read out of the source rather than listed here. See
#: ``surface_inventory``: the completeness check below it counts ROUTES, and a
#: second method on a route somebody else already reaches is invisible to it.
SURFACE = inventory()


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
        if i and parts[i - 1] in (
            "computers",
            "snapshots",
            "builds",
            "webhooks",
            "ssh-keys",
            "api-keys",
            "secrets",
        ):
            # `computers/:id/secrets` is a literal third segment, never an id:
            # only the account store's `secrets/:id` is at position 1.
            if parts[i - 1] == "secrets" and i != 1:
                return seg
            return ":id"
        if i == 3 and parts[0] == "computers" and parts[2] == "activities":
            return ":activity"
        if i == 3 and parts[0] == "computers" and parts[2] == "windows":
            return ":window"
        if i == 3 and parts[0] == "computers" and parts[2] == "exec":
            return ":pid"
        if i == 3 and parts[0] == "computers" and parts[2] == "executions":
            return ":executionId"
        if i == 3 and parts[0] == "computers" and parts[2] == "results":
            return ":resultId"
        if i == 3 and parts[0] == "computers" and parts[2] == "artifacts":
            return ":artifactId"
        return seg

    # A template ref's two halves, pinned to a THREE-segment path under
    # `templates` — which is what keeps the two-segment literals,
    # `templates/schema` and `templates/validate`, reducing to themselves. The
    # platform's own patternFor pins them the same way and for the same reason,
    # and a mirror that reduced them differently would compare two different
    # tables and call them equal. Two placeholders and not one, because a
    # namespace is an account id and a name is not.
    if len(parts) == 3 and parts[0] == "templates":
        return "templates/:namespace/:name"
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
    if path.endswith("/files/list"):
        return httpx.Response(200, json=GUEST_DIRECTORY)
    if path.endswith("/signals"):
        return httpx.Response(200, json=SIGNAL_PAGE)
    if "/activities" in path:
        if path.endswith("/activities"):
            return httpx.Response(200, json=ACTIVITY_PAGE)
        if path.endswith("/results"):
            return httpx.Response(200, json=ACTIVITY_RESULTS)
        return httpx.Response(200, json=ACTIVITY)
    # The account's store, which is `secrets` at the root; a computer's
    # bindings are the `/secrets` under `computers/:id`, further down.
    store = path.replace("/api/v1", "", 1)
    if store == "/secrets":
        return httpx.Response(200 if get else 201, json=SECRET_LIST if get else SECRET)
    if store.startswith("/secrets/"):
        return httpx.Response(200, json={"ok": True} if request.method == "DELETE" else SECRET)
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
    if path.endswith("/retained-output"):
        return httpx.Response(
            201, json=result_manifest(execution_id=path.split("/executions/", 1)[1].split("/")[0])
        )
    if "/results/" in path:
        if request.method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/output"):
            return page(b"", offset=int(request.url.params["offset"]))
        return httpx.Response(200, json=result_manifest())
    if path.endswith("/artifacts") and request.method == "POST":
        body = json.loads(request.content)
        association = (
            {
                "kind": "caller_selected",
                "execution_id": body["execution_id"],
                "verified_at": "2026-09-16T12:00:00.123456789Z",
            }
            if "execution_id" in body
            else None
        )
        return httpx.Response(201, json=artifact_manifest(execution_association=association))
    if "/artifacts/" in path:
        if request.method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/download"):
            return download(b"abc")
        return httpx.Response(200, json=artifact_manifest())
    if path.endswith("/exec"):
        if (
            json.loads(request.content).get("retain_output") is not None
            and json.loads(request.content).get("retain_output") is not False
        ):
            return httpx.Response(
                200,
                json={
                    "exit_code": 0,
                    "stdout_b64": "",
                    "stderr_b64": "",
                    "timed_out": False,
                    "out_truncated": False,
                    "err_truncated": False,
                    "result_id": RESULT_ID,
                },
            )
        return httpx.Response(
            200,
            json={
                "exit_code": 0,
                "stdout_b64": "",
                "stderr_b64": "",
                "timed_out": False,
                "pid": 4242,
            },
        )
    if "/executions/" in path:
        identity = path.split("/executions/", 1)[1].split("/")[0]
        if path.endswith("/output"):
            return httpx.Response(
                200,
                json={
                    "execution_id": identity,
                    "stdout_b64": "",
                    "stderr_b64": "",
                    "stdout_offset": int(request.url.params["stdout_offset"]),
                    "stderr_offset": int(request.url.params["stderr_offset"]),
                    "stdout_more": False,
                    "stderr_more": False,
                    "diagnostic_b64": "",
                    "diagnostic_truncated": False,
                },
            )
        return httpx.Response(
            200,
            json={
                "execution_id": identity,
                "computer_id": "vm-1",
                "pid": 4242,
                "status": "running",
                "started_at": "2026-09-15T12:00:00Z",
                "output_source": "volatile_guest_files",
            },
        )
    if "/exec/" in path:
        return httpx.Response(200, json=EXEC_STATUS)
    # Both verbs on one path, told apart by the method: the read answers text
    # and the write answers an ack, and a mock that gave both one shape would
    # let a decoder reading the wrong field pass.
    if path.endswith("/clipboard"):
        return httpx.Response(200, json={"text": "on the clipboard"} if get else {"ok": True})
    if path.endswith("/windows"):
        return httpx.Response(200, json={"windows": [WINDOW]})
    if "/windows/" in path:
        return httpx.Response(200, json={"ok": True, "window": WINDOW, "gone": False})
    if path.endswith("/templates/schema"):
        return httpx.Response(200, json={"$id": f"{BASE}/templates/schema"})
    if path.endswith("/templates/validate"):
        return httpx.Response(200, json=TEMPLATE_CHECK)
    if path.endswith("/builds/bld-1/events"):
        return httpx.Response(
            200, content=BUILD_EVENTS, headers={"Content-Type": "text/event-stream"}
        )
    if path.endswith("/progress"):
        return httpx.Response(200, json=BUILD_PROGRESS)
    if path.endswith("/builds"):
        return httpx.Response(200, json=[TEMPLATE_BUILD] if get else TEMPLATE_BUILD)
    if "/builds/" in path:
        return httpx.Response(200, json=TEMPLATE_BUILD)
    if path.endswith(("/templates", "/sizes")):
        # A publish is a POST to the collection and answers with the one
        # template it stored, the same way a snapshot POST does below.
        return httpx.Response(200, json=[] if get else PUBLISHED_TEMPLATE)
    # The store's ref route, which is three segments and is therefore NOT
    # `/templates`. DELETE and GET answer different shapes, which is the point:
    # a retire has no document left to hand back.
    if "/templates/" in path:
        return httpx.Response(
            200, json=RETIRED_TEMPLATES if request.method == "DELETE" else PUBLISHED_TEMPLATE
        )
    # Collections list on GET and return a single object on POST — getting this
    # backwards is what made the first version of this test fail.
    if path.endswith("/snapshots"):
        # Three shapes on two routes: the account-wide GET lists, a computer's
        # GET answers holdings — a count, a size and a fingerprint, not a
        # listing — and the POST returns the one snapshot it took.
        if not get:
            return httpx.Response(200, json=SNAPSHOT)
        return httpx.Response(200, json=HOLDINGS if "/computers/" in path else [SNAPSHOT])
    if path.endswith("/account"):
        return httpx.Response(200, json=account_report())
    if path.endswith("/usage"):
        return httpx.Response(200, json=USAGE)
    if path.endswith("/moves"):
        return httpx.Response(200, json=MOVES)
    if path.endswith("/retention"):
        return httpx.Response(200, json=RETENTION)
    # Eight routes, four shapes: the collection lists on GET and answers the
    # created subscription WITH its secret on POST; a rotate is the other route
    # carrying the secret; a test answers the one delivery it queued and the
    # deliveries route lists them; every other read or write on one
    # subscription answers the subscription, and a delete answers an ack.
    if path.endswith("/webhooks"):
        return httpx.Response(200 if get else 201, json=[WEBHOOK] if get else WEBHOOK_CREATED)
    if path.endswith("/deliveries"):
        return httpx.Response(200, json=[DELIVERY])
    if path.endswith("/rotate"):
        return httpx.Response(200, json=WEBHOOK_CREATED)
    if path.endswith("/test"):
        return httpx.Response(202, json=DELIVERY)
    if "/webhooks/" in path:
        return httpx.Response(200, json={"ok": True} if request.method == "DELETE" else WEBHOOK)
    # The caller's keys: the collection lists on GET and answers the one key it
    # registered on POST; a delete answers an ack. A computer's SSH setting is
    # one shape on both verbs.
    if path.endswith("/whoami"):
        return httpx.Response(200, json=WHOAMI)
    if path.endswith("/api-keys"):
        return httpx.Response(200 if get else 201, json=[API_KEY] if get else API_KEY_CREATED)
    if "/api-keys/" in path:
        return httpx.Response(200, json={"ok": True})
    if path.endswith("/ssh-keys"):
        return httpx.Response(200 if get else 201, json=[SSH_KEY] if get else SSH_KEY)
    if "/ssh-keys/" in path:
        return httpx.Response(200, json={"ok": True})
    if path.endswith("/ssh"):
        return httpx.Response(200, json=SSH_ACCESS)
    if path.endswith("/secrets"):
        return httpx.Response(200, json=SECRET_BINDINGS)
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
    # The document format, and the store on top of it (platform OPL-3568,
    # OPL-3789, OPL-3830). Both spellings of the ref routes, because `version` is
    # a parameter like any other and a call that never sends one is the gap the
    # parameter half of this test exists to see.
    client.templates.schema()
    client.templates.validate("apiVersion: mandala/v1")
    client.templates.publish("apiVersion: mandala/v1")
    client.templates.get("acc-1", "devbox")
    client.templates.get("acc-1", "devbox", version="1.0.0")
    client.templates.retire("acc-1", "devbox", version="1.0.0")
    client.templates.retire("acc-1", "devbox")
    # Compiling one (platform OPL-3791, OPL-3794). `no_reuse` on one of the two,
    # for the same reason.
    client.builds.start("apiVersion: mandala/v1")
    client.builds.start("apiVersion: mandala/v1", no_reuse=True)
    client.builds.list()
    # Both spellings, the way the computer listing is exercised below: a build
    # listing fails closed on a degraded fleet like every other fan-out, and
    # OPL-3840 is what made the way out of it something a client can send.
    client.builds.list(allow_partial=True)
    client.builds.get("bld-1")
    client.builds.progress("bld-1")
    # The poll on top of progress. It shares `GET builds/:id/progress` with the
    # call above, so the route half of this file cannot tell whether it was ever
    # driven — see `surface_inventory`, which is what does.
    client.builds.wait("bld-1")
    for _ in client.builds.events("bld-1"):
        break
    client.sizes.list()
    client.computers.list()
    client.computers.get("vm-1")
    c = client.computers.create(template="base")
    client.computers.launch(template="base")
    # Everything a create can name, in the two shapes that are allowed: a size
    # stands in for a template and a shape together, so it cannot be combined
    # with the four it replaces.
    client.computers.create(
        name="dev",
        template="base",
        template_transfer="prepare-token",
        cpu=2,
        ram_mb=4096,
        disk_gb=40,
        resolution="1920x1080",
        start=False,
    )
    client.computers.create(size="small")
    # Secrets bound at create, one as a variable and one as a file.
    client.computers.create(
        template="base",
        secrets=[
            {"secret_id": "csec-0123456789abcdef", "env": "API_TOKEN"},
            {"secret_id": "csec-0123456789abcde0", "file": "kubeconfig"},
        ],
    )
    # The create/delete pair as one scope. Both its routes are reached by their
    # own methods elsewhere here, so nothing but the inventory sees this line.
    with client.computers.ephemeral(template="base"):
        pass
    c.refresh()
    # The three readiness waits, which poll routes the calls around them already
    # reach: `GET computers/:id` for the first two and `POST computers/:id/exec`
    # for the guest probe.
    c.wait_until_built()
    c.wait_until_running()
    c.wait_for_guest()
    c.wait_for_secrets()
    c.start()
    c.start(resume_only=True)
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
    c.paste("Café")
    c.paste("ls", shift=True)
    c.key("ctrl", "c")
    c.hold_key("shift", seconds=1)
    c.wait(1)
    c.cursor_position()
    c.exec("true")
    c.exec("true", retain_output={"max_bytes_per_stream": 64, "retention_seconds": 60})
    c.retain_execution_output(EXECUTION_ID, max_bytes_per_stream=64, retention_seconds=60)
    c.result(RESULT_ID)
    c.result_output(RESULT_ID, stream="stdout", offset=0, limit=64)
    c.delete_result(RESULT_ID)
    c.publish_artifact(
        "/tmp/a",
        expected_size=3,
        expected_sha256=artifact_manifest()["sha256"],
        execution_id=EXECUTION_ID,
        max_bytes=64,
        retention_seconds=60,
    )
    c.artifact(ARTIFACT_ID)
    c.download_artifact(ARTIFACT_ID, max_bytes=64)
    c.delete_artifact(ARTIFACT_ID)
    c.exec("true", cwd="/tmp", env={"CI": "1"})
    # `desktop` is the wire's `session`, and the only value it takes.
    c.exec("true", desktop=True)
    # Sugar over the exec above, and therefore invisible to the route check.
    c.open("https://example.com")
    job = c.start_exec("sleep 60")
    job.poll()
    job.kill()
    c.background_command(4242).poll()
    c.execution("exec_0123456789abcdef0123456789abcdef")
    c.execution_output(
        "exec_0123456789abcdef0123456789abcdef", stdout_offset=7, stderr_offset=13, limit=1024
    )
    c.windows()
    c.windows(include_all=True)
    c.window_action("0x2600003", "focus")
    c.window_action("0x2600003", "move", x=10, y=20)
    c.window_action("0x2600003", "resize", width=800, height=600)

    c.clipboard()
    c.set_clipboard("on the clipboard")
    c.resize(cpu=4, ram_mb=8192)
    c.resize(disk_gb=64)
    # All three sizing fields in one call: the platform reads exactly these off a
    # move body, and the parameter sweep is what proves the SDK sends them.
    # `move` on the handle is the mouse pointer — see Computer.relocate.
    c.relocate(ram_mb=26000, cpu=2, disk_gb=64)
    # The wait on the move, which reads the same account-wide listing as
    # `moves.list()` below and is a different method for doing it.
    c.wait_for_move()
    client.moves.list()
    client.account.read()
    # Both bounds, because a call that names neither cannot show the parameter
    # sweep that this SDK can send either.
    client.usage.read()
    client.usage.read(since=datetime(2026, 8, 1, tzinfo=timezone.utc), until="2026-08-22T00:00:00Z")
    c.set_idle_suspend(15)
    c.set_idle_suspend(None)
    c.read_file("/home/user/out.txt")
    c.read_text_file("/home/user/out.txt")
    c.read_file_part("/home/user/out.txt", offset=0, length=1024)
    c.read_file_part("/var/log/build.log", offset=-4096)
    c.download_file("/home/user/out.txt", io.BytesIO())
    c.write_file("/home/user/in.txt", b"hello")
    # Create-only: sent only when asked for (OPL-4994).
    c.write_file("/home/user/new.txt", b"hello", overwrite=False)
    # A transfer that refuses to resume the computer (OPL-5026), both halves.
    c.read_file("/home/user/out.txt", no_wake=True)
    c.write_file("/home/user/in.txt", b"hello", no_wake=True)
    c.list_directory("/home/user")
    # Passive history (OPL-5026): signals and API activity.
    c.signals()
    c.signals("c-1", limit=10)
    c.activities()
    c.activities("chg-1", changes=True)
    c.activity(ACTIVITY_ID)
    c.activity_results(ACTIVITY_ID)
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
    # The streaming entry point the two calls above wait out for you. All three
    # are `POST computers/:id/agent`, so the route check sees one method here.
    with closing(c.agent_stream("do the thing", model_key="sk-ant-test")) as events:
        for _ in events:
            break
    client.computers.list(allow_partial=True)
    # The listing narrowed to one lifecycle state, and narrowed to a TERMINAL
    # one: `deleted` and `lost` are answered from the platform's record alone
    # and are the only rows this parameter is the sole way to see.
    client.computers.list(state="deleted")
    client.snapshots.list()
    client.snapshots.list(include_unfinished=True, allow_partial=True)
    client.snapshots.restore("snap-1")
    client.snapshots.clone("snap-1", name="from-snap")
    client.snapshots.clone("snap-1", memory=False, inherit_secrets=True)
    # Both waits on the deletion (OPL-4576). The default polls the listing for
    # the row's absence, so it is sent against an id the mock does not list —
    # SNAPSHOT is `snap-1`, and a deletion of that one would poll until its
    # half-hour deadline. `wait=False` is the escape, and neither sends a
    # parameter the other does not.
    client.snapshots.delete("snap-2")
    client.snapshots.delete("snap-1", wait=False)
    # The other half of the schedule: when they are taken is a computer's, how
    # long they are kept is the account's.
    client.snapshots.retention()
    # Account webhooks (platform OPL-3923, OPL-4300). Every field on the create
    # and, separately, on the update — the update sends only what is named, so
    # one call naming all five is what puts each of them on the wire.
    client.webhooks.list()
    client.webhooks.create("https://ci.example.com/mandala")
    client.webhooks.create(
        "https://ci.example.com/mandala",
        description="CI",
        events=["process.exited"],
        computers=["vm-1"],
        enabled=False,
    )
    client.webhooks.get("whk-2b7d4c809f3c1a7e")
    client.webhooks.update("whk-2b7d4c809f3c1a7e", enabled=True)
    client.webhooks.update(
        "whk-2b7d4c809f3c1a7e",
        url="https://ci.example.com/other",
        description="",
        events=[],
        computers=[],
        enabled=False,
    )
    client.webhooks.rotate("whk-2b7d4c809f3c1a7e")
    client.webhooks.test("whk-2b7d4c809f3c1a7e")
    client.webhooks.deliveries("whk-2b7d4c809f3c1a7e")
    client.webhooks.delete("whk-2b7d4c809f3c1a7e")
    # SSH access: the caller's keys, with and without a label, and one
    # computer's switch in both directions.
    client.ssh_keys.list()
    client.ssh_keys.add(SSH_KEY["public_key"])
    client.ssh_keys.add(SSH_KEY["public_key"], name="laptop")
    client.ssh_keys.remove(SSH_KEY["id"])
    c.ssh_access()
    c.set_ssh_access(True)
    c.set_ssh_access(False)
    # A computer's secret bindings, read and replaced against the version read.
    bound = c.secrets()
    c.set_secrets(
        [{"secret_id": "csec-0123456789abcdef", "env": "API_TOKEN"}], version=bound.version
    )
    # The account's secret store (OPL-4984): every field on every route, and the
    # create-or-replace convenience on top of the list, the create and the
    # replace.
    client.secrets.list()
    client.secrets.list(workspace_id="ws-1")
    client.secrets.get(SECRET["id"])
    client.secrets.get(SECRET["id"], workspace_id="ws-1")
    client.secrets.create("OPENAI_API_KEY", "sk-test")
    client.secrets.create("OPENAI_API_KEY", "sk-test", workspace_id="ws-1")
    client.secrets.replace(SECRET["id"], "sk-new", revision_id=SECRET["revision_id"])
    client.secrets.replace(
        SECRET["id"], "sk-new", revision_id=SECRET["revision_id"], workspace_id="ws-1"
    )
    client.secrets.set("OPENAI_API_KEY", "sk-new")
    client.secrets.delete(SECRET["id"], revision_id=SECRET["revision_id"])
    client.secrets.delete(SECRET["id"], revision_id=SECRET["revision_id"], workspace_id="ws-1")
    # Who the credential is, and the holder's keys (OPL-5053).
    client.account.whoami()
    client.api_keys.list()
    client.api_keys.create()
    client.api_keys.create(name="ci", workspace_id="wsp-1")
    client.api_keys.revoke(API_KEY["id"])
    c.delete(purge_snapshots=True, expect="fp-abc")
    c.delete()


async def exercise_everything_async(client: mc.AsyncClient) -> None:
    """The async mirror of exercise_everything."""
    await client.templates.list()
    await client.templates.schema()
    await client.templates.validate("apiVersion: mandala/v1")
    await client.templates.publish("apiVersion: mandala/v1")
    await client.templates.get("acc-1", "devbox")
    await client.templates.get("acc-1", "devbox", version="1.0.0")
    await client.templates.retire("acc-1", "devbox", version="1.0.0")
    await client.templates.retire("acc-1", "devbox")
    await client.builds.start("apiVersion: mandala/v1")
    await client.builds.start("apiVersion: mandala/v1", no_reuse=True)
    await client.builds.list()
    await client.builds.list(allow_partial=True)
    await client.builds.get("bld-1")
    await client.builds.progress("bld-1")
    await client.builds.wait("bld-1")
    async for _ in client.builds.events("bld-1"):
        break
    await client.sizes.list()
    await client.computers.list()
    await client.computers.get("vm-1")
    c = await client.computers.create(template="base")
    await client.computers.launch(template="base")
    await client.computers.create(
        name="dev",
        template="base",
        template_transfer="prepare-token",
        cpu=2,
        ram_mb=4096,
        disk_gb=40,
        resolution="1920x1080",
        start=False,
    )
    await client.computers.create(size="small")
    await client.computers.create(
        template="base",
        secrets=[
            {"secret_id": "csec-0123456789abcdef", "env": "API_TOKEN"},
            {"secret_id": "csec-0123456789abcde0", "file": "kubeconfig"},
        ],
    )
    async with client.computers.ephemeral(template="base"):
        pass
    await c.refresh()
    await c.wait_until_built()
    await c.wait_until_running()
    await c.wait_for_guest()
    await c.wait_for_secrets()
    await c.start()
    await c.start(resume_only=True)
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
    await c.paste("Café")
    await c.paste("ls", shift=True)
    await c.key("ctrl", "c")
    await c.hold_key("shift", seconds=1)
    await c.wait(1)
    await c.cursor_position()
    await c.exec("true")
    await c.exec("true", retain_output={"max_bytes_per_stream": 64, "retention_seconds": 60})
    await c.retain_execution_output(EXECUTION_ID, max_bytes_per_stream=64, retention_seconds=60)
    await c.result(RESULT_ID)
    await c.result_output(RESULT_ID, stream="stdout", offset=0, limit=64)
    await c.delete_result(RESULT_ID)
    await c.publish_artifact(
        "/tmp/a",
        expected_size=3,
        expected_sha256=artifact_manifest()["sha256"],
        execution_id=EXECUTION_ID,
        max_bytes=64,
        retention_seconds=60,
    )
    await c.artifact(ARTIFACT_ID)
    await c.download_artifact(ARTIFACT_ID, max_bytes=64)
    await c.delete_artifact(ARTIFACT_ID)
    await c.exec("true", cwd="/tmp", env={"CI": "1"})
    await c.exec("true", desktop=True)
    await c.open("https://example.com")
    job = await c.start_exec("sleep 60")
    await job.poll()
    await job.kill()
    await c.background_command(4242).poll()
    await c.execution("exec_0123456789abcdef0123456789abcdef")
    await c.execution_output(
        "exec_0123456789abcdef0123456789abcdef", stdout_offset=7, stderr_offset=13, limit=1024
    )
    await c.windows()
    await c.windows(include_all=True)
    await c.window_action("0x2600003", "focus")
    await c.window_action("0x2600003", "move", x=10, y=20)
    await c.window_action("0x2600003", "resize", width=800, height=600)

    await c.clipboard()
    await c.set_clipboard("on the clipboard")
    await c.resize(cpu=4, ram_mb=8192)
    await c.resize(disk_gb=64)
    await c.relocate(ram_mb=26000, cpu=2, disk_gb=64)
    await c.wait_for_move()
    await client.moves.list()
    await client.account.read()
    await client.usage.read()
    await client.usage.read(
        since=datetime(2026, 8, 1, tzinfo=timezone.utc), until="2026-08-22T00:00:00Z"
    )
    await c.set_idle_suspend(15)
    await c.set_idle_suspend(None)
    await c.read_file("/home/user/out.txt")
    await c.read_text_file("/home/user/out.txt")
    await c.read_file_part("/home/user/out.txt", offset=0, length=1024)
    await c.read_file_part("/var/log/build.log", offset=-4096)
    await c.download_file("/home/user/out.txt", io.BytesIO())
    await c.write_file("/home/user/in.txt", b"hello")
    await c.write_file("/home/user/new.txt", b"hello", overwrite=False)
    await c.read_file("/home/user/out.txt", no_wake=True)
    await c.write_file("/home/user/in.txt", b"hello", no_wake=True)
    await c.list_directory("/home/user")
    await c.signals()
    await c.signals("c-1", limit=10)
    await c.activities()
    await c.activities("chg-1", changes=True)
    await c.activity(ACTIVITY_ID)
    await c.activity_results(ACTIVITY_ID)
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
    async with aclosing(c.agent_stream("do the thing", model_key="sk-ant-test")) as events:
        async for _ in events:
            break
    await client.computers.list(allow_partial=True)
    await client.computers.list(state="deleted")
    await client.snapshots.list()
    await client.snapshots.list(include_unfinished=True, allow_partial=True)
    await client.snapshots.restore("snap-1")
    await client.snapshots.clone("snap-1", name="from-snap")
    await client.snapshots.clone("snap-1", memory=False, inherit_secrets=True)
    # See the sync exercise: the waiting delete is sent against an id the mock
    # does not list, and the other is the `wait=False` escape.
    await client.snapshots.delete("snap-2")
    await client.snapshots.delete("snap-1", wait=False)
    await client.snapshots.retention()
    await client.webhooks.list()
    await client.webhooks.create("https://ci.example.com/mandala")
    await client.webhooks.create(
        "https://ci.example.com/mandala",
        description="CI",
        events=["process.exited"],
        computers=["vm-1"],
        enabled=False,
    )
    await client.webhooks.get("whk-2b7d4c809f3c1a7e")
    await client.webhooks.update("whk-2b7d4c809f3c1a7e", enabled=True)
    await client.webhooks.update(
        "whk-2b7d4c809f3c1a7e",
        url="https://ci.example.com/other",
        description="",
        events=[],
        computers=[],
        enabled=False,
    )
    await client.webhooks.rotate("whk-2b7d4c809f3c1a7e")
    await client.webhooks.test("whk-2b7d4c809f3c1a7e")
    await client.webhooks.deliveries("whk-2b7d4c809f3c1a7e")
    await client.webhooks.delete("whk-2b7d4c809f3c1a7e")
    await client.ssh_keys.list()
    await client.ssh_keys.add(SSH_KEY["public_key"])
    await client.ssh_keys.add(SSH_KEY["public_key"], name="laptop")
    await client.ssh_keys.remove(SSH_KEY["id"])
    await c.ssh_access()
    await c.set_ssh_access(True)
    await c.set_ssh_access(False)
    bound = await c.secrets()
    await c.set_secrets(
        [{"secret_id": "csec-0123456789abcdef", "env": "API_TOKEN"}], version=bound.version
    )
    await client.secrets.list()
    await client.secrets.list(workspace_id="ws-1")
    await client.secrets.get(SECRET["id"])
    await client.secrets.get(SECRET["id"], workspace_id="ws-1")
    await client.secrets.create("OPENAI_API_KEY", "sk-test")
    await client.secrets.create("OPENAI_API_KEY", "sk-test", workspace_id="ws-1")
    await client.secrets.replace(SECRET["id"], "sk-new", revision_id=SECRET["revision_id"])
    await client.secrets.replace(
        SECRET["id"], "sk-new", revision_id=SECRET["revision_id"], workspace_id="ws-1"
    )
    await client.secrets.set("OPENAI_API_KEY", "sk-new")
    await client.secrets.delete(SECRET["id"], revision_id=SECRET["revision_id"])
    await client.secrets.delete(
        SECRET["id"], revision_id=SECRET["revision_id"], workspace_id="ws-1"
    )
    await client.account.whoami()
    await client.api_keys.list()
    await client.api_keys.create()
    await client.api_keys.create(name="ci", workspace_id="wsp-1")
    await client.api_keys.revoke(API_KEY["id"])
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


@respx.mock
def test_every_public_method_is_named_in_the_exercise(client: mc.Client) -> None:
    """The claim the route check above cannot make.

    ``ALLOWED - called_routes(...) == UNIMPLEMENTED`` proves every route was
    reached by SOMETHING. It says nothing about a method that shares a route
    with one already exercised, and on this surface most of them do: three
    methods are `POST computers/:id/exec`, three are `POST computers/:id/agent`,
    and every mouse and keyboard call is `POST computers/:id/input`. Eight
    methods had gone missing that way by the time this was written (OPL-3900).

    The inventory is derived from the source, so closing this is adding the call
    rather than editing a list — and a method added tomorrow is in the inventory
    before anyone thinks about coverage.
    """
    respx.route(host="api.test").mock(side_effect=api_handler)
    surface = half(SURFACE, asynchronous=False)
    with record_named_calls(surface, [exercise_everything]) as named:
        exercise_everything(client)

    missed = names(surface) - named
    assert not missed, (
        f"public methods the exercise never calls: {sorted(missed)}. Add them to "
        "exercise_everything() — sharing a route with a method that IS called is "
        "not coverage."
    )


@respx.mock
async def test_every_public_async_method_is_named_in_the_exercise(
    async_client: mc.AsyncClient,
) -> None:
    """The same claim for the other half.

    Its own test rather than a parity comparison, and that is the point:
    ``test_both_clients_hit_exactly_the_same_routes`` is satisfied by two halves
    that are missing the same method, which is exactly how a method goes missing
    — it is added to one half and mirrored into the other, and forgotten in both
    exercises at once.
    """
    respx.route(host="api.test").mock(side_effect=api_handler)
    surface = half(SURFACE, asynchronous=True)
    with record_named_calls(surface, [exercise_everything_async]) as named:
        await exercise_everything_async(async_client)

    missed = names(surface) - named
    assert not missed, (
        f"public async methods the exercise never calls: {sorted(missed)}. "
        "Add them to exercise_everything_async()."
    )


def test_the_two_halves_offer_the_same_methods() -> None:
    """Parity read off the source, before either exercise runs.

    The route and parameter parity tests next door compare what the two halves
    DO, which cannot see a method the async side never grew — that half simply
    makes no call, and a call nobody makes matches a call nobody makes.
    """

    def paired(surface: dict[type, frozenset[str]]) -> set[str]:
        return {
            f"{cls.__name__.removeprefix('Async')}.{method}"
            for cls, methods in surface.items()
            for method in methods
        }

    assert paired(half(SURFACE, asynchronous=True)) == paired(half(SURFACE, asynchronous=False))


@respx.mock
def test_a_method_sharing_a_reached_route_is_caught(client: mc.Client) -> None:
    """The regression test for OPL-3900, on a real pair rather than a mock one.

    ``Computer.open`` is sugar over ``Computer.exec``: same route, same verb,
    different method. So the two exercises below reach an IDENTICAL set of
    routes, and every route-shaped check in this file is equally happy with
    both — which is the hole. Only the inventory tells them apart.

    Without this, the new assertion above would be as unfalsifiable as the one
    it was written to shore up.
    """
    surface = half(SURFACE, asynchronous=False)

    def forgot_open(c: mc.Computer) -> None:
        c.exec("true")

    def called_open(c: mc.Computer) -> None:
        c.exec("true")
        c.open("https://example.com")

    route = respx.route(host="api.test").mock(side_effect=api_handler)
    with record_named_calls(surface, [forgot_open]) as forgetful:
        forgot_open(client.computers.get("vm-1"))
    forgetful_routes = called_routes(route.calls)

    respx.reset()
    route = respx.route(host="api.test").mock(side_effect=api_handler)
    with record_named_calls(surface, [called_open]) as complete:
        called_open(client.computers.get("vm-1"))

    assert called_routes(route.calls) == forgetful_routes, (
        "the two exercises must reach the same routes, or this proves nothing "
        "about a method that shares one"
    )
    assert "Computer.open" in names(surface)
    assert "Computer.open" not in forgetful
    assert "Computer.open" in complete


def test_pattern_for_treats_ids_as_ids() -> None:
    assert pattern_for("/computers/vm-1/start") == "computers/:id/start"
    assert pattern_for("/snapshots/snap-1/clone") == "snapshots/:id/clone"
    assert pattern_for("/computers/vm-1/snapshots") == "computers/:id/snapshots"
    # A computer whose id looks like a route segment is still an id.
    assert pattern_for("/computers/audit") == "computers/:id"
    # Every collection whose next segment is an id, including the ones this
    # client does not call yet: a mirrored route that cannot be matched is one
    # the first call to it would report as off the table.
    assert pattern_for("/builds/bld-1/events") == "builds/:id/events"
    assert pattern_for("/webhooks/whk-1/deliveries") == "webhooks/:id/deliveries"
    assert pattern_for("/ssh-keys/key-1") == "ssh-keys/:id"


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

    # Recognition is identity now (OPL-3901), so a checkout can be the platform
    # and still be missing the manifest this reads, or hold one this cannot
    # compare: that is the state the script reports at exit 1, and it fails here
    # with the same sentence rather than reading through it.
    try:
        manifest = check_surface.read_manifest(platform)
    except check_surface.ManifestError as error:
        pytest.fail(str(error))

    # Compared as sets rather than through main(), so a drift prints as the
    # routes that differ instead of as a non-zero exit code.
    assert check_surface.routes(manifest) == ALLOWED
    # And the same for what each of them takes. `Range` on the download is the
    # reason this half exists: a whole feature, on a route the line above was
    # already satisfied by.
    assert check_surface.parameter_drift(check_surface.parameters(manifest), PARAMETERS) == []
    # And the mirrored NUMBERS, which drift the same way and were the half this
    # test imported without ever calling: MAX_CLIPBOARD_BYTES can diverge from
    # the platform's clipboard limit through a green run of this suite.
    assert check_surface.constant_drift(manifest) == []


def test_allowlist_excludes_the_daemons_internal_routes() -> None:
    """The ops endpoints are not tenant API and never should be.

    The previous test proves the SDK stays inside ALLOWED; this proves ALLOWED
    itself stays honest, so widening it later is a deliberate act rather than a
    quiet one.

    ``retention`` WAS in this set and came out of it deliberately (OPL-3767,
    OPL-3783), which is the act this test exists to force. Two things had to be
    true first. The platform put ``GET retention`` on its public allowlist — so
    the READ is tenant API now, answered from the plan catalogue by the control
    plane rather than forwarded to a daemon at all. And the reason recorded here
    for withholding it was wrong besides: ``PUT /retention`` *is* owner-scoped,
    it sets the calling tenant's own policy. What keeps the WRITE off every
    surface is that the plan owns retention, so a tenant setting its own would
    be granting itself history it has not paid for — a different argument, and
    one a head-segment check cannot enforce, since it cannot tell a GET from a
    PUT. The test below is what holds that line.
    """
    internal = {"audit", "host", "fleet"}
    reachable = {pattern.split("/")[0] for _, pattern in ALLOWED}
    assert not (reachable & internal)


def test_retention_is_reachable_only_to_read() -> None:
    """What the head-segment check above can no longer say.

    The plan owns the window, so a write to it is a tenant granting itself a
    longer history than it pays for. The platform refuses one on both its
    surfaces; this is the mirror of that refusal, so a PUT could not be added to
    ALLOWED without deleting a test.
    """
    assert {method for method, pattern in ALLOWED if pattern == "retention"} == {"GET"}


def test_execution_path_placeholder_stays_at_the_identity_position() -> None:
    assert (
        pattern_for("/computers/vm-1/executions/exec_" + "a" * 32)
        == "computers/:id/executions/:executionId"
    )
    assert (
        pattern_for("/computers/vm-1/executions/exec_" + "a" * 32 + "/output")
        == "computers/:id/executions/:executionId/output"
    )
    assert pattern_for("/computers/executions/start") == "computers/:id/start"


def test_retained_path_placeholders_do_not_hide_literal_segments() -> None:
    assert (
        pattern_for(f"/computers/vm-1/results/{RESULT_ID}/output")
        == "computers/:id/results/:resultId/output"
    )
    assert (
        pattern_for(f"/computers/vm-1/artifacts/{ARTIFACT_ID}/download")
        == "computers/:id/artifacts/:artifactId/download"
    )
    assert pattern_for("/computers/results/start") == "computers/:id/start"
    assert pattern_for("/computers/artifacts/start") == "computers/:id/start"
    # An activity id is an id now that the SDK reaches the route (OPL-5026),
    # and `results` after it stays the literal the platform's pattern has.
    assert (
        pattern_for("/computers/vm-1/activities/act-1/results")
        == "computers/:id/activities/:activity/results"
    )
    # The account store's id is at position 1; a computer's bindings route is
    # a literal `secrets` at position 2 and must not become an id.
    assert pattern_for("/secrets/csec-1") == "secrets/:id"
    assert pattern_for("/computers/vm-1/secrets") == "computers/:id/secrets"
