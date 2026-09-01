"""The SDK's own public request-making surface, read off its source.

The rest of ``test_surface.py`` compares this SDK to the platform: every call
lands on an allowlisted route, every documented parameter is sent or pinned.
This module answers the question those cannot, which points the other way — is
every method the SDK offers actually driven by the exercise at all?

The completeness check next door counted ROUTES (OPL-3900)::

    assert ALLOWED - called_routes(route.calls) == UNIMPLEMENTED

which proves every route was reached by something. That is a different claim
from "every method was called" the moment two methods share a route, and on this
surface many do: ``exec``, ``open`` and ``wait_for_guest`` are all
``POST computers/:id/exec``; ``agent``, ``agent_once`` and ``agent_stream`` are
all ``POST computers/:id/agent``; every mouse and keyboard method is
``POST computers/:id/input``. Add a second method to one of those routes, forget
to add it to the exercise, and nothing goes red — the route was already reached
by its neighbour. Eight methods had already gone that way when this was
written: ``open``, ``agent_stream``, ``wait_until_built``, ``wait_until_running``,
``wait_for_guest``, ``wait_for_move``, ``Computers.ephemeral`` and
``Builds.wait`` shipped with no surface coverage at all, on a suite whose stated
design is that the surface is enumerable.

Sync/async parity does not close it either: parity proves the two halves agree,
which two halves that are both missing the same method do.

**Derived, not maintained.** A hand-written list of public methods is a second
place to forget the method, one step further back — the same failure with an
extra hop. So the inventory is read out of the source: a public method is
request-making when its body reaches ``self._t.<verb>``, directly or through the
private helpers it calls. That is the whole of how this SDK talks to the wire —
:class:`~mandala_computer._client.Transport` is reached one way, through one
attribute, from every resource and both computer classes — so the rule has no
holes to fall through, and a method added tomorrow is in the inventory the
moment it is written rather than when somebody remembers it.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import mandala_computer

#: The attribute every resource and both computer classes hold their transport
#: on. One name, checked literally: a class that reached the wire some other way
#: would be invisible here, so the narrow rule is deliberate — it is the
#: convention this SDK actually follows, and a second spelling should fail this
#: check rather than be quietly accommodated by a looser one.
TRANSPORT = "_t"

#: Every method on :class:`~mandala_computer._client.Transport` that puts a
#: request on the wire. ``request`` is the one they all end in; the rest are the
#: shaping wrappers around it — a JSON object, an array, a listing with its
#: short-answer header, raw bytes, a byte range, a stream of frames.
VERBS = frozenset(
    {
        "request",
        "sse",
        "json",
        "json_object",
        "json_object_or_empty",
        "json_array",
        "listing",
        "binary",
        "binary_part",
    }
)

#: Public transport members that deliberately do not put a request on the
#: wire. Together with :data:`VERBS`, this makes the classification exhaustive:
#: adding a second request wrapper (or any other transport spelling) must be
#: classified here rather than silently disappearing from the inventory.
NON_REQUEST_TRANSPORT = frozenset({"aclose", "base_url", "close", "phase_ceiling"})


class _Reaches(ast.NodeVisitor):
    """What one method body touches: the transport, and its own siblings.

    Both are collected in one pass because the second is what makes the first
    complete. ``Computer.click`` never names the transport — it calls
    ``self._input``, which does — and a rule that only looked for the direct
    touch would call the entire input surface non-requesting.
    """

    def __init__(self) -> None:
        self.transport = False
        self.unclassified_transport: set[str] = set()
        self.siblings: set[str] = set()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        inner = node.value
        if (
            isinstance(inner, ast.Attribute)
            and inner.attr == TRANSPORT
            and isinstance(inner.value, ast.Name)
            and inner.value.id == "self"
        ):
            if node.attr in VERBS:
                self.transport = True
            elif node.attr not in NON_REQUEST_TRANSPORT:
                self.unclassified_transport.add(node.attr)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            self.siblings.add(func.attr)
        self.generic_visit(node)


def _methods(cls: ast.ClassDef) -> dict[str, _Reaches]:
    found: dict[str, _Reaches] = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            reaches = _Reaches()
            for statement in node.body:
                reaches.visit(statement)
            if reaches.unclassified_transport:
                members = ", ".join(
                    f"self.{TRANSPORT}.{name}" for name in sorted(reaches.unclassified_transport)
                )
                raise ValueError(
                    f"unclassified transport member in {cls.name}.{node.name}: {members}"
                )
            found[node.name] = reaches
    return found


def requesting_methods(source: str) -> dict[str, frozenset[str]]:
    """Public request-making methods in one module, by the class defining them.

    A method is request-making when its body reaches the transport, or calls
    something on ``self`` that does — closed over transitively, so a chain of
    private helpers is followed to the end. Classes with none are left out
    entirely rather than mapped to an empty set: a model or an exception is not
    a surface with nothing on it, it is not this kind of thing at all.
    """
    found: dict[str, frozenset[str]] = {}
    for cls in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef)):
        bodies = _methods(cls)
        requesting = {name for name, reaches in bodies.items() if reaches.transport}
        # Widen until nothing new is reached. Bounded by the method count, since
        # each pass either adds a name or stops, and a cycle of mutually
        # recursive helpers settles rather than spinning.
        while True:
            grown = {
                name
                for name, reaches in bodies.items()
                if name not in requesting and reaches.siblings & requesting
            }
            if not grown:
                break
            requesting |= grown
        public = frozenset(name for name in requesting if not name.startswith("_"))
        if public:
            found[cls.name] = public
    return found


def _package_modules() -> Iterator[ModuleType]:
    package = Path(mandala_computer.__file__).parent
    for path in sorted(package.glob("*.py")):
        name = path.stem
        yield importlib.import_module(
            "mandala_computer" if name == "__init__" else f"mandala_computer.{name}"
        )


def inventory() -> dict[type, frozenset[str]]:
    """Every public request-making callable in the package, by its class.

    Every module is read, rather than a named few. A list of modules to look in
    is the same maintenance hazard as a list of methods: the module somebody
    adds is the one that is not on it.
    """
    found: dict[type, frozenset[str]] = {}
    for module in _package_modules():
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        for class_name, methods in requesting_methods(source).items():
            cls = getattr(module, class_name, None)
            # A class the module defines but does not bind under its own name is
            # not reachable by a caller either, so it is not public surface.
            if isinstance(cls, type):
                found[cls] = methods
    return found


def half(found: dict[type, frozenset[str]], *, asynchronous: bool) -> dict[type, frozenset[str]]:
    """One of the two clients' worth of the inventory.

    Split on the module rather than on the ``Async`` in the class name, so the
    two halves are the two files somebody edits rather than a naming convention
    that a class could be renamed out of without anything noticing.
    """
    return {
        cls: methods
        for cls, methods in found.items()
        if cls.__module__.rsplit(".", 1)[-1].startswith("_async") is asynchronous
    }


def names(found: dict[type, frozenset[str]]) -> set[str]:
    """The inventory as the labels :func:`record_named_calls` records."""
    return {f"{cls.__name__}.{name}" for cls, methods in found.items() for name in methods}


@contextmanager
def record_named_calls(
    found: dict[type, frozenset[str]], callers: Sequence[Callable[..., Any]]
) -> Iterator[set[str]]:
    """Record which of the inventory's callables ``callers`` calls by name.

    Only calls made DIRECTLY from one of ``callers`` count, which is the whole
    point of the frame check. ``Computer.agent`` drives ``agent_stream``, and
    ``wait_for_guest`` drives ``exec`` — so counting calls from anywhere would
    let a method be "covered" by whichever neighbour happens to delegate to it,
    which is the same borrowed coverage as sharing a route. What this proves is
    that the exercise NAMES the method: its own signature, its own defaults, its
    own request.

    Everything is put back on the way out, including when the exercise raises.
    """
    codes = {caller.__code__ for caller in callers}
    recorded: set[str] = set()
    restore: list[tuple[type, str, Any]] = []

    def wrap(cls: type, method: str, original: Any) -> Any:
        label = f"{cls.__name__}.{method}"

        @functools.wraps(original)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            frame = inspect.currentframe()
            caller = frame.f_back if frame is not None else None
            if caller is not None and caller.f_code in codes:
                recorded.add(label)
            return original(*args, **kwargs)

        return wrapper

    try:
        for cls, methods in found.items():
            for method in sorted(methods):
                # `vars`, not `getattr`: restoring an inherited method with
                # `setattr` would bind it onto the subclass and leave the class
                # permanently different from how it was found. Nothing in this
                # inventory is inherited today, and a KeyError here is the right
                # way to hear about the first one that is.
                original = vars(cls)[method]
                restore.append((cls, method, original))
                setattr(cls, method, wrap(cls, method, original))
        yield recorded
    finally:
        for cls, method, original in reversed(restore):
            setattr(cls, method, original)
