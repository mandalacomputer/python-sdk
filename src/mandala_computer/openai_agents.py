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
import re
import shlex
import tarfile
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import IO, Any, Literal, NoReturn, TypeVar, cast

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
    from agents.sandbox.snapshot import (
        NoopSnapshot,
        SnapshotBase,
        SnapshotSpec,
        resolve_snapshot,
    )
    from agents.sandbox.types import ExecResult, ExposedPortEndpoint, User
    from agents.sandbox.util.tar_utils import (
        UnsafeTarMemberError,
        validate_tar_bytes,
        validate_tarfile,
    )
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
from ._models import PublicUrl

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

#: The largest workspace archive a restore can upload to a computer.
_ARCHIVE_LIMIT = 2 * 1024 * 1024 * 1024

#: How much of an archive is held in memory before it spills to a temp file.
_SPOOL_IN_MEMORY = 64 * 1024 * 1024

#: How much of a file one read request asks for.
_READ_PART = 8 * 1024 * 1024

#: How often, and how long in the background, a start that failed but may
#: have been admitted is watched.
_ADMITTED_POLL = 0.25
_ADMITTED_WATCH = 300.0

#: How long a create whose outcome is unknown is asked about again, under its
#: idempotency key, while the platform says it is still being carried out.
_CREATE_SETTLE = 300.0

#: How long closing the client waits for its sessions' shutdowns.
_CLOSE_WAIT = 300.0

