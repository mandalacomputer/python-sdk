"""The async client must mirror the sync one.

Two implementations of the same API drift: a method gets added to one, a
parameter gets renamed in the other, and the divergence is only discovered by a
user. These tests make the sync client the spec and hold the async one to it.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import mandala_computer as mc
from mandala_computer import _async_resources, _resources


def test_the_installed_package_declares_its_inline_types() -> None:
    package = Path(mc.__file__).resolve().parent
    assert (package / "py.typed").is_file()


def public_names(cls: type) -> set[str]:
    """Every attribute a caller can reach — methods and field accessors alike.

    ``inspect.getmembers(cls, callable)`` silently drops properties, because a
    ``property`` object is not callable. Enumerating only those left the whole
    field surface — ``base_url``, ``vnc``, ``resolution``, ``screen``,
    ``is_building`` and the rest — out of the comparison, so one could be added
    to or renamed on a single half and this suite would still pass.
    """
    return {name for name in dir(cls) if not name.startswith("_")}


def public_methods(cls: type) -> dict[str, inspect.Signature]:
    """The callables alone. A property has no call signature to compare."""
    return {
        name: inspect.signature(fn)
        for name, fn in inspect.getmembers(cls, callable)
        if not name.startswith("_")
    }


def params(sig: inspect.Signature) -> list[tuple[str, Any, Any]]:
    """Parameter names, kinds, and defaults — everything a caller can observe."""
    return [(p.name, p.kind, p.default) for p in sig.parameters.values()]


PAIRS = [
    (mc.Client, mc.AsyncClient),
    (mc.Computer, mc.AsyncComputer),
    (mc.BackgroundCommand, mc.AsyncBackgroundCommand),
    (_resources.Computers, _async_resources.AsyncComputers),
    (_resources.Moves, _async_resources.AsyncMoves),
    (_resources.Snapshots, _async_resources.AsyncSnapshots),
    (_resources.Templates, _async_resources.AsyncTemplates),
    (_resources.Sizes, _async_resources.AsyncSizes),
]


def test_same_public_names() -> None:
    for sync_cls, async_cls in PAIRS:
        sync_names = public_names(sync_cls)
        async_names = public_names(async_cls)
        # close/aclose is the one intentional difference — the async variant
        # cannot be spelled the same because it must be awaited.
        sync_names.discard("close")
        async_names.discard("aclose")
        assert sync_names == async_names, (
            f"{sync_cls.__name__} vs {async_cls.__name__}: "
            f"only in sync {sorted(sync_names - async_names)}, "
            f"only in async {sorted(async_names - sync_names)}"
        )


def test_same_call_signatures() -> None:
    for sync_cls, async_cls in PAIRS:
        sync_methods = public_methods(sync_cls)
        async_methods = public_methods(async_cls)
        for name, sig in sync_methods.items():
            if name == "close":
                continue
            assert params(sig) == params(async_methods[name]), (
                f"{sync_cls.__name__}.{name} and {async_cls.__name__}.{name} "
                "take different arguments"
            )


def test_async_io_methods_are_awaitable_or_iterable() -> None:
    """Anything that talks to the API must be awaited or iterated, not called.

    A coroutine function for the ordinary case, and an async generator for the
    one route that answers with a stream. Both are checked, because the failure
    this guards against is the same either way: a plain method doing IO on the
    async client blocks the event loop, and nothing about calling it says so.
    """
    non_io = {
        "ephemeral",  # an async context manager, not a coroutine function
        # Builds a handle around a pid the caller already has. It makes no
        # request — the first poll() is what discovers whether the daemon still
        # knows that pid — so awaiting it would promise IO that never happens.
        "background_command",
    }
    for _, async_cls in PAIRS:
        for name in public_names(async_cls):
            # Reading a field makes no request, so it is not a coroutine. The
            # guard is live now that properties are enumerated at all.
            if name in non_io or isinstance(
                inspect.getattr_static(async_cls, name, None), property
            ):
                continue
            fn = getattr(async_cls, name)
            assert inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn), (
                f"{async_cls.__name__}.{name} is neither a coroutine function "
                "nor an async generator"
            )


def test_a_sync_generator_is_mirrored_by_an_async_one() -> None:
    """The stream route specifically, since the check above accepts either.

    Without this, an `agent_stream` that lost its `yield` on the async side and
    became an ordinary coroutine returning a list would pass every other test
    here — same name, same parameters, still awaitable — while quietly turning
    a stream a caller reports progress from into a minutes-long silence.
    """
    assert inspect.isgeneratorfunction(mc.Computer.agent_stream)
    assert inspect.isasyncgenfunction(mc.AsyncComputer.agent_stream)


def test_field_accessors_are_shared_not_copied() -> None:
    """Both handles read fields through the same code, so they cannot disagree."""
    from mandala_computer._computer import ComputerFields

    assert issubclass(mc.Computer, ComputerFields)
    assert issubclass(mc.AsyncComputer, ComputerFields)
    for field in ("id", "name", "status", "os", "template", "cpu", "ram_mb", "disk_gb", "raw"):
        assert getattr(mc.Computer, field) is getattr(mc.AsyncComputer, field)

    from mandala_computer._computer import BackgroundCommandFields

    assert issubclass(mc.BackgroundCommand, BackgroundCommandFields)
    assert issubclass(mc.AsyncBackgroundCommand, BackgroundCommandFields)
    for field in ("pid", "command", "started_at", "raw"):
        assert getattr(mc.BackgroundCommand, field) is getattr(mc.AsyncBackgroundCommand, field)


def test_wait_for_guest_docstring_renders_as_prose() -> None:
    """`inspect.getdoc` strips the *common* indent, so one shallow line ruins it.

    A paragraph left a level short of the rest of the body dedents to nothing
    while every other line keeps four spaces, and `help()` then prints the
    remainder of the method's documentation as an indented literal block
    (Sphinx warns about it too). It happened to the same paragraph in both
    halves, which is what makes this a parity test.
    """
    for cls in (mc.Computer, mc.AsyncComputer):
        doc = inspect.getdoc(cls.wait_for_guest)
        assert doc is not None
        indented = [line for line in doc.splitlines() if line.strip() and line[0].isspace()]
        assert not indented, f"{cls.__name__}.wait_for_guest: {indented}"
