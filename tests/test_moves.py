"""The resize that needs the computer moved, from the refusal to the outcome.

OPL-3774. A resize past what a computer's host can run is refused with an OFFER —
``move: {required, possible}`` on the body — and this SDK bound neither half of
it. There was no method to take it up, and ``ConflictError``'s own docstring told
readers that every 409 clears itself, which this one never does: the host cannot
run that size and will not grow.

So what is pinned here is the seam rather than the move. The platform's own tests
own whether a move works; these own whether a caller can tell this 409 from the
ones that clear, and whether the four terminal states stay four different things
— ``moved`` most of all, because that is the one where the computer HAS changed
hardware and reading it as a failure sends somebody looking for a machine that is
no longer where it was.

Both halves of the client, because both have the methods and
:mod:`tests.test_parity` only proves the signatures match — not that the async
one does the same thing with them.
"""

from __future__ import annotations

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"

COMPUTER = {
    "id": "vm-1",
    "name": "dev",
    "status": "stopped",
    "os": "linux",
    "template": "base",
    "cpu": 2,
    "ram_mb": 2048,
    "disk_gb": 20,
}

MOVING = {
    "computer_id": "vm-1",
    "state": "moving",
    "detail": "",
    "live": True,
    "cpu": 2,
    "ram_mb": 26000,
    "started_at": "2026-08-23T02:00:12.699Z",
}
DONE = {**MOVING, "state": "done", "live": False, "finished_at": "2026-08-23T02:00:17.336Z"}

REFUSAL = "26000 MB of RAM is more than the host this computer is on can run."


@pytest.fixture
def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


def offer(possible: bool) -> httpx.Response:
    """The 409 the platform answers a resize it cannot run where it is."""
    return httpx.Response(
        409, json={"error": REFUSAL, "move": {"required": True, "possible": possible}}
    )


def computer(client: mc.Client) -> mc.Computer:
    respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
    return client.computers.get("vm-1")


class TestTheRefusalThatOffersAMove:
    @respx.mock
    def test_arrives_as_its_own_class_with_the_branch_read_off_the_body(
        self, client: mc.Client
    ) -> None:
        c = computer(client)
        respx.patch(f"{BASE}/computers/vm-1").mock(offer(True))
        with pytest.raises(mc.MoveRequiredError) as caught:
            c.resize(ram_mb=26000)
        assert caught.value.move_possible is True
        # The platform's own sentence survives: it is the account of what will
        # not fit and what moving costs, written for whoever has to agree to it.
        assert REFUSAL in str(caught.value)

    @respx.mock
    def test_says_so_when_there_is_nowhere_to_move_to(self, client: mc.Client) -> None:
        # Still a MoveRequiredError — the resize still needs a move — and the
        # flag is what says there is nothing to call. ``move.required`` is true
        # in both cases, which is exactly why the second field is the one read.
        c = computer(client)
        respx.patch(f"{BASE}/computers/vm-1").mock(offer(False))
        with pytest.raises(mc.MoveRequiredError) as caught:
            c.resize(ram_mb=999_999)
        assert caught.value.move_possible is False

    @respx.mock
    def test_is_still_a_conflict_error(self, client: mc.Client) -> None:
        # A subclass rather than a sibling, so ``except ConflictError`` written
        # before this existed still catches it. What changes is only what a
        # caller should do about it.
        c = computer(client)
        respx.patch(f"{BASE}/computers/vm-1").mock(offer(True))
        with pytest.raises(mc.ConflictError):
            c.resize(ram_mb=26000)

    @respx.mock
    def test_leaves_an_ordinary_conflict_alone(self, client: mc.Client) -> None:
        c = computer(client)
        respx.patch(f"{BASE}/computers/vm-1").mock(
            httpx.Response(409, json={"error": "this computer is running"})
        )
        with pytest.raises(mc.ConflictError) as caught:
            c.resize(ram_mb=4096)
        assert not isinstance(caught.value, mc.MoveRequiredError)

    @respx.mock
    @pytest.mark.parametrize(
        "move",
        [
            "yes",
            {"required": True},
            {"possible": True},
            None,
            {"required": True, "possible": "yes"},
        ],
    )
    def test_is_not_fooled_by_a_move_shaped_key_that_is_not_one(
        self, client: mc.Client, move: object
    ) -> None:
        # Absent and malformed get the same answer, and it is the conservative
        # one. A ``move`` that is a string, or a dict with no boolean
        # ``possible``, must not become an offer with ``move_possible`` quietly
        # False — that would tell a caller nowhere in the region can run their
        # size on the strength of a field nobody sent.
        c = computer(client)
        respx.patch(f"{BASE}/computers/vm-1").mock(
            httpx.Response(409, json={"error": "refused", "move": move})
        )
        with pytest.raises(mc.ConflictError) as caught:
            c.resize(ram_mb=4096)
        assert not isinstance(caught.value, mc.MoveRequiredError)