#: How long shutdown waits for changes already under way to finish.
_DRAIN_WAIT = 30.0

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
    down: ``"delete"`` (the default, as with the SDK's other backends)
    destroys it and its disk; ``"stop"`` keeps the disk; ``"suspend"`` keeps
    the disk and the RAM, so a resume picks up running processes as they
    were. With a snapshot store, the delete happens only after the last
    confirmed save of the whole workspace (otherwise the computer is
    stopped), and files written after that save by background processes you
    started are lost with it. Use ``"stop"`` for a run you mean to resume. A
    kept computer is reattached by
    :meth:`MandalaSandboxClient.resume` and keeps using its plan's disk until
    it is deleted; its end time stops it if nothing else does.

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
    public_url_ids: dict[int, list[str]] = Field(default_factory=dict)
    #: Labels of the public URLs this session asked for, by port, recorded
    #: before each create is sent. A URL carrying one is the session's, even
    #: when its create's answer was lost: shutdown and a resume onto the same
    #: computer look for them. One is dropped once its create was refused or
    #: its URL is gone.
    public_url_labels: dict[int, list[str]] = Field(default_factory=dict)
    #: Something on this computer may have changed, or may still be running,
    #: that the session could not see: a lost command start or write, a stop
    #: that was not confirmed, or a change in flight when this was saved. A
    #: resume onto the same computer keeps it, so no save there counts as
    #: complete and shutdown stops rather than deletes; a new computer clears it.
    uncertain: bool = False
    #: The computer's workspace was set up by a start that succeeded, a restore
    #: included. Only then is its disk the truth: one that never got there may
    #: hold a half-restored workspace, so a resume onto it restores again.
    initialized: bool = False


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
        live: set[MandalaSandboxSession] | None = None,
        owners: dict[str, MandalaSandboxSession] | None = None,
    ) -> None:
        self.state = state
        self._computer = computer
        self._reaper = reaper if reaper is not None else _Reaper()
        #: The client's sessions not yet shut down; this one leaves it then.
        self._live = live if live is not None else set()
        self._live.add(self)
        #: The client's record of which session holds which computer. A
        #: session taking a computer becomes its holder; one finishing its
        #: shutdown lets it go. Late cleanup stops a computer only while no
        #: other session holds it.
        self._owners = owners if owners is not None else {}
        if state.computer_id:
            self._owners[state.computer_id] = self
        self._workspace_root_ready = state.workspace_root_ready
        #: Set when shutdown begins: from then on changes are refused.
        self._shut_down = False
        self._shutdown_task: asyncio.Future[None] | None = None
        #: How shutdown left the computer, once it has.
        self._released: str | None = None
        #: What the session set out to do to the computer, once it has begun.
        self._releasing: str | None = None
        #: Work handed to the client's cleanup and not yet finished.
        self._unsettled = 0
        #: Background work finished after shutdown acted on the computer.
        self._settled_after_release = False
        #: Bumped before anything that may change the workspace. A save covers
        #: the generation it started at, and the computer's disk is the only
        #: copy of the workspace whenever the two differ.
        self._generation = 0
        self._saved_generation: int | None = None
        #: Changes started and not yet finished.
        self._in_flight = 0
        #: Anything reaching the computer and not yet finished, changes included.
        self._busy = 0
        #: The resume that made this session started the computer.
        self._started_by_resume = False
        #: One save at a time, capture and store both, so saves land in order.
        self._save_lock = asyncio.Lock()
        #: A command start whose answer was lost: something may be running that
        #: this session cannot see or stop, so no later save is trusted as
        #: complete, and shutdown stops the computer rather than deleting it.
        self._contaminated = state.uncertain
        #: One resolution per port at a time, so a port gets one URL.
        self._port_locks: dict[int, asyncio.Lock] = {}
        #: Which recorded label each known URL carries.
        self._label_of: dict[str, str] = {}
        #: Set once start() has fully succeeded, a restore included. Until then
        #: the workspace may be empty or half restored, so it is never saved
        #: over a snapshot, and the computer is never deleted.
        self._workspace_ready = False

    @contextlib.asynccontextmanager
    async def _mutating(self, refuse: Callable[[], Exception]) -> AsyncIterator[None]:
        """Around anything that may change the workspace.

        The generation goes up before it starts, and it counts as in flight
        until it ends, so a save that captures the workspace meanwhile knows it
        may have missed it. The save's own commands are exempt from that
        accounting, and only from that: they still reach the computer, so they
        are counted, refused and followed up like any other operation. Once
        shutdown has begun nothing else may change it: ``refuse()`` is raised,
        since a change landing then could be deleted with the computer.
        """
        save = _SAVING.get()
        if save is not None and save.session is self:
            async with self._active(refuse):
                yield
            return
        async with self._active(refuse):
            self._generation += 1
            self._in_flight += 1
            # Saved while this runs, the state must say it is uncertain.
            self.state.uncertain = True
            try:
                yield
            finally:
                self._in_flight -= 1
                self.state.uncertain = self._contaminated or self._in_flight > 0

    @contextlib.asynccontextmanager
    async def _active(self, refuse: Callable[[], Exception]) -> AsyncIterator[None]:
        """Around anything that reaches the computer, outside shutdown's own calls.

        Refused once shutdown has begun, so nothing new reaches a computer being
        stopped, suspended or deleted (or wakes one); counted while it runs, so
        shutdown can wait for it first. One that outlasted shutdown's wait and
        finished after shutdown put the computer down may have woken it, so the
        computer is stopped again, and shutdown's last step does the same.
        """
        if self._shut_down:
            raise refuse()
        self._busy += 1
        try:
            yield
        except BaseException as error:
            if _outcome_unknown(error):
                # Cancelled, or failed after a request may have gone out: its
                # effect may still reach the computer.
                self._taint(f"a request ended with its outcome unknown ({type(error).__name__})")
            raise
        finally:
            self._busy -= 1
            await _shielded(self._landed_late())

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
        def refuse() -> Exception:
            return ExecTransportError(
                command=[str(part) for part in command],
                context={"backend": _BACKEND_ID, "reason": "session_shutting_down"},
                retryable=False,
            )

        async with self._mutating(refuse):
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
            if not _clean_refusal(error):
                # Not a clean refusal: the command may have started anyway.
                self._taint(f"the answer to a command start was lost ({type(error).__name__})")
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
            settled = await _shielded(
                _after(
                    submit,
                    _kill_quietly,
                    self._reaper,
                    f"a command start on computer {self.state.computer_id}",
                    owner=self,
                )
            )
            if not settled:
                self._taint("an abandoned command start was not confirmed stopped")
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ExecTimeoutError(
                command=argv, timeout_s=timeout, context={"backend": _BACKEND_ID}
            ) from None
        return await self._collect(job, argv, timeout, deadline)

    def _taint(self, why: str) -> None:
        """A command may be running unseen: no later save counts as complete."""
        self._contaminated = True
        self.state.uncertain = True
        logger.warning(
            "%s on computer %s; it may still be running, so this session's saves are "
            "no longer trusted as complete and shutdown will keep the computer",
            why,
            self.state.computer_id,
        )

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
            if not finished and self._shut_down:
                if self._released not in (None, "delete"):
                    # Shutdown already put the computer down and this command
                    # reached it anyway: kill it; the stop is applied again as
                    # this leaves.
                    await _shielded(_kill_quietly(job))
                # Before shutdown acts, a kill could wake the computer; the
                # stop or delete ends it instead.
                self._taint(f"command {job.pid} was left to shutdown to end")
            elif not finished and not await _shielded(_kill_quietly(job)):
                self._taint(f"command {job.pid} could not be confirmed stopped")

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
        clock = _Clock(argv, timeout, deadline, lambda: self._shut_down)
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
        clock = _Clock(argv, timeout, deadline, lambda: self._shut_down)
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
        if self._shut_down:
            raise ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": _BACKEND_ID, "detail": "session_shutting_down"},
                retryable=False,
            )
        lock = self._port_locks.setdefault(port, asyncio.Lock())

        def refuse() -> Exception:
            return ExposedPortUnavailableError(
                port=port,
                exposed_ports=self.state.exposed_ports,
                reason="backend_unavailable",
                context={"backend": _BACKEND_ID, "detail": "session_shutting_down"},
                retryable=False,
            )

        async with self._active(refuse), lock:
            return await self._resolve_port(port)

    async def _settle_resolutions(self) -> None:
        """Wait, bounded, for resolutions in flight, so their URLs are removed too."""
        for port, lock in list(self._port_locks.items()):
            try:
                await asyncio.wait_for(lock.acquire(), _ANSWER_WAIT)
            except asyncio.TimeoutError:
                logger.warning(
                    "a public URL for port %s on computer %s was still being made at "
                    "shutdown; it is removed if it lands",
                    port,
                    self.state.computer_id,
                )
                continue
            lock.release()

    async def _resolve_port(self, port: int) -> ExposedPortEndpoint:
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
            candidates = [u for u in listing.public_urls if u.port == port and not u.disabled]
            mine = set(self.state.public_url_labels.get(port, []))
            # One this session's earlier, abandoned create made comes first.
            served = next((u for u in candidates if u.label in mine), None) or next(
                iter(candidates), None
            )
            wanted = self.state.public_url_access
            if (
                served is not None
                and served.label in mine
                and served.id not in self.state.public_url_ids.get(port, [])
            ):
                # Made by this session's abandoned create (it carries the label
                # the session gave it): take it as the session's own.
                self._label_of[served.id] = served.label
                await _shielded(self._own_url(port, served.id))
            if served is None:
                # A label only this session uses, so a URL whose answer is lost
                # can be told apart from anyone else's on the same port.
                label = f"agents-{self.state.session_id.hex[:12]}-{port}-{uuid.uuid4().hex[:6]}"
                self.state.public_url_labels.setdefault(port, []).append(label)
                create = asyncio.ensure_future(
                    self._computer.create_public_url(port, label=label, access=wanted)
                )
                try:
                    served = await asyncio.shield(create)
                except asyncio.CancelledError:
                    await _shielded(self._settle_url(create, port, label))
                    raise
                except MandalaError as error:
                    if _clean_refusal(error):
                        self._drop_label(port, label)  # refused: nothing was made
                    else:
                        # It may have been made: find it, so shutdown removes it.
                        await _shielded(self._adopt_new_url(port, label))
                    raise
                self._label_of[served.id] = label
                if not await _shielded(self._own_url(port, served.id)):
                    raise ExposedPortUnavailableError(
                        port=port,
                        exposed_ports=self.state.exposed_ports,
                        reason="backend_unavailable",
                        context={"backend": _BACKEND_ID, "detail": "session_shutting_down"},
                        retryable=False,
                    )
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

    async def _own_url(self, port: int, url_id: str) -> bool:
        """Take a URL the session made as its own; the only place that does.

        Once shutdown has begun, its URL cleanup may already have run, so the
        URL is deleted instead of recorded. True when it was recorded.
        """
        if self._shut_down:
            await self._remove_url_quietly(port, url_id)
            return False
        self.state.public_url_ids.setdefault(port, []).append(url_id)
        return True

    def _drop_label(self, port: int, label: str) -> None:
        labels = self.state.public_url_labels.get(port, [])
        if label in labels:
            labels.remove(label)
        if not labels:
            self.state.public_url_labels.pop(port, None)

    def _forget_url(self, port: int, url_id: str) -> None:
        """A URL confirmed gone: its label need not be looked for again."""
        label = self._label_of.pop(url_id, None)
        if label is not None:
            self._drop_label(port, label)

    async def _reconcile_urls(self) -> None:
        """Take as the session's every URL on the computer carrying a recorded label."""
        if not self.state.public_url_labels:
            return
        try:
            listing = await asyncio.wait_for(self._computer.public_urls(), _CLEANUP_TIMEOUT)
        except Exception as error:  # noqa: BLE001 - the labels stay, for another try
            logger.warning(
                "could not list the public URLs on computer %s to find this session's (%s)",
                self.state.computer_id,
                type(error).__name__,
            )
            return
        for url in listing.public_urls:
            if url.label in self.state.public_url_labels.get(url.port, []):
                self._label_of[url.id] = url.label
                owned = self.state.public_url_ids.setdefault(url.port, [])
                if url.id not in owned:
                    owned.append(url.id)

    async def _remove_url_quietly(self, port: int, url_id: str) -> None:
        try:
            await self._computer.delete_public_url(url_id)
            self._forget_url(port, url_id)
        except NotFoundError:
            self._forget_url(port, url_id)
        except MandalaError as error:
            logger.warning(
                "could not remove a public URL for port %s on computer %s (%s)",
                port,
                self.state.computer_id,
                type(error).__name__,
            )

    async def _settle_url(self, create: asyncio.Future[PublicUrl], port: int, label: str) -> None:
        """Own what an abandoned URL create made, or remove it when it lands late."""
        try:
            served = await asyncio.wait_for(asyncio.shield(create), _ANSWER_WAIT)
        except asyncio.TimeoutError:
            computer_id = self.state.computer_id

            lock = self._port_locks.setdefault(port, asyncio.Lock())

            async def remove(late: PublicUrl) -> None:
                async with lock:
                    if late.id in self.state.public_url_ids.get(port, []):
                        return  # a later resolution took it as the session's own
                    try:
                        await self._computer.delete_public_url(late.id)
                        self._drop_label(port, label)
                    except MandalaError as error:
                        logger.warning(
                            "could not remove a public URL for port %s on computer %s that "
                            "was made after its request was abandoned (%s)",
                            port,
                            computer_id,
                            type(error).__name__,
                        )

            async def failed(error: BaseException) -> None:
                async with lock:
                    # Its answer was lost: it may still have made the URL.
                    if isinstance(error, MandalaError) and not _clean_refusal(error):
                        await self._sweep_new_urls(port, label)

            self._reaper.later(
                create, remove, f"a public URL create for port {port}", failed, owner=self
            )
            return
        except MandalaError as error:
            if _clean_refusal(error):
                self._drop_label(port, label)
            else:
                await self._adopt_new_url(port, label)
            return
        except Exception:  # noqa: BLE001 - refused before it was sent
            return
        self._label_of[served.id] = label
        await self._own_url(port, served.id)

    async def _sweep_new_urls(self, port: int, label: str) -> None:
        """Delete the URL an abandoned create may have made, known by its label."""
        owned = set(self.state.public_url_ids.get(port, []))
        try:
            listing = await asyncio.wait_for(self._computer.public_urls(), _CLEANUP_TIMEOUT)
        except Exception as error:  # noqa: BLE001 - reported; nothing else to do
            logger.warning(
                "a public URL for port %s on computer %s may have been made after its "
                "request was abandoned and could not be checked (%s)",
                port,
                self.state.computer_id,
                type(error).__name__,
            )
            return
        for url in listing.public_urls:
            if url.port == port and url.label == label and url.id not in owned:
                await self._remove_url_quietly(port, url.id)

    async def _adopt_new_url(self, port: int, label: str) -> None:
        """Record a public URL a lost create may have made, as this session's.

        Once the session is shutting down, one found is deleted instead.
        """
        try:
            listing = await asyncio.wait_for(self._computer.public_urls(), _CLEANUP_TIMEOUT)
        except Exception as error:  # noqa: BLE001 - reported; nothing else to do
            logger.warning(
                "a public URL for port %s on computer %s may have been made and could "
                "not be checked (%s); its label is kept, so shutdown looks for it",
                port,
                self.state.computer_id,
                type(error).__name__,
            )
            return
        for url in listing.public_urls:
            if url.port == port and url.label == label:
                self._label_of[url.id] = label
                await self._own_url(port, url.id)

    # --- files ------------------------------------------------------------

    async def read(self, path: Path, *, user: str | User | None = None) -> io.IOBase:
        _refuse_user(user, op="read")
        async with self._active(lambda: _closing_read_error(path)):
            return await self._read(path)

    async def _read(self, path: Path) -> io.IOBase:
        workspace_path = await self._validate_path_access(path)
        sink = io.BytesIO()
        try:
            # Range by range; once shutdown begins the next range is not asked
            # for, so a late request cannot wake a computer it put down.
            await self._computer.download_file(
                sandbox_path_str(workspace_path),
                cast(IO[bytes], _SinkUntilClosing(sink, lambda: self._shut_down)),
                part_size=_READ_PART,
            )
        except _Closing:
            raise _closing_read_error(path) from None
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
        async with self._active(lambda: _closing_read_error(path)):
            return await self._read_prefix(path, max_bytes)

    async def _read_prefix(self, path: Path, max_bytes: int) -> bytes:
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
        async with self._mutating(lambda: _closing_write_error(path)):
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
            try:
                await self._put(target, bytes(payload))
            except asyncio.CancelledError:
                self._taint(f"a write of {target} was abandoned after it was sent")
                raise
            except MandalaError as error:
                if not _clean_refusal(error):
                    # Sent, and the answer lost or failed: it may still land.
                    self._taint(f"a write of {target} ended uncertainly")
                raise
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
        try:
            done = await self._computer.upload_file(
                guest_path, _StopWhenClosing(payload, lambda: self._shut_down), size=len(payload)
            )
        except BaseException as error:
            # Chunks already staged stay on the computer's disk until the upload
            # is cancelled; the SDK tags the error with the upload's id.
            upload_id = getattr(error, "upload_id", None)
            if isinstance(upload_id, str):
                await _shielded(self._cancel_upload_quietly(guest_path, upload_id))
            raise
        # The receipt outlives the upload until it is removed.
        await self._cancel_upload_quietly(guest_path, done.upload_id)

    async def _cancel_upload_quietly(self, guest_path: str, upload_id: str) -> None:
        try:
            await asyncio.wait_for(
                # Once closing, it must not wake a computer shutdown has put down.
                self._computer.cancel_file_upload(guest_path, upload_id, no_wake=self._shut_down),
                _CLEANUP_TIMEOUT,
            )
        except Exception as error:  # noqa: BLE001 - reported; the original error is raised
            logger.warning(
                "could not remove upload staging for %s on computer %s (%s)",
                guest_path,
                self.state.computer_id,
                type(error).__name__,
            )

    async def running(self) -> bool:
        # A failed read is raised, not taken for "not running": that answer
        # makes the SDK start the session again.
        await self._computer.refresh()
        return self._computer.status == "running"

    # --- workspace archives -------------------------------------------------

    async def persist_workspace(self) -> io.IOBase:
        root = self._workspace_root_path()
        async with self._active(lambda: _closing_read_error(posix_path_for_error(root))):
            return await self._persist_workspace()

    async def _persist_workspace(self) -> io.IOBase:
        # Captured now: a change still in flight may or may not be in the tar.
        save = _SAVING.get()
        if save is not None and save.session is self:
            save.in_flight_at_capture = self._in_flight
        root = self._workspace_root_path()
        error_root = posix_path_for_error(root)
        archive = f"{_GUEST_TMP}/mandala-agents-persist-{uuid.uuid4().hex}.tar"
        skips = self._persist_workspace_skip_relpaths()
        # Exclusions are paths, not patterns: GNU tar (the computer's) is told
        # so; any other tar gets them with their wildcard characters escaped.
        literal = " ".join(_tar_excludes(skips, escape=False))
        escaped = " ".join(_tar_excludes(skips, escape=True))
        where = f"-C {shlex.quote(sandbox_path_str(root))} -cf {shlex.quote(archive)} ."
        script = (
            "if tar --version 2>/dev/null | grep -q 'GNU tar'; then "
            f"tar --no-wildcards --anchored {literal} {where}; "
            f"else tar {escaped} {where}; fi"
            f" && wc -c < {shlex.quote(archive)}"
        )
        sink: Any = None
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
            size = int(result.stdout.decode("ascii", errors="replace").strip() or -1)
            if size > _ARCHIVE_LIMIT:
                raise _ArchiveTooLarge(size)
            # Closed below on every path, or handed on inside the rewrite.
            sink = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY)  # noqa: SIM115
            await self._computer.download_file(
                archive,
                cast(
                    IO[bytes],
                    _SinkUntilClosing(_Capped(sink, _ARCHIVE_LIMIT), lambda: self._shut_down),
                ),
                part_size=_READ_PART,
            )
        except _Closing:
            if sink is not None:
                sink.close()
            raise _closing_read_error(error_root) from None
        except _ArchiveTooLarge as error:
            if sink is not None:
                sink.close()
            raise _too_large(error_root, error.size) from None
        except WorkspaceArchiveReadError:
            if sink is not None:
                sink.close()
            raise
        except (MandalaError, ExecTransportError, ExecTimeoutError, ValueError) as error:
            if sink is not None:
                sink.close()
            raise WorkspaceArchiveReadError(
                path=error_root,
                context={"backend": _BACKEND_ID, **_error_context(error)},
                cause=error,
                retryable=_retryable(error),
            ) from error
        finally:
            await self._remove_quietly(archive)
        try:
            sink.seek(0)
            restorable = _restorable_archive(sink, sandbox_path_str(root), _ARCHIVE_LIMIT)
        except _ArchiveTooLarge as error:
            raise _too_large(error_root, error.size) from None
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
        finally:
            sink.close()
        return cast(io.IOBase, restorable)

    async def hydrate_workspace(self, data: io.IOBase) -> None:
        async with self._mutating(lambda: _closing_write_error(self._workspace_root_path())):
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
        try:
            await self._save()
        except BaseException:
            # The SDK's own close skips shutdown when stop() raises, which would
            # leave the computer and its URLs up. Shut down here (it stops rather
            # than deletes, the save being unconfirmed), then raise the save's
            # own error. A later shutdown() finds it done.
            await _shielded(self._shutdown_quietly())
            raise

    async def _save(self) -> None:
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
            if self._shut_down and not isinstance(self.state.snapshot, NoopSnapshot):
                # The computer may be going; a save now could overwrite the
                # snapshot with a workspace that is about to be deleted.
                raise WorkspaceArchiveReadError(
                    path=posix_path_for_error(self._workspace_root_path()),
                    context={"backend": _BACKEND_ID, "reason": "session_shutting_down"},
                    retryable=False,
                )
            # Unconfirmed until this attempt succeeds: a failing store may
            # already have overwritten what an earlier save confirmed.
            self._saved_generation = None
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
        # This is the session's shutdown, so a later one waits for it.
        self._begin_shutdown(self._put_down_quietly("delete"))
        assert self._shutdown_task is not None
        await asyncio.shield(self._shutdown_task)

    def _workspace_unsaved(self) -> bool:
        if not self._workspace_ready and self._workspace_state_preserved_on_start():
            return True  # a kept disk the session never finished starting on
        return not isinstance(self.state.snapshot, NoopSnapshot) and (
            not self._workspace_ready or self._saved_generation != self._generation
        )

    async def _after_start(self) -> None:
        await super()._after_start()
        self._workspace_ready = True
        self.state.initialized = True
        # Disk wins here too: a later start of this session (after an idle
        # suspend, say) treats the workspace as kept, so only ephemeral
        # manifest entries are applied again and edited files are left alone.
        self._set_start_state_preserved(True, system=True)

    async def _drained(self) -> bool:
        """Wait, bounded, for everything already reaching the computer to finish."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _DRAIN_WAIT
        while self._busy and loop.time() < deadline:
            await asyncio.sleep(_POLL_FIRST)
        return not self._busy

    async def _shutdown_quietly(self) -> None:
        try:
            await self.shutdown()
        except Exception as error:  # noqa: BLE001 - the save's error is the one raised
            logger.warning(
                "could not shut down computer %s after a failed save (%s)",
                self.state.computer_id,
                type(error).__name__,
            )

    async def _remove_quietly(self, guest_path: str) -> None:
        try:
            await self._exec_internal("rm", "-f", "--", guest_path, timeout=_CLEANUP_TIMEOUT)
        except Exception:  # noqa: BLE001 - best effort; the file is in a temp dir
            logger.debug("could not remove %s from %s", guest_path, self.state.computer_id)

    # --- shutdown -----------------------------------------------------------

    async def _shutdown_backend(self) -> None:
        # One shutdown, run as its own task: every call waits for that same
        # task, and a caller's cancellation leaves it running to the end.
        if self._shutdown_task is None:
            self._begin_shutdown(self._shut_down_computer())
        assert self._shutdown_task is not None
        await asyncio.shield(self._shutdown_task)

    async def _settled_late(self) -> None:
        """After background cleanup finishes: undo what it may have woken."""
        await self._landed_late()

    async def _landed_late(self) -> None:
        """Work that may have reached the computer just finished.

        Once the session has begun putting the computer down, that work may
        have woken it. While the action is in flight this is only noted, and
        the action stops the computer again when it returns; once it has
        stopped or suspended the computer, it is stopped again now.
        """
        if self._releasing is None:
            return
        self._settled_after_release = True
        if self._released not in (None, "delete") and not self._held_by_another():
            await _stop_quietly(self._computer)

    def _shutdown_now(self) -> asyncio.Future[None]:
        """This session's shutdown task, started if it has not been."""
        if self._shutdown_task is None:
            self._begin_shutdown(self._shut_down_computer())
        assert self._shutdown_task is not None
        return self._shutdown_task

    def _begin_shutdown(self, body: Awaitable[None]) -> None:
        """Start this session's one shutdown; from here on, changes are refused."""
        self._shut_down = True
        self._shutdown_task = asyncio.ensure_future(body)
        self._shutdown_task.add_done_callback(lambda _: self._let_go())

    def _let_go(self) -> None:
        self._live.discard(self)
        if self._owners.get(self.state.computer_id) is self:
            del self._owners[self.state.computer_id]

    def _held_by_another(self) -> bool:
        """Another session has since taken this computer: leave it to that one."""
        holder = self._owners.get(self.state.computer_id)
        return holder is not None and holder is not self

    async def start(self) -> None:
        try:
            await super().start()
        except BaseException:
            # A start that failed or was cancelled midway: a failed context
            # entry never reaches its exit, and the SDK's own hook does not
            # see a cancellation, so the computer is put down here.
            await _shielded(self._abandon_start())
            raise

    async def _abandon_start(self) -> None:
        if self._workspace_ready or self._shutdown_task is not None:
            return
        if self._workspace_state_preserved_on_start():
            if not self._started_by_resume:
                return  # it was running before; leave it as it was
            # A kept computer this resume started: stop it, keeping its disk,
            # and take down its URLs, as a shutdown would.
            self._begin_shutdown(self._shut_down_computer("stop"))
            assert self._shutdown_task is not None
            await asyncio.shield(self._shutdown_task)
            return
        await self._after_start_failed()

    async def _put_down_quietly(self, mode: ShutdownMode) -> None:
        await self._put_down(mode)

    async def _undo_start(self, starts: list[asyncio.Future[AsyncComputer]]) -> None:
        """Stop the computer a failed resume started, and again if a start lands late."""
        await self._shut_down_computer("stop")
        for start in starts:
            if not start.done():
                await _after(
                    start,
                    self._again,
                    self._reaper,
                    f"the start of {self.computer_id}",
                    self._admitted_maybe,
                    owner=self,
                )
            elif start.cancelled() or start.exception() is not None:
                # The request failed, but the start may have been admitted and
                # still take effect: follow it until the computer settles.
                await self._admitted_maybe(start.exception() if not start.cancelled() else None)

    async def _again(self, _: object) -> None:
        await self._landed_late()

    async def _admitted_maybe(self, error: BaseException | None) -> None:
        """After a start that failed: watch for it taking effect anyway, then stop it."""
        if isinstance(error, MandalaError) and _clean_refusal(error):
            return  # refused outright: nothing was admitted
        if await self._watch_admitted(_ANSWER_WAIT):
            return
        # Not settled yet: the client's cleanup keeps watching.
        follow = asyncio.ensure_future(self._watch_admitted(_ADMITTED_WATCH))
        self._reaper.later(
            follow, self._again, f"a start admitted on {self.computer_id}", owner=self
        )

    async def _watch_admitted(self, limit: float) -> bool:
        """Poll until the computer settles; stop it if a start took effect. True once settled."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + limit
        while True:
            status = await _status_or_none(self._computer)
            if status == "running":
                if self._held_by_another():
                    return True  # another session runs it now
                self._settled_after_release = True
                await _stop_quietly(self._computer)
            elif status is not None and _settled_down(self._computer):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_ADMITTED_POLL)

    async def _shut_down_computer(self, forced: ShutdownMode | None = None) -> None:
        """The whole release: the computer put down, then its URLs removed.

        ``forced`` overrides ``on_shutdown``: a rolled-back resume stops the
        computer it started, whatever the session would do at its end.
        """
        mode = forced or self.state.on_shutdown
        computer = self._computer
        if not await self._drained():
            # A change is still landing and could not be waited out: keep the
            # disk it is landing on.
            logger.warning(
                "a change to the workspace of computer %s was still under way at "
                "shutdown; stopping it instead of deleting it",
                computer.id,
            )
            mode = "stop"  # not delete, and not a suspend a late change would wake
        if mode == "suspend" and (self._unsettled or self._contaminated):
            # Something handed to the client's cleanup, or a request whose
            # outcome was lost, may still reach the computer and wake it: stop
            # it instead (a delete stays a delete).
            mode = "stop"
        # Held across the decision and its action, so no save runs between them.
        locked = await _acquire_within(self._save_lock, _DRAIN_WAIT)
        if not locked:
            # A save has held on too long (a stalled upload, say). It cannot be
            # confirmed now, so nothing is deleted: the computer is stopped,
            # keeping its disk, and its URLs come down, without the lock.
            logger.warning(
                "a save on computer %s did not finish in time for shutdown; stopping "
                "the computer instead",
                computer.id,
            )
            mode = "stop"
        try:
            await self._release(mode)
        finally:
            if locked:
                self._save_lock.release()

    async def _release(self, mode: ShutdownMode) -> None:
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
        # The computer first, so that is done whatever happens to the rest; a
        # stopped computer serves nothing, so its URLs can come down after.
        outcome = await self._put_down(mode)
        if outcome != "delete":
            await self._settle_resolutions()
            await self._remove_public_urls()
            # Background work landed after the stop: make sure once more,
            # unless another session has taken the computer meanwhile.
            if self._settled_after_release and not self._held_by_another():
                await _stop_quietly(self._computer)

    async def _put_down(self, mode: ShutdownMode) -> str | None:
        """Stop, suspend or delete the computer: the only way the session does.

        Marked before the action, so work landing while it is in flight is
        noted; recorded after with what actually happened, so work landing
        later stops it again. If anything landed during the action, or the
        computer reads running after it, it is stopped again here.
        """
        self._releasing = mode
        landed_before = self._settled_after_release
        self._settled_after_release = False
        outcome = await self._act(mode)
        # Anything short of a confirmed delete leaves the computer to watch:
        # "uncertain" when the action could not be confirmed at all.
        self._released = outcome or "uncertain"
        if outcome != "delete":
            woken = self._settled_after_release
            if not woken:
                # Running, or a start admitted on it (memory held while it
                # reads stopped), is not down.
                woken = await _status_or_none(self._computer) is not None and not (
                    _settled_down(self._computer)
                )
            if woken and not self._held_by_another():
                await _stop_quietly(self._computer)
                self._released = "stop"
        self._settled_after_release = self._settled_after_release or landed_before
        return self._released

    async def _act(self, mode: ShutdownMode) -> str | None:
        """Delete, suspend or stop the computer, and say which it ended up being.

        ``"delete"``, ``"suspend"`` or ``"stop"``, or ``None`` when nothing took.
        """
        computer = self._computer
        try:
            if mode == "delete":
                try:
                    await computer.delete()
                    return "delete"
                except NotFoundError:
                    return "delete"
                except MandalaError as error:
                    # Not deleted: it and its URLs are still up. Stop it, and
                    # its URLs come down after.
                    logger.warning(
                        "could not delete computer %s on shutdown (%s); stopping it "
                        "and removing its public URLs instead",
                        computer.id,
                        type(error).__name__,
                    )
                    mode = "stop"
            if mode == "suspend":
                try:
                    await computer.suspend()
                    return "suspend"
                except MandalaError as error:
                    status = await _status_or_none(computer)
                    if status == "suspended" and _settled_down(computer):
                        return "suspend"
                    if status == "stopped" and _settled_down(computer):
                        return "stop"
                    # Not suspendable now: stopping keeps the disk, which is the
                    # part of the session a resume can always rebuild from.
                    logger.warning(
                        "could not suspend computer %s (%s); stopping it instead",
                        computer.id,
                        type(error).__name__,
                    )
            # The read only saves a needless stop: unless it confirms the
            # computer is stopped, stop it, whether or not the read worked.
            # Stopped with memory still held is a start being admitted, not a
            # stopped computer: it is stopped anyway.
            if await _status_or_none(computer) != "stopped" or not _settled_down(computer):
                try:
                    await computer.stop()
                except MandalaError as error:
                    if not _accepted_unread(error):
                        raise
                    # The stop was accepted; only reading the computer back
                    # failed. It counts as stopped.
            return "stop"
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
        return None

    async def _remove_public_urls(self) -> None:
        # Those whose create answers were lost, found by their labels, too.
        await self._reconcile_urls()
        for port, url_ids in list(self.state.public_url_ids.items()):
            kept = []
            for url_id in url_ids:
                try:
                    await asyncio.wait_for(
                        self._computer.delete_public_url(url_id), _CLEANUP_TIMEOUT
                    )
                    self._forget_url(port, url_id)
                except NotFoundError:
                    self._forget_url(port, url_id)
                except (MandalaError, asyncio.TimeoutError) as error:
                    logger.warning(
                        "could not remove the public URL for port %s on %s (%s)",
                        port,
                        self.state.computer_id,
                        type(error).__name__,
                    )
                    kept.append(url_id)
            if kept:
                self.state.public_url_ids[port] = kept
            else:
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
        self._live: set[MandalaSandboxSession] = set()
        self._owners: dict[str, MandalaSandboxSession] = {}
        #: Set when aclose() begins: no create or resume may begin after it.
        self._closing = False
        #: Creates and resumes under way, which aclose() waits for first.
        self._calls: set[asyncio.Future[SandboxSession]] = set()

    async def aclose(self) -> None:
        """Shut down sessions still open, finish outstanding cleanup, then close.

        A session its caller never closed is shut down as its ``on_shutdown``
        says, bounded. Cleanup still waiting on a platform answer (a command
        start, a create) is given a bounded time to finish; what is left is
        logged and dropped. The API client is closed if this one made it.
        """
        self._closing = True
        # Creates and resumes under way first: each that finishes now undoes
        # its own computer rather than handing back a session.
        if self._calls:
            _, unfinished = await asyncio.wait(list(self._calls), timeout=_CLOSE_WAIT)
            for call in unfinished:
                logger.warning("a create or resume had not finished at client close")
                call.cancel()
            if unfinished:
                await asyncio.wait(unfinished)
        # Cleanup waiting on answers, while the computers are still up.
        await self._reaper.aclose(_CLEANUP_TIMEOUT)
        # The shutdown tasks themselves are waited on, not a call that gives
        # up on them, and the transport they use is closed only after.
        shutdowns = {session._shutdown_now(): session for session in list(self._live)}
        if shutdowns:
            _, pending = await asyncio.wait(list(shutdowns), timeout=_CLOSE_WAIT)
            for task in pending:
                logger.warning(
                    "the shutdown of computer %s had not finished at client close; "
                    "it was cancelled",
                    shutdowns[task].computer_id,
                )
                task.cancel()
            if pending:
                await asyncio.wait(pending)
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
        return await self._call(
            self._create_session(snapshot=snapshot, manifest=manifest, options=options),
            Path((manifest or Manifest()).root),
        )

    async def resume(self, state: SandboxSessionState) -> SandboxSession:
        return await self._call(self._resume_session(state), Path(state.manifest.root))

    async def _call(self, work: Awaitable[SandboxSession], root: Path) -> SandboxSession:
        """A create or resume the client can wait for, and refuse once closing."""
        if self._closing:
            if isinstance(work, Coroutine):
                work.close()
            raise _client_closing(root)
        task = asyncio.ensure_future(work)
        self._calls.add(task)
        task.add_done_callback(self._calls.discard)
        session = await task
        if self._closing:
            # Finished after close began: put its computer down rather than
            # hand back a session nothing will close.
            inner = session._inner
            if isinstance(inner, MandalaSandboxSession):
                if inner._shutdown_task is None:
                    kept = inner._workspace_state_preserved_on_start()
                    inner._begin_shutdown(inner._put_down_quietly("stop" if kept else "delete"))
                assert inner._shutdown_task is not None
                await _shielded(inner._shutdown_task)
            raise _client_closing(root)
        return session

    async def _create_session(
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

        # One budget for the whole launch: create, start and readiness.
        deadline = _launch_deadline(options.launch_timeout_s)
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
        computer = await self._make_computer(state, deadline)
        inner = MandalaSandboxSession(
            state=state,
            computer=computer,
            reaper=self._reaper,
            live=self._live,
            owners=self._owners,
        )
        return self._wrap_session(inner, instrumentation=self._instrumentation)

    async def _resume_session(self, state: SandboxSessionState) -> SandboxSession:
        if not isinstance(state, MandalaSandboxSessionState):
            raise TypeError("MandalaSandboxClient.resume expects a MandalaSandboxSessionState")
        state.assert_path_grants_rebound()
        _check_manifest(state.manifest)
        # One budget for the whole launch: reattach or create, start, readiness.
        deadline = _launch_deadline(state.launch_timeout_s)

        computer: AsyncComputer | None = None
        try:
            if state.computer_id:
                computer = await _within(deadline, self._client.computers.get(state.computer_id))
        except NotFoundError:
            computer = None
        except (MandalaError, TimeoutError) as error:
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

        if computer is not None:
            # A previous session's shutdown of this computer may still be under
            # way: wait for it to finish before taking the computer over, so
            # its last stops cannot land on the resumed one.
            previous = self._owners.get(computer.id)
            pending = previous._shutdown_task if previous is not None else None
            if pending is not None and not pending.done():
                try:
                    await _within(deadline, asyncio.shield(pending))
                except TimeoutError as error:
                    _raise_start_error(state, error, "reattach")
                # Read it again: that shutdown may have changed it.
                try:
                    await _within(deadline, computer.refresh())
                except (MandalaError, TimeoutError) as error:
                    _raise_start_error(state, error, "reattach")

        # Disk wins only for a disk a start finished setting up.
        preserved = computer is not None and state.initialized
        if computer is not None and not state.initialized:
            state.workspace_root_ready = False
        if computer is not None:
            # The session exists, and the client counts it, before the start:
            # a client closed meanwhile shuts it down too.
            inner = MandalaSandboxSession(
                state=state,
                computer=computer,
                reaper=self._reaper,
                live=self._live,
                owners=self._owners,
            )
            inner._set_start_state_preserved(preserved, system=preserved)
            was_running = computer.status == "running"
            inner._started_by_resume = not was_running
            starts: list[asyncio.Future[AsyncComputer]] = []
            # Everything from here until the session is handed back is one
            # unit within the launch budget: any failure, timeout or
            # cancellation in it is rolled back.
            try:
                await _bring_up(computer, deadline, starts)
                if not was_running and state.ends_in_hours is not None:
                    await _within(
                        deadline, computer.set_end_time(ends_in_hours=state.ends_in_hours)
                    )
                # URLs an earlier session made but lost track of are this one's.
                await _within(deadline, inner._reconcile_urls())
                return self._wrap_session(inner, instrumentation=self._instrumentation)
            except BaseException as error:
                if not was_running:
                    # This resume started it; a failure or a cancellation after
                    # that must not leave it running, possibly with no end time.
                    # Stopped, it keeps its disk for the next attempt, and a
                    # start still on its way is stopped again once it lands.
                    inner._begin_shutdown(inner._undo_start(starts))
                    assert inner._shutdown_task is not None
                    await _shielded(inner._shutdown_task)
                else:
                    inner._live.discard(inner)
                if isinstance(error, (MandalaError, TimeoutError)):
                    _raise_start_error(state, error, "start")
                raise
        else:
            state.computer_id = ""
            state.workspace_root_ready = False
            state.public_url_ids = {}
            state.public_url_labels = {}  # a new computer has none of them
            state.uncertain = False  # a new computer: nothing unseen on it
            state.initialized = False
            computer = await self._make_computer(state, deadline)

        inner = MandalaSandboxSession(
            state=state,
            computer=computer,
            reaper=self._reaper,
            live=self._live,
            owners=self._owners,
        )
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

    async def _create(self, state: MandalaSandboxSessionState, deadline: float) -> AsyncComputer:
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
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                try:
                    return await send()
                except MandalaError as error:
                    if error.idempotency_key is None:
                        raise
                # The outcome is unknown: the same key answers with the first
                # attempt's computer, once its create has finished.
                delay = 0.25
                while True:
                    try:
                        return await send()
                    except APIError as error:
                        if error.code != "idempotency_in_progress":
                            raise
                        if loop.time() - started > _CREATE_SETTLE:
                            raise
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 5.0)
            except MandalaError as error:
                unresolved = error.idempotency_key is not None or (
                    isinstance(error, APIError) and error.code == "idempotency_in_progress"
                )
                if unresolved:
                    # Never learned whether it was made: look for it by the
                    # session's name, which only it carries.
                    if not generated:
                        logger.warning(
                            "a create of computer %r (idempotency key %s) ended with its "
                            "outcome unknown; it may exist",
                            name,
                            key,
                        )
                    await self._remove_unconfirmed(name, generated)
                elif not isinstance(error, APIError):
                    # Answered, then refused here: the computer exists.
                    await self._remove_unconfirmed(name, generated)
                raise

        task = asyncio.ensure_future(attempt())
        try:
            return await asyncio.wait_for(asyncio.shield(task), _left(deadline))
        except (asyncio.CancelledError, asyncio.TimeoutError, _LaunchTimeout) as error:
            # The create may still land: wait for it, and delete what it made.
            await _shielded(_after(task, _delete_quietly, self._reaper, f"the create of {name!r}"))
            if isinstance(error, asyncio.CancelledError):
                raise
            _raise_start_error(state, _LaunchTimeout(), "create")
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

    async def _make_computer(
        self, state: MandalaSandboxSessionState, deadline: float
    ) -> AsyncComputer:
        """Create a computer from ``state``'s inputs and wait for its guest.

        A computer that was created and then did not come up is deleted before
        the error is raised, cancellation included, so a failed create leaves
        nothing behind.

        A platform that cannot keep end times fails the session here, before
        any work, rather than running without one.
        """
        computer = await self._create(state, deadline)
        state.computer_id = computer.id
        try:
            await _bring_up(computer, deadline, ends=state.ends_in_hours is not None)
        except BaseException as error:
            await _shielded(_delete_quietly(computer))
            if isinstance(error, (MandalaError, TimeoutError)):
                _raise_start_error(state, error, "start")
            raise
        return computer


# --- helpers ----------------------------------------------------------------


class _EndTimePassed(MandalaError):
    """A computer created with an end time was found stopped without one."""


def _end_time_ahead(computer: AsyncComputer) -> bool:
    """Whether the computer still carries an end time that has not passed."""
    ends = computer.raw.get("ends_at")
    if not isinstance(ends, str) or not ends:
        return False
    try:
        moment = datetime.fromisoformat(ends.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        return False
    return moment > datetime.now(timezone.utc)


class _LaunchTimeout(TimeoutError):
    """The launch budget (``launch_timeout_s``) ran out."""


async def _acquire_within(lock: asyncio.Lock, timeout: float) -> bool:
    """Take ``lock`` within ``timeout``; False, and not holding it, if not."""
    take = asyncio.ensure_future(lock.acquire())
    done, _ = await asyncio.wait({take}, timeout=timeout)
    if done:
        return True
    take.cancel()
    try:
        await take
    except asyncio.CancelledError:
        return False
    # Taken just as it was given up on: let it go again.
    lock.release()
    return False


async def _within(deadline: float, step: Awaitable[_ReadT]) -> _ReadT:
    """One launch step, given only what is left of the budget."""
    try:
        left = _left(deadline)
    except _LaunchTimeout:
        if isinstance(step, Coroutine):
            step.close()
        raise
    try:
        return await asyncio.wait_for(step, left)
    except asyncio.TimeoutError:
        raise _LaunchTimeout() from None


def _launch_deadline(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


def _left(deadline: float) -> float:
    """What is left of a launch budget; raises once none is."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise _LaunchTimeout()
    return remaining


