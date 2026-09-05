"""The values a caller controls, and what a hostile shape of them does.

OPL-3869, after three reviews found the same defect three times. The pattern is
never a missing check — it is a check made against something OTHER than what is
sent:

* a ``str`` subclass answers the regex, the membership test or the ``strip``
  with one buffer and the serialiser with another
* an ``int`` subclass answers ``> 0`` honestly and the FORMATTER with a path
  segment, or lies to the comparison and passes an unchanged negative onward
* ``"false"`` is a non-empty string, so every bare truthiness test reads it as
  yes — on parameters that arm a machine, a snapshot or a schedule

Two hunts and one adversarial review each fixed the sites they happened to name,
and each time a later pass found more. So this file is an INVENTORY rather than
a list of cases: every caller-controlled string, bool and guarded number that
reaches the wire gets a row, and a new builder with no row here is the thing to
notice in review.

The helpers are :func:`canonical`, :func:`flag`, :func:`whole` and :func:`real`.
What each guarantees is the same one thing: the value CHECKED is the value SENT.
"""

from __future__ import annotations

import pytest

import mandala_computer as mc

A = mc._api
C = mc._computer


class Liar(str):
    """Passes a string check, then serialises as something else."""

    def __str__(self) -> str:
        return " injected"

    def __eq__(self, other: object) -> bool:  # membership tests ask this
        return True

    def __hash__(self) -> int:
        return hash("move")


class Crooked(int):
    """Answers every ordered comparison the way that gets it through."""

    def __gt__(self, other: object) -> bool:
        return True

    def __ge__(self, other: object) -> bool:
        return True

    def __lt__(self, other: object) -> bool:
        return False

    def __le__(self, other: object) -> bool:
        return False


class Forger(int):
    """Passes a positive check, writes a path segment when formatted."""

    def __format__(self, spec: str) -> str:
        return "../stop"

    def __str__(self) -> str:
        return "../stop"


# --- booleans that ARM something -------------------------------------------
#
# Not the ones that merely widen a listing: `allow_partial` and `include=all`
# stay on truthiness deliberately, because reading one of those generously
# destroys nothing. These five start a machine, capture RAM, arm a schedule,
# force a capture, or decide whether a deadline is sent at all.
ARMING_FLAGS = [
    (
        "create start",
        lambda v: A.create_body(
            name=None,
            template=None,
            cpu=None,
            ram_mb=None,
            disk_gb=None,
            start=v,
            resolution=None,
            size=None,
        ),
    ),
    ("snapshot memory", lambda v: A.snapshot_body(v)),
    ("schedule enabled", lambda v: A.schedule_body(enabled=v, hour=4, minute=0, tz="UTC")),
    ("screenshot fresh", lambda v: A.screenshot_params(None, v)),
    (
        "exec background",
        lambda v: A.exec_body(
            command="x", timeout=30, background=v, desktop=False, cwd=None, env=None
        ),
    ),
]


@pytest.mark.parametrize(("name", "build"), ARMING_FLAGS, ids=[n for n, _ in ARMING_FLAGS])
@pytest.mark.parametrize("truthy", ["false", "0", 1, 0, "no"])
def test_an_arming_flag_refuses_anything_that_is_not_a_bool(name, build, truthy) -> None:
    """``"false"`` is truthy, and it is what a config file or a CLI argument gives."""
    with pytest.raises(ValueError, match="True or False"):
        build(truthy)


# --- numbers whose guard is an overridable comparison ----------------------
REJECTS_NEGATIVE = [
    ("screenshot width", lambda v: A.screenshot_params(v)),
    (
        "agent max_steps",
        lambda v: A.agent_body(prompt="x", system=None, max_steps=v, model=None, stream=False),
    ),
    ("scroll amount", lambda v: A.scroll_body(None, None, "up", v)),
    ("idle_suspend_min", lambda v: A.idle_suspend_body(v)),
    ("schedule hour", lambda v: A.schedule_body(enabled=True, hour=v, minute=0, tz="UTC")),
    (
        "exec timeout",
        lambda v: A.exec_body(
            command="x", timeout=v, background=False, desktop=False, cwd=None, env=None
        ),
    ),
    ("wait duration", lambda v: A.wait_body(v)),
]


