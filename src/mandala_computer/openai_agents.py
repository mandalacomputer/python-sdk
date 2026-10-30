"""A Mandala computer as the sandbox of the OpenAI Agents SDK (OPL-6048).

The Agents SDK runs a ``SandboxAgent``'s shell, file and patch tools inside a
sandbox that a *sandbox client* provides. This module is that client for
Mandala: each sandbox session is one Mandala computer, created on demand and
released when the run ends::

    from agents import Runner
    from agents.run import RunConfig
    from agents.sandbox import SandboxAgent, SandboxRunConfig
    from agents.sandbox.capabilities import Shell

    from mandala_computer.openai_agents import (
        MandalaSandboxClient,
        MandalaSandboxClientOptions,
    )

    agent = SandboxAgent(name="builder", instructions="...", capabilities=[Shell()])
    async with MandalaSandboxClient() as client:              # MANDALA_API_KEY
        result = await Runner.run(
            agent,
            "Write and run a hello-world script.",
            run_config=RunConfig(
                sandbox=SandboxRunConfig(
                    client=client,
                    options=MandalaSandboxClientOptions(size="small"),
                ),
            ),
        )

``openai-agents`` is an optional dependency, ``pip install
'mandala-computer[openai-agents]'``: ``mandala_computer`` itself imports
without it, and only this module needs it. The supported release line is
0.24; the client implements the SDK's documented provider hooks, some of which
are underscored, so a newer line is not assumed to work until it is tested.

The Mandala API key stays on :class:`MandalaSandboxClient` (or on the
:class:`~mandala_computer.AsyncClient` you hand it). Neither the options nor
the serialized session state carry it, so a saved run state can be stored and
resumed by a process holding its own key.

What a session supports, and what it refuses rather than approximating:

* commands run on the computer as ``root`` with no display, through background
  execution, so a command may run longer than one HTTP request and a timeout
  stops it rather than leaving it running;
* reads and writes go through the file API; workspace snapshots are a tar of
  the workspace root, so any SDK snapshot store works;
* exposed ports are served through the computer's public URLs, on 443 with TLS;
* PTY sessions, a ``user`` other than the default, manifest users and groups,
  mount entries and host-path grants are refused with a
  ``ConfigurationError`` before anything is created. Nothing ever falls back to
  running on the machine this code runs on.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import io
import logging
import math
import posixpath
import shlex
import tarfile
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn, TypeVar

try:
    from agents.sandbox.errors import (
        ConfigurationError,
        ErrorCode,
        ExecNonZeroError,
        ExecTimeoutError,
        ExecTransportError,
        ExposedPortUnavailableError,
        WorkspaceArchiveReadError,
        WorkspaceArchiveWriteError,
        WorkspaceReadNotFoundError,
        WorkspaceStartError,
        WorkspaceWriteTypeError,
    )
    from agents.sandbox.manifest import Manifest
    from agents.sandbox.session import (
        BaseSandboxClient,
        BaseSandboxClientOptions,
        BaseSandboxSession,
        Dependencies,
        Instrumentation,
        SandboxSession,
        SandboxSessionState,
    )
    from agents.sandbox.session.runtime_helpers import (
        RESOLVE_WORKSPACE_PATH_HELPER,
        RuntimeHelperScript,
    )
    from agents.sandbox.session.tar_workspace import shell_tar_exclude_args
    from agents.sandbox.snapshot import (
        NoopSnapshot,
        SnapshotBase,
        SnapshotSpec,
        resolve_snapshot,
    )
    from agents.sandbox.types import ExecResult, ExposedPortEndpoint, User
    from agents.sandbox.util.tar_utils import UnsafeTarMemberError, validate_tar_bytes
    from agents.sandbox.workspace_paths import posix_path_for_error, sandbox_path_str
    from pydantic import ConfigDict, Field, field_validator, model_validator
    from typing_extensions import Self
except ImportError as error:  # pragma: no cover - exercised by the import test
    raise ImportError(
        "mandala_computer.openai_agents needs the openai-agents package (0.24): "
        "pip install 'mandala-computer[openai-agents]'"
    ) from error

from . import AsyncClient
from ._async_computer import AsyncBackgroundCommand, AsyncComputer
from ._client import FILE_SIZE_LIMIT
from ._exceptions import (
    APIError,
    MandalaError,
    NotFoundError,
    RangeNotSatisfiableError,
    is_transient,
)

__all__ = [
    "MandalaSandboxClient",
    "MandalaSandboxClientOptions",
    "MandalaSandboxSession",
    "MandalaSandboxSessionState",
]

logger = logging.getLogger(__name__)

#: What a session does with its computer when the sandbox shuts down.
ShutdownMode = Literal["delete", "stop", "suspend"]

#: The identity the runner checks before resuming a saved sandbox state, and
#: the ``type`` the options and the state register under.
_BACKEND_ID = "mandala"

#: Statuses a computer comes back from by being started (or waited on).
_STARTABLE = frozenset({"running", "stopped", "suspended", "building"})
#: Statuses that never run again; a resume replaces such a computer.
_BROKEN = frozenset({"build-failed", "half-removed"})

#: The first wait between polls of a running command, doubled up to the ceiling.
_POLL_FIRST = 0.05
_POLL_CEILING = 1.0
#: The least time a command's start may take before it counts against the
#: command's own timeout.
_SUBMIT_FLOOR = 2.0
#: The most output one read asks for, per stream.
_READ_LIMIT = 1024 * 1024
#: How many transient read failures in a row a command survives.
_POLL_SETBACKS = 5
#: How long one kill or cleanup command may take before it is abandoned.
_CLEANUP_TIMEOUT = 30.0

#: How long an abandoned request's answer is waited for before the client's
#: cleanup takes it over.
_ANSWER_WAIT = 30.0


class _Save:
    """One save in progress: whose, and what it saw when it captured."""

    def __init__(self, session: object, in_flight: int) -> None:
        self.session = session
        self.in_flight_at_capture = in_flight


#: The save running in this context: its own session's commands do not count
#: as changes to the workspace it is saving.
_SAVING: contextvars.ContextVar[_Save | None] = contextvars.ContextVar(
    "mandala_agents_saving", default=None
)

#: Where archives are staged inside the computer on the way in and out.
_GUEST_TMP = "/tmp"

#: Users that are the default identity of a command, which is not a refusal.
_DEFAULT_USERS = frozenset({"root"})


class MandalaSandboxClientOptions(BaseSandboxClientOptions):
    """How :class:`MandalaSandboxClient` makes a computer for a new session.

    Every field is optional: with none, the computer gets the platform's default
    template and shape. ``size`` names a catalogue shape
    (``client.sizes.list()``) and is the likeliest to start in about a second;
    it cannot be combined with ``template``, ``cpu``, ``ram_mb`` or ``disk_gb``.

    ``ends_in_hours`` is a safety end time: at that moment the platform stops
    the computer, keeping its disk, whatever happened to the process that made
    it. It is 24 hours unless you set another (``None`` for none), and a resume
    that starts the computer again sets it again from then. It is sent with
    the create; an answer that does not confirm it fails the session before
    any work.

    ``on_shutdown`` is what happens to the computer when the session shuts
    down: ``"delete"`` (the default) destroys it and its disk; ``"stop"`` keeps
    the disk; ``"suspend"`` keeps the disk and the RAM, so a resume picks up
    running processes as they were. A kept computer is reattached by
    :meth:`MandalaSandboxClient.resume` and keeps using its plan's disk until
    it is deleted.

    ``exposed_ports`` are the ports the agent may ask the SDK to expose; each
    is served through a public URL the first time it is resolved.
    ``public_url_access`` is that URL's access (``"public"`` or ``"private"``),
    or ``None`` for the platform's default for the plan. A private URL needs an
    API key or a signed-in browser to open.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["mandala"] = "mandala"
    template: str | None = None
    size: str | None = None
    cpu: int | None = None
    ram_mb: int | None = None
    disk_gb: int | None = None
    name: str | None = None
    workspace_id: str | None = None
    ends_in_hours: float | None = 24.0
    on_shutdown: ShutdownMode = "delete"
    exposed_ports: tuple[int, ...] = ()
    public_url_access: Literal["public", "private"] | None = None
    launch_timeout_s: float = Field(default=300.0, gt=0)

    @field_validator("exposed_ports")
    @classmethod
    def _ports(cls, ports: tuple[int, ...]) -> tuple[int, ...]:
        return _checked_ports(ports)

    @field_validator("ends_in_hours")
    @classmethod
    def _hours(cls, hours: float | None) -> float | None:
        return _checked_hours(hours)

    @model_validator(mode="after")
    def _size_alone(self) -> MandalaSandboxClientOptions:
        _check_size_alone(self.size, self.template, self.cpu, self.ram_mb, self.disk_gb)
        return self


