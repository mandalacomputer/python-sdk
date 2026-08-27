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
import time
from unittest import mock

import httpx
import pytest
import respx

import mandala_computer as mc
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
    """The platform reads the PRESENCE of the key, not its value.

    ``no_reuse=false`` would therefore ask for the opposite of what it says.
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
