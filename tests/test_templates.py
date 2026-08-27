"""The template store and the builds that compile documents into images.

OPL-3835. The clients were three platform tickets behind here: the store
(OPL-3789), the retire (OPL-3830) and the builds (OPL-3791/3794) were all
reachable from an API key and from none of these SDKs. ``Templates`` had exactly
one method, ``list()``.

What is pinned below is the seam rather than the platform. The platform's own
tests own whether a publish stores anything; these own whether a caller can tell
a retired ref from a name that never existed, whether an unset version can widen
a retire from one version to all of them, and whether a wait reports a failed
build as an outcome rather than as an exception.

Both halves of the client, because both have the methods and
:mod:`tests.test_parity` only proves the signatures match — not that the async
one does the same thing with them.
"""

from __future__ import annotations

import json
import pathlib
import time
from unittest import mock

import httpx
import pytest
import respx

import mandala_computer as mc
import mandala_computer._models
from mandala_computer import _api

BASE = "https://api.test/api/v1"

PUBLISHED = {
    "ref": "acc-1/devbox@1.0.0",
    "doc_digest": "sha256:aaaa",
    "document": {"apiVersion": "mandala/v1", "kind": "Template"},
    "template": {
        "name": "devbox",
        "label": "My desktop",
        "os": "linux",
        "cpu": 2,
        "ram_mb": 4096,
        "disk_gb": 30,
    },
    "versions": ["1.0.0"],
    "published_at": "2026-08-26T12:00:00.000Z",
}

RETIRED = {
    "retired": ["acc-1/devbox@1.0.0"],
    "retired_at": "2026-08-26T13:00:00.000Z",
    "versions": [],
    "templates": 0,
    # Deliberately not 0 while `templates` is: a retired ref still counts, and a
    # fixture where the two agreed would let a decoder that read one field for
    # both pass every assertion below.
    "refs_claimed": 1,
}

DONE = {
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

RUNNING = {**DONE, "status": "running", "done": False, "phase": "copying"}

FAILED = {
    **DONE,
    "status": "failed",
    "phase": "failed",
    "error": "apt-get returned 100",
    "steps": [{"n": 1, "kind": "apt", "label": "nosuchpkg", "status": "failed"}],
}


def sse(*events: tuple[str, dict]) -> httpx.Response:
    body = "".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events)
    return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


@pytest.fixture
def async_client() -> mc.AsyncClient:
    return mc.AsyncClient("com_test", base_url=BASE)


# --- publishing -----------------------------------------------------------


