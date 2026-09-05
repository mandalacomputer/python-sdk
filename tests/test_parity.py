"""The async client must mirror the sync one.

Two implementations of the same API drift: a method gets added to one, a
parameter gets renamed in the other, and the divergence is only discovered by a
user. These tests make the sync client the spec and hold the async one to it.
"""

from __future__ import annotations

import ast
import inspect
import sys
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
    (_resources.Builds, _async_resources.AsyncBuilds),
    (_resources.Sizes, _async_resources.AsyncSizes),
    (_resources.Usage, _async_resources.AsyncUsage),
    (_resources.Webhooks, _async_resources.AsyncWebhooks),
]


def test_both_halves_declare_the_same_resources() -> None:
    """``__all__`` is each module's account of its own surface, and one half
    giving a different account from the other is the drift this file is for.

    Nothing star-imports either module — ``__init__`` names every class it
    re-exports — so an omission breaks nothing at runtime and is invisible
    until a documentation build or a ``dir()``-driven tool goes looking. That
    is exactly how it happened: ``Webhooks`` arrived on both halves with
    OPL-4302 and was added to only the async list (adversarial review,
    OPL-4478). PAIRS is the ground truth for what both lists must hold.
    """
    pairs = [(s, a) for s, a in PAIRS if s.__module__ == _resources.__name__]
    assert {s.__name__ for s, _ in pairs} == set(_resources.__all__)
    assert {a.__name__ for _, a in pairs} == set(_async_resources.__all__)


def test_no_sphinx_attribute_comment_is_stranded_on_a_class() -> None:
    """``#:`` documents the assignment BELOW it, so one landing on a ``class``
    is describing something that is no longer there.

    A class carries its own docstring and has no use for the syntax, so the
    only way the two meet is a constant moving out from under its comment and
    leaving the comment behind — which is what happened to
    ``RATE_LIMITED_FLOOR``'s, stranded above ``_LastPoll`` in ``_resources``
    while the constant itself moved to ``_computer`` (adversarial review,
    OPL-4478). Sphinx renders the orphan against whatever follows it, so the
    published documentation says a poll-outcome enum is a sleep floor.
    """
    package = Path(mc.__file__).resolve().parent
    stranded = [
        f"{path.name}:{n}: {lines[n].strip()}"
        for path in sorted(package.glob("*.py"))
        for lines in [path.read_text().splitlines()]
        for n in range(1, len(lines))
        if lines[n].lstrip().startswith("class ") and lines[n - 1].strip().startswith("#:")
    ]
    assert not stranded, stranded


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
        # Builds the stream object. The socket is opened by the first step of
        # the iteration, which is where the `await` is and where it belongs —
        # `async for ev in c.events()` reads correctly and `await c.events()`
        # would be an await for nothing. The stream itself is held to the rule
        # this test exists for by `test_the_async_event_stream_is_iterated_not_awaited`.
        "events",
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


def test_the_async_event_stream_is_iterated_not_awaited() -> None:
    """`events` is exempt above, so the thing it returns carries the rule instead.

    The exemption is about the factory, not about the IO: opening the socket
    still has to be something a caller awaits. Without this, an `events()` that
    lost its async iteration and became an ordinary object with a blocking
    `__iter__` would pass every test here — same name, same parameters, same
    exemption — while stalling the event loop of everyone who used it.
    """
    assert inspect.isasyncgenfunction(mc.AsyncEventStream.__aiter__)
    assert inspect.iscoroutinefunction(mc.AsyncEventStream.aclose)
    assert inspect.isgeneratorfunction(mc.EventStream.__iter__)


def test_the_two_event_streams_offer_the_same_reading() -> None:
    """One is the other awaited, so neither may grow a way to look at a stream.

    The halves share `_StreamBase`, so this is cheap to keep true and expensive
    to discover broken: a property added to one is a fact about a connection
    that half of this SDK's users cannot read.
    """
    sync_names = public_names(mc.EventStream)
    async_names = public_names(mc.AsyncEventStream)
    # close/aclose is the one intentional difference, and the async half keeps
    # BOTH: `close` there is the non-awaiting stop a hook inside the stream's
    # own machinery uses, which has nowhere to await a closing handshake.
    async_names.discard("aclose")
    assert sync_names == async_names, (
        f"only in sync {sorted(sync_names - async_names)}, "
        f"only in async {sorted(async_names - sync_names)}"
    )


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


def test_no_docstring_body_opens_as_an_indented_block() -> None:
    """The general form of the test above, which found the same thing again.

    The strip is of the COMMON indent, so a body written one level deeper than
    its summary keeps that level and renders as a block quote — and any line
    that happens to sit at the summary's own depth escapes the quote, which is
    how :class:`OriginUnreachableError` read: twenty-eight lines quoted and one
    paragraph loose among them (adversarial review, OPL-4479). Every docstring
    in the package rather than one method, because the defect is invisible in
    the source: the file looks tidy, and only the render is wrong.

    A literal block is the one legitimate reason to open a body indented, and
    reST says so with a ``::`` the line before — which is the exemption here.
    Module docstrings are left out for the same reason from the other end: the
    package's own opens with the worked example a caller sees from ``help()``
    rather than with a summary and paragraphs under it.
    """
    package = Path(mc.__file__).resolve().parent
    kinds = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    offenders = []
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, kinds):
                continue
            doc = ast.get_docstring(node, clean=False)
            if not doc:
                continue
            lines = inspect.cleandoc(doc).splitlines()
            body = [line for line in lines[1:] if line.strip()]
            if body and body[0][:1].isspace() and not lines[0].rstrip().endswith("::"):
                owner = getattr(node, "name", "<module>")
                offenders.append(f"{path.name}:{owner}: {body[0].strip()[:60]}")
    assert not offenders, offenders


def test_every_module_declares_what_the_package_re_exports() -> None:
    """``__all__`` is each module's account of its own surface, and the package's
    export list is the thing to check it against.

    Nothing star-imports these modules, so a name missing from one breaks
    nothing at runtime and stays invisible until a documentation build or a
    ``dir()``-driven tool goes looking — which is exactly why it drifts, and
    why ``Move``, ``Retention`` and ``MoveRequiredError`` were all public,
    re-exported and undeclared at once (adversarial review, OPL-4479).

    The whole package rather than one module:
    :func:`tests.test_agent.test_the_module_declares_everything_the_package_re_exports`
    made this claim for ``_agent`` alone, and the three names above sat in the
    two modules it does not read.
    """
    undeclared, unbound = [], []
    for name in mc.__all__:
        # Where the name was DEFINED, which for a type alias is `typing` and
        # for a constant is nowhere — neither of them a module of this package
        # with an account to give.
        home = getattr(getattr(mc, name), "__module__", "")
        module = sys.modules.get(home) if home.startswith(f"{mc.__name__}.") else None
        declared = getattr(module, "__all__", None)
        if declared is not None and name not in declared:
            undeclared.append(f"{home}.__all__ omits {name}")
    for module_name, module in sorted(sys.modules.items()):
        if not module_name.startswith("mandala_computer."):
            continue
        unbound += [
            f"{module_name}.__all__ names {name}, which it does not define"
            for name in getattr(module, "__all__", ())
            if not hasattr(module, name)
        ]
    assert not undeclared, undeclared
    assert not unbound, unbound