async def _bring_up(
    computer: AsyncComputer,
    deadline: float,
    starts: list[asyncio.Future[AsyncComputer]] | None = None,
    *,
    ends: bool = False,
) -> None:
    """Wait for ``computer`` to be built, start it if nothing is, and wait for its guest.

    The start runs as its own task, kept in ``starts`` when given, so a caller
    cancelled meanwhile can see whether it is still on its way.
    """

    def left() -> float:
        return _left(deadline)

    if computer.status == "building":
        await computer.wait_until_built(timeout=left())
    if computer.status in ("stopped", "suspended") or computer.start_error:
        if ends and not _end_time_ahead(computer):
            # Created with an end time that is no longer on it: it has passed
            # (and stopped it) while the disk was prepared. Starting it would
            # leave it running with none.
            raise _EndTimePassed(
                f"{computer.id} is {computer.status} and its end time has passed; "
                "it was not started"
            )
        start = asyncio.ensure_future(computer.start())
        if starts is not None:
            starts.append(start)
        try:
            await asyncio.wait_for(asyncio.shield(start), left())
        except asyncio.TimeoutError:
            raise _LaunchTimeout() from None
    if computer.status != "running":
        await computer.wait_until_running(timeout=left())
    await computer.wait_for_guest(timeout=left())