@respx.mock
def test_publish_sends_the_document_as_bytes(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/templates").mock(return_value=httpx.Response(201, json=PUBLISHED))
    client.templates.publish("apiVersion: mandala/v1\nkind: Template")
    # The platform reads JSON or YAML off the body itself. An envelope would be a
    # document its validator never sees — and would parse as JSON, so the failure
    # would be a complaint about the WRAPPER's fields.
    assert route.calls.last.request.content == b"apiVersion: mandala/v1\nkind: Template"


@respx.mock
def test_publish_refuses_an_empty_document_without_a_round_trip(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/templates")
    with pytest.raises(ValueError):
        client.templates.publish("   ")
    assert not route.called


@respx.mock
def test_publish_reads_the_document_back_as_an_object(client: mc.Client) -> None:
    respx.post(f"{BASE}/templates").mock(return_value=httpx.Response(201, json=PUBLISHED))
    t = client.templates.publish("apiVersion: mandala/v1")
    assert t.ref == "acc-1/devbox@1.0.0"
    assert t.doc_digest == "sha256:aaaa"
    assert t.document["apiVersion"] == "mandala/v1"
    assert t.versions == ["1.0.0"]
    assert t.template.ram_mb == 4096


@respx.mock
def test_a_shipped_template_has_no_published_at(client: mc.Client) -> None:
    """None stays None rather than becoming "".

    A shipped template was not published by anybody — it is compiled into the
    daemon — and an empty timestamp reads as one that is known and blank rather
    than one that does not apply.
    """
    body = {k: v for k, v in PUBLISHED.items() if k != "published_at"}
    respx.get(f"{BASE}/templates/system/base").mock(return_value=httpx.Response(200, json=body))
    assert client.templates.get("system", "base").published_at is None


# --- checking -------------------------------------------------------------


@respx.mock
def test_validate_does_not_raise_for_an_invalid_document(client: mc.Client) -> None:
    """An invalid document is the ANSWER to the question this method asks.

    The platform says so with a 200. Raising would make the one method whose job
    is to report problems the one method you cannot use to see them.
    """
    respx.post(f"{BASE}/templates/validate").mock(
        return_value=httpx.Response(
            200,
            json={"valid": False, "problems": ["spec.os is required", "version is required"]},
        )
    )
    check = client.templates.validate("apiVersion: mandala/v1")
    assert check.valid is False
    assert len(check.problems) == 2
    assert check.ref is None


@respx.mock
def test_validate_carries_both_digests(client: mc.Client) -> None:
    respx.post(f"{BASE}/templates/validate").mock(
        return_value=httpx.Response(
            200,
            json={"valid": True, "doc_digest": "sha256:aaaa", "build_digest": "sha256:bbbb"},
        )
    )
    check = client.templates.validate("apiVersion: mandala/v1")
    assert check.doc_digest == "sha256:aaaa"
    assert check.build_digest == "sha256:bbbb"


# --- naming a version -----------------------------------------------------


@respx.mock
def test_no_version_omits_the_parameter(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/templates/acc-1/devbox").mock(
        return_value=httpx.Response(200, json=PUBLISHED)
    )
    client.templates.get("acc-1", "devbox")
    assert "version" not in route.calls.last.request.url.params


@respx.mock
def test_an_empty_version_is_refused_rather_than_sent(client: mc.Client) -> None:
    """The defect this exists to be on the right side of.

    ``?version=`` — what most clients serialise for an unset optional string —
    read as "no version was named" on the platform and retired an ENTIRE
    template, irreversibly. The platform answers 400 for it now; this SDK cannot
    send it at all, which is the stronger guarantee: omission and emptiness mean
    different things on a retire, and only one of them is recoverable.
    """
    route = respx.delete(f"{BASE}/templates/acc-1/devbox")
    with pytest.raises(ValueError):
        client.templates.retire("acc-1", "devbox", version="")
    assert not route.called


@respx.mock
@pytest.mark.parametrize("bad", ["1.0", "abc", "1.0.0.0", "01.0.0", "v1.0.0"])
def test_a_malformed_version_is_refused(client: mc.Client, bad: str) -> None:
    route = respx.get(f"{BASE}/templates/acc-1/devbox")
    with pytest.raises(ValueError):
        client.templates.get("acc-1", "devbox", version=bad)
    assert not route.called


@respx.mock
def test_a_well_formed_version_is_sent(client: mc.Client) -> None:
    route = respx.get(f"{BASE}/templates/acc-1/devbox").mock(
        return_value=httpx.Response(200, json=PUBLISHED)
    )
    client.templates.get("acc-1", "devbox", version="1.10.0")
    assert route.calls.last.request.url.params["version"] == "1.10.0"


@respx.mock
def test_the_ref_goes_in_the_path_as_two_segments(client: mc.Client) -> None:
    """The platform reduces ``templates/<a>/<b>`` to ``templates/:namespace/:name``.

    A ref handed over whole would be percent-encoded into a single segment and
    reach a route that does not exist — a 404 about a name, describing a URL.
    """
    route = respx.get(f"{BASE}/templates/acc-1/devbox").mock(
        return_value=httpx.Response(200, json=PUBLISHED)
    )
    client.templates.get("acc-1", "devbox", version="1.0.0")
    assert route.calls.last.request.url.path.endswith("/templates/acc-1/devbox")


# --- retiring -------------------------------------------------------------


@respx.mock
def test_retire_reports_what_went_and_both_counts(client: mc.Client) -> None:
    respx.delete(f"{BASE}/templates/acc-1/devbox").mock(
        return_value=httpx.Response(200, json=RETIRED)
    )
    gone = client.templates.retire("acc-1", "devbox")
    assert gone.retired == ["acc-1/devbox@1.0.0"]
    assert gone.versions == []
    assert gone.templates == 0
    # The two numbers move differently, and this is the one place a caller sees
    # it: retiring gave a template row back and gave no ref back.
    assert gone.refs_claimed == 1


@respx.mock
def test_a_retired_ref_keeps_the_platforms_sentence_about_when_it_went(
    client: mc.Client,
) -> None:
    """A retired ref is not an unknown ref, and the platform's 404 says which.

    A client that kept only the status would throw that away — and the date is
    the whole answer to "when did my script stop working".
    """
    respx.get(f"{BASE}/templates/acc-1/devbox").mock(
        return_value=httpx.Response(
            404,
            json={
                "error": (
                    "acc-1/devbox@1.0.0 was retired on 2026-08-26T13:00:00.000Z. A ref names "
                    "one document for ever, so this one cannot be published again."
                )
            },
        )
    )
    with pytest.raises(mc.NotFoundError, match="retired on 2026-08-26"):
        client.templates.get("acc-1", "devbox", version="1.0.0")


# --- builds ---------------------------------------------------------------


@respx.mock
def test_build_start_sends_bytes_and_reads_the_job(client: mc.Client) -> None:
    route = respx.post(f"{BASE}/builds").mock(
        return_value=httpx.Response(
            202,
            json={
                "id": "bld-1",
                "ref": "acc-1/devbox@1.0.0",
                "status": "running",
                "started_at": "2026-08-26T12:00:00.000Z",
            },
        )
    )
    build = client.builds.start("apiVersion: mandala/v1")
    assert route.calls.last.request.content == b"apiVersion: mandala/v1"
    assert build.id == "bld-1"
    assert build.finished_at is None


@respx.mock
def test_no_reuse_is_sent_only_when_asked_for(client: mc.Client) -> None:
    """``no_reuse=true`` is the only spelling the platform acts on.

    ``server/buildjob.go`` reads ``Get("no_reuse") == "true"`` and ``lib/apidoc``
    gives the parameter ``enum: ['true']``, so the key is omitted rather than
    sent as ``false``. This docstring used to say the platform read the key's
    PRESENCE and that ``no_reuse=false`` forced a rebuild — the claim the fix
    commit disproved and left pinned here, one edit away from being acted on.
    """
    route = respx.post(f"{BASE}/builds").mock(
        return_value=httpx.Response(202, json={"id": "bld-1", "status": "running"})
    )
    client.builds.start("apiVersion: mandala/v1")
    assert "no_reuse" not in route.calls[0].request.url.params
    client.builds.start("apiVersion: mandala/v1", no_reuse=True)
    assert route.calls[1].request.url.params["no_reuse"] == "true"


@respx.mock
def test_progress_reads_the_steps_in_order(client: mc.Client) -> None:
    respx.get(f"{BASE}/builds/bld-1/progress").mock(return_value=httpx.Response(200, json=DONE))
    p = client.builds.progress("bld-1")
    assert p.done is True
    assert [s.kind for s in p.steps] == ["apt", "finish"]
    assert p.steps[0].status == "done"


@respx.mock
def test_events_yields_every_progress_and_stops_after_done(client: mc.Client) -> None:
    respx.get(f"{BASE}/builds/bld-1/events").mock(
        return_value=sse(("progress", RUNNING), ("done", DONE))
    )
    assert [p.status for p in client.builds.events("bld-1")] == ["running", "succeeded"]


@respx.mock
def test_a_stream_error_says_it_is_not_the_build(client: mc.Client) -> None:
    """An ``error`` event is the STREAM failing, not the build.

    A caller told "the build failed" would go and read a document that is fine,
    so this names what actually happened and points at the poll that can still
    answer.
    """
    respx.get(f"{BASE}/builds/bld-1/events").mock(
        return_value=sse(("error", {"error": "host went away"}))
    )
    with pytest.raises(mc.MandalaError, match="says nothing about the build itself"):
        list(client.builds.events("bld-1"))


@respx.mock
def test_wait_returns_when_the_build_is_done(client: mc.Client) -> None:
    respx.get(f"{BASE}/builds/bld-1/progress").mock(return_value=httpx.Response(200, json=DONE))
    assert client.builds.wait("bld-1", poll=0.01).status == "succeeded"


@respx.mock
def test_wait_does_not_raise_for_a_build_that_failed(client: mc.Client) -> None:
    """The rule the move work established.

    ``succeeded`` and ``failed`` are two situations with two remedies — one has
    an image, the other has a step to fix — and an exception flattens them into
    "something went wrong".
    """
    respx.get(f"{BASE}/builds/bld-1/progress").mock(return_value=httpx.Response(200, json=FAILED))
    out = client.builds.wait("bld-1", poll=0.01)
    assert out.status == "failed"
    assert out.error == "apt-get returned 100"
    assert out.steps[0].status == "failed"


@respx.mock
def test_wait_times_out_without_stopping_the_build(client: mc.Client) -> None:
    respx.get(f"{BASE}/builds/bld-1/progress").mock(return_value=httpx.Response(200, json=RUNNING))
    with pytest.raises(mc.TimeoutError, match="only this wait has"):
        client.builds.wait("bld-1", timeout=0.05, poll=0.01)


@respx.mock
def test_wait_gives_up_at_once_on_a_failure_polling_cannot_fix(client: mc.Client) -> None:
    """A build that does not exist is not going to start existing.

    A wait against a bad id must not spend its whole timeout discovering that.
    """
    route = respx.get(f"{BASE}/builds/bld-nope/progress").mock(
        return_value=httpx.Response(404, json={"error": "no such build"})
    )
    with pytest.raises(mc.NotFoundError):
        client.builds.wait("bld-nope", timeout=5.0, poll=0.01)
    assert route.call_count == 1


@respx.mock
def test_wait_keeps_polling_through_a_failure_that_might_clear(client: mc.Client) -> None:
    respx.get(f"{BASE}/builds/bld-1/progress").mock(
        side_effect=[
            httpx.Response(503, json={"error": "no hypervisor could answer"}),
            httpx.Response(200, json=DONE),
        ]
    )
    assert client.builds.wait("bld-1", poll=0.01).status == "succeeded"


# --- the async half does the same thing ------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_async_retire_reports_the_same_counts(async_client: mc.AsyncClient) -> None:
    respx.delete(f"{BASE}/templates/acc-1/devbox").mock(
        return_value=httpx.Response(200, json=RETIRED)
    )
    async with async_client as c:
        gone = await c.templates.retire("acc-1", "devbox")
    assert gone.retired == ["acc-1/devbox@1.0.0"]
    assert gone.refs_claimed == 1


@respx.mock
@pytest.mark.asyncio
async def test_async_refuses_an_empty_version_too(async_client: mc.AsyncClient) -> None:
    route = respx.delete(f"{BASE}/templates/acc-1/devbox")
    async with async_client as c:
        with pytest.raises(ValueError):
            await c.templates.retire("acc-1", "devbox", version="")
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_async_wait_does_not_raise_for_a_failed_build(
    async_client: mc.AsyncClient,
) -> None:
    respx.get(f"{BASE}/builds/bld-1/progress").mock(return_value=httpx.Response(200, json=FAILED))
    async with async_client as c:
        out = await c.builds.wait("bld-1", poll=0.01)
    assert out.status == "failed"
    assert out.error == "apt-get returned 100"


@respx.mock
@pytest.mark.asyncio
async def test_async_events_stop_after_done(async_client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/builds/bld-1/events").mock(
        return_value=sse(("progress", RUNNING), ("done", DONE))
    )
    async with async_client as c:
        seen = [p.status async for p in c.builds.events("bld-1")]
    assert seen == ["running", "succeeded"]


# --- what an adversarial review found (OPL-3835) --------------------------


class Shifty(str):
    """A ``str`` subclass that answers differently the second time it is asked.

    Not exotic: it is the shape of every guard that validates a value and then
    hands the ORIGINAL on to something that stringifies or encodes it again.
    """

    def __str__(self) -> str:
        return ""

    def encode(self, *a: object, **k: object) -> bytes:
        return b".."


@respx.mock
def test_a_str_subclass_cannot_smuggle_an_empty_version(client: mc.Client) -> None:
    """The critical one, and it is the irreversible branch.

    ``template_version_params`` matched the regex against the buffer and returned
    the object; httpx serialises a query value with ``str(value)``. A subclass
    holding ``1.2.3`` whose ``__str__`` answers ``""`` therefore passed the check
    and sent ``?version=`` — which on a retire means every version of the name.
    """
    route = respx.delete(f"{BASE}/templates/acc-1/devbox").mock(
        return_value=httpx.Response(200, json=RETIRED)
    )
    client.templates.retire("acc-1", "devbox", version=Shifty("1.2.3"))
    # Canonicalised, so the value httpx sends is the one the regex approved.
    assert route.calls.last.request.url.params["version"] == "1.2.3"


@respx.mock
def test_a_str_subclass_cannot_smuggle_a_dot_segment(client: mc.Client) -> None:
    """The same hole in ``seg``, which is older than this branch.

    ``quote()`` calls the value's own ``encode()``, so a subclass holding ``x``
    that encodes as ``b".."`` passed the dot-segment guard and became a path the
    client normalises into a different route.
    """
    route = respx.get(f"{BASE}/templates/acc-1/x").mock(
        return_value=httpx.Response(200, json=PUBLISHED)
    )
    client.templates.get("acc-1", Shifty("x"))
    assert route.calls.last.request.url.path.endswith("/templates/acc-1/x")


def test_a_non_string_is_refused_rather_than_coerced() -> None:
    # str(None) is "None", which is a plausible id and a nonsense one.
    for bad in (None, 1, {}, [], True):
        with pytest.raises(ValueError):
            _api.canonical(bad, "id")


@respx.mock
def test_wait_gives_up_at_once_on_a_failure_that_is_not_transient(client: mc.Client) -> None:
    """A 400 is a defect, not a phase.

    The first version re-raised a short list of permanent classes and swallowed
    everything else, so this burned the full 1800-second default before
    surfacing as a misleading timeout.
    """
    route = respx.get(f"{BASE}/builds/bld-1/progress").mock(
        return_value=httpx.Response(400, json={"error": "that is not a build id"})
    )
    with pytest.raises(mc.APIError):
        client.builds.wait("bld-1", timeout=5.0, poll=0.01)
    assert route.call_count == 1


@respx.mock
def test_wait_bounds_each_poll_by_what_is_left_of_the_deadline(client: mc.Client) -> None:
    """The deadline was only checked AFTER the request returned.

    So a one-second wait could sit in a single request for the client's default
    sixty — and for ever against a caller-supplied client with no timeout. The
    same cap ``Computer.wait_until_built`` passes to its refresh.
    """
    seen: list[float | None] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions.get("timeout", {}).get("read"))
        return httpx.Response(200, json=RUNNING)

    respx.get(f"{BASE}/builds/bld-1/progress").mock(side_effect=record)
    with pytest.raises(mc.TimeoutError):
        client.builds.wait("bld-1", timeout=0.25, poll=0.01)
    assert seen, "no poll was made"
    assert all(t is not None and t <= 0.25 for t in seen), seen


@respx.mock
def test_the_timeout_does_not_quote_a_stale_reading_in_the_present_tense(
    client: mc.Client,
) -> None:
    respx.get(f"{BASE}/builds/bld-1/progress").mock(
        side_effect=[
            httpx.Response(200, json=RUNNING),
            *[httpx.Response(503, json={"error": "no hypervisor could answer"})] * 20,
        ]
    )
    with pytest.raises(mc.TimeoutError, match="could not be reached"):
        client.builds.wait("bld-1", timeout=0.2, poll=0.01)


@respx.mock
def test_a_stream_that_ends_without_a_done_is_not_a_completed_build(
    client: mc.Client,
) -> None:
    """Returning normally made a truncated stream indistinguishable from a
    finished one, so a caller looping over it reported a build it had stopped
    watching as a build that ended."""
    respx.get(f"{BASE}/builds/bld-1/events").mock(return_value=sse(("progress", RUNNING)))
    with pytest.raises(mc.MandalaError, match="ended without a final event"):
        list(client.builds.events("bld-1"))


@respx.mock
def test_a_malformed_done_is_a_protocol_error_not_a_skipped_event(client: mc.Client) -> None:
    """Skipping it left the loop waiting on a connection the platform had
    finished with — holding one of the account's eight stream slots."""
    body = 'event: done\ndata: "not a record"\n\n'
    respx.get(f"{BASE}/builds/bld-1/events").mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Type": "text/event-stream"}
        )
    )
    with pytest.raises(mc.MandalaError, match="malformed final event"):
        list(client.builds.events("bld-1"))