class TestRelocate:
    @respx.mock
    def test_sends_the_sizing_group_and_answers_the_202(self, client: mc.Client) -> None:
        c = computer(client)
        route = respx.post(f"{BASE}/computers/vm-1/move").mock(httpx.Response(202, json=MOVING))
        move = c.relocate(ram_mb=26000, cpu=2)

        import json as _json

        assert _json.loads(route.calls.last.request.content) == {"ram_mb": 26000, "cpu": 2}
        # The 202, as the operation stood when it was accepted — not the
        # outcome. A method that pretended otherwise would report every move as
        # finished the instant it started.
        assert (move.live, move.state, move.ram_mb) == (True, "moving", 26000)
        # And the dimension nobody asked to change stays absent rather than
        # arriving as 0, which on a move would read as a resize to nothing.
        assert move.disk_gb is None

    def test_will_not_be_called_without_the_size_that_did_not_fit(self, client: mc.Client) -> None:
        # ram_mb is keyword-only and required, unlike on resize(): the platform
        # fills an omitted one from the computer's current size and then refuses
        # the move for not needing one, so a call without it can only ever be
        # refused. A TypeError at the call site is cheaper than a 409 three tiers
        # away.
        with respx.mock:
            c = computer(client)
        with pytest.raises(TypeError):
            c.relocate(cpu=4)  # type: ignore[call-arg]


