"""The async client must mirror the sync one.

Two implementations of the same API drift: a method gets added to one, a
parameter gets renamed in the other, and the divergence is only discovered by a
user. These tests make the sync client the spec and hold the async one to it.
"""

from __future__ import annotations

import inspect
from typing import Any

import mandala_computer as mc
from mandala_computer import _async_resources, _resources


def public_methods(cls: type) -> dict[str, inspect.Signature]:
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
    (_resources.Computers, _async_resources.AsyncComputers),
    (_resources.Snapshots, _async_resources.AsyncSnapshots),
    (_resources.Templates, _async_resources.AsyncTemplates),
    (_resources.Sizes, _async_resources.AsyncSizes),
]


def test_same_method_names() -> None:
    for sync_cls, async_cls in PAIRS:
        sync_names = set(public_methods(sync_cls))
        async_names = set(public_methods(async_cls))
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


def test_async_io_methods_are_coroutines() -> None:
    """Anything that talks to the API must be awaitable, or it silently blocks."""
    non_io = {"ephemeral"}  # an async context manager, not a coroutine function
    for _, async_cls in PAIRS:
        for name, fn in public_methods(async_cls).items():
            if name in non_io or isinstance(
                inspect.getattr_static(async_cls, name, None), property
            ):
                continue
            assert inspect.iscoroutinefunction(getattr(async_cls, name)), (
                f"{async_cls.__name__}.{name} is not a coroutine function"
            )
            assert fn is not None


def test_field_accessors_are_shared_not_copied() -> None:
    """Both handles read fields through the same code, so they cannot disagree."""
    from mandala_computer._computer import ComputerFields

    assert issubclass(mc.Computer, ComputerFields)
    assert issubclass(mc.AsyncComputer, ComputerFields)
    for field in ("id", "name", "status", "os", "template", "cpu", "ram_mb", "disk_gb", "raw"):
        assert getattr(mc.Computer, field) is getattr(mc.AsyncComputer, field)