@pytest.mark.parametrize(
    ("field", "value"),
    [("versions", "1.2.3"), ("versions", 7), ("versions", {"a": 1})],
)
def test_a_list_field_that_is_not_a_list_degrades_rather_than_raising(
    field: str, value: object
) -> None:
    """``d.get("x") or []`` guards None and nothing else.

    A number raised a bare TypeError out of a public method; a string iterated by
    character, so ``"1.2.3"`` decoded to ``['1', '.', '2', '.', '3']``. This
    module's contract is that malformed fields are preserved in ``raw`` and never
    rejected.
    """
    t = mc.PublishedTemplate.from_api({field: value})
    assert t.versions == []
    assert t.raw[field] == value


def test_the_other_list_fields_degrade_too() -> None:
    assert mc.RetiredTemplates.from_api({"retired": 7}).retired == []
    assert mc.TemplateCheck.from_api({"problems": 7}).problems == []
    assert mc.BuildProgress.from_api({"steps": "nope"}).steps == []


# --- and the async half, which has the same two loops ----------------------


@respx.mock
@pytest.mark.asyncio
async def test_async_wait_gives_up_at_once_on_a_non_transient_failure(
    async_client: mc.AsyncClient,
) -> None:
    route = respx.get(f"{BASE}/builds/bld-1/progress").mock(
        return_value=httpx.Response(400, json={"error": "that is not a build id"})
    )
    async with async_client as c:
        with pytest.raises(mc.APIError):
            await c.builds.wait("bld-1", timeout=5.0, poll=0.01)
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_async_stream_without_a_done_raises(async_client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/builds/bld-1/events").mock(return_value=sse(("progress", RUNNING)))
    async with async_client as c:
        with pytest.raises(mc.MandalaError, match="ended without a final event"):
            [p async for p in c.builds.events("bld-1")]


@respx.mock
def test_a_short_build_listing_arrives_as_a_refusal(client: mc.Client) -> None:
    """It never reaches a caller as a short list, and that is the point.

    lib/hvproxy does set X-GC-Incomplete on a short build listing, but
    ``forward`` in lib/surface applies its strict-inventory check to every v1
    route generically — so the response becomes a 503 before any client sees it.
    A previous version of this method believed the opposite and returned a
    Listing to carry a flag that cannot arrive.
    """
    respx.get(f"{BASE}/builds").mock(
        return_value=httpx.Response(
            503,
            json={
                "error": (
                    "Right now a hypervisor cannot be reached, so this list would be "
                    "incomplete. Retry, or pass allow_partial=1 to accept a partial answer."
                )
            },
            headers={"X-GC-Incomplete": "0"},
        )
    )
    with pytest.raises(mc.UnavailableError, match="would be incomplete"):
        client.builds.list()


@respx.mock
def test_a_complete_build_listing_is_an_ordinary_list(client: mc.Client) -> None:
    respx.get(f"{BASE}/builds").mock(
        return_value=httpx.Response(200, json=[{"id": "bld-1", "status": "running"}])
    )
    builds = client.builds.list()
    assert len(builds) == 1
    assert builds[0].id == "bld-1"


# --- what /code-review found on top of the adversarial pass ----------------


class Sneaky(str):
    """A ``str`` subclass whose ``strip()`` lies about being empty."""

    def strip(self, *a: object) -> str:
        return "x"


def test_the_document_guard_canonicalises_before_it_checks() -> None:
    """The one guard the previous pass said it had fixed and had not.

    ``seg`` and ``template_version_params`` canonicalise first; this one checked
    ``strip()`` on the caller's object and canonicalised afterwards, so a
    subclass overriding ``strip`` passed the emptiness check and encoded to
    nothing — an empty body on the wire, under a comment claiming otherwise.
    """
    with pytest.raises(ValueError):
        _api.template_document(Sneaky(""))


def test_a_trailing_newline_is_not_a_version() -> None:
    """Python's ``$`` also matches just before a trailing newline.

    So ``"1.0.0\n"`` satisfied the anchored pattern and went out as
    ``?version=1.0.0%0A``. The platform's grammar is a JavaScript regex where
    ``$`` is end-of-input, so it answers 400 — the exact round trip this guard
    exists to save.
    """
    with pytest.raises(ValueError):
        _api.template_version_params("1.0.0\n")
    assert _api.template_version_params("1.0.0") == {"version": "1.0.0"}


@pytest.mark.parametrize(
    ("err", "retried"),
    [
        (mc.ConflictError("busy", status=409), True),
        (mc.RateLimitError("slow down", status=429), True),
        (mc.UnavailableError("no host", status=503), True),
        (mc.GatewayTimeoutError("gateway", status=504), True),
        (mc.OriginUnreachableError("origin", status=523), True),
        (mc.OriginResponseError("origin", status=520), True),
        (mc.TimeoutError("poll ran long"), True),
        (mc.MandalaError("body did not parse"), True),
        (mc.APIError("bad request", status=400), False),
        (mc.NotFoundError("no such build", status=404), False),
        (mc.PlanLimitError("no", status=402), False),
        (mc.OriginTLSError("cert", status=525), False),
    ],
)
def test_the_retry_policy_keeps_the_passing_failures_and_drops_the_decisions(
    err: BaseException, retried: bool
) -> None:
    """A 4xx is a request refused on its merits; a 5xx is a passing outage.

    The allow-list this replaced retried only three classes, so a 504 or a poll
    that ran past its own cap ended a fourteen-minute wait — and
    OriginUnreachableError's own docstring calls itself a passing outage.
    """
    from mandala_computer._resources import is_transient

    assert is_transient(err) is retried


def test_a_rate_limit_is_retried_no_sooner_than_it_asked() -> None:
    """A 429 retried on a fixed five-second poll is the loop that caused it."""
    from mandala_computer._resources import retry_delay

    assert retry_delay(5.0, mc.RateLimitError("slow", status=429, retry_after=30.0)) == 30.0
    assert retry_delay(5.0, mc.RateLimitError("slow", status=429)) == 5.0
    assert retry_delay(5.0, mc.UnavailableError("away", status=503)) == 5.0


@respx.mock
def test_wait_rides_out_a_gateway_timeout(client: mc.Client) -> None:
    respx.get(f"{BASE}/builds/bld-1/progress").mock(
        side_effect=[
            httpx.Response(504, json={"error": "gateway timeout"}),
            httpx.Response(200, json=DONE),
        ]
    )
    assert client.builds.wait("bld-1", poll=0.01).status == "succeeded"


def test_a_template_row_carries_its_ref() -> None:
    """Since OPL-3789 a published template is named by its ref and nothing else.

    A listing that drops it cannot tell a caller how to launch their own
    template, which is what the platform's publicTemplate publishes it for.
    """
    t = mc.Template.from_api({"name": "devbox", "ref": "acc-1/devbox@1.0.0"})
    assert t.ref == "acc-1/devbox@1.0.0"
    # Absent stays absent: a host too old to advertise refs sends none.
    assert mc.Template.from_api({"name": "base"}).ref is None


# --- what the second review pass found -------------------------------------


class Disarming(str):
    """A ``str`` subclass that is truthy, absolute, and empty on the wire."""

    def __bool__(self) -> bool:
        return True

    def __str__(self) -> str:
        return ""

    def startswith(self, *a: object, **k: object) -> bool:
        return True


def test_the_purge_interlock_cannot_be_disarmed_by_a_str_subclass() -> None:
    """``canonical()`` reached three guards, not "every guard in _api.py".

    ``delete_params`` still tested ``if not expect`` on the caller's object and
    sent that object, so a subclass answering True here and "" to ``str()`` put
    ``?expect=`` on the wire — and ``checkExpectation`` in server/vm.go reads an
    empty expectation as NO expectation. The interlock was silently disarmed on
    the one route that destroys a computer and its snapshots together.
    """
    sent = _api.delete_params(purge_snapshots=True, expect=Disarming("abc"))
    assert sent is not None
    # Canonicalised, so what httpx sends is the fingerprint the check agreed to
    # rather than whatever __str__ felt like answering the second time.
    assert type(sent["expect"]) is str
    assert str(sent["expect"]) == "abc"
    # And one that is genuinely empty is still refused.
    with pytest.raises(ValueError, match="fingerprint"):
        _api.delete_params(purge_snapshots=True, expect=Disarming(""))


def test_a_guest_path_cannot_be_emptied_after_the_absoluteness_check() -> None:
    sent = _api.files_params(Disarming("/home/user/a.txt"))
    assert type(sent["path"]) is str
    assert str(sent["path"]) == "/home/user/a.txt"
    # A relative path that only CLAIMS to be absolute is refused on its real
    # contents, not on what startswith answered.
    with pytest.raises(ValueError, match="absolute"):
        _api.files_params(Disarming("home/user/a.txt"))


def test_a_retry_after_does_not_set_the_pace_for_the_rest_of_the_wait() -> None:
    """The ratchet.

    ``poll = retry_delay(poll, err)`` overwrote the loop's own interval, so one
    429 with ``Retry-After: 30`` turned a five-second poll into a thirty-second
    one for every later iteration. The TypeScript twin keeps ``pollMs``
    immutable and recomputes, giving [30, 5, 5] where this gave [30, 30, 30].
    """
    from mandala_computer._resources import retry_delay

    poll = 5.0
    slow = mc.RateLimitError("slow down", status=429, retry_after=30.0)
    away = mc.UnavailableError("no host", status=503)
    assert [retry_delay(poll, slow), retry_delay(poll, away), retry_delay(poll, away)] == [
        30.0,
        5.0,
        5.0,
    ]


@respx.mock
def test_wait_sleeps_the_retry_after_once_and_then_returns_to_its_poll(
    client: mc.Client,
) -> None:
    """The loop-level half of the same thing: `delay` is reset every iteration."""
    slept: list[float] = []
    respx.get(f"{BASE}/builds/bld-1/progress").mock(
        side_effect=[
            httpx.Response(429, json={"error": "slow down"}, headers={"Retry-After": "0.05"}),
            httpx.Response(503, json={"error": "no host"}),
            httpx.Response(200, json=DONE),
        ]
    )
    real_sleep = time.sleep

    def record(seconds: float) -> None:
        slept.append(seconds)
        real_sleep(0)

    with mock.patch("mandala_computer._resources.time.sleep", record):
        assert client.builds.wait("bld-1", poll=0.01).status == "succeeded"
    assert slept[0] > slept[1], slept


def test_408_is_repeatable_by_definition() -> None:
    """RFC 9110 defines it as a request the client may repeat unchanged.

    Cloudflare fronts this surface and emits it; it is the edge saying it waited
    long enough, not a decision about anything.
    """
    from mandala_computer._resources import is_transient

    assert is_transient(mc.APIError("request timeout", status=408)) is True
    assert is_transient(mc.APIError("bad request", status=400)) is False


def test_a_malformed_body_is_transient_and_the_docstring_says_so() -> None:
    """Deliberate, and the reversal is named rather than left as a surprise.

    A bare MandalaError is what a dropped connection and a captive portal both
    raise. The comment that used to sit over the catch claimed the opposite.
    """
    from mandala_computer._resources import is_transient

    assert is_transient(mc.MandalaError("expected JSON, got <html>")) is True
    assert "TRANSIENT" in (is_transient.__doc__ or "")


# The adversarial-review pass on the branch (OPL-3835). Every one of these was
# a live defect in the commits above, and every one of them fails OPEN — the
# wrong reading is the permissive, expensive or destructive one.


@pytest.mark.parametrize("value", ["false", "0", 0, 1, "", None, [], object()])
def test_no_reuse_refuses_anything_that_is_not_a_bool(value: object) -> None:
    """``build_params("false")`` asked for a rebuild.

    Truthiness on a flag that ARMS something reads every non-empty string as
    yes, and ``"false"`` is what a config file, an environment variable or a CLI
    argument produces. A rebuild copies a multi-gigabyte base image.
    """
    with pytest.raises(ValueError, match="no_reuse must be True or False"):
        _api.build_params(value)  # type: ignore[arg-type]


def test_no_reuse_still_takes_real_bools() -> None:
    assert _api.build_params(True) == {"no_reuse": "true"}
    assert _api.build_params(False) == {}


@pytest.mark.parametrize("value", ["false", "0", 0, 1, "", None])
def test_purging_snapshots_refuses_anything_that_is_not_a_bool(value: object) -> None:
    """The same coercion on the one route that destroys a computer AND its
    snapshots. The ``expect`` fingerprint did not cover this: it binds the purge
    to the set that was looked at, not to whether a purge was meant at all."""
    with pytest.raises(ValueError, match="purge_snapshots must be True or False"):
        _api.delete_params(purge_snapshots=value, expect="sha256:abc")  # type: ignore[arg-type]


def test_purging_snapshots_still_takes_real_bools() -> None:
    assert _api.delete_params(purge_snapshots=False, expect=None) is None
    assert _api.delete_params(purge_snapshots=True, expect="sha256:abc") == {
        "snapshots": "delete",
        "expect": "sha256:abc",
    }


#: Values no decoder can read. ``0``/``1`` and ``"true"``/``"false"`` are NOT
#: here: those are unambiguous and are decoded, which is what keeps a backend
#: that stringifies its booleans from getting the cautious answer on every flag
#: of every response, for ever (/code-review, OPL-3835).
UNREADABLE = ["maybe", "", 2, -1, [], {}, 1.5]


@pytest.mark.parametrize("value", UNREADABLE)
def test_a_value_no_decoder_can_read_gets_its_field_s_cautious_answer(value: object) -> None:
    """``valid`` decides whether a document is publishable and ``done`` ends a
    wait, so for those the cautious answer is False. ``unmatched`` is the other
    direction: False would say the per-step positions are trustworthy, which is
    the thing we cannot tell. The value survives in ``raw`` either way."""
    check = mc.TemplateCheck.from_api({"valid": value})
    assert check.valid is False
    assert check.raw["valid"] == value

    progress = mc.BuildProgress.from_api({"done": value, "unmatched": value})
    assert progress.done is False
    assert progress.unmatched is True


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("false", False),
        ("TRUE", True),
        ("False", False),
        (" true ", True),
    ],
)
def test_the_unambiguous_encodings_are_decoded_not_refused(wire: object, expected: bool) -> None:
    """The original bug was TRUTHINESS, not recognition.

    ``bool("false")`` was True and that is wrong, but ``"false"`` still plainly
    means false. Routing it to the cautious answer instead would give a backend
    that encodes booleans this way the wrong value on every flag of every
    response — permanently — and with ``timed_out`` cautious that means
    ``ExecResult.ok`` is never true and ``wait_for_guest`` always times out.
    """
    assert mc.ExecResult.from_api({"exit_code": 0, "timed_out": wire}).timed_out is expected
    assert mc.Move.from_api({"live": wire}).live is expected
    assert mc.TemplateCheck.from_api({"valid": wire}).valid is expected