_ReadT = TypeVar("_ReadT")


class _Clock:
    """One command's deadline, the reads bounded by it and the pauses between them."""

    def __init__(
        self,
        argv: Sequence[str],
        timeout: float | None,
        deadline: float | None,
        closing: Callable[[], bool],
    ):
        self._closing = closing
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
            if self._closing():
                # The computer is being put down: another read could wake it.
                raise ExecTransportError(
                    command=self._argv,
                    context={"backend": _BACKEND_ID, "reason": "session_shutting_down"},
                    retryable=False,
                )
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
        then: Callable[[_ReadT], Awaitable[object]],
        what: str,
        failed: Callable[[BaseException], Awaitable[object]] | None = None,
        owner: MandalaSandboxSession | None = None,
    ) -> None:
        """Finish ``pending`` in the background; ``owner`` counts it as unsettled."""
        if owner is not None:
            owner._unsettled += 1

        async def run() -> None:
            try:
                try:
                    result = await pending
                except BaseException as error:  # noqa: BLE001 - handed to ``failed``
                    if failed is not None:
                        await failed(error)
                else:
                    await then(result)
            finally:
                if owner is not None:
                    owner._unsettled -= 1
                    await owner._settled_late()

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
    then: Callable[[_ReadT], Awaitable[object]],
    reaper: _Reaper,
    what: str,
    failed: Callable[[BaseException], Awaitable[object]] | None = None,
    owner: MandalaSandboxSession | None = None,
) -> bool:
    """Undo what an abandoned request made: now if it answers soon, later if not.

    True only when that is settled: the request was cleanly refused, or what it
    made was undone. Handed to the client's cleanup, or undone uncertainly, is
    False.
    """
    try:
        result = await asyncio.wait_for(asyncio.shield(pending), _ANSWER_WAIT)
    except asyncio.TimeoutError:
        reaper.later(pending, then, what, failed, owner)
        return False
    except MandalaError as error:
        if _clean_refusal(error):
            return True
        if failed is not None:
            await failed(error)
        return False
    except Exception:  # noqa: BLE001 - refused before it was sent
        return True
    return await then(result) is not False