class TestWaitForMove:
    @respx.mock
    def test_answers_the_move_once_it_has_stopped(self, client: mc.Client) -> None:
        c = computer(client)
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": [DONE]}))
        move = c.wait_for_move(poll=0.01)
        assert (move.state, move.live, move.finished_at) == ("done", False, DONE["finished_at"])

    @respx.mock
    def test_picks_this_computers_move_out_of_the_accounts(self, client: mc.Client) -> None:
        # The listing is account-wide and one move runs at a time — but a
        # FINISHED row for another computer stays for a day, so "the first row"
        # is the wrong answer often enough to be worth pinning.
        c = computer(client)
        respx.get(f"{BASE}/moves").mock(
            httpx.Response(
                200,
                json={"moves": [{**DONE, "computer_id": "vm-other", "state": "failed"}, DONE]},
            )
        )
        move = c.wait_for_move(poll=0.01)
        assert (move.computer_id, move.state) == ("vm-1", "done")

    @respx.mock
    @pytest.mark.parametrize("state", ["moved", "failed", "lost"])
    def test_does_not_raise_for_a_move_that_ended_badly(
        self, client: mc.Client, state: str
    ) -> None:
        # The decision worth stating out loud. The three are three situations
        # with three remedies, and an exception flattens them into one — which is
        # how ``moved``, where the computer really has changed hardware, gets
        # read as "nothing happened". The caller reads ``state``.
        c = computer(client)
        respx.get(f"{BASE}/moves").mock(
            httpx.Response(200, json={"moves": [{**DONE, "state": state, "detail": "a reason"}]})
        )
        move = c.wait_for_move(poll=0.01)
        assert (move.state, move.detail) == (state, "a reason")

    @respx.mock
    def test_gives_up_on_its_own_deadline_without_stopping_the_move(
        self, client: mc.Client
    ) -> None:
        c = computer(client)
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": [MOVING]}))
        with pytest.raises(mc.TimeoutError) as caught:
            c.wait_for_move(timeout=0.05, poll=0.01)
        # The sentence has to say the move survives the wait, because it does:
        # there is no calling back a disk crossing between two hosts.
        assert "has not stopped" in str(caught.value)

    @respx.mock
    def test_waits_on_the_live_row_and_not_a_stale_one_listed_first(
        self, client: mc.Client
    ) -> None:
        """``GET /moves`` keeps a finished move for a day, and that day covers
        this computer's own. A machine moved this morning and moved again now
        has two rows and nothing orders them, so taking the first match ended
        the wait on the morning's outcome while the disk copy was still
        running — with ``moved``, which reads as "the move landed and the
        resize did not", sending the caller on to resize a machine in flight
        (adversarial review, OPL-4222)."""
        c = computer(client)
        stale = {**DONE, "state": "moved", "ram_mb": 4096}
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": [stale, MOVING]}))
        with pytest.raises(mc.TimeoutError) as caught:
            c.wait_for_move(timeout=0.05, poll=0.01)
        assert "state moving" in str(caught.value)

    @respx.mock
    @pytest.mark.parametrize(
        "order", ["platform", "reversed"], ids=["newest-first", "oldest-first"]
    )
    def test_answers_with_the_newest_finished_row_when_none_is_live(
        self, client: mc.Client, order: str
    ) -> None:
        """Nothing live left to find, so the latest ``started_at`` is the answer
        — read off the rows and not off their order.

        The platform sends them newest-first (``ORDER BY started_at DESC``), so
        position would answer this correctly today. Depending on it is how the
        first version of this fix took the LAST row, guessed oldest-first, and
        picked exactly the stale outcome it was written to avoid
        (/code-review, OPL-4222).
        """
        c = computer(client)
        morning = {**DONE, "state": "moved", "ram_mb": 4096, "started_at": "2026-08-23T02:00:12Z"}
        afternoon = {**DONE, "started_at": "2026-08-23T14:00:00Z"}
        rows = [afternoon, morning] if order == "platform" else [morning, afternoon]
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": rows}))
        assert c.wait_for_move(poll=0.01).state == "done"

    @respx.mock
    def test_falls_back_to_the_platforms_order_when_the_stamps_cannot_decide(
        self, client: mc.Client
    ) -> None:
        """Unreadable or identical stamps leave position, which is newest-first."""
        c = computer(client)
        newest = {**DONE, "started_at": ""}
        older = {**DONE, "state": "moved", "ram_mb": 4096, "started_at": ""}
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": [newest, older]}))
        assert c.wait_for_move(poll=0.01).state == "done"

    @respx.mock
    def test_a_malformed_envelope_is_not_a_computer_that_was_deleted(
        self, client: mc.Client
    ) -> None:
        """``_my_move`` degraded a non-list ``moves`` to ``None``, and
        ``wait_for_move`` reads ``None`` as the platform reaping a move along
        with its computer — the exact misdiagnosis ``move_rows`` was written to
        prevent, on the one path that polls (/code-review, OPL-4222)."""
        c = computer(client)
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": "moving"}))
        with pytest.raises(mc.MandalaError, match="array of objects"):
            c.wait_for_move(timeout=5, poll=0.01)

    @respx.mock
    def test_stops_when_the_move_stops_being_listed(self, client: mc.Client) -> None:
        # MandalaError and not a timeout: waiting longer cannot bring back a row
        # the platform reaped, and spending the whole deadline to say so is the
        # failure this branch exists to avoid.
        c = computer(client)
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": []}))
        with pytest.raises(mc.MandalaError) as caught:
            c.wait_for_move(timeout=5, poll=0.01)
        assert not isinstance(caught.value, mc.TimeoutError)
        assert "deleted" in str(caught.value)