def test_null_reads_false_with_absent_because_this_api_means_not_applicable() -> None:
    """Two reviews disagreed about ``null`` and the codebase settles it.

    ``cpu``, ``ram_mb``, ``disk_gb``, ``finished_at`` and ``exit_code`` all use
    ``null`` for NOT APPLICABLE, so ``live: null`` on a finished move means not
    live. Reading it as "the host cannot tell" made ``wait_for_move`` poll a
    finished move for its full fifteen minutes and then raise, and made
    ``timed_out: null`` burn ``wait_for_guest``'s three minutes on a guest that
    answered on the first probe (/code-review, OPL-3835).
    """
    assert mc.Move.from_api({}).live is False, "absent: an older host"
    assert mc.Move.from_api({"live": None}).live is False, "null: not applicable"
    assert mc.Move.from_api({"live": "maybe"}).live is True, "unreadable: the cautious answer"
    assert mc.Move.from_api({"live": False}).live is False, "a real boolean is itself"

    assert mc.ExecResult.from_api({"exit_code": 0, "timed_out": None}).ok is True
    assert mc.Move.from_api({"state": "done", "live": None}).live is False


def test_real_booleans_still_decode() -> None:
    assert mc.TemplateCheck.from_api({"valid": True}).valid is True
    assert mc.BuildProgress.from_api({"done": True, "unmatched": True}).done is True
    assert mc.BuildProgress.from_api({"done": True, "unmatched": True}).unmatched is True