class MandalaSandboxSessionState(SandboxSessionState):
    """What a saved run needs to find its computer again, or make another.

    The computer's id, the creation inputs a replacement is made from, the
    shutdown mode, and the public URLs this session made (so a resumed session
    can remove them). No credential: the resuming client brings its own key.
    """

    type: Literal["mandala"] = "mandala"
    computer_id: str
    template: str | None = None
    size: str | None = None
    cpu: int | None = None
    ram_mb: int | None = None
    disk_gb: int | None = None
    name: str | None = None
    workspace_id: str | None = None
    ends_in_hours: float | None = None
    on_shutdown: ShutdownMode = "delete"
    public_url_access: Literal["public", "private"] | None = None
    launch_timeout_s: float = 300.0
    #: Public URLs this session created, by port. Ones that already existed are
    #: reused and never removed.
    public_url_ids: dict[int, str] = Field(default_factory=dict)


class MandalaSandboxSession(BaseSandboxSession):
    """One sandbox session on one Mandala computer.

    Obtain one from :class:`MandalaSandboxClient`; the runner wraps it in the
    SDK's instrumented ``SandboxSession``.
    """

    state: MandalaSandboxSessionState

    def __init__(
        self,
        *,
        state: MandalaSandboxSessionState,
        computer: AsyncComputer,
        reaper: _Reaper | None = None,
    ) -> None:
        self.state = state
        self._computer = computer
        self._reaper = reaper if reaper is not None else _Reaper()
        self._workspace_root_ready = state.workspace_root_ready
        self._shut_down = False
        #: Bumped before anything that may change the workspace. A save covers
        #: the generation it started at, and the computer's disk is the only
        #: copy of the workspace whenever the two differ.
        self._generation = 0
        self._saved_generation: int | None = None
        #: Changes started and not yet finished.
        self._in_flight = 0
        #: One save at a time, capture and store both, so saves land in order.
        self._save_lock = asyncio.Lock()
        #: A command start whose answer was lost: something may be running that
        #: this session cannot see or stop, so no later save is trusted as
        #: complete, and shutdown stops the computer rather than deleting it.
        self._contaminated = False
        #: Set once start() has fully succeeded, a restore included. Until then
        #: the workspace may be empty or half restored, so it is never saved
        #: over a snapshot, and the computer is never deleted.
        self._workspace_ready = False

    @contextlib.contextmanager
    def _mutating(self) -> Iterator[None]:
        """Around anything that may change the workspace.

        The generation goes up before it starts, and it counts as in flight
        until it ends, so a save that captures the workspace meanwhile knows it
        may have missed it. The save's own commands are exempt, and only those.
        """
        save = _SAVING.get()
        if save is not None and save.session is self:
            yield
            return
        self._generation += 1
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1

    @property
    def computer(self) -> AsyncComputer:
        """The computer this session runs on."""
        return self._computer

    @property
    def computer_id(self) -> str:
        """The id of the computer this session runs on."""
        return self.state.computer_id

    # --- refusals ---------------------------------------------------------

    def _prepare_exec_command(
        self,
        *command: str | Path,
        shell: bool | list[str],
        user: str | User | None,
    ) -> list[str]:
        _refuse_user(user, op="exec")
        return super()._prepare_exec_command(*command, shell=shell, user=None)

    def supports_pty(self) -> bool:
        return False

    # --- startup ----------------------------------------------------------

    def _runtime_helpers(self) -> tuple[RuntimeHelperScript, ...]:
        return (RESOLVE_WORKSPACE_PATH_HELPER,)

    def _current_runtime_helper_cache_key(self) -> object | None:
        return self.state.computer_id

    async def _validate_path_access(self, path: Path | str, *, for_write: bool = False) -> Path:
        return await self._validate_remote_path_access(path, for_write=for_write)

    def _mark_workspace_root_ready_from_probe(self) -> None:
        super()._mark_workspace_root_ready_from_probe()
        self._workspace_root_ready = True

    async def _prepare_backend_workspace(self) -> None:
        _check_manifest(self.state.manifest)
        if self._workspace_state_preserved_on_start() and self._workspace_root_ready:
            return
        root = self._workspace_root_path()
        try:
            result = await self._exec_internal("mkdir", "-p", "--", sandbox_path_str(root))
        except Exception as error:
            raise WorkspaceStartError(path=root, cause=error) from error
        if not result.ok():
            raise WorkspaceStartError(
                path=root,
                context={
                    "reason": "workspace_root_nonzero_exit",
                    "exit_code": result.exit_code,
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )
        self._workspace_root_ready = True

    # --- commands ---------------------------------------------------------

    async def _exec_internal(
        self,
        *command: str | Path,
        timeout: float | None = None,
    ) -> ExecResult:
        with self._mutating():
            return await self._exec_command(*command, timeout=timeout)

    async def _exec_command(
        self,
        *command: str | Path,
        timeout: float | None,
    ) -> ExecResult:
        argv = [str(part) for part in command]
        line = shlex.join(argv)
        cwd = self.state.manifest.root if self._workspace_root_ready else None
        env = await self.state.manifest.environment.resolve()
        loop = asyncio.get_running_loop()
        # The deadline starts before the command is sent, so a slow start counts.
        deadline = None if timeout is None else loop.time() + max(timeout, 0.0)
        submit = asyncio.ensure_future(self._computer.start_exec(line, cwd=cwd, env=env or None))
        try:
            if deadline is None:
                job = await asyncio.shield(submit)
            else:
                # Bounded by the time left, with a floor: a start the platform
                # takes a moment to answer is not the command running long.
                budget = max(deadline - loop.time(), _SUBMIT_FLOOR)
                job = await asyncio.wait_for(asyncio.shield(submit), budget)
        except ValueError as error:
            # Refused before sending: an environment too large for the platform,
            # say. Not worth retrying with the same input.
            raise ExecTransportError(
                command=argv,
                context={"backend": _BACKEND_ID, "reason": "invalid_request"},
                cause=error,
                retryable=False,
            ) from error
        except MandalaError as error:
            if not (
                isinstance(error, APIError) and error.status is not None and error.status < 500
            ):
                # Not a clean refusal: the command may have started anyway.
                self._contaminated = True
                logger.warning(
                    "the answer to a command start on computer %s was lost (%s); the "
                    "command may be running, so this session's saves are no longer "
                    "trusted as complete and shutdown will keep the computer",
                    self.state.computer_id,
                    type(error).__name__,
                )
                raise ExecTransportError(
                    command=argv,
                    context={"backend": _BACKEND_ID, **_error_context(error)},
                    cause=error,
                    retryable=False,
                ) from error
            _raise_exec_transport(error, argv)
        except (asyncio.CancelledError, asyncio.TimeoutError) as error:
            # The start may still land: wait for it, out of the caller's
            # cancellation, and stop the command it started.
            await _shielded(
                _after(
                    submit,
                    _kill_quietly,
                    self._reaper,
                    f"a command start on computer {self.state.computer_id}",
                )
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ExecTimeoutError(
                command=argv, timeout_s=timeout, context={"backend": _BACKEND_ID}
            ) from None
        return await self._collect(job, argv, timeout, deadline)

    async def _collect(
        self,
        job: AsyncBackgroundCommand,
        argv: Sequence[str],
        timeout: float | None,
        deadline: float | None,
    ) -> ExecResult:
        """Read a started command to its end, or stop it at ``deadline``.

        The deadline is checked after every read, whether or not more output is
        waiting, and each read is itself bounded by the time left, so neither a
        command that never stops printing nor a slow read outlasts it. A read
        that reports the command finished is returned as finished: there is
        nothing left to stop. Anything else that ends this (a timeout, a failed
        read, a cancellation) stops the command.
        """
        finished = False
        try:
            if job.execution_id is not None:
                result = await self._collect_by_offset(job.execution_id, argv, timeout, deadline)
            else:
                result = await self._collect_by_cursor(job, argv, timeout, deadline)
            finished = True
            return result
        finally:
            if not finished:
                await _shielded(_kill_quietly(job))

    async def _collect_by_offset(
        self,
        execution_id: str,
        argv: Sequence[str],
        timeout: float | None,
        deadline: float | None,
    ) -> ExecResult:
        """Output read at offsets this reader keeps, so a failed read is safe to repeat.

        Each round reads the command's state first and then its output to the
        current end, so an ``exited`` state is followed by a read that has seen
        everything the command printed.
        """
        clock = _Clock(argv, timeout, deadline)
        stdout, stderr = bytearray(), bytearray()
        while True:
            meta = await clock.read(lambda: self._computer.execution(execution_id))
            if meta.output_unavailable is not None or meta.status == "lost":
                raise ExecTransportError(
                    command=argv,
                    context={
                        "backend": _BACKEND_ID,
                        "reason": "output_lost",
                        "detail": meta.output_unavailable or meta.status,
                    },
                    retryable=False,
                )
            while True:
                part = await clock.read(
                    functools.partial(
                        self._computer.execution_output,
                        execution_id,
                        stdout_offset=len(stdout),
                        stderr_offset=len(stderr),
                        limit=_READ_LIMIT,
                    )
                )
                stdout += part.stdout
                stderr += part.stderr
                if not (part.stdout_more or part.stderr_more):
                    break
                clock.check()
            if meta.status == "exited" and meta.exit_code is not None:
                return ExecResult(
                    stdout=bytes(stdout), stderr=bytes(stderr), exit_code=meta.exit_code
                )
            clock.check()
            await clock.pause()

    async def _collect_by_cursor(
        self,
        job: AsyncBackgroundCommand,
        argv: Sequence[str],
        timeout: float | None,
        deadline: float | None,
    ) -> ExecResult:
        """The older read, for a platform that gives no execution id.

        Each poll consumes the output it returns, so a poll whose answer was
        lost cannot be asked again without losing that output: any failure
        fails the command, and a gap in the reported offsets is refused.
        """
        clock = _Clock(argv, timeout, deadline)
        stdout, stderr = bytearray(), bytearray()
        while True:
            status = await clock.read(job.poll, retry=False)
            for name, got, before in (
                ("stdout", status.stdout, len(stdout)),
                ("stderr", status.stderr, len(stderr)),
            ):
                reported = status.raw.get(f"{name}_offset")
                if (
                    isinstance(reported, int)
                    and not isinstance(reported, bool)
                    and reported != before + len(got)
                ):
                    raise ExecTransportError(
                        command=argv,
                        context={"backend": _BACKEND_ID, "reason": "output_lost", "detail": name},
                        retryable=False,
                    )
            stdout += status.stdout
            stderr += status.stderr
            if status.output_unreadable or status.output_unavailable is not None:
                raise ExecTransportError(
                    command=argv,
                    context={
                        "backend": _BACKEND_ID,
                        "reason": "output_lost",
                        "detail": status.output_unavailable or "unreadable",
                    },
                    retryable=False,
                )
            if status.drained:
                if status.exit_code is None:
                    raise ExecTransportError(
                        command=argv,
                        context={
                            "backend": _BACKEND_ID,
                            "reason": "killed" if status.killed else "no_exit_code",
                        },
                        retryable=False,
                    )
                return ExecResult(
                    stdout=bytes(stdout), stderr=bytes(stderr), exit_code=status.exit_code
                )
            clock.check()
            if status.more:
                continue
            await clock.pause()

    async def _resolve_exposed_port(self, port: int) -> ExposedPortEndpoint:
        try:
            listing = await self._computer.public_urls()
            if not listing.enabled:
                raise ExposedPortUnavailableError(
                    port=port,
                    exposed_ports=self.state.exposed_ports,
                    reason="backend_unavailable",
                    context={
                        "backend": _BACKEND_ID,
                        "detail": listing.reason or "public_urls_disabled",
                    },
                    retryable=False,
                )
            served = next(
                (u for u in listing.public_urls if u.port == port and not u.disabled), None
            )
            wanted = self.state.public_url_access
            if served is None:
                served = await self._computer.create_public_url(port, access=wanted)
                self.state.public_url_ids[port] = served.id
            elif wanted is not None and served.access != wanted:
                # Reused, it must still be as open as asked and no more.
                served = await self._computer.update_public_url(served.id, access=wanted)
                if served.access != wanted:
                    raise ExposedPortUnavailableError(
                        port=port,
                        exposed_ports=self.state.exposed_ports,
                        reason="backend_unavailable",
                        context={"backend": _BACKEND_ID, "detail": "access_not_applied"},
                        retryable=False,
                    )
        except ExposedPortUnavailableError:
            raise
        except (MandalaError, ValueError) as error:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": _BACKEND_ID, **_error_context(error)},
                cause=error,
                retryable=_retryable(error),
            ) from error
        return ExposedPortEndpoint(host=served.hostname, port=443, tls=True)

    # --- files ------------------------------------------------------------

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        _refuse_user(user, op="read")
        workspace_path = await self._validate_path_access(path)
        sink = io.BytesIO()
        try:
            await self._computer.download_file(sandbox_path_str(workspace_path), sink)
        except NotFoundError as error:
            raise WorkspaceReadNotFoundError(path=path, cause=error) from error
        except MandalaError as error:
            raise WorkspaceArchiveReadError(
                path=path,
                context={"backend": _BACKEND_ID, **_error_context(error)},
                cause=error,
                retryable=_retryable(error),
            ) from error
        sink.seek(0)
        return sink

    async def _read_bounded(self, path: Path, *, max_bytes: int) -> bytes:
        workspace_path = await self._validate_path_access(path)
        try:
            part = await self._computer.read_file_part(
                sandbox_path_str(workspace_path), offset=0, length=max_bytes
            )
        except RangeNotSatisfiableError as error:
            if error.size == 0:
                return b""
            raise WorkspaceArchiveReadError(path=path, retryable=False) from None
        except NotFoundError:
            raise WorkspaceReadNotFoundError(path=path) from None
        except MandalaError as error:
            raise WorkspaceArchiveReadError(path=path, retryable=_retryable(error)) from None
        return part.data[:max_bytes]

    async def write(
        self,
        path: Path,
        data: io.IOBase,
        *,
        user: str | User | None = None,
    ) -> None:
        _refuse_user(user, op="write")
        with self._mutating():
            await self._write(path, data)

    async def _write(self, path: Path, data: io.IOBase) -> None:
        payload = data.read()
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=path, actual_type=type(payload).__name__)
        workspace_path = await self._validate_path_access(path, for_write=True)
        target = sandbox_path_str(workspace_path)
        parent = sandbox_path_str(workspace_path.parent)
        try:
            made = await self._exec_internal("mkdir", "-p", "--", parent)
            if not made.ok():
                raise ExecNonZeroError(made, command=("mkdir", "-p", "--", parent))
            await self._put(target, bytes(payload))
        except (MandalaError, ValueError, ExecNonZeroError, ExecTransportError) as error:
            raise WorkspaceArchiveWriteError(
                path=workspace_path,
                context={"backend": _BACKEND_ID, **_error_context(error)},
                cause=error,
                retryable=_retryable(error),
            ) from error

    async def _put(self, guest_path: str, payload: bytes) -> None:
        """One file into the computer, in one request or in chunks past the limit."""
        if len(payload) <= FILE_SIZE_LIMIT:
            await self._computer.write_file(guest_path, payload)
            return
        done = await self._computer.upload_file(guest_path, io.BytesIO(payload), size=len(payload))
        try:
            # The receipt outlives the upload until it is removed.
            await self._computer.cancel_file_upload(guest_path, done.upload_id)
        except MandalaError:
            logger.debug("could not remove the upload receipt for %s", guest_path)

    async def running(self) -> bool:
        # A failed read is raised, not taken for "not running": that answer
        # makes the SDK start the session again.
        await self._computer.refresh()
        return self._computer.status == "running"

    # --- workspace archives -------------------------------------------------

    async def persist_workspace(self) -> io.IOBase:
        # Captured now: a change still in flight may or may not be in the tar.
        save = _SAVING.get()
        if save is not None and save.session is self:
            save.in_flight_at_capture = self._in_flight
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        archive = f"{_GUEST_TMP}/mandala-agents-persist-{uuid.uuid4().hex}.tar"
        excludes = " ".join(shell_tar_exclude_args(self._persist_workspace_skip_relpaths()))
        script = (
            f"tar {excludes} -C {shlex.quote(sandbox_path_str(root))} -cf {shlex.quote(archive)} ."
        )
        try:
            result = await self._exec_internal("sh", "-c", script)
            if not result.ok():
                raise WorkspaceArchiveReadError(
                    path=error_root,
                    context={
                        "backend": _BACKEND_ID,
                        "reason": "snapshot_nonzero_exit",
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.decode("utf-8", errors="replace"),
                    },
                    retryable=False,
                )
            sink = io.BytesIO()
            await self._computer.download_file(archive, sink)
        except WorkspaceArchiveReadError:
            raise
        except (MandalaError, ExecTransportError, ExecTimeoutError) as error:
            raise WorkspaceArchiveReadError(
                path=error_root,
                context={"backend": _BACKEND_ID, **_error_context(error)},
                cause=error,
                retryable=_retryable(error),
            ) from error
        finally:
            await self._remove_quietly(archive)
        try:
            restorable = _restorable_archive(sink.getvalue(), sandbox_path_str(root))
        except UnsafeTarMemberError as error:
            # Saved as it is, this snapshot would be refused when it is restored:
            # fail the persist instead, so the run reports it and shutdown keeps
            # the computer that still has the files.
            raise WorkspaceArchiveReadError(
                path=error_root,
                context={
                    "backend": _BACKEND_ID,
                    "reason": "unrestorable_member",
                    "member": error.member,
                    "detail": str(error),
                },
                cause=error,
                retryable=False,
            ) from error
        return io.BytesIO(restorable)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        with self._mutating():
            await self._hydrate(data)

    async def _hydrate(self, data: io.IOBase) -> None:
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        raw = data.read()
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, (bytes, bytearray)):
            raise WorkspaceWriteTypeError(path=error_root, actual_type=type(raw).__name__)
        try:
            validate_tar_bytes(bytes(raw), allow_external_symlink_targets=False)
        except UnsafeTarMemberError as error:
            raise WorkspaceArchiveWriteError(
                path=error_root,
                context={
                    "reason": "unsafe_or_invalid_tar",
                    "member": error.member,
                    "detail": str(error),
                },
                cause=error,
            ) from error

        archive = f"{_GUEST_TMP}/mandala-agents-hydrate-{uuid.uuid4().hex}.tar"
        quoted_root = shlex.quote(sandbox_path_str(root))
        script = (
            f"mkdir -p -- {quoted_root} && tar -C {quoted_root} -xf {shlex.quote(archive)}; "
            f"status=$?; rm -f -- {shlex.quote(archive)}; exit $status"
        )
        try:
            await self._put(archive, bytes(raw))
            result = await self._exec_internal("sh", "-c", script)
        except (MandalaError, ValueError, ExecTransportError, ExecTimeoutError) as error:
            await self._remove_quietly(archive)
            raise WorkspaceArchiveWriteError(
                path=error_root,
                context={"backend": _BACKEND_ID, **_error_context(error)},
                cause=error,
                retryable=_retryable(error),
            ) from error
        if not result.ok():
            raise WorkspaceArchiveWriteError(
                path=error_root,
                context={
                    "backend": _BACKEND_ID,
                    "reason": "hydrate_nonzero_exit",
                    "exit_code": result.exit_code,
                    "stderr": result.stderr.decode("utf-8", errors="replace"),
                },
            )
        self._workspace_root_ready = True

    async def _persist_snapshot(self) -> None:
        # The whole persist, archive and store both: only a store that returned
        # counts as saved. A failure or a cancellation anywhere leaves it unsaved.
        if not self._workspace_ready and not isinstance(self.state.snapshot, NoopSnapshot):
            # The session never finished starting: the workspace may be empty
            # or half restored, and saving it would replace a good snapshot.
            raise WorkspaceArchiveReadError(
                path=posix_path_for_error(self._workspace_root_path()),
                context={"backend": _BACKEND_ID, "reason": "workspace_not_ready"},
                retryable=False,
            )
        async with self._save_lock:
            started_at = self._generation
            save = _Save(self, self._in_flight)
            # Scoped to this call and what it awaits: a write from another task
            # while the save is uploading still counts as a change.
            token = _SAVING.set(save)
            try:
                await super()._persist_snapshot()
            finally:
                _SAVING.reset(token)
            # Saved as of when the save began, and only if nothing was mid-change
            # when the workspace was captured; anything since is not in it.
            if save.in_flight_at_capture == 0 and not self._contaminated:
                self._saved_generation = started_at

    async def _can_reuse_restorable_snapshot_workspace(self) -> bool:
        # Disk wins: while the computer exists its disk is the truth, and a
        # snapshot only rebuilds a computer that is gone. So a resume onto the
        # same computer, or a second start of this session, never restores.
        if self._workspace_state_preserved_on_start() or self._workspace_ready:
            return True
        return await super()._can_reuse_restorable_snapshot_workspace()

    async def _after_start_failed(self) -> None:
        await super()._after_start_failed()
        if self._workspace_state_preserved_on_start() or self._workspace_ready:
            return
        # A computer made for this session that never finished starting (its
        # restore failed, say) holds nothing: delete it rather than keep it.
        self._shut_down = True
        await _shielded(_delete_quietly(self._computer))

    def _workspace_unsaved(self) -> bool:
        if not self._workspace_ready and self._workspace_state_preserved_on_start():
            return True  # a kept disk the session never finished starting on
        return not isinstance(self.state.snapshot, NoopSnapshot) and (
            not self._workspace_ready or self._saved_generation != self._generation
        )

    async def _after_start(self) -> None:
        await super()._after_start()
        self._workspace_ready = True

    async def _remove_quietly(self, guest_path: str) -> None:
        try:
            await self._exec_internal("rm", "-f", "--", guest_path, timeout=_CLEANUP_TIMEOUT)
        except Exception:  # noqa: BLE001 - best effort; the file is in a temp dir
            logger.debug("could not remove %s from %s", guest_path, self.state.computer_id)

    # --- shutdown -----------------------------------------------------------

    async def _shutdown_backend(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        mode = self.state.on_shutdown
        computer = self._computer
        if mode == "delete" and self._workspace_unsaved():
            # A snapshot store is configured and nothing confirms the workspace
            # reached it (the persist failed, was cancelled, or never ran), so
            # the computer's disk may be the only copy: stop it, not delete it.
            logger.warning(
                "the workspace of computer %s was not confirmed saved; stopping it "
                "instead of deleting it so its disk is kept",
                computer.id,
            )
            mode = "stop"
        if mode != "delete":
            await self._remove_public_urls()
        try:
            if mode == "delete":
                try:
                    await computer.delete()
                except NotFoundError:
                    pass
                return
            if mode == "suspend":
                try:
                    await computer.suspend()
                    return
                except MandalaError as error:
                    await computer.refresh()
                    if computer.status in ("suspended", "stopped"):
                        return
                    # Not suspendable now: stopping keeps the disk, which is the
                    # part of the session a resume can always rebuild from.
                    logger.warning(
                        "could not suspend computer %s (%s); stopping it instead",
                        computer.id,
                        type(error).__name__,
                    )
            await computer.refresh()
            if computer.status not in ("stopped",):
                await computer.stop()
        except MandalaError as error:
            # Best effort, like the SDK's own backends: shutdown runs during
            # cleanup and must not mask the run's outcome. The computer's end
            # time, if it has one, still stops it.
            logger.warning(
                "could not %s computer %s on shutdown (%s); it may still be running",
                mode,
                computer.id,
                type(error).__name__,
            )

    async def _remove_public_urls(self) -> None:
        for port, url_id in list(self.state.public_url_ids.items()):
            try:
                await self._computer.delete_public_url(url_id)
            except NotFoundError:
                pass
            except MandalaError as error:
                logger.warning(
                    "could not remove the public URL for port %s on %s (%s)",
                    port,
                    self.state.computer_id,
                    type(error).__name__,
                )
                continue
            del self.state.public_url_ids[port]


class MandalaSandboxClient(BaseSandboxClient[MandalaSandboxClientOptions]):
    """Creates, resumes and releases Mandala computers for the Agents SDK.

    ``client`` is the :class:`~mandala_computer.AsyncClient` to make them with;
    without one, a client is made from the environment (``MANDALA_API_KEY``, or
    a saved profile) and closed by :meth:`aclose`. One you pass stays yours to
    close. Use this as ``async with`` to close it at the end.

    Options may be omitted from ``SandboxRunConfig``: the platform's defaults
    and a 24-hour end time are used.
    """

    backend_id = _BACKEND_ID
    supports_default_options = True

    def __init__(
        self,
        client: AsyncClient | None = None,
        *,
        instrumentation: Instrumentation | None = None,
        dependencies: Dependencies | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client if client is not None else AsyncClient()
        self._instrumentation = (
            instrumentation if instrumentation is not None else Instrumentation()
        )
        self._dependencies = dependencies
        self._reaper = _Reaper()

    async def aclose(self) -> None:
        """Finish outstanding cleanup, then close the API client if this one made it.

        Cleanup still waiting on a platform answer (a command start, a create)
        is given a bounded time to finish; what is left is logged and dropped.
        """
        await self._reaper.aclose(_CLEANUP_TIMEOUT)
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def create(
        self,
        *,
        snapshot: SnapshotSpec | SnapshotBase | None = None,
        manifest: Manifest | None = None,
        options: MandalaSandboxClientOptions | None = None,
    ) -> SandboxSession:
        if options is None:
            options = MandalaSandboxClientOptions()
        if not isinstance(options, MandalaSandboxClientOptions):
            raise TypeError(
                "MandalaSandboxClient.create expects MandalaSandboxClientOptions, "
                f"not {type(options).__name__}"
            )
        manifest = manifest if manifest is not None else Manifest()
        self._validate_manifest_for_create(manifest)
        _check_manifest(manifest)

        session_id = uuid.uuid4()
        state = MandalaSandboxSessionState(
            session_id=session_id,
            manifest=manifest,
            snapshot=resolve_snapshot(snapshot, str(session_id)),
            computer_id="",
            template=options.template,
            size=options.size,
            cpu=options.cpu,
            ram_mb=options.ram_mb,
            disk_gb=options.disk_gb,
            name=options.name,
            workspace_id=options.workspace_id,
            ends_in_hours=options.ends_in_hours,
            on_shutdown=options.on_shutdown,
            public_url_access=options.public_url_access,
            launch_timeout_s=options.launch_timeout_s,
            exposed_ports=options.exposed_ports,
        )
        computer = await self._make_computer(state)
        inner = MandalaSandboxSession(state=state, computer=computer, reaper=self._reaper)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, MandalaSandboxSessionState):
            raise TypeError("MandalaSandboxClient.resume expects a MandalaSandboxSessionState")
        state.assert_path_grants_rebound()
        _check_manifest(state.manifest)

        computer: AsyncComputer | None = None
        try:
            if state.computer_id:
                computer = await self._client.computers.get(state.computer_id)
        except NotFoundError:
            computer = None
        except MandalaError as error:
            _raise_start_error(state, error, "reattach")

        if computer is not None and computer.status in _BROKEN:
            # A computer that will never run again: clear it away and replace it.
            try:
                await computer.delete()
            except MandalaError as error:
                logger.warning(
                    "could not delete unusable computer %s (%s)",
                    computer.id,
                    type(error).__name__,
                )
            computer = None
        elif computer is not None and computer.status not in _STARTABLE:
            raise WorkspaceStartError(
                path=posix_path_for_error(Path(state.manifest.root)),
                message=f"computer {state.computer_id} is {computer.status!r} and cannot be resumed",
                context={"backend": _BACKEND_ID, "computer_id": state.computer_id},
                retryable=False,
            )

        preserved = computer is not None
        if computer is not None:
            was_running = computer.status == "running"
            try:
                await _bring_up(computer, state.launch_timeout_s)
                if not was_running and state.ends_in_hours is not None:
                    await computer.set_end_time(ends_in_hours=state.ends_in_hours)
            except BaseException as error:
                if not was_running:
                    # This resume started it; a failure or a cancellation after
                    # that must not leave it running, possibly with no end time.
                    # Stopped, it keeps its disk for the next attempt.
                    await _shielded(_stop_quietly(computer))
                if isinstance(error, MandalaError):
                    _raise_start_error(state, error, "start")
                raise
        else:
            state.computer_id = ""
            state.workspace_root_ready = False
            state.public_url_ids = {}
            computer = await self._make_computer(state)

        inner = MandalaSandboxSession(state=state, computer=computer, reaper=self._reaper)
        inner._set_start_state_preserved(preserved, system=preserved)
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def delete(self, session: SandboxSession) -> SandboxSession:
        """Release the session's computer as its ``on_shutdown`` says, once.

        The runner shuts a session down before calling this, so it is usually
        already done; it is here for sessions closed some other way.
        """
        inner = session._inner
        if not isinstance(inner, MandalaSandboxSession):
            raise TypeError("MandalaSandboxClient.delete expects a MandalaSandboxSession")
        await inner.shutdown()
        return session

    def deserialize_session_state(self, payload: dict[str, object]) -> SandboxSessionState:
        return self._deserialize_session_state_payload(payload, MandalaSandboxSessionState)

    async def _create(self, state: MandalaSandboxSessionState) -> AsyncComputer:
        """The create request, cancellable without leaving a computer behind.

        The end time travels in the create itself, so even a computer that is
        somehow left behind stops on its own.

        The request, and its recovery, run as one task of their own. A create
        whose outcome is unknown (a dropped connection, a timeout, a 5xx) is
        sent once more under the same idempotency key, which answers with the
        first attempt's computer rather than making a second. A caller
        cancelled meanwhile waits, shielded, for that task and deletes the
        computer it comes back with, or leaves that to the client's cleanup if
        the answer is slow. A create whose outcome is still unknown after that
        is logged with its name and key.

        An answer that does not confirm the end time is raised by the SDK with
        no handle on the computer. The session's own name finds it, so it is
        deleted; a computer you named is reported rather than looked up, since
        another computer may share that name.

        Without a name of its own the computer is named after the session, so
        one that does leak can be told apart.
        """
        generated = state.name is None
        name = state.name if state.name is not None else f"agents-{state.session_id.hex[:12]}"
        key = uuid.uuid4().hex
        abandoned = False

        async def send() -> AsyncComputer:
            return await self._client.computers.create(
                name=name,
                size=state.size,
                template=state.template,
                cpu=state.cpu,
                ram_mb=state.ram_mb,
                disk_gb=state.disk_gb,
                workspace_id=state.workspace_id,
                ends_in_hours=state.ends_in_hours,
                idempotency_key=key,
            )

        async def attempt() -> AsyncComputer:
            try:
                try:
                    return await send()
                except MandalaError as error:
                    if error.idempotency_key is None:
                        raise
                    return await send()  # the same key: the same create
            except MandalaError as error:
                if error.idempotency_key is not None:
                    if abandoned:
                        logger.warning(
                            "a create of computer %r (idempotency key %s) was abandoned "
                            "with its outcome unknown; it may exist",
                            name,
                            key,
                        )
                elif not isinstance(error, APIError):
                    # Answered, then refused here: the computer exists.
                    await self._remove_unconfirmed(name, generated)
                raise

        task = asyncio.ensure_future(attempt())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            abandoned = True
            await _shielded(_after(task, _delete_quietly, self._reaper, f"the create of {name!r}"))
            raise
        except (MandalaError, ValueError) as error:
            _raise_start_error(state, error, "create")

    async def _remove_unconfirmed(self, name: str, generated: bool) -> None:
        """Delete a computer whose create answer was refused, found by its name."""
        if not generated:
            logger.warning(
                "computer %r was created but its answer was refused; it was not deleted, "
                "since another computer may share that name",
                name,
            )
            return
        try:
            found = [c for c in await self._client.computers.list() if c.name == name]
            for computer in found:
                await _delete_quietly(computer)
        except MandalaError as error:
            logger.warning(
                "could not find computer %r to delete it (%s)", name, type(error).__name__
            )

    async def _make_computer(self, state: MandalaSandboxSessionState) -> AsyncComputer:
        """Create a computer from ``state``'s inputs and wait for its guest.

        A computer that was created and then did not come up is deleted before
        the error is raised, cancellation included, so a failed create leaves
        nothing behind.

        A platform that cannot keep end times fails the session here, before
        any work, rather than running without one.
        """
        computer = await self._create(state)
        state.computer_id = computer.id
        try:
            await _bring_up(computer, state.launch_timeout_s)
        except BaseException as error:
            await _shielded(_delete_quietly(computer))
            if isinstance(error, MandalaError):
                _raise_start_error(state, error, "start")
            raise
        return computer


# --- helpers ----------------------------------------------------------------


async def _bring_up(computer: AsyncComputer, timeout: float) -> None:
    """Wait for ``computer`` to be built, start it if nothing is, and wait for its guest."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout

    def left() -> float:
        return max(deadline - loop.time(), 1.0)

    if computer.status == "building":
        await computer.wait_until_built(timeout=left())
    if computer.status in ("stopped", "suspended") or computer.start_error:
        await computer.start()
    if computer.status != "running":
        await computer.wait_until_running(timeout=left())
    await computer.wait_for_guest(timeout=left())


_ReadT = TypeVar("_ReadT")


class _Clock:
    """One command's deadline, the reads bounded by it and the pauses between them."""

    def __init__(self, argv: Sequence[str], timeout: float | None, deadline: float | None):
        self._argv = argv
        self._timeout = timeout
        self._deadline = deadline
        self._loop = asyncio.get_running_loop()
        self._delay = _POLL_FIRST

    def expired(self) -> NoReturn:
        raise ExecTimeoutError(
            command=self._argv, timeout_s=self._timeout, context={"backend": _BACKEND_ID}
        )

    def left(self) -> float | None:
        return None if self._deadline is None else self._deadline - self._loop.time()

    def check(self) -> None:
        left = self.left()
        if left is not None and left <= 0:
            self.expired()

    async def pause(self) -> None:
        left = self.left()
        wait = self._delay if left is None else min(self._delay, left)
        await asyncio.sleep(max(wait, 0.0))
        self._delay = min(self._delay * 2, _POLL_CEILING)

    async def read(self, call: Callable[[], Awaitable[_ReadT]], *, retry: bool = True) -> _ReadT:
        """One read, bounded by the time left, and asked again while transient."""
        setbacks = 0
        while True:
            left = self.left()
            try:
                if left is None:
                    return await call()
                # At least one short read, so a command that has already
                # finished is still seen to have finished at timeout 0.
                return await asyncio.wait_for(call(), max(left, _POLL_FIRST))
            except MandalaError as error:
                setbacks += 1
                if not retry or not is_transient(error) or setbacks > _POLL_SETBACKS:
                    _raise_exec_transport(error, self._argv)
                self.check()
                pause = error.retry_after if isinstance(error, APIError) else None
                wait = min(pause or self._delay, _POLL_CEILING * 5)
                left = self.left()
                await asyncio.sleep(max(wait if left is None else min(wait, left), 0.0))
                self._delay = min(self._delay * 2, _POLL_CEILING)
            except asyncio.TimeoutError:
                self.expired()


class _Reaper:
    """Cleanup that outlives the call that started it.

    Held by the client, so its tasks stay referenced, and finished (or logged
    and cancelled) when the client closes.
    """

    def __init__(self) -> None:
        self._tasks: dict[asyncio.Future[None], str] = {}

    def later(
        self,
        pending: asyncio.Future[_ReadT],
        then: Callable[[_ReadT], Awaitable[None]],
        what: str,
    ) -> None:
        async def run() -> None:
            try:
                result = await pending
            except BaseException:  # noqa: BLE001 - it failed, so nothing to undo
                return
            await then(result)

        task = asyncio.ensure_future(run())
        self._tasks[task] = what
        task.add_done_callback(lambda done: self._tasks.pop(done, None))

    async def aclose(self, timeout: float) -> None:
        if not self._tasks:
            return
        _, pending = await asyncio.wait(list(self._tasks), timeout=timeout)
        for task in pending:
            logger.warning(
                "closing before %s answered; whatever it made was not cleaned up",
                self._tasks.get(task, "a request"),
            )
            task.cancel()


async def _after(
    pending: asyncio.Future[_ReadT],
    then: Callable[[_ReadT], Awaitable[None]],
    reaper: _Reaper,
    what: str,
) -> None:
    """Undo what an abandoned request made: now if it answers soon, later if not."""
    try:
        result = await asyncio.wait_for(asyncio.shield(pending), _ANSWER_WAIT)
    except asyncio.TimeoutError:
        reaper.later(pending, then, what)
        return
    except Exception:  # noqa: BLE001 - it failed, so nothing to undo
        return
    await then(result)


async def _kill_quietly(job: AsyncBackgroundCommand) -> None:
    try:
        await asyncio.wait_for(job.kill(), _CLEANUP_TIMEOUT)
    except Exception:  # noqa: BLE001 - it may have ended on its own meanwhile
        logger.debug("could not stop guest command %s", job.pid)


async def _stop_quietly(computer: AsyncComputer) -> None:
    try:
        await asyncio.wait_for(computer.stop(), _CLEANUP_TIMEOUT)
    except Exception as error:  # noqa: BLE001 - reported; the end time may still apply
        logger.warning(
            "could not stop computer %s after a failed resume (%s)",
            computer.id,
            type(error).__name__,
        )


def _restorable_archive(raw: bytes, root: str) -> bytes:
    """``raw`` rewritten into what :meth:`hydrate_workspace` will accept.

    Restoring refuses hard links and symlinks that point out of the workspace,
    as every SDK backend does. A hard link becomes a regular file holding the
    same bytes. A symlink whose absolute target is inside the workspace root
    becomes the equivalent relative one. A symlink that points outside the
    workspace cannot be restored at all, so it raises ``UnsafeTarMemberError``
    naming it, and the result is checked against the restore policy before it
    is returned.
    """
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source,
        tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as target,
    ):
        contents: dict[str, bytes] = {}
        for member in source.getmembers():
            name = posixpath.normpath(member.name)
            if member.isreg():
                handle = source.extractfile(member)
                data = handle.read() if handle is not None else b""
                contents[name] = data
                target.addfile(member, io.BytesIO(data))
            elif member.islnk():
                linked = posixpath.normpath(member.linkname)
                if linked not in contents:
                    raise UnsafeTarMemberError(
                        member=member.name,
                        reason=f"hard link to a file not in the archive: {member.linkname}",
                    )
                copy = tarfile.TarInfo(member.name)
                copy.size = len(contents[linked])
                copy.mode, copy.mtime = member.mode, member.mtime
                copy.uid, copy.gid = member.uid, member.gid
                copy.uname, copy.gname = member.uname, member.gname
                contents[name] = contents[linked]
                target.addfile(copy, io.BytesIO(contents[linked]))
            elif member.issym() and member.linkname.startswith("/"):
                # Rebased only when made of plain components: a ``..`` after a
                # symlinked component means something else on disk than it does
                # as text, so it is not resolved here but refused.
                parts = PurePosixPath(member.linkname).parts[1:]
                if ".." in parts:
                    raise UnsafeTarMemberError(
                        member=member.name,
                        reason=f"absolute symlink target with '..': {member.linkname}",
                    )
                root_parts = PurePosixPath(root).parts[1:]
                if tuple(parts[: len(root_parts)]) != tuple(root_parts):
                    raise UnsafeTarMemberError(
                        member=member.name,
                        reason=f"symlink points outside the workspace: {member.linkname}",
                    )
                relative = posixpath.relpath(
                    posixpath.join(".", *parts[len(root_parts) :]),
                    posixpath.join(".", posixpath.dirname(name)),
                )
                member.linkname = relative
                # A long target travels in a PAX header, which would win over
                # the field just set when the member is written again.
                member.pax_headers = {
                    k: v for k, v in member.pax_headers.items() if k != "linkpath"
                }
                target.addfile(member)
            else:
                target.addfile(member)
    result = out.getvalue()
    validate_tar_bytes(result, allow_external_symlink_targets=False)
    return result


