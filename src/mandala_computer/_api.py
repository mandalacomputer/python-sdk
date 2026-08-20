"""Paths and request bodies, shared by the sync and async clients.

Every route the SDK can reach and every payload it can send is built here. The
two clients differ only in awaits; if either built its own URLs or bodies, they
could drift apart silently — and the surface test that pins the SDK to the
platform's allowlist would only be checking one of them.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

# --- paths ----------------------------------------------------------------

TEMPLATES = "templates"
SIZES = "sizes"
COMPUTERS = "computers"
SNAPSHOTS = "snapshots"


def seg(value: str) -> str:
    """One path segment, percent-encoded — including ``/``.

    Every id the caller can hand us goes through here. Computer and snapshot
    ids are minted by the platform in a known alphabet, so in the ordinary case
    this changes nothing; but ``get()`` takes whatever string it is given, and
    an id is the one part of these URLs that does not come from this file. An
    unescaped ``/`` would not merely 404 — it would re-point the request at a
    route nobody meant, with the account's bearer token on it, and a ``?``
    would put query keys on a request whose own parameters are what interlocks
    like the snapshot-purge fingerprint are carried in.

    Encoding cannot answer ``.`` and ``..``: ``quote`` leaves a dot alone
    whatever ``safe`` says, and the dot-segment removal in RFC 3986 is applied
    by the client to the assembled URL, so ``get("..")`` would climb a level
    and address ``/api/v1`` itself — and an id of ``..`` would turn one
    computer's ``/snapshots`` into the account's whole snapshot list, which is
    a bad thing to hand a purge loop. An empty id does the same to the
    collection route. Neither is a real id, so both are refused here.
    """
    if not value.strip("."):
        raise ValueError(f"id must not be empty or all dots: {value!r}")
    return quote(value, safe="")


def computer(computer_id: str) -> str:
    return f"computers/{seg(computer_id)}"


def computer_action(computer_id: str, action: str) -> str:
    """start | stop | suspend | restart | clone | screenshot | input | exec |
    snapshots | schedule."""
    return f"computers/{seg(computer_id)}/{action}"


def exec_handle(computer_id: str, pid: int) -> str:
    """A backgrounded command, addressed by the guest pid ``exec`` answered with.

    Not ``computer_action``: the pid is a second path segment, and the platform's
    own ``patternFor`` reduces it to ``:pid`` rather than to ``:id`` — it names
    something inside a computer rather than a thing the platform owns.
    """
    return f"computers/{seg(computer_id)}/exec/{pid}"


def window(computer_id: str, window_id: str) -> str:
    """One window on the guest's desktop, addressed by its X id.

    The window id has the most room to surprise of any id here — it is whatever
    the guest's window manager called a window, rather than something the
    platform minted — but it is encoded by the same :func:`seg` every other id
    goes through, because the consequence of a stray separator does not depend
    on who chose the string.
    """
    return f"computers/{seg(computer_id)}/windows/{seg(window_id)}"


def snapshot(snapshot_id: str) -> str:
    return f"snapshots/{seg(snapshot_id)}"


def snapshot_action(snapshot_id: str, action: str) -> str:
    """restore | clone."""
    return f"snapshots/{seg(snapshot_id)}/{action}"


def files(computer_id: str) -> str:
    return f"computers/{seg(computer_id)}/files"


def is_absolute_guest_path(path: str) -> bool:
    r"""Whether a guest would read this path as absolute, on either family.

    Nothing here knows which OS the guest runs — a ``Computer`` does not say —
    and the two families disagree about what absolute means. A leading ``/`` is
    absolute on Linux and *drive-relative* on Windows, where the daemon's own
    ``validGuestPath`` refuses it and wants ``C:\...`` or a ``\\`` UNC share. So
    both spellings pass here and the server, which does know which guest it is
    talking to, keeps the final say.

    What this still catches without a round trip is the mistake worth catching:
    a bare relative path, which has no working directory to be relative to on
    either family.
    """
    if path.startswith(("/", "\\\\")):
        return True
    # Drive-qualified: C:\ or C:/, both of which the daemon accepts.
    return (
        len(path) >= 3
        and path[0].isascii()
        and path[0].isalpha()
        and path[1] == ":"
        and path[2] in "\\/"
    )


def looks_windows_guest_path(path: str) -> bool:
    r"""Whether the path is spelled the way only a Windows guest spells it.

    Weaker than :func:`is_absolute_guest_path` on purpose: this asks which
    *family* a path belongs to, not whether it is absolute, so the
    drive-relative ``C:notes.txt`` counts here and does not there.

    What it is for is the rules that must not be applied to the other family. A
    ``\`` separates nothing on Linux and a ``:`` is an ordinary character in a
    Linux filename, so treating every path as possibly-Windows quietly renames
    ``/tmp/a:b.txt`` to ``b.txt``. A leading ``/`` says Linux; a drive or a UNC
    prefix says Windows; a bare relative name says neither, and is left to the
    permissive reading, where the two families agree anyway.
    """
    if path.startswith("\\\\"):  # UNC share
        return True
    return len(path) >= 2 and path[0].isascii() and path[0].isalpha() and path[1] == ":"


def files_params(path: str) -> dict[str, str]:
    """The query naming which guest file, checked before the round trip.

    The path must be absolute: nothing about a transfer runs in a shell, so a
    relative path has no working directory to be relative to. The daemon
    refuses it too, but this mistake is knowable without the round trip.
    """
    if not is_absolute_guest_path(path):
        raise ValueError(f"guest path must be absolute: {path!r}")
    return {"path": path}


def partial_params(allow_partial: bool) -> dict[str, str] | None:
    """The opt-in to a knowingly short fan-out listing.

    Omitted rather than sent as ``0`` when the caller did not ask: the platform
    reads the key's presence, and an explicit falsey value is the kind of thing
    a proxy or a future server version could read either way. Nothing is the
    unambiguous spelling of "I did not ask for this".
    """
    return {"allow_partial": "1"} if allow_partial else None


def snapshot_listing_params(*, include_unfinished: bool, allow_partial: bool) -> dict[str, str]:
    """The query on ``GET /snapshots``.

    ``include=unfinished`` widens the listing to deletions that began and did
    not finish. They are not restorable or clonable — their state reads
    ``deleting`` — but they still hold objects and are still billed, so this is
    the flag for a question about storage rather than about what can be used.
    """
    params = dict(partial_params(allow_partial) or {})
    if include_unfinished:
        params["include"] = "unfinished"
    return params


def windows_params(include_all: bool) -> dict[str, str] | None:
    """``include=all`` to keep the desktop's own furniture in the listing.

    Off by default because panels, docks and the wallpaper window are not
    windows a caller acts on — a stock guest showing one terminal has five.
    """
    return {"include": "all"} if include_all else None


def delete_params(*, purge_snapshots: bool, expect: str | None) -> dict[str, str] | None:
    """The query that turns a delete into a delete-and-purge.

    ``expect`` is required here, and the platform's own rule is weaker — it
    accepts an unguarded purge, for callers with no way to read the holdings.
    This SDK has one call away, so the refusal costs nothing and buys the
    interlock: the fingerprint binds the sweep to the set that was actually
    looked at, and the daemon refuses it if a capture has landed since. Without
    it the purge is bound to whatever the set happens to be at the moment it
    fires, which is not the thing anybody agreed to destroy.

    ``expect`` is dropped rather than carried when nothing is being purged. A
    stale fingerprint on an ordinary delete would refuse it for a reason that
    has nothing to do with what was asked.
    """
    if not purge_snapshots:
        return None
    if not expect:
        raise ValueError(
            "purging snapshots needs the fingerprint from snapshot_holdings(): "
            "read it, check the count and size are what you meant to destroy, "
            "and pass it as expect=. Nothing has been deleted."
        )
    return {"snapshots": "delete", "expect": expect}


# --- responses ------------------------------------------------------------


def computer_payload(data: Any) -> dict[str, Any]:
    """Flatten a response that is one computer, in either shape it can arrive in.

    A create whose guest was made and then would not boot answers 201 with
    ``{"computer": {...}, "start_error": "..."}`` rather than an error alone —
    deliberately, so the caller learns the id of the machine it is now paying
    for instead of having to list to find it.

    Read as an ordinary computer that envelope is a computer with no id: every
    field reads off the wrapper, finds nothing, and the id the platform went out
    of its way to return is the one thing dropped. So it is unwrapped here, and
    the failure travels on the record beside the fields it belongs to — see
    :attr:`~mandala_computer.Computer.start_error`.

    Every response that is one computer goes through this, not just the create.
    The envelope is the platform's shape for "here is your machine, and here is
    what went wrong with it", and a second route answering that way should not
    need a second discovery of this function.
    """
    if not isinstance(data, Mapping):
        return {}
    inner = data.get("computer")
    if not isinstance(inner, Mapping):
        return dict(data)
    # start_error kept alongside the fields rather than in a parallel return, so
    # it survives into `raw` and cannot be dropped by a caller that only wanted
    # the computer. A refresh replaces the record and clears it, which is right:
    # it describes one start attempt, not the machine.
    return {**inner, "start_error": data.get("start_error")}


# --- bodies ---------------------------------------------------------------


def create_body(
    *,
    name: str | None,
    template: str | None,
    cpu: int | None,
    ram_mb: int | None,
    disk_gb: int | None,
    start: bool,
    resolution: str | None = None,
    size: str | None = None,
) -> dict[str, Any]:
    """Build a create payload, omitting anything unset.

    Omission is meaningful: the server applies the template's defaults only when
    a key is absent, so sending explicit nulls would override them with nothing.

    A ``size`` names a template and a shape together, so combining it with any
    of the four it stands in for is refused here — the server refuses it too,
    but this mistake is knowable without the round trip, and the server's
    refusal exists for callers who are not this SDK.
    """
    if size is not None and any(v is not None for v in (template, cpu, ram_mb, disk_gb)):
        raise ValueError(
            "size already names a template and a shape; send size alone, "
            "or template/cpu/ram_mb/disk_gb without it"
        )
    body: dict[str, Any] = {"start": start}
    for key, value in (
        ("name", name),
        ("size", size),
        ("template", template),
        ("cpu", cpu),
        ("ram_mb", ram_mb),
        ("disk_gb", disk_gb),
        ("resolution", resolution),
    ):
        if value is not None:
            body[key] = value
    return body


def name_body(name: str | None) -> dict[str, Any]:
    return {} if name is None else {"name": name}


def rename_body(name: str) -> dict[str, Any]:
    """The rename payload.

    Empty is refused here rather than at the server, which refuses it too. On
    create an omitted name means "you pick one"; on rename it can only mean a
    caller cleared the field, and a round trip to be told so is a round trip
    that never had to happen.
    """
    if not name.strip():
        raise ValueError("name must not be empty")
    return {"name": name}


def exec_body(
    command: str,
    timeout_s: int,
    desktop: bool = False,
    *,
    background: bool = False,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build an exec payload.

    ``session`` is omitted rather than sent empty when ``desktop`` is false: the
    server's default is the system context, and the only value it accepts is
    ``"desktop"``.

    ``timeout_s`` is omitted alongside ``background`` rather than sent and
    ignored. The server does ignore it — not waiting is the whole request — but
    a payload carrying a deadline that means nothing is a payload somebody will
    later read as a promise the platform never made.

    ``cwd`` must be absolute for the reason a file transfer's path must be: the
    guest agent inherits whatever directory it was started in, so a relative one
    resolves somewhere nobody named.
    """
    body: dict[str, Any] = {"command": command}
    if not background:
        body["timeout_s"] = timeout_s
    if desktop:
        body["session"] = "desktop"
    if background:
        body["background"] = True
    if cwd is not None:
        if not is_absolute_guest_path(cwd):
            raise ValueError(f"cwd must be absolute: {cwd!r}")
        body["cwd"] = cwd
    if env:
        body["env"] = dict(env)
    return body