@respx.mock
def test_a_done_that_says_the_build_is_still_running_is_malformed(client: mc.Client) -> None:
    """Checking the payload's SHAPE left its semantics unchecked.

    ``event: done`` carrying ``{"status": "running", "done": false}`` was yielded
    as ordinary progress and then ended the iterator normally — the truncated
    stream arriving through the front door.
    """
    respx.get(f"{BASE}/builds/bld-1/events").mock(
        return_value=sse(("progress", RUNNING), ("done", RUNNING))
    )
    with pytest.raises(mc.MandalaError, match="malformed final event"):
        list(client.builds.events("bld-1"))


@respx.mock
def test_a_done_naming_a_terminal_status_without_the_flag_is_accepted(
    client: mc.Client,
) -> None:
    """The fallback is for a host too old to send the flag at all.

    ``done`` ABSENT and a terminal status is believed; the platform derives the
    flag from the job and the status from the phase, and a host that sends only
    one is still saying the build is over.
    """
    payload = {k: v for k, v in DONE.items() if k != "done"}
    respx.get(f"{BASE}/builds/bld-1/events").mock(return_value=sse(("done", payload)))
    assert [p.status for p in client.builds.events("bld-1")] == ["succeeded"]


@respx.mock
def test_a_present_done_beats_the_status_it_disagrees_with(client: mc.Client) -> None:
    """A contradiction resolves in favour of ``done``, not the status.

    The platform derives ``done`` from the JOB and the status from the phase,
    and the phase comes out of a log the document's own steps write into. So a
    present flag is authoritative and the status is a fallback for a host that
    sent no flag — not a second opinion about one that did.
    """
    respx.get(f"{BASE}/builds/bld-1/events").mock(
        return_value=sse(("done", {**DONE, "done": False}))
    )
    with pytest.raises(mc.MandalaError, match="malformed final event"):
        list(client.builds.events("bld-1"))