async def _kill_quietly(job: AsyncBackgroundCommand) -> bool:
    """Stop a command; True only when the platform's answer says it has stopped."""
    try:
        status = await asyncio.wait_for(job.kill(), _CLEANUP_TIMEOUT)
    except Exception:  # noqa: BLE001 - reported to the caller as unconfirmed
        logger.debug("could not stop guest command %s", job.pid)
        return False
    return status.done


def _clean_refusal(error: MandalaError) -> bool:
    """A refusal that did nothing: a 4xx answer, not a lost or failed one."""
    return isinstance(error, APIError) and error.status is not None and error.status < 500


def _accepted_unread(error: MandalaError) -> bool:
    """A lifecycle call the platform accepted, whose read-back alone failed.

    The SDK reports that as a plain ``MandalaError`` caused by the read's error,
    distinct from any failure of the call itself.
    """
    return type(error) is MandalaError and isinstance(error.__cause__, MandalaError)


def _outcome_unknown(error: BaseException) -> bool:
    """Whether an operation ended with a request it sent possibly still to land.

    A cancellation, or a platform failure anywhere in the chain that is not a
    clean refusal. Refusals made here (paths, users, a session shutting down)
    and plain command results are not.
    """
    if isinstance(error, asyncio.CancelledError):
        return True
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, _Closing):
            return False
        if isinstance(current, MandalaError):
            return not _clean_refusal(current)
        current = current.__cause__ or current.__context__
    return False