async def _delete_quietly(computer: AsyncComputer) -> None:
    try:
        await asyncio.wait_for(computer.delete(), _CLEANUP_TIMEOUT)
    except NotFoundError:
        pass
    except Exception as error:  # noqa: BLE001 - reported, and the end time still applies
        logger.warning(
            "could not delete computer %s after a failed start (%s)",
            computer.id,
            type(error).__name__,
        )


async def _shielded(cleanup: Any) -> None:
    """Run a cleanup coroutine to the end even when the caller is being cancelled."""
    task = asyncio.ensure_future(cleanup)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _refuse_user(user: str | User | None, *, op: Literal["exec", "read", "write"]) -> None:
    name = user.name if isinstance(user, User) else user
    if name is None or name in _DEFAULT_USERS:
        return
    raise ConfigurationError(
        message=(f"Mandala sandboxes run as root; running as user {name!r} is not supported"),
        error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
        op=op,
        context={"backend": _BACKEND_ID, "user": name},
        retryable=False,
    )


def _check_manifest(manifest: Manifest) -> None:
    """Refuse what a session cannot honour, before anything is created."""
    refused: list[str] = []
    if manifest.users:
        refused.append("manifest users")
    if manifest.groups:
        refused.append("manifest groups")
    if manifest.mount_targets():
        refused.append("mount entries")
    if any(grant.host_path is not None for grant in manifest.extra_path_grants):
        refused.append("host-path grants")
    if refused:
        raise ConfigurationError(
            message=f"Mandala sandboxes do not support {', '.join(refused)}",
            error_code=ErrorCode.SANDBOX_CONFIG_INVALID,
            op="start",
            context={"backend": _BACKEND_ID, "unsupported": refused},
            retryable=False,
        )