@respx.mock
def test_wait_ends_on_the_same_rule_events_does(client: mc.Client) -> None:
    """The two used different rules, which is how the same payload ended one and
    left the other polling to its deadline."""
    payload = {k: v for k, v in DONE.items() if k != "done"}
    respx.get(f"{BASE}/builds/bld-1/progress").mock(return_value=httpx.Response(200, json=payload))
    assert client.builds.wait("bld-1", timeout=5.0, poll=0.01).status == "succeeded"


@pytest.mark.asyncio
@respx.mock
async def test_the_async_wait_ends_on_it_too(async_client: mc.AsyncClient) -> None:
    payload = {k: v for k, v in DONE.items() if k != "done"}
    respx.get(f"{BASE}/builds/bld-1/progress").mock(return_value=httpx.Response(200, json=payload))
    p = await async_client.builds.wait("bld-1", timeout=5.0, poll=0.01)
    assert p.status == "succeeded"


@pytest.mark.asyncio
@respx.mock
async def test_the_async_half_rejects_the_same_done(async_client: mc.AsyncClient) -> None:
    respx.get(f"{BASE}/builds/bld-1/events").mock(
        return_value=sse(("progress", RUNNING), ("done", RUNNING))
    )
    with pytest.raises(mc.MandalaError, match="malformed final event"):
        [p async for p in async_client.builds.events("bld-1")]


@pytest.mark.parametrize("status", [301, 302, 307, 308, 303])
def test_a_redirect_is_a_decision_about_the_url_not_a_passing_failure(status: int) -> None:
    """The rule spelled as "everything that is not 4xx" swept in 3xx.

    httpx is left on its default of not following redirects and every non-2xx
    becomes an APIError, so a moved endpoint or a base_url missing its path was
    retried until the wait's own deadline — half an hour, ending in a
    TimeoutError naming nothing about the redirect that caused it.
    """
    from mandala_computer._resources import is_transient

    assert is_transient(mc.APIError("moved", status=status)) is False


def test_5xx_is_still_transient() -> None:
    from mandala_computer._resources import is_transient

    assert is_transient(mc.APIError("bad gateway", status=502)) is True
    assert is_transient(mc.APIError("server error", status=500)) is True


@pytest.mark.parametrize(
    ("timeout", "poll", "what"),
    [
        (1.0, -1, "poll"),
        (1.0, float("nan"), "poll"),
        (1.0, float("inf"), "poll"),
        (float("nan"), 5.0, "timeout"),
        (float("inf"), 5.0, "timeout"),
        (-1.0, 5.0, "timeout"),
        (True, 5.0, "timeout"),
        ("30", 5.0, "timeout"),
    ],
)
def test_wait_refuses_a_timeout_or_poll_it_cannot_honour(
    client: mc.Client, timeout: object, poll: object, what: str
) -> None:
    """``poll=-1`` raised a bare ValueError out of ``time.sleep`` in the sync
    half and returned instantly in the async one — a tight loop against a
    metered endpoint. ``timeout=nan`` loses every ``remaining <= 0`` comparison,
    so the deadline the docstring promises never arrived."""
    with pytest.raises(ValueError, match=f"{what} must be a finite, non-negative"):
        client.builds.wait("bld-1", timeout=timeout, poll=poll)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_async_wait_refuses_them_identically(async_client: mc.AsyncClient) -> None:
    """The two halves diverged here, which is the part that makes it a parity
    defect as well as a correctness one."""
    with pytest.raises(ValueError, match="poll must be a finite, non-negative"):
        await async_client.builds.wait("bld-1", timeout=1.0, poll=-1)
    with pytest.raises(ValueError, match="timeout must be a finite, non-negative"):
        await async_client.builds.wait("bld-1", timeout=float("nan"))