def _tar_excludes(relpaths: Iterable[Path], *, escape: bool) -> list[str]:
    """``--exclude`` options naming exactly these workspace paths."""
    options = []
    for rel in sorted(relpaths, key=lambda path: path.as_posix()):
        rel_posix = rel.as_posix().lstrip("/")
        if not rel_posix or rel_posix in {".", "/"}:
            continue
        if escape:
            rel_posix = re.sub(r"([\\\[\]*?])", r"\\\1", rel_posix)
        options.append(f"--exclude={shlex.quote(f'./{rel_posix}')}")
    return options


def _settled_down(computer: AsyncComputer) -> bool:
    """Stopped or suspended with nothing admitted: no memory held for a start.

    Only a reported zero confirms that; a value that is missing or unreadable
    is not taken for one.
    """
    held = computer.raw.get("running_ram_mb")
    return (
        computer.status in ("stopped", "suspended")
        and isinstance(held, (int, float))
        and not isinstance(held, bool)
        and held == 0
    )


async def _status_or_none(computer: AsyncComputer) -> str | None:
    """The computer's status, or ``None`` when it cannot be read."""
    try:
        await computer.refresh()
    except MandalaError:
        return None
    return computer.status


async def _stop_quietly(computer: AsyncComputer) -> None:
    try:
        await asyncio.wait_for(computer.stop(), _CLEANUP_TIMEOUT)
    except Exception as error:  # noqa: BLE001 - reported; the end time may still apply
        logger.warning(
            "could not stop computer %s after a failed resume (%s)",
            computer.id,
            type(error).__name__,
        )


