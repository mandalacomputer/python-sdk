"""The platform surface mirrors, kept free of test-runner imports.

``scripts/check_surface.py`` compares these against the real tables upstream.
They used to live in ``tests/test_surface.py``, which imports pytest, httpx and
respx at module level — so the comparison path, the one that does real work,
could not run without the test extra. The test still owns UNIMPLEMENTED and the
exercise; this module is just the tables.
"""

from __future__ import annotations

# (method, pattern) with ids replaced by ":id" — mirrors surface.ts V1_ROUTES.
ALLOWED = {
    ("GET", "templates"),
    # The template document format (platform OPL-3568): the published JSON
    # Schema, and a check of a document against it that stores nothing.
    ("GET", "templates/schema"),
    ("POST", "templates/validate"),
    # The store (platform OPL-3789, OPL-3830): publish a document under a ref of
    # your own, read one back, retire one.
    ("POST", "templates"),
    ("GET", "templates/:namespace/:name"),
    ("DELETE", "templates/:namespace/:name"),
    # Compiling a document into an image (platform OPL-3791, OPL-3794). The job,
    # its record, and the two halves of watching it — a poll and a stream.
    ("POST", "builds"),
    ("GET", "builds"),
    ("GET", "builds/:id"),
    ("GET", "builds/:id/progress"),
    ("GET", "builds/:id/events"),
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
    # Taking up the offer a refused resize makes, and reading how it went.
    # `moves` is a collection rather than `computers/:id/move`, which is the
    # platform's decision: a per-computer read could not tell a computer with no
    # move from an id that does not exist.
    ("POST", "computers/:id/move"),
    ("GET", "moves"),
    ("GET", "computers/:id/screenshot"),
    ("POST", "computers/:id/input"),
    ("POST", "computers/:id/exec"),
    ("GET", "computers/:id/exec/:pid"),
    ("DELETE", "computers/:id/exec/:pid"),
    ("GET", "computers/:id/windows"),
    ("POST", "computers/:id/windows/:window"),
    # The desktop's clipboard (platform OPL-3743, OPL-3768). Session-only for
    # its first month; on v1 since the shape settled by not moving.
    ("GET", "computers/:id/clipboard"),
    ("PUT", "computers/:id/clipboard"),
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
    # What the account has used. Account-scoped like `moves`, and for a related
    # reason: the figures include computers that have since been deleted, which
    # is precisely the line an unexplained invoice is about.
    ("GET", "usage"),
    # How long the automatic snapshots a schedule takes are kept. Account-scoped
    # like `usage` and `moves`, and read-only on every surface: the plan owns
    # retention.
    ("GET", "retention"),
    # Account webhooks (platform OPL-3923, OPL-4300): eight local routes, all
    # answered by the control plane. A subscription is control-plane data the
    # way a template in the store is, so there is no daemon behind any of them.
    # The secret is answered by exactly two — the create and the rotate — and
    # never by a read.
    ("GET", "webhooks"),
    ("POST", "webhooks"),
    ("GET", "webhooks/:id"),
    ("PATCH", "webhooks/:id"),
    ("DELETE", "webhooks/:id"),
    ("POST", "webhooks/:id/rotate"),
    ("POST", "webhooks/:id/test"),
    ("GET", "webhooks/:id/deliveries"),
    # Reachable, and not reached from here — see UNIMPLEMENTED.
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
    # Neither takes a query parameter or a header. The validate route's body is
    # the document itself, raw rather than a JSON envelope with named fields —
    # so it contributes nothing here, the same way the file upload's does not.
    "GET templates/schema": set(),
    "POST templates/validate": set(),
    # The publish and the build take their document the same way, raw, so they
    # contribute no body fields either.
    "POST templates": set(),
    # `version` on both halves of the ref route, and the two mean different
    # things by omission: the newest on a read, every version on a retire. An
    # EMPTY one is refused by this SDK before it is sent — see
    # `_api.template_version_params`, and the platform defect it exists to be on
    # the right side of.
    "GET templates/:namespace/:name": {"query:version"},
    "DELETE templates/:namespace/:name": {"query:version"},
    "POST builds": {"query:no_reuse"},
    # The third fan-out listing, and the last to be able to say so: the platform
    # has answered 503 on this route since it started merging across the fleet,
    # and only documented the way out of it in OPL-3840.
    "GET builds": {"query:allow_partial"},
    "GET builds/:id": set(),
    "GET builds/:id/progress": set(),
    "GET builds/:id/events": set(),
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
    # The sizing group and nothing else. The platform reads only these three off
    # a move body and ignores the rest, so a name sent here would be dropped in
    # silence — which is why relocate() has no room for one.
    "POST computers/:id/move": {"body:cpu", "body:ram_mb", "body:disk_gb"},
    "GET moves": set(),
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
    "GET computers/:id/clipboard": set(),
    "PUT computers/:id/clipboard": {"body:text"},
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
    # Both bounds, and both optional: with neither, the platform answers over the
    # account's current billing period. Sent as `from`/`to` — the SDK spells them
    # `since`/`until` because `from` is a keyword.
    "GET usage": {"query:from", "query:to"},
    "GET retention": set(),
    # The same five fields on the create and the update. The update sends only
    # the ones a caller named, so the exercise names every one of them at least
    # once — a field only ever omitted would read here as never sent.
    "GET webhooks": set(),
    "POST webhooks": {
        "body:url",
        "body:description",
        "body:events",
        "body:computers",
        "body:enabled",
    },
    "GET webhooks/:id": set(),
    "PATCH webhooks/:id": {
        "body:url",
        "body:description",
        "body:events",
        "body:computers",
        "body:enabled",
    },
    "DELETE webhooks/:id": set(),
    "POST webhooks/:id/rotate": set(),
    "POST webhooks/:id/test": set(),
    "GET webhooks/:id/deliveries": set(),
}