@respx.mock
def test_zero_is_allowed_because_every_sibling_wait_takes_it(client: mc.Client) -> None:
    """``poll=0`` is what this repository's own tests pass to ``wait_until_built``,
    ``wait_until_running`` and ``wait_for_guest`` at twenty-odd call sites, and
    ``timeout=0`` is an already-expired deadline the siblings answer with the
    TimeoutError their callers catch. Refusing both was a wider break than the
    bug it fixed."""
    respx.get(f"{BASE}/builds/bld-1/progress").mock(return_value=httpx.Response(200, json=DONE))
    assert client.builds.wait("bld-1", timeout=5.0, poll=0).status == "succeeded"
    with pytest.raises(mc.TimeoutError):
        client.builds.wait("bld-1", timeout=0, poll=0)


@respx.mock
def test_the_timeout_that_reaches_the_transport_is_the_whole_remaining_budget(
    client: mc.Client,
) -> None:
    """The cap is an upper bound on each operation, not a quarter-share of one.

    Asserted on ``request.extensions["timeout"]`` — what httpx actually carries
    — because respx does NOT enforce timeouts: a handler that sleeps past the
    read timeout returns 200 anyway, so a test that merely times a mocked
    response proves nothing and would pass against the divided implementation
    too. That was the first version of this test.

    Dividing the budget across httpx's four settings so they SUM to it was
    arithmetic about a total that does not exist — ``read`` is an inactivity
    timeout that httpcore restarts on every chunk — and it broke the callers it
    was meant to help: with eight seconds left a legitimate three-second refresh
    began failing at two, and neither ``wait_until_built`` nor
    ``wait_until_running`` catches a timeout on its refresh.
    """
    route = respx.get(f"{BASE}/builds/bld-1/progress").mock(
        return_value=httpx.Response(200, json=DONE)
    )
    client.builds.wait("bld-1", timeout=8.0, poll=0)
    sent = route.calls.last.request.extensions["timeout"]
    assert sent["read"] == pytest.approx(8.0, abs=0.5), sent
    assert sent["connect"] == pytest.approx(8.0, abs=0.5), sent
    # A quarter-share would have been 2.0 on every phase.
    assert sent["read"] > 4.0, sent


def test_the_cap_still_tightens_a_looser_client_timeout() -> None:
    """What it is for: a one-second wait must not inherit a sixty-second read,
    and a client with no timeout at all must not wait for ever."""
    from mandala_computer._client import _BaseTransport

    current = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
    capped = _BaseTransport._cap_budget(current, 1.0)
    assert capped.read == 1.0
    assert capped.connect == 1.0

    none_at_all = httpx.Timeout(connect=None, read=None, write=None, pool=None)
    assert _BaseTransport._cap_budget(none_at_all, 2.0).read == 2.0


def test_the_cap_leaves_a_tighter_client_timeout_alone() -> None:
    from mandala_computer._client import _BaseTransport

    current = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=10.0)
    capped = _BaseTransport._cap_budget(current, 1800.0)
    assert capped.read == 60.0
    assert capped.connect == 10.0


def test_template_can_still_be_built_the_way_it_could_before_ref() -> None:
    """``Template`` is exported, so its field order is its constructor.

    ``ref`` added ahead of ``label`` broke every positional construction that
    worked on the previous release — fixtures and downstream code alike, while
    decoding never noticed because ``from_api`` passes by keyword.
    """
    t = mc.Template("ubuntu", "Ubuntu 24.04", "linux", 2, 4096, 40)
    assert t.name == "ubuntu"
    assert t.label == "Ubuntu 24.04"
    assert t.disk_gb == 40
    assert t.ref is None
    assert mc.Template.from_api({"name": "u", "ref": "acc-1/u@1.0.0"}).ref == "acc-1/u@1.0.0"


# The second adversarial-review pass, on the fix commits themselves. Two of
# these are defects the FIRST pass's fixes introduced or left behind.


@pytest.mark.parametrize("value", ["false", "true", 0, 1, "", None, []])
def test_force_stop_refuses_anything_that_is_not_a_bool(value: object) -> None:
    """The third arming flag, missed when the other two were hardened.

    ``stop(force="false")`` pulled the power and lost whatever the guest had not
    written to disk — the same defect as the snapshot purge, on the same kind of
    string a config file produces.
    """
    with pytest.raises(ValueError, match="force must be True or False"):
        _api.stop_params(value)  # type: ignore[arg-type]


def test_force_stop_still_takes_real_bools() -> None:
    assert _api.stop_params(True) == {"force": "true"}
    assert _api.stop_params(False) is None


@pytest.mark.parametrize(
    ("model", "field", "cautious", "steers"),
    [
        (mc.Move, "live", True, "wait_for_move would hand back a mid-migration computer"),
        (mc.ExecStatus, "running", True, "ExecStatus.done would end a poll on a live command"),
        (
            mc.ExecStatus,
            "exited",
            False,
            "with running cautious, done stays False and polling goes on",
        ),
        (
            mc.ExecStatus,
            "more",
            False,
            "more is a BACKOFF switch: True means poll again with no sleep",
        ),
        (
            mc.ExecResult,
            "timed_out",
            True,
            "it decides ExecResult.ok, and through it wait_for_guest",
        ),
        (mc.TemplateCheck, "valid", False, "a document is not publishable until it is known to be"),
        (
            mc.BuildProgress,
            "done",
            False,
            "a wait does not return until the build is known to be over",
        ),
        (mc.UsageReport, "degraded", True, "a total that may be short must not read as complete"),
        (mc.UsageReport, "unmetered", True, "the other half of the same caveat, named with it"),
        (mc.Snapshot, "unreachable", False, "True throws away every other field on a full row"),
        (
            mc.Snapshot,
            "orphaned",
            False,
            "True steers a restore into a clone, which bills a computer",
        ),
    ],
)
def test_every_wire_boolean_fails_the_safe_way_for_its_own_field(
    model: type, field: str, cautious: bool, steers: str
) -> None:
    """Not "True for caveats" — the question is what the flag makes a caller DO.

    ``more`` reads like a caveat and is the counter-example: it is a backoff
    switch, so a cautious True made the loop in ``start_exec``'s own docstring
    neither break nor sleep (/code-review, OPL-3835).
    """
    decoded = model.from_api({field: "maybe"})
    assert getattr(decoded, field) is cautious, steers
    assert decoded.raw[field] == "maybe"