@pytest.mark.parametrize(("name", "build"), REJECTS_NEGATIVE, ids=[n for n, _ in REJECTS_NEGATIVE])
def test_a_guarded_number_is_judged_on_its_real_value(name, build) -> None:
    """The comparison has to see the buffer, not the subclass's opinion of it."""
    with pytest.raises((ValueError, TypeError)):
        build(Crooked(-1))


def test_a_pid_cannot_format_itself_into_another_route() -> None:
    """``exec_handle`` interpolates the pid, so ``__format__`` is the attack."""
    assert A.exec_handle("vm-1", Forger(1)) == "computers/vm-1/exec/1"


def test_a_pid_still_accepts_the_decimal_string_that_crosses_a_process() -> None:
    """A job id out of a queue arrives as text; refusing it was a regression."""
    assert A.exec_handle("vm-1", "4242") == "computers/vm-1/exec/4242"
    for bad in ("", "  ", "-1", "0", "4.2", "0x10", True, 0, -1, None, 1.5):
        with pytest.raises(ValueError):
            A.guest_pid(bad)


def test_an_oversized_pid_string_says_what_a_pid_should_be() -> None:
    """The guard's own message, not CPython's about digit counts.

    `isdecimal` admits a string of any length and `int` refuses one past its
    integer-string limit, so an absurd pid raised out of the conversion with a
    message naming neither the argument nor what it should have been. The limit
    is configurable down to 640, so what matters is that the function answers
    for its own contract, not the exact ceiling.
    """
    # Nines rather than zeros so the guard under test is the only thing that
    # can refuse this: `int("0" * 5000)` is 0 where the conversion limit is
    # lifted, and `number <= 0` would reject it without the new handler ever
    # running, leaving the test green against the old code.
    with pytest.raises(ValueError, match="pid must be a positive integer"):
        A.guest_pid("9" * 5000)
    assert A.guest_pid("0000004242") == 4242
    # Padding carries no magnitude at any length, and `_exact_int` reads this
    # string exactly — the two helpers must not disagree about it.
    assert A.guest_pid("0" * 5000 + "4242") == 4242


def test_a_duration_keeps_the_type_it_was_given() -> None:
    """Normalising must not turn ``wait(5)`` into ``5.0`` on the wire."""
    assert A.wait_body(5)["duration"] == 5
    assert isinstance(A.wait_body(5)["duration"], int)
    assert A.wait_body(0.5)["duration"] == 0.5


# --- strings checked one way and serialised another ------------------------
def test_a_usage_bound_sends_the_stamp_it_validated() -> None:
    """The one call whose output somebody compares against an invoice."""

    class Shifted(str):
        def __str__(self) -> str:
            return "2030-01-01T00:00:00Z"

    params = A.usage_params("2026-08-01T00:00:00Z", Shifted("2026-08-02T00:00:00Z"))
    assert type(params["to"]) is str
    assert str(params["to"]) == "2026-08-02T00:00:00Z"


def test_a_membership_test_cannot_be_answered_by_the_value_itself() -> None:
    """``in`` asks the value's own ``__eq__``, which a subclass owns.

    The buffer here is NOT a valid action, and the subclass claims equality with
    one. Before the guard canonicalised first, that claim was the whole test and
    the invalid buffer went out.
    """
    for build in (
        lambda v: A.window_body(v),
        lambda v: A.scroll_body(None, None, v, 1),
    ):
        with pytest.raises(ValueError):
            build(Liar("nonsense"))


def test_every_guarded_string_refuses_a_non_string() -> None:
    """One exception type for one class of mistake, so a caller can catch it."""
    for build in (
        lambda v: A.clipboard_body(v),
        lambda v: A.open_url_command(v),
        lambda v: A.agent_body(prompt=v, system=None, max_steps=None, model=None, stream=False),
        lambda v: A.exec_body(v, 30),
        lambda v: A.type_body(v),
        lambda v: A.key_body((v,)),
        # The sibling of ``key_body``, and the same tuple: a null in a chord
        # reaches the guest agent exactly as readily from here.
        lambda v: A.hold_key_body((v,), 1.0),
        lambda v: A.schedule_body(enabled=True, hour=4, minute=0, tz=v),
    ):
        with pytest.raises(ValueError, match="must be a string"):
            build(None)