def open_url_command(url: str) -> str:
    """Build the shell command that puts ``url`` on the guest's screen.

    The browser is named rather than asked for. ``xdg-open`` is the portable way
    to want this and is installed on the base template, along with ``exo-open``,
    ``sensible-browser`` and ``x-www-browser`` — and every one of them exits 0
    and launches nothing, because the image's default-browser association points
    at a desktop entry it does not ship. Exit 0 and an unchanged screen is the
    worst shape a failure can take, so this asks for Firefox, which is the only
    browser on the image anyway.

    This function is the one place that decision lives. When the platform fixes
    the association (OPL-3376), this is the line that changes, rather than every
    caller's prompt.

    Detached, because a browser does not exit on its own: in the foreground the
    call would block until the timeout killed it and come back as a failure,
    having opened the window anyway.
    """
    url = url.strip()
    if not url:
        raise ValueError("url must not be empty")
    # shlex.quote stops the URL reaching the shell as anything but one argument.
    # It cannot stop the *browser* reading a leading dash as a flag, and no URL
    # starts with one, so that is refused outright rather than quoted.
    if url.startswith("-"):
        raise ValueError(f"url must not start with '-': {url!r}")
    return f"nohup firefox {shlex.quote(url)} >/dev/null 2>&1 &"


def snapshot_body(memory: bool, name: str | None = None) -> dict[str, Any]:
    """A capture request. An omitted name asks the platform to generate one."""
    body: dict[str, Any] = {"memory": memory}
    if name is not None:
        body["name"] = name
    return body