def _run_documented_poll_loop(statuses: list[mc.ExecStatus], limit: int = 6) -> dict[str, int]:
    """The loop printed verbatim in ``Computer.start_exec``'s docstring, run.

    Bounded, and it reports what it did rather than asserting: the point of
    these tests is which of break / sleep / spin actually happens.
    """
    reads = sleeps = 0
    for status in statuses[:limit]:
        reads += 1
        if status.done and not status.more:
            return {"reads": reads, "sleeps": sleeps, "broke": 1}
        if not status.more:
            sleeps += 1
    return {"reads": reads, "sleeps": sleeps, "broke": 0}


def test_a_finished_command_lets_the_documented_loop_break() -> None:
    """``more`` cautious-True made this loop neither break nor sleep: an
    unbounded zero-delay loop against a metered endpoint on a command that had
    already finished (/code-review, OPL-3835)."""
    finished = mc.ExecStatus.from_api({"pid": 7, "running": False, "exited": True, "more": False})
    assert _run_documented_poll_loop([finished] * 6) == {"reads": 1, "sleeps": 0, "broke": 1}


def test_a_null_status_lets_the_loop_break_because_null_is_not_applicable() -> None:
    """``null`` reads False with absent, so a finished command reported with
    nulls still terminates the loop. Routing null to the cautious answer is what
    made ``wait_for_move`` poll a finished move for its full timeout."""
    nulls = mc.ExecStatus.from_api({"pid": 7, "running": None, "exited": None, "more": None})
    assert nulls.done is True
    assert _run_documented_poll_loop([nulls] * 6)["broke"] == 1


def test_an_unreadable_status_backs_OFF_rather_than_spinning() -> None:
    """The honest limit of this, stated rather than claimed away.

    A status whose ``running`` cannot be read is not asserted to be finished —
    that would end a poll on a live command — so the loop does NOT terminate.
    What the cautious ``more`` buys is that it sleeps between reads instead of
    spinning: a polite poll against a host sending nonsense, not a hot loop.
    Terminating here would need a tri-state ``done``, which is a bigger change
    than this branch.
    """
    junk = mc.ExecStatus.from_api(
        {"pid": 7, "running": "maybe", "exited": "maybe", "more": "maybe"}
    )
    assert junk.running is True and junk.more is False
    assert junk.done is False
    ran = _run_documented_poll_loop([junk] * 6)
    assert ran["broke"] == 0, "it cannot honestly break on a status it could not read"
    assert ran["sleeps"] == ran["reads"], "but every read must be preceded by a sleep"


def test_the_cautious_reading_keeps_a_poll_loop_going() -> None:
    """The two exec flags meet in ``done``, so they have to be chosen together."""
    unreadable = mc.ExecStatus.from_api({"pid": 1, "running": "maybe", "exited": "maybe"})
    assert unreadable.done is False, "a command this client cannot read is not finished"


def test_the_cautious_reading_keeps_a_move_wait_going() -> None:
    assert mc.Move.from_api({"state": "moving", "live": "maybe"}).live is True


def test_an_unreadable_exec_is_not_reported_as_a_success() -> None:
    r = mc.ExecResult.from_api({"exit_code": 0, "timed_out": "maybe"})
    assert r.timed_out is True
    assert r.ok is False, "ok must not bless an exec whose timed_out could not be read"


def test_no_wire_boolean_is_left_on_truthiness() -> None:
    """The rule is every boolean, not a list somebody has to keep current.

    Three holes have been found in this scan, which is why it now checks its own
    coverage. It read ``src/...`` relative to the cwd and errored anywhere but
    the repo root. Its regex pinned one spelling, so ``bool(d["x"])`` walked
    past. And its lookahead required a FOLLOWING decorator or class, so it
    silently dropped the last ``from_api`` in the module — which was
    ``ExecResult``'s, one this branch changed (/code-review, OPL-3835). A scan
    that quietly covers less than it claims is worse than no scan.
    """
    import re

    modules = ["_models", "_computer", "_async_computer"]
    for name in modules:
        source = pathlib.Path(getattr(mc, name).__file__).read_text()
        bodies = re.findall(r"def from_api\(.*?(?=\n    @|\n@|\nclass |\Z)", source, re.DOTALL)
        assert len(bodies) == source.count("def from_api("), (
            f"{name}: scanned {len(bodies)} of {source.count('def from_api(')} decoders"
        )
        offenders = [call for body in bodies for call in re.findall(r"\bbool\([^)]*\)", body)]
        assert not offenders, (name, offenders)
        # And nothing anywhere in these modules may read a caveat flag raw.
        for flag in ("unreachable", "orphaned", "degraded", "unmetered", "live", "timed_out"):
            raw = re.findall(rf'(?<!_flag\()[\w.]*get\("{flag}"[^)]*\)', source)
            assert not raw, (name, flag, raw)


def test_template_keeps_the_raw_positional_slot_too() -> None:
    """Moving ``ref`` to the end fixed the six and broke the seventh.

    ``raw`` was the seventh positional field. With ``ref`` ahead of it,
    ``Template(..., disk_gb, raw_dict)`` bound the mapping to ``ref`` and left
    ``raw`` empty — silently, because a mapping is a fine value for an optional
    field. ``kw_only`` is what leaves every existing position alone.
    """
    t = mc.Template("ubuntu", "Ubuntu 24.04", "linux", 2, 4096, 40, {"name": "ubuntu"})
    assert t.raw == {"name": "ubuntu"}
    assert t.ref is None
    with pytest.raises(TypeError):
        mc.Template("ubuntu", "Ubuntu", "linux", 2, 4096, 40, {}, "acc/u@1.0.0")  # type: ignore[misc]


@respx.mock
def test_a_429_without_a_retry_after_still_backs_off(client: mc.Client) -> None:
    """``poll`` cannot be the floor, because ``poll=0`` is allowed.

    ``retry_delay`` only consulted ``Retry-After``, and a RateLimitError raised
    without that header carries ``retry_after=None``. With ``poll=0`` the delay
    was therefore zero, so a wait that hit a rate limiter retried it back to
    back for the full half-hour default — the loop the function exists to break,
    with its one brake removed.
    """
    from mandala_computer._resources import RATE_LIMITED_FLOOR, retry_delay

    bare = mc.RateLimitError("slow down", status=429)
    assert bare.retry_after is None
    assert retry_delay(0, bare) == RATE_LIMITED_FLOOR
    assert retry_delay(5.0, bare) == 5.0

    told = mc.RateLimitError("slow down", status=429, retry_after=30.0)
    assert retry_delay(0, told) == 30.0
    assert retry_delay(60.0, told) == 60.0

    # A non-429 keeps the caller's poll, zero included: nothing asked us to wait.
    assert retry_delay(0, mc.APIError("boom", status=503)) == 0


@respx.mock
def test_the_floor_is_actually_slept_by_a_wait(client: mc.Client) -> None:
    slept: list[float] = []
    respx.get(f"{BASE}/builds/bld-1/progress").mock(
        side_effect=[
            httpx.Response(429, json={"error": "slow down"}),
            httpx.Response(200, json=DONE),
        ]
    )
    with mock.patch("mandala_computer._resources.time.sleep", slept.append):
        assert client.builds.wait("bld-1", timeout=30, poll=0).status == "succeeded"
    assert slept and slept[0] >= 1.0, slept