def test_an_optional_string_that_is_present_is_still_a_string() -> None:
    """``None`` means omit and cannot be the probe here, so these are fed a
    number instead. ``name`` is checked by ``_require_optional_name``;
    ``template``, ``size`` and ``resolution`` sit beside it on the same body
    and had no check at all."""
    for build in (
        lambda v: A.create_body(
            name=None, template=v, cpu=None, ram_mb=None, disk_gb=None, start=False
        ),
        lambda v: A.create_body(
            name=None,
            template=None,
            cpu=None,
            ram_mb=None,
            disk_gb=None,
            start=False,
            resolution=v,
        ),
        lambda v: A.create_body(
            name=None, template=None, cpu=None, ram_mb=None, disk_gb=None, start=False, size=v
        ),
    ):
        with pytest.raises(ValueError, match="must be a string"):
            build(123)


def test_a_count_cannot_be_a_bool() -> None:
    """Bools are ints, so ``cpu=True`` passed every numeric check and
    ``json.dumps`` wrote a boolean. The platform wants a count, not JSON
    ``true``."""
    # ValueError, not the TypeError ``whole`` defaults to: these guards were
    # added with the helper rather than before it, so nothing already reads
    # their type, and ``canonical``, ``real`` and ``scroll_body`` all refuse a
    # wrong shape with a ValueError. A caller wrapping ``create()`` in
    # ``except ValueError`` should not catch the size/template conflict and
    # miss the bool beside it.
    with pytest.raises(ValueError):
        A.create_body(name=None, template=None, cpu=True, ram_mb=None, disk_gb=None, start=False)
    with pytest.raises(ValueError):
        A.resize_body(cpu=True, ram_mb=None, disk_gb=None)
    with pytest.raises(ValueError):
        A.move_body(ram_mb=True, cpu=None, disk_gb=None)
    with pytest.raises(ValueError):
        A.window_body("resize", width=True, height=1)
    # Both halves of window_body agree (OPL-4214). The pointer guards keep the
    # TypeError their own tests read -- `_coordinate` takes the type from its
    # caller rather than owning one.
    with pytest.raises(ValueError):
        A.window_body("move", x=True, y=1)
    # POSITIONALLY, with the real signature. Written as
    # `click_body(x=True, y=1, button="left")` this raised its TypeError on the
    # keyword — `click_body` is `(action, x, y, modifiers)` and has never taken
    # a `button` — so it passed without ever reaching `_whole_point`, and would
    # have gone on passing if the pointer guards started accepting JSON `true`
    # as a coordinate (adversarial review, OPL-4222).
    with pytest.raises(TypeError, match="x must be an integer coordinate"):
        A.click_body("left_click", True, 1, ())


def test_a_count_or_a_duration_is_refused_when_it_is_negative() -> None:
    """`-1` snapshots deleted is not a small answer, it is an unusable one.

    `_require_whole` exists to stop a wire value that cannot be the thing it
    claims to be from reaching a caller as a real one — it already refuses
    `0.9`, `True`, `[]` and `1e309` for both of its call sites. A negative is
    the same failure one line short: neither a count of snapshots destroyed nor
    a number of idle minutes has a negative reading (adversarial review,
    OPL-4480).
    """
    for bad in (-1, -30, -1.0, "-5"):
        with pytest.raises(mc.MandalaError):
            C._require_whole(bad, "bad")

    # Zero is a real answer for both fields and stays one.
    assert C._require_whole(0, "bad") == 0
    assert C._require_whole(-0.0, "bad") == 0
    assert C._require_whole(7, "bad") == 7


def test_a_fractional_pid_is_refused_rather_than_truncated() -> None:
    """`int(3.9)` is 3, and 3 is a DIFFERENT process in the guest.

    The sibling field refuses a fraction because a truncated count is a
    misreported number. Here the same truncation produces a handle whose
    `kill()` lands on another process — a side effect on the wrong target, which
    is strictly worse (adversarial review, OPL-4480).
    """
    for bad in (3.9, float("inf"), float("nan")):
        with pytest.raises(mc.MandalaError):
            C._require_background_pid({"pid": bad})

    # A pid that IS whole still starts, however it was spelled on the wire.
    C._require_background_pid({"pid": 3.0})
    C._require_background_pid({"pid": 4242})
    C._require_background_pid({"pid": "4242"})
