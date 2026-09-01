#!/usr/bin/env python
"""Drive the event stream against a REAL computer, once, and delete it.

The rest of this repository mocks the transport and stands in for the socket,
which is the right way to pin behaviour and the wrong way to learn what the
platform actually sends: the fixtures were written from the same reading of the
reference that produced the code they check, so a wrong reading is asserted
rather than caught. This is the other direction. The TypeScript sibling's first
run of its own version found ``Computer.windows()`` broken against the live
platform — a method every mock in that suite said worked, because the fixture
agreed with the bug.

Not read-only. It CREATES a computer, drives it for a minute or so and deletes
it in a ``finally``. That is somebody's hypervisor and a few minutes of billable
time, so it lives here rather than in ``tests/`` and is never part of ``pytest``.

    MANDALA_API_KEY=... python scripts/smoke_events.py

What it proves, in the order the checks run: that a desktop announces itself
rather than being screenshotted for; that a second wait on an up desktop returns
at once instead of forever; that the opening frame carries the vocabulary and
the desktop; that a background command's exit arrives off the wire with its real
code; that a window opening is described, in the same coordinates the listing
gives; that a stored cursor resumes; and that a suspended computer is refused
with a sentence naming the suspend.
"""

from __future__ import annotations

import os
import sys
import time

import mandala_computer as mc

KEY = (os.environ.get("MANDALA_API_KEY") or "").strip()
if not KEY:
    print("smoke_events — no MANDALA_API_KEY, so nothing to call. Skipped.")
    sys.exit(0)

STARTED = time.monotonic()
FAILURES = 0


def el() -> str:
    return f"+{time.monotonic() - STARTED:.1f}s"


def check(what: str, good: object, detail: str = "") -> None:
    global FAILURES
    suffix = f" — {detail}" if detail else ""
    if good:
        print(f"  ok   {el()} {what}{suffix}")
    else:
        FAILURES += 1
        print(f"  FAIL {el()} {what}{suffix}")


def main() -> int:
    client = mc.Client(KEY)
    print(f"smoke_events — {client.base_url}\n")
    vm = client.computers.create(template="base", name=f"sdk-events-{int(time.time())}")
    print(f"  created {vm.id} ({vm.status}) {el()}")
    try:
        vm.wait_until_running(timeout=180)

        # The headline. On a computer that has just been created this is the
        # real event; on one somebody else brought up it is the opening frame's
        # state arriving in the shape the caller is already reading.
        ready = vm.wait_for("computer.ready", timeout=240)
        check("computer.ready arrives", ready.type == "computer.ready", f"source={ready.source}")

        # And now the desktop IS up, so the event cannot happen again for this
        # session. A raw socket waiting here waits forever; this must not.
        at = time.monotonic()
        again = vm.wait_for("computer.ready", timeout=30)
        took = time.monotonic() - at
        check(
            "a second wait returns at once on an up desktop",
            again.synthesized and took < 15,
            f"{took:.1f}s",
        )

        # Closed in a `with`, and `reconnect=False`. A stream left parked at its
        # yield keeps its socket open and reconnects on its own while the waits
        # below are opening streams of their own.
        with vm.events(reconnect=False) as stream:
            first = next(iter(stream), None)
            check("the opening frame lands before the first event", first is not None)
            types = stream.event_types or []
            check(
                "it advertises the guest half of the vocabulary",
                "window.opened" in types,
                f"{len(types)} types",
            )
            check(
                "it carries the desktop this stream joined",
                stream.windows is not None,
                f"{len(stream.windows or [])} windows",
            )
            kept = stream.cursor
            check("the stream kept a cursor to resume from", isinstance(kept, str), str(kept))

        job = vm.start_exec("sleep 4; exit 7")
        # Matched on the pid, because a wait returns the first exit on the WHOLE
        # computer and a freshly booted guest has others of its own — session
        # setup, desktop autostart, whatever the template runs. Taking the first
        # one asserts a pid and a code about a process nobody asked about.
        exited = None
        with vm.events(since=kept, timeout=90) as stream:
            for ev in stream:
                if ev.type == "process.exited" and ev.pid == job.pid:
                    exited = ev
                    break
        check(
            "process.exited carries the pid and the real code",
            exited is not None and exited.exit_code == 7 and exited.lost is False,
            f"pid={getattr(exited, 'pid', None)} code={getattr(exited, 'exit_code', None)}",
        )

        # The window half. `open()` is what causes it, so the stream is opened
        # BEFORE the call — one opened afterwards joins at the head and misses
        # the event it is waiting for.
        with vm.events(timeout=120) as stream:
            reader = iter(stream)
            vm.open("https://example.com")
            opened = None
            for ev in reader:
                if ev.type == "window.opened" and ev.window is not None:
                    opened = ev
                    break
        check(
            "window.opened describes the window, and says the guest said so",
            opened is not None and opened.source == "guest",
            f"{opened.window.wm_class if opened and opened.window else '?'}",
        )
        assert opened is not None and opened.window is not None

        # The coordinate convention, which the platform pins in its own e2e for
        # the same reason: an event and a listing that disagree send every click
        # to the wrong place.
        listed = next((w for w in vm.windows() if w.id == opened.window.id), None)
        check(
            "the event and the listing put the window in the same place",
            listed is not None and (listed.x, listed.y) == (opened.window.x, opened.window.y),
            f"event={(opened.window.x, opened.window.y)} listing="
            f"{(listed.x, listed.y) if listed else None}",
        )
        check("a window on the screen reads as visible", listed is not None and listed.visible)

        # What a process restart would do: come back from a stored cursor and be
        # handed what happened while nobody was listening.
        vm.start_exec("true")
        seen: list[str] = []
        with vm.events(since=kept, reconnect=False, timeout=45) as stream:
            for ev in stream:
                seen.append(ev.type)
                if ev.type == "process.exited":
                    break
        check(
            "a resume is handed what happened while it was gone",
            "process.exited" in seen,
            ",".join(seen) or "nothing",
        )
        check(
            "and no manufactured readiness in front of it",
            "computer.ready" not in seen,
            ",".join(seen) or "nothing",
        )

        # The refusal a websocket carries no status for in a browser, and which
        # this SDK reads straight off the 409 the platform sent.
        vm.suspend()
        try:
            vm.wait_for("computer.ready", timeout=20)
            refused: BaseException | None = None
        except mc.MandalaError as err:
            refused = err
        check(
            "a suspended computer is refused, and the refusal names the suspend",
            refused is not None and "suspended" in str(refused),
            str(refused)[:90],
        )
    finally:
        # In a `finally`: a smoke test that leaves a computer behind on its own
        # failure bills somebody for the bug it found.
        try:
            vm.delete()
            print(f"  deleted {vm.id} {el()}")
        except mc.MandalaError as err:
            print(f"  delete failed: {err}")

    print(f"\nsmoke_events — all checks passed {el()}" if not FAILURES else f"\n{FAILURES} FAILED")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