class _Closing(Exception):
    """The session began shutting down in the middle of a chunked upload."""


class _StopWhenClosing(io.BytesIO):
    """An upload's source that ends the upload, between chunks, once shutdown begins.

    A chunk sent after shutdown could land on a computer being deleted, or
    wake one it suspended.
    """

    def __init__(self, payload: bytes, closing: Callable[[], bool]) -> None:
        super().__init__(payload)
        self._closing = closing

    def read(self, size: int | None = -1, /) -> bytes:
        if self._closing():
            raise _Closing("the session is shutting down")
        return super().read(size)


class _SinkUntilClosing(io.RawIOBase):
    """A download's sink that ends the download, between ranges, once closing."""

    def __init__(self, sink: Any, closing: Callable[[], bool]) -> None:
        self._sink = sink
        self._closing = closing

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        self._sink.write(data)
        if self._closing():
            raise _Closing("the session is shutting down")
        return len(data)


def _client_closing(root: Path) -> WorkspaceStartError:
    return WorkspaceStartError(
        path=posix_path_for_error(root),
        message="the Mandala sandbox client is closing",
        context={"backend": _BACKEND_ID, "reason": "client_closing"},
        retryable=False,
    )


def _closing_read_error(path: Path) -> WorkspaceArchiveReadError:
    return WorkspaceArchiveReadError(
        path=path,
        context={"backend": _BACKEND_ID, "reason": "session_shutting_down"},
        retryable=False,
    )