# The eight things the window manager will do to one window. Checked here rather
# than left to the server, because a typo'd action is knowable without the round
# trip and the error naming the set is more use than a 400 naming the field.
WINDOW_ACTIONS = (
    "focus",
    "raise",
    "minimize",
    "maximize",
    "unmaximize",
    "close",
    "move",
    "resize",
)


def window_body(
    action: str,
    *,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict[str, Any]:
    """One action on one window, with the geometry the action needs.

    Half a point and half a size are refused rather than completed with a zero,
    for the reason ``drag_body`` refuses half an origin: a caller naming only
    ``x`` meant to name a position, and quietly filling the other half moves the
    window to the edge of the screen while the call reports success. The action
    happens, in the wrong place, and nothing says so.
    """
    if action not in WINDOW_ACTIONS:
        raise ValueError(f"action must be one of {WINDOW_ACTIONS}")
    if (x is None) != (y is None):
        raise ValueError("give both x and y, or neither")
    if (width is None) != (height is None):
        raise ValueError("give both width and height, or neither")
    body: dict[str, Any] = {"action": action}
    if x is not None and y is not None:
        body["x"], body["y"] = x, y
    if width is not None and height is not None:
        body["width"], body["height"] = width, height
    return body


def resize_body(*, cpu: int | None, ram_mb: int | None, disk_gb: int | None) -> dict[str, Any]:
    """A new shape for a stopped computer.

    Its own body rather than a field on a general update, because the platform
    refuses a resize in combination with a rename or an idle window and is right
    to: a resize needs the computer stopped and the other two do not, so one
    request cannot honour both without applying half of it. Three methods that
    each send one group is the shape that cannot ask for the refused thing.

    A disk grows only. That is the server's rule, not checked here — shrinking
    is a coherent request that this SDK has no way to know is refused for this
    computer, and guessing at the current size to reject it would be a client
    inventing a limit.
    """
    body = {
        key: value
        for key, value in (("cpu", cpu), ("ram_mb", ram_mb), ("disk_gb", disk_gb))
        if value is not None
    }
    if not body:
        raise ValueError("resize() needs at least one of cpu, ram_mb or disk_gb")
    return body


def idle_suspend_body(minutes: int | None) -> dict[str, Any]:
    """How long this computer may sit untouched before its host suspends it.

    ``None`` is sent, not omitted, and that is the whole reason this is not
    folded into a generic body builder that drops falsey values: an explicit
    null is how the override is cleared, returning the computer to whatever its
    host is sweeping at. Dropped, it would mean "change nothing", which is the
    opposite request.

    ``0`` is the third state and the only one that is not a duration: it pins
    the computer against the sweep entirely. That is what a long job started
    inside the guest needs — a build or a batch run sends nothing from outside,
    so it is idle by every measure the host can take, and it would otherwise be
    suspended under its own feet. So ``0`` and ``None`` are opposites here
    rather than two spellings of "no setting", and only a negative is refused.

    The platform requires this to be the only field in the PATCH, which is why
    it has a method of its own rather than a keyword on ``rename``.
    """
    if minutes is not None and minutes < 0:
        raise ValueError(
            f"idle_suspend_min cannot be negative: {minutes!r}. Send 0 to stop this computer "
            "being suspended for idleness, or None to follow its host's own window"
        )
    return {"idle_suspend_min": minutes}


def schedule_body(*, enabled: bool, hour: int, minute: int, tz: str) -> dict[str, Any]:
    if not 0 <= hour <= 23:
        raise ValueError("hour must be 0-23")
    if not 0 <= minute <= 59:
        raise ValueError("minute must be 0-59")
    return {"enabled": enabled, "hour": hour, "minute": minute, "tz": tz}


# --- input ----------------------------------------------------------------
#
# The verb set is Anthropic's computer tool, in full. The platform accepts both
# that vocabulary and this SDK's flatter one, so these bodies use whichever is
# clearer for each action — what matters is that every verb a computer-use model
# can emit has a method here, because the alternative is every user of this SDK
# writing the same seven stubs.


def pointer_body(action: str, x: int, y: int) -> dict[str, Any]:
    return {"action": action, "x": x, "y": y}


def _whole_point(x: int | None, y: int | None) -> None:
    """Refuse half a coordinate, for the reason :func:`drag_body` refuses half
    an origin.

    Omitting both is a real request — "wherever the pointer already is" — and a
    different one from (0, 0). Giving one of the two is neither: it reads as a
    caller who meant to name a point, and zero-filling the half they left out
    produces a click or a scroll that succeeds somewhere else entirely. Nothing
    reports that, which is what makes it worth a ``ValueError`` here.
    """
    if (x is None) != (y is None):
        raise ValueError("give both x and y, or neither")


def click_body(
    action: str, x: int | None, y: int | None, modifiers: tuple[str, ...]
) -> dict[str, Any]:
    """A click, optionally at a point and optionally with keys held down.

    No coordinate means "where the pointer already is", which is a real and
    different request from clicking (0, 0) — so the keys are omitted rather than
    sent as zeros.
    """
    _whole_point(x, y)
    body: dict[str, Any] = {"action": action}
    if x is not None and y is not None:
        body["x"] = x
        body["y"] = y
    if modifiers:
        body["text"] = "+".join(modifiers)
    return body


def drag_body(from_x: int | None, from_y: int | None, to_x: int, to_y: int) -> dict[str, Any]:
    """A press, a move, and a release — one gesture, not two clicks.

    ``start_coordinate`` is omitted when the caller did not give one, which asks
    the platform to drag from wherever the pointer is. It refuses that if nothing
    has moved the pointer yet, rather than guessing at an origin.

    Half an origin is refused here rather than dropped. ``drag(90, 80,
    from_x=10)`` reads as a caller who meant to name a starting point, and
    silently ignoring the half they gave produces a drag that succeeds while
    selecting a different region — the worst shape a mistake can take, because
    nothing reports it.
    """
    if (from_x is None) != (from_y is None):
        raise ValueError("give both from_x and from_y, or neither")
    body: dict[str, Any] = {"action": "left_click_drag", "coordinate": [to_x, to_y]}
    if from_x is not None and from_y is not None:
        body["start_coordinate"] = [from_x, from_y]
    return body


def button_body(action: str, x: int | None, y: int | None) -> dict[str, Any]:
    """left_mouse_down / left_mouse_up, optionally moving first."""
    _whole_point(x, y)
    body: dict[str, Any] = {"action": action}
    if x is not None and y is not None:
        body["x"] = x
        body["y"] = y
    return body


_SCROLL_DIRECTIONS = ("up", "down", "left", "right")


def scroll_body(
    x: int | None, y: int | None, direction: str, amount: int, modifiers: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A wheel scroll, optionally at a point and optionally with keys held.

    The coordinate is omitted when the caller did not give one, which scrolls
    whatever is under the pointer. Sending zeros instead would move the pointer
    to the top-left corner first and scroll whatever happens to be there — which
    is what a defaulted ``scroll()`` did before the coordinate keys became
    optional.
    """
    if direction not in _SCROLL_DIRECTIONS:
        raise ValueError(f"direction must be one of {_SCROLL_DIRECTIONS}")
    _whole_point(x, y)
    body: dict[str, Any] = {
        "action": "scroll",
        "scroll_direction": direction,
        "amount": amount,
    }
    if x is not None and y is not None:
        # The tool-native spelling, not the flat pair. The platform reads a flat
        # x/y of 0,0 on a scroll as "no position" — it has to, because that is
        # what this SDK sent for every defaulted scroll before the arguments
        # became optional — so a caller who genuinely means the top-left corner
        # cannot say so that way. `coordinate` has no such history and is
        # unambiguous, which makes scroll(0, 0) mean the corner again.
        body["coordinate"] = [x, y]
    if modifiers:
        body["text"] = "+".join(modifiers)
    return body


def type_body(text: str) -> dict[str, Any]:
    return {"action": "type", "text": text}


def key_body(keys: tuple[str, ...]) -> dict[str, Any]:
    if not keys:
        raise ValueError("key() needs at least one key")
    return {"action": "key", "keys": list(keys)}


def hold_key_body(keys: tuple[str, ...], seconds: float) -> dict[str, Any]:
    if not keys:
        raise ValueError("hold_key() needs at least one key")
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    return {"action": "hold_key", "keys": list(keys), "duration": seconds}


def wait_body(seconds: float) -> dict[str, Any]:
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    return {"action": "wait", "duration": seconds}


def cursor_body() -> dict[str, Any]:
    return {"action": "cursor_position"}


def screenshot_params(width: int | None, fresh: bool = False) -> dict[str, Any] | None:
    """``w`` downscales, ``fresh`` skips the cache.

    A bare screenshot may be served from a frame up to 1.5 seconds old, which is
    right for a thumbnail and wrong for a loop: a model shown the frame from
    before its own click concludes the click missed and clicks again, and the
    second one lands on whatever the first one opened. ``fresh`` is therefore
    not an optimisation to reach for when a screenshot looks stale — it is what
    every screenshot feeding a decision wants, and the cost of it is one capture.

    Sent as ``1`` rather than ``true``: the platform documents the parameter as
    that single value and matches on it.
    """
    params: dict[str, Any] = {}
    if width:
        params["w"] = width
    if fresh:
        params["fresh"] = 1
    return params or None


#: The platform's ceiling on ``max_steps``, mirrored.
#:
#: ``MAX_MAX_STEPS`` in the platform's ``web/lib/agent.ts``, and kept in step by
#: ``scripts/check_surface.py`` — a mirror nobody compares is a comment, and one
#: that drifts refuses a run the platform would have taken.
#:
#: Capped rather than obeyed for the reason the platform gives: each step is a
#: model call plus a screenshot on the caller's own key, so a ``max_steps`` of
#: ten thousand is a request to spend their money for an hour on a task that has
#: plainly gone wrong.
MAX_STEPS = 100


def agent_body(
    prompt: str,
    *,
    stream: bool,
    system: str | None = None,
    max_steps: int | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """One agent run's request.

    An empty prompt is refused here rather than sent. The platform would answer
    400, but a run is the one call on this surface where a round trip is not the
    whole cost of getting it wrong: the request that comes back is billed
    against the caller's own model key, and nothing else in this file lets a
    caller spend money to be told they typed nothing.

    ``max_steps`` is checked for the same reason — it is the spending bound, and
    a zero or a negative is a request to do no work, which is not what anybody
    means by it. It is checked the way the platform checks it, rather than only
    at the near end of the range: a whole number, at least 1, and no more than
    :data:`MAX_STEPS`. A ``2.5`` or a ``10000`` that got through here would come
    back as the 400 this function exists to save the caller.
    """
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if max_steps is not None:
        if not isinstance(max_steps, int):
            raise ValueError("max_steps must be a whole number")
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if max_steps > MAX_STEPS:
            raise ValueError(f"max_steps may not exceed {MAX_STEPS}")
    body: dict[str, Any] = {"prompt": prompt, "stream": stream}
    for key, value in (("system", system), ("max_steps", max_steps), ("model", model)):
        if value is not None:
            body[key] = value
    return body


def stop_params(force: bool) -> dict[str, Any] | None:
    """``force=true`` pulls the power instead of asking the guest to shut down.

    Absent, the guest is asked and given time to do it. Present, it is not —
    the equivalent of holding the button in, and anything the guest had not
    written to disk is lost with it. Kept off by default for that reason: this
    is what to reach for when a guest will not come down on its own, not the
    ordinary way to stop one.
    """
    return {"force": "true"} if force else None