class TestMovesCollection:
    @respx.mock
    def test_lists_the_accounts_moves_unwrapped(self, client: mc.Client) -> None:
        route = respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": [DONE]}))
        (move,) = client.moves.list()
        assert route.called
        # The platform answers ``{"moves": [...]}``; a caller gets the list.
        assert (move.computer_id, move.live) == ("vm-1", False)

    @respx.mock
    def test_answers_an_empty_list_rather_than_raising(self, client: mc.Client) -> None:
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": []}))
        assert client.moves.list() == []

    @respx.mock
    @pytest.mark.parametrize(
        "moves",
        [{"computer_id": "vm-1"}, "moving", 7, None],
        ids=["object", "string", "number", "null"],
    )
    def test_refuses_an_envelope_that_is_not_an_array(
        self, client: mc.Client, moves: object
    ) -> None:
        """Every other listing goes through ``json_array``, which insists on an
        array of objects. This one answered ``[]`` — and an empty moves listing
        means something specific, since ``wait_for_move`` reads it as a move the
        platform reaped along with its computer (adversarial review, OPL-4222)."""
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": moves}))
        with pytest.raises(mc.MandalaError, match="array of objects"):
            client.moves.list()

    @respx.mock
    def test_refuses_a_row_that_is_not_an_object(self, client: mc.Client) -> None:
        """It reached ``Move.from_api`` and came back out as ``AttributeError:
        'str' object has no attribute 'get'`` — a bare builtin escaping a public
        method, past the MandalaError this SDK promises."""
        respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": [DONE, "junk"]}))
        with pytest.raises(mc.MandalaError, match="array of objects"):
            client.moves.list()

    @pytest.mark.parametrize("bad", ["oops", False, [], float("nan")], ids=str)
    def test_an_unreadable_size_is_not_a_resize_to_nothing(self, bad: object) -> None:
        """``None`` on these three means "not being changed" and never "changed
        to nothing" — the field docstring says so, on the fields this operation
        exists to grow. ``_num`` answered 0 for anything it could not read, so a
        caller reading ``move.cpu is not None`` as "this dimension changed" was
        told a resize to zero CPUs was in flight (adversarial review,
        OPL-4222)."""
        move = mc.Move.from_api({**MOVING, "cpu": bad, "ram_mb": bad, "disk_gb": bad})
        assert (move.cpu, move.ram_mb, move.disk_gb) == (None, None, None)
        # A real zero is still a real answer, which is why this is `_opt_num`
        # and not a truthiness test.
        assert mc.Move.from_api({**MOVING, "cpu": 0}).cpu == 0


class TestAsyncHalf:
    """The async client does the same things, not merely the same shapes.

    test_parity proves the signatures match. It cannot prove the bodies do, and
    these two methods are new enough on both halves that a copy-paste slip would
    pass every test in this file up to here.
    """

    @respx.mock
    @pytest.mark.asyncio
    async def test_relocates_and_waits(self) -> None:
        async with mc.AsyncClient("com_test", base_url=BASE) as client:
            respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
            c = await client.computers.get("vm-1")
            respx.post(f"{BASE}/computers/vm-1/move").mock(httpx.Response(202, json=MOVING))
            started = await c.relocate(ram_mb=26000)
            assert started.live is True

            respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": [DONE]}))
            assert (await c.wait_for_move(poll=0.01)).state == "done"
            assert [m.computer_id for m in await client.moves.list()] == ["vm-1"]

    @respx.mock
    @pytest.mark.asyncio
    async def test_waits_on_the_live_row_and_refuses_a_malformed_envelope(self) -> None:
        """Both OPL-4222 fixes on the twin. ``_my_move`` is shared, but the two
        wait loops are not, and this is the call site."""
        async with mc.AsyncClient("com_test", base_url=BASE) as client:
            respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
            c = await client.computers.get("vm-1")

            stale = {**DONE, "state": "moved", "ram_mb": 4096}
            respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": [stale, MOVING]}))
            with pytest.raises(mc.TimeoutError, match="state moving"):
                await c.wait_for_move(timeout=0.05, poll=0.01)

            respx.get(f"{BASE}/moves").mock(httpx.Response(200, json={"moves": "moving"}))
            with pytest.raises(mc.MandalaError, match="array of objects"):
                await c.wait_for_move(timeout=5, poll=0.01)
            with pytest.raises(mc.MandalaError, match="array of objects"):
                await client.moves.list()

    @respx.mock
    @pytest.mark.asyncio
    async def test_raises_the_typed_refusal(self) -> None:
        async with mc.AsyncClient("com_test", base_url=BASE) as client:
            respx.get(f"{BASE}/computers/vm-1").mock(httpx.Response(200, json=COMPUTER))
            c = await client.computers.get("vm-1")
            respx.patch(f"{BASE}/computers/vm-1").mock(offer(True))
            with pytest.raises(mc.MoveRequiredError) as caught:
                await c.resize(ram_mb=26000)
            assert caught.value.move_possible is True