def _closing_write_error(path: Path) -> WorkspaceArchiveWriteError:
    return WorkspaceArchiveWriteError(
        path=path,
        context={"backend": _BACKEND_ID, "reason": "session_shutting_down"},
        retryable=False,
    )


class _ArchiveTooLarge(Exception):
    """An archive past what a restore can upload; ``size`` is how far it got."""

    def __init__(self, size: int) -> None:
        super().__init__(size)
        self.size = size


def _too_large(path: Path, size: int) -> WorkspaceArchiveReadError:
    # Larger than a restore can upload: saved, it could never be used.
    return WorkspaceArchiveReadError(
        path=path,
        context={
            "backend": _BACKEND_ID,
            "reason": "archive_too_large",
            "size": size,
            "limit": _ARCHIVE_LIMIT,
        },
        retryable=False,
    )


class _Capped(io.RawIOBase):
    """A writable sink that refuses to grow past ``limit`` bytes."""

    def __init__(self, sink: Any, limit: int) -> None:
        self._sink = sink
        self._limit = limit
        self._written = 0

    def writable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._written

    def write(self, data: Any) -> int:
        size = len(data)
        if self._written + size > self._limit:
            raise _ArchiveTooLarge(self._written + size)
        self._sink.write(data)
        self._written += size
        return size


def _restorable_archive(source: Any, root: str, limit: int) -> Any:
    """``source`` rewritten into what :meth:`hydrate_workspace` will accept.

    Restoring refuses hard links and symlinks that point out of the workspace,
    as every SDK backend does. A hard link becomes a regular file holding the
    same bytes. A symlink whose absolute target is inside the workspace root
    becomes the equivalent relative one. A symlink that points outside the
    workspace cannot be restored at all, so it raises ``UnsafeTarMemberError``
    naming it, and the result is checked against the restore policy before it
    is returned.

    Written to a spooled file and counted as it grows, hard-link copies
    included: past ``limit`` it raises ``_ArchiveTooLarge``. Returned rewound.
    """
    # Returned to the caller, who closes it; closed here only on failure.
    out = tempfile.SpooledTemporaryFile(max_size=_SPOOL_IN_MEMORY)  # noqa: SIM115
    try:
        with (
            tarfile.open(fileobj=source, mode="r:") as tar,
            tarfile.open(
                fileobj=_Capped(out, limit), mode="w", format=tarfile.PAX_FORMAT
            ) as target,
        ):
            files: dict[str, tarfile.TarInfo] = {}
            for member in tar:
                name = posixpath.normpath(member.name)
                if member.isreg():
                    files[name] = member
                    target.addfile(member, tar.extractfile(member))
                elif member.islnk():
                    linked = files.get(posixpath.normpath(member.linkname))
                    if linked is None:
                        raise UnsafeTarMemberError(
                            member=member.name,
                            reason=f"hard link to a file not in the archive: {member.linkname}",
                        )
                    copy = tarfile.TarInfo(member.name)
                    copy.size = linked.size
                    copy.mode, copy.mtime = member.mode, member.mtime
                    copy.uid, copy.gid = member.uid, member.gid
                    copy.uname, copy.gname = member.uname, member.gname
                    files[name] = linked
                    target.addfile(copy, tar.extractfile(linked))
                elif member.issym() and member.linkname.startswith("/"):
                    # Rebased only when made of plain components: a ``..`` after
                    # a symlinked component means something else on disk than it
                    # does as text, so it is not resolved here but refused.
                    parts = PurePosixPath(member.linkname).parts[1:]
                    raw = member.linkname.split("/")[1:]
                    if "" in raw or "." in raw:
                        # A trailing "/" or "/.", or an empty or "." component,
                        # changes what the link resolves to (a trailing one
                        # requires a directory) and would be lost in a rebase.
                        raise UnsafeTarMemberError(
                            member=member.name,
                            reason=f"absolute symlink target not in plain form: {member.linkname}",
                        )
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
                    member.linkname = posixpath.relpath(
                        posixpath.join(".", *parts[len(root_parts) :]),
                        posixpath.join(".", posixpath.dirname(name)),
                    )
                    # A long target travels in a PAX header, which would win
                    # over the field just set when the member is written again.
                    member.pax_headers = {
                        k: v for k, v in member.pax_headers.items() if k != "linkpath"
                    }
                    target.addfile(member)
                else:
                    target.addfile(member)
        out.seek(0)
        with tarfile.open(fileobj=out, mode="r:") as check:
            validate_tarfile(check, allow_external_symlink_targets=False)
        out.seek(0)
        return out
    except BaseException:
        out.close()
        raise


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


async def _shielded(cleanup: Awaitable[_ReadT]) -> _ReadT:
    """Run a cleanup coroutine to the end even when the caller is being cancelled."""
    task = asyncio.ensure_future(cleanup)
    try:
        return await asyncio.shield(task)
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
    if isinstance(error, TimeoutError):
        context["reason"] = "timeout"
    if isinstance(error, _EndTimePassed):
        context["reason"] = "end_time_passed"
    if state.computer_id:
        context["computer_id"] = state.computer_id
    raise WorkspaceStartError(
        path=posix_path_for_error(Path(state.manifest.root)),
        message=f"could not {stage} the Mandala computer: {type(error).__name__}",
        context=context,
        cause=error,
        retryable=_retryable(error),
    ) from error