def _checked_ports(ports: tuple[int, ...]) -> tuple[int, ...]:
    for port in ports:
        if isinstance(port, bool) or not 1 <= port <= 65535 or port == 22:
            raise ValueError(f"exposed port must be 1 to 65535 and not 22, not {port!r}")
    return ports


def _checked_hours(hours: float | None) -> float | None:
    if hours is None:
        return None
    if isinstance(hours, bool) or not math.isfinite(hours) or not 0 < hours <= 366 * 24:
        raise ValueError(f"ends_in_hours must be more than 0 and at most 8784, not {hours!r}")
    return hours


def _check_size_alone(size: str | None, *others: object) -> None:
    if size is not None and any(value is not None for value in others):
        raise ValueError("size cannot be combined with template, cpu, ram_mb or disk_gb")


def _retryable(error: BaseException) -> bool | None:
    if isinstance(error, MandalaError):
        return is_transient(error)
    retryable = getattr(error, "retryable", None)
    return retryable if isinstance(retryable, bool) else None


def _error_context(error: BaseException) -> dict[str, object]:
    """What about a failure is safe to put in an SDK error's context.

    The class, and an API error's status and code. Not the message or the body,
    which are the platform's words and may quote what was sent.
    """
    context: dict[str, object] = {"provider_error": type(error).__name__}
    if isinstance(error, APIError):
        context["status"] = error.status
        if error.code is not None:
            context["code"] = error.code
    return context


def _raise_exec_transport(error: MandalaError, argv: Sequence[str]) -> NoReturn:
    raise ExecTransportError(
        command=argv,
        context={"backend": _BACKEND_ID, **_error_context(error)},
        cause=error,
        retryable=_retryable(error),
    ) from error


def _raise_start_error(
    state: MandalaSandboxSessionState, error: BaseException, stage: str
) -> NoReturn:
    context: dict[str, object] = {"backend": _BACKEND_ID, "stage": stage, **_error_context(error)}
    if state.computer_id:
        context["computer_id"] = state.computer_id
    raise WorkspaceStartError(
        path=posix_path_for_error(Path(state.manifest.root)),
        message=f"could not {stage} the Mandala computer: {type(error).__name__}",
        context=context,
        cause=error,
        retryable=_retryable(error),
    ) from error