def test_the_newest_finished_move_wins_across_rfc3339_spellings() -> None:
    """A fractional stamp sorts before a whole one of the same second, as strings.

    ``'.'`` (46) precedes ``'Z'`` (90), so ``02:00:12.999Z < 02:00:12Z`` — the
    later move reads as the older one. Go's ``RFC3339`` and ``RFC3339Nano``
    produce exactly that mix, and ``_my_move`` compared the raw strings. With no
    live row it then returned the stale outcome, which is the misread OPL-4222
    fixed for live-versus-finished: ``moved`` means the machine HAS changed
    hosts, so acting on the older row resizes a computer that is elsewhere
    (OPL-4232).
    """
    c = mc.Computer(None, {"id": "vm-1"})  # type: ignore[arg-type]
    listing = {
        "moves": [
            # The LATER move, spelled with a fraction.
            {
                "computer_id": "vm-1",
                "state": "moved",
                "live": False,
                "started_at": "2026-08-23T02:00:12.999Z",
            },
            # The EARLIER one, spelled without.
            {
                "computer_id": "vm-1",
                "state": "done",
                "live": False,
                "started_at": "2026-08-23T02:00:12Z",
            },
        ]
    }
    picked = c._my_move(listing)
    assert picked is not None
    assert picked.state == "moved"
    assert picked.started_at == "2026-08-23T02:00:12.999Z"


def test_the_newest_finished_move_wins_across_utc_suffix_spellings() -> None:
    """``Z`` and ``+00:00`` are the same instant; ``'+'`` precedes ``'Z'``.

    A later ``02:00:12.500+00:00`` sorted *before* an earlier ``02:00:12Z``,
    so ``_my_move`` could pick the stale finished row.
    """
    c = mc.Computer(None, {"id": "vm-1"})  # type: ignore[arg-type]
    listing = {
        "moves": [
            {
                "computer_id": "vm-1",
                "state": "moved",
                "live": False,
                "started_at": "2026-08-23T02:00:12.500+00:00",
            },
            {
                "computer_id": "vm-1",
                "state": "done",
                "live": False,
                "started_at": "2026-08-23T02:00:12Z",
            },
        ]
    }
    picked = c._my_move(listing)
    assert picked is not None
    assert picked.state == "moved"
    assert picked.started_at == "2026-08-23T02:00:12.500+00:00"


def test_an_undateable_row_never_displaces_one_that_can_be_dated() -> None:
    """`max` is choosing what to hand back; an unreadable stamp is not evidence."""
    c = mc.Computer(None, {"id": "vm-1"})  # type: ignore[arg-type]
    listing = {
        "moves": [
            {"computer_id": "vm-1", "state": "failed", "live": False, "started_at": "not a stamp"},
            {
                "computer_id": "vm-1",
                "state": "done",
                "live": False,
                "started_at": "2026-08-23T02:00:12Z",
            },
        ]
    }
    picked = c._my_move(listing)
    assert picked is not None
    assert picked.state == "done"
