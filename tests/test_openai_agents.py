"""The OpenAI Agents SDK sandbox client (OPL-6048), against a stand-in API.

The stand-in (``tests/openai_agents_fake.py``) runs the "guest" on this
machine, so these exercise the real Agents SDK session machinery — its path
helper, manifest application, tar snapshots and the ``Runner`` — over the real
Mandala SDK's requests. What they cannot establish is how a real computer
behaves; that is the platform's own suite and a live run.
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from agents import RunConfig, Runner
from agents.sandbox import Manifest, SandboxAgent, SandboxRunConfig, User
from agents.sandbox.capabilities import Shell
from agents.sandbox.entries import S3Mount
from agents.sandbox.entries.mounts.base import InContainerMountStrategy
from agents.sandbox.entries.mounts.patterns import RcloneMountPattern
from agents.sandbox.errors import (
    ConfigurationError,
    ExecTimeoutError,
    ExecTransportError,
    ExposedPortUnavailableError,
    InvalidManifestPathError,
    WorkspaceArchiveReadError,
    WorkspaceArchiveWriteError,
    WorkspaceReadNotFoundError,
    WorkspaceStartError,
)
from agents.sandbox.session import BaseSandboxClientOptions, SandboxSessionState
from agents.sandbox.snapshot import LocalSnapshotSpec
from agents.testing import ScriptedModel, assistant_message, function_call
from tests.openai_agents_fake import FakePlatform

import mandala_computer.openai_agents as oa
from mandala_computer.openai_agents import (
    MandalaSandboxClient,
    MandalaSandboxClientOptions,
    MandalaSandboxSession,
    MandalaSandboxSessionState,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in guest is POSIX")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def fake(root: Path) -> Iterator[FakePlatform]:
    platform = FakePlatform(wipe=[root])
    yield platform
    platform.close()


@pytest.fixture
async def client(fake: FakePlatform) -> AsyncIterator[MandalaSandboxClient]:
    async with MandalaSandboxClient(fake.client()) as sandbox_client:
        yield sandbox_client


def manifest(root: Path, **kw: object) -> Manifest:
    return Manifest(root=str(root), **kw)  # type: ignore[arg-type]


def inner(session: object) -> MandalaSandboxSession:
    found = session._inner  # type: ignore[attr-defined]
    assert isinstance(found, MandalaSandboxSession)
    return found


def sent_create(fake: FakePlatform) -> dict[str, object]:
    (request,) = fake.calls("POST", "/computers")
    body = json.loads(request.content)
    assert isinstance(body, dict)
    return body


# --- options ------------------------------------------------------------------


def test_options_parse_by_type_and_refuse_unknown_fields() -> None:
    parsed = BaseSandboxClientOptions.parse({"type": "mandala", "size": "small"})
    assert isinstance(parsed, MandalaSandboxClientOptions)
    assert parsed.size == "small" and parsed.ends_in_hours == 24.0
    assert parsed.on_shutdown == "delete"
    with pytest.raises(ValueError):
        MandalaSandboxClientOptions(image="python:3.14")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "kw",
    [
        {"size": "small", "template": "base"},
        {"size": "small", "cpu": 2},
        {"exposed_ports": (22,)},
        {"exposed_ports": (0,)},
        {"ends_in_hours": 0},
        {"ends_in_hours": 9000},
        {"on_shutdown": "kill"},
    ],
)
def test_options_refuse_what_the_platform_would(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        MandalaSandboxClientOptions(**kw)  # type: ignore[arg-type]


# --- create -------------------------------------------------------------------


async def test_create_sends_the_options_and_waits_for_the_guest(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(
        manifest=manifest(root),
        options=MandalaSandboxClientOptions(size="small", name="agent", ends_in_hours=2),
    )
    assert sent_create(fake) == {"name": "agent", "size": "small", "start": True}
    # The end time is set on the computer once it exists.
    assert json.loads(fake.calls("PATCH", "/vm-1")[0].content) == {"ends_in_hours": 2}
    # The readiness probe ran before create returned.
    probe = json.loads(fake.calls("POST", "/exec")[0].content)
    assert probe["command"] == "exit 0" and "background" not in probe
    assert inner(session).computer_id == "vm-1"
    assert session.state.computer_id == "vm-1"  # type: ignore[attr-defined]


async def test_create_without_options_uses_the_defaults(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    await client.create(manifest=manifest(root))
    assert sent_create(fake) == {"start": True}
    assert json.loads(fake.calls("PATCH", "/vm-1")[0].content) == {"ends_in_hours": 24.0}


async def test_no_end_time_when_none_is_asked_for(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    await client.create(
        manifest=manifest(root), options=MandalaSandboxClientOptions(ends_in_hours=None)
    )
    assert not fake.calls("PATCH")


async def test_a_platform_without_end_times_fails_the_create_and_cleans_up(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    fake.fail[("PATCH", "vm-1")] = (
        409,
        {"error": "this host cannot keep end times", "reason": "unsupported"},
    )
    with pytest.raises(WorkspaceStartError) as caught:
        await client.create(manifest=manifest(root))
    assert caught.value.context["stage"] == "set the end time of"
    assert caught.value.retryable is False
    assert fake.computers == {}


async def test_a_computer_that_does_not_come_up_is_deleted(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    fake.create_status = "build-failed"
    with pytest.raises(WorkspaceStartError) as caught:
        await client.create(
            manifest=manifest(root), options=MandalaSandboxClientOptions(launch_timeout_s=1)
        )
    assert caught.value.context["computer_id"] == "vm-1"
    assert fake.computers == {}


async def test_a_refused_create_keeps_the_platform_code(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    fake.fail[("POST", "computers")] = (402, {"error": "plan limit", "code": "PLAN_REQUIRED"})
    with pytest.raises(WorkspaceStartError) as caught:
        await client.create(manifest=manifest(root))
    error = caught.value
    assert error.retryable is False
    assert error.context["status"] == 402 and error.context["code"] == "PLAN_REQUIRED"
    assert error.context["provider_error"] == "PlanLimitError"


# --- refusals -------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"users": [User(name="sandbox")]},
        {
            "entries": {
                "data": S3Mount(
                    bucket="b",
                    mount_strategy=InContainerMountStrategy(pattern=RcloneMountPattern()),
                )
            }
        },
    ],
)
async def test_unsupported_manifests_are_refused_before_anything_is_made(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, extra: dict[str, object]
) -> None:
    with pytest.raises(ConfigurationError):
        await client.create(manifest=manifest(root, **extra))
    assert fake.requests == []


async def test_other_users_and_pty_are_refused(client: MandalaSandboxClient, root: Path) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    with pytest.raises(ConfigurationError):
        await session.exec("id", user="sandbox")
    with pytest.raises(ConfigurationError):
        await session.read(Path("x"), user=User(name="sandbox"))
    with pytest.raises(ConfigurationError):
        await session.write(Path("x"), io.BytesIO(b""), user="sandbox")
    # The default identity is not a refusal.
    assert (await session.exec("id -u", user="root")).ok()
    assert not session.supports_pty()
    with pytest.raises(NotImplementedError):
        await session.pty_exec_start("sh")
    await session.aclose()


# --- commands and files ---------------------------------------------------------


async def test_exec_runs_in_the_workspace_with_the_manifest_environment(
    client: MandalaSandboxClient, root: Path
) -> None:
    from agents.sandbox.manifest import Environment

    session = await client.create(
        manifest=manifest(root, environment=Environment(value={"GREETING": "hello"}))
    )
    await session.start()
    result = await session.exec("printf '%s:' \"$GREETING\"; pwd; echo oops >&2; exit 3")
    assert result.exit_code == 3
    assert result.stdout.decode() == f"hello:{root.resolve()}\n" or result.stdout.decode() == (
        f"hello:{root}\n"
    )
    assert result.stderr == b"oops\n"
    await session.aclose()


async def test_a_timed_out_command_is_stopped(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    with pytest.raises(ExecTimeoutError):
        await session.exec("sleep 30", timeout=0.3)
    killed = [job for job in fake.jobs.values() if job.killed]
    assert len(killed) == 1 and "sleep 30" in killed[0].command
    await session.aclose()


async def test_a_lost_poll_is_a_transport_error_and_stops_the_command(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    next_pid = 1000 + len(fake.jobs)
    fake.fail[("GET", str(next_pid))] = (500, {"error": "boom"})
    with pytest.raises(ExecTransportError) as caught:
        await session.exec("sleep 30")
    assert caught.value.context["status"] == 500
    assert fake.jobs[next_pid].killed
    fake.fail.clear()
    await session.aclose()


async def test_a_transient_poll_failure_is_asked_again(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    next_pid = 1000 + len(fake.jobs)
    fake.fail_once[("GET", str(next_pid))] = (
        409,
        {"error": "the host's service is restarting", "reason": "contention"},
    )
    result = await session.exec("printf ok")
    assert result.ok() and result.stdout == b"ok"
    assert not fake.jobs[next_pid].killed
    await session.aclose()


async def test_write_read_and_bounded_read(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    await session.write(Path("notes/today.txt"), io.BytesIO(b"hello world"))
    assert (root / "notes" / "today.txt").read_bytes() == b"hello world"
    assert (await session.read(Path("notes/today.txt"))).read() == b"hello world"
    assert await session.read_bounded(Path("notes/today.txt"), max_bytes=5) == b"hello"
    await session.write(Path("empty"), io.BytesIO(b""))
    assert (await session.read(Path("empty"))).read() == b""
    assert await session.read_bounded(Path("empty"), max_bytes=5) == b""
    with pytest.raises(WorkspaceReadNotFoundError):
        await session.read(Path("missing.txt"))
    await session.aclose()


async def test_a_write_past_one_request_goes_in_chunks(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oa, "FILE_SIZE_LIMIT", 4)
    session = await client.create(manifest=manifest(root))
    await session.start()
    await session.write(Path("big.bin"), io.BytesIO(b"0123456789"))
    assert (root / "big.bin").read_bytes() == b"0123456789"
    assert fake.calls("PUT", "/files/uploads") and fake.calls("DELETE", "/files/uploads")
    assert fake.uploads == {}
    await session.aclose()


async def test_paths_outside_the_workspace_are_refused(
    client: MandalaSandboxClient, root: Path, tmp_path: Path
) -> None:
    (tmp_path / "secret").write_text("no")
    session = await client.create(manifest=manifest(root))
    await session.start()
    with pytest.raises(InvalidManifestPathError):
        await session.read(Path("../secret"))
    # A symlink inside the workspace that points out of it is resolved in the
    # guest and refused too.
    (root / "link").symlink_to(tmp_path / "secret")
    with pytest.raises(InvalidManifestPathError):
        await session.read(Path("link"))
    with pytest.raises(InvalidManifestPathError):
        await session.write(Path("link"), io.BytesIO(b"overwritten"))
    assert (tmp_path / "secret").read_text() == "no"
    await session.aclose()


# --- exposed ports --------------------------------------------------------------


async def test_an_exposed_port_is_served_through_a_public_url(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(
        manifest=manifest(root),
        options=MandalaSandboxClientOptions(exposed_ports=(8080,), public_url_access="private"),
    )
    await session.start()
    endpoint = await session.resolve_exposed_port(8080)
    assert (endpoint.host, endpoint.port, endpoint.tls) == ("agent-8080-0.mandala.dev", 443, True)
    assert endpoint.url_for("http") == "https://agent-8080-0.mandala.dev/"
    assert json.loads(fake.calls("POST", "/public-urls")[0].content) == {
        "port": 8080,
        "access": "private",
    }
    # Resolved again, the same URL is reused rather than another made.
    assert (await session.resolve_exposed_port(8080)).host == endpoint.host
    assert len(fake.calls("POST", "/public-urls")) == 1
    with pytest.raises(ExposedPortUnavailableError) as caught:
        await session.resolve_exposed_port(9000)
    assert caught.value.context["reason"] == "not_configured"
    await session.aclose()


async def test_an_exposed_port_where_public_urls_are_off(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    fake.public_urls_enabled = False
    session = await client.create(
        manifest=manifest(root), options=MandalaSandboxClientOptions(exposed_ports=(8080,))
    )
    with pytest.raises(ExposedPortUnavailableError) as caught:
        await session.resolve_exposed_port(8080)
    assert caught.value.retryable is False
    assert caught.value.context["detail"] == "Public URLs are off here."
    await session.aclose()


async def test_a_refused_public_url_keeps_its_code(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    fake.fail[("POST", "public-urls")] = (409, {"error": "limit", "code": "PUBLIC_URL_LIMIT"})
    session = await client.create(
        manifest=manifest(root), options=MandalaSandboxClientOptions(exposed_ports=(8080,))
    )
    with pytest.raises(ExposedPortUnavailableError) as caught:
        await session.resolve_exposed_port(8080)
    assert caught.value.context["code"] == "PUBLIC_URL_LIMIT"
    assert caught.value.retryable is False
    fake.fail.clear()
    await session.aclose()


# --- shutdown -------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "status"), [("stop", "stopped"), ("suspend", "suspended")])
async def test_shutdown_keeps_the_computer_when_asked(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, mode: str, status: str
) -> None:
    session = await client.create(
        manifest=manifest(root),
        options=MandalaSandboxClientOptions(on_shutdown=mode, exposed_ports=(8080,)),  # type: ignore[arg-type]
    )
    await session.start()
    await session.resolve_exposed_port(8080)
    await session.aclose()
    await client.delete(session)  # what the runner calls next; nothing more happens
    assert fake.computers["vm-1"]["status"] == status
    # The public URL this session made is removed; the computer is kept.
    assert fake.public_urls["vm-1"] == []
    assert not fake.calls("DELETE", "/vm-1")


async def test_a_refused_suspend_falls_back_to_stop(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(
        manifest=manifest(root), options=MandalaSandboxClientOptions(on_shutdown="suspend")
    )
    fake.fail[("POST", "suspend")] = (409, {"error": "busy", "reason": "contention"})
    await session.aclose()
    assert fake.computers["vm-1"]["status"] == "stopped"


async def test_shutdown_deletes_by_default_and_tolerates_repeats(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    await session.aclose()
    await session.aclose()
    await client.delete(session)
    assert fake.computers == {}
    assert len(fake.calls("DELETE", "/vm-1")) == 1


async def test_a_failed_delete_does_not_mask_the_run(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = await client.create(manifest=manifest(root))
    fake.fail[("DELETE", "vm-1")] = (500, {"error": "boom"})
    await session.aclose()
    assert "vm-1" in caplog.text


# --- state and resume -----------------------------------------------------------


async def test_state_round_trips_without_the_api_key(
    client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(
        manifest=manifest(root),
        options=MandalaSandboxClientOptions(template="dev", on_shutdown="stop"),
    )
    payload = client.serialize_session_state(session.state)
    text = json.dumps(payload)
    assert "gck_secret_test_key" not in text
    assert payload["type"] == "mandala" and payload["computer_id"] == "vm-1"
    restored = client.deserialize_session_state(json.loads(text))
    assert isinstance(restored, MandalaSandboxSessionState)
    assert restored.computer_id == "vm-1" and restored.template == "dev"
    assert restored.on_shutdown == "stop"
    # The SDK's own registry picks the type out of a bare payload too.
    assert isinstance(SandboxSessionState.parse(json.loads(text)), MandalaSandboxSessionState)
    await session.aclose()


async def test_resume_reattaches_a_running_computer(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    await session.write(Path("kept.txt"), io.BytesIO(b"still here"))
    payload = client.serialize_session_state(session.state)

    resumed = await client.resume(client.deserialize_session_state(payload))
    await resumed.start()
    assert inner(resumed).computer_id == "vm-1"
    assert (await resumed.read(Path("kept.txt"))).read() == b"still here"
    assert len(fake.calls("POST", "/computers")) == 1
    assert not fake.calls("POST", "/start")
    await resumed.aclose()


async def test_resume_starts_a_stopped_computer_and_renews_its_end_time(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(
        manifest=manifest(root),
        options=MandalaSandboxClientOptions(on_shutdown="stop", ends_in_hours=3),
    )
    await session.start()
    await session.write(Path("kept.txt"), io.BytesIO(b"on disk"))
    await session.aclose()
    assert fake.computers["vm-1"]["status"] == "stopped"

    payload = client.serialize_session_state(session.state)
    resumed = await client.resume(client.deserialize_session_state(payload))
    await resumed.start()
    assert fake.computers["vm-1"]["status"] == "running"
    assert len(fake.calls("POST", "/vm-1/start")) == 1
    # Once at create, once more when the resume started it again.
    patches = [json.loads(r.content) for r in fake.calls("PATCH", "/vm-1")]
    assert patches == [{"ends_in_hours": 3}, {"ends_in_hours": 3}]
    assert (await resumed.read(Path("kept.txt"))).read() == b"on disk"
    await resumed.aclose()


async def test_resume_replaces_a_missing_computer_from_the_snapshot(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, tmp_path: Path
) -> None:
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    session = await client.create(
        manifest=manifest(root),
        snapshot=LocalSnapshotSpec(base_path=snapshots),
        options=MandalaSandboxClientOptions(template="dev"),
    )
    await session.start()
    await session.write(Path("src/app.py"), io.BytesIO(b"print('hi')\n"))
    await session.exec("mkdir -p data && printf x > data/blob")
    await session.aclose()  # persists the workspace, then deletes the computer
    assert fake.computers == {} and not root.exists()

    payload = client.serialize_session_state(session.state)
    resumed = await client.resume(client.deserialize_session_state(payload))
    assert inner(resumed).computer_id == "vm-2"
    assert json.loads(fake.calls("POST", "/computers")[1].content)["template"] == "dev"
    await resumed.start()
    assert (root / "src" / "app.py").read_bytes() == b"print('hi')\n"
    assert (await resumed.read(Path("data/blob"))).read() == b"x"
    await resumed.aclose()


async def test_persist_and_hydrate_round_trip(
    client: MandalaSandboxClient, root: Path, tmp_path: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    await session.write(Path("a/b/c.txt"), io.BytesIO(b"deep"))
    await session.exec("ln -s a/b/c.txt shortcut")
    archive = (await session.persist_workspace()).read()
    await session.exec("rm -rf a shortcut")
    await session.hydrate_workspace(io.BytesIO(archive))
    assert (root / "a" / "b" / "c.txt").read_bytes() == b"deep"
    assert (root / "shortcut").is_symlink()
    # Nothing staged is left in the guest's temporary directory.
    assert not list(Path("/tmp").glob("mandala-agents-*"))
    await session.aclose()


async def test_hydrate_refuses_an_archive_that_escapes(
    client: MandalaSandboxClient, root: Path
) -> None:
    import tarfile

    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        member = tarfile.TarInfo("../escape.txt")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    session = await client.create(manifest=manifest(root))
    await session.start()
    with pytest.raises(WorkspaceArchiveWriteError):
        await session.hydrate_workspace(io.BytesIO(raw.getvalue()))
    await session.aclose()


async def test_resume_raises_when_the_platform_cannot_be_read(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    payload = client.serialize_session_state(session.state)
    fake.fail[("GET", "vm-1")] = (403, {"error": "forbidden"})
    with pytest.raises(WorkspaceStartError) as caught:
        await client.resume(client.deserialize_session_state(payload))
    assert caught.value.context["status"] == 403
    # It did not make a second computer on the strength of an unreadable one.
    assert len(fake.calls("POST", "/computers")) == 1
    fake.fail.clear()
    await session.aclose()


# --- the Runner -----------------------------------------------------------------


async def test_a_runner_drives_the_session_end_to_end(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    model = ScriptedModel(
        [
            [
                function_call(
                    "exec_command",
                    {"cmd": "printf 'mandala works\\n' > note.txt && cat note.txt"},
                    call_id="call-1",
                )
            ],
            [assistant_message("done")],
        ]
    )
    agent = SandboxAgent(
        name="mandala",
        model=model,
        capabilities=[Shell()],
        default_manifest=manifest(root),
    )
    result = await Runner.run(
        agent,
        "Write a note.",
        run_config=RunConfig(
            tracing_disabled=True,
            # Options as a payload are parsed by the SDK into this client's type.
            sandbox=SandboxRunConfig(client=client, options={"type": "mandala", "size": "small"}),
        ),
    )
    model.assert_complete()
    outputs = [item.output for item in result.new_items if item.type == "tool_call_output_item"]
    assert any("mandala works" in str(output) for output in outputs), outputs
    assert sent_create(fake) == {"size": "small", "start": True}
    # The runner owned the session, so the computer is gone with it.
    assert fake.computers == {}


# --- packaging ------------------------------------------------------------------


def test_the_package_imports_without_openai_agents() -> None:
    script = (
        "import builtins\n"
        "real = builtins.__import__\n"
        "def guard(name, *args, **kwargs):\n"
        "    if name == 'agents' or name.startswith('agents.'):\n"
        "        raise ImportError(name)\n"
        "    return real(name, *args, **kwargs)\n"
        "builtins.__import__ = guard\n"
        "import mandala_computer\n"
        "try:\n"
        "    import mandala_computer.openai_agents\n"
        "except ImportError as e:\n"
        "    print('refused:', e)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout
    assert "refused: mandala_computer.openai_agents needs the openai-agents package" in out
    assert "pip install 'mandala-computer[openai-agents]'" in out


def test_importing_the_package_does_not_import_the_agents_sdk() -> None:
    script = "import sys, mandala_computer\nprint('agents' in sys.modules)\n"
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "False"


async def test_cancelling_a_command_stops_it(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    task = asyncio.ensure_future(session.exec("sleep 30"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(job.killed for job in fake.jobs.values() if "sleep 30" in job.command)
    await session.aclose()


# --- review round 1 -------------------------------------------------------------


async def test_a_noisy_command_still_times_out_and_is_stopped(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    with pytest.raises(ExecTimeoutError):
        await asyncio.wait_for(
            session.exec("while true; do echo noise; done", timeout=0.5), timeout=20
        )
    noisy = [job for job in fake.jobs.values() if "noise" in job.command]
    assert len(noisy) == 1 and noisy[0].killed
    await session.aclose()


async def test_a_long_output_is_read_to_the_end(client: MandalaSandboxClient, root: Path) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    result = await session.exec("seq 1 20000")
    assert result.ok()
    assert result.stdout.decode().split() == [str(n) for n in range(1, 20001)]
    await session.aclose()


async def test_links_in_the_workspace_survive_a_snapshot(
    client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    await session.exec(
        "mkdir -p .venv/bin && printf 'interp' > .venv/bin/python3.12"
        " && ln -s python3.12 .venv/bin/python"
        f" && ln -s {root}/.venv/bin/python3.12 .venv/bin/python3"
        " && printf 'shared' > data.txt && ln data.txt data-link.txt"
    )
    archive = (await session.persist_workspace()).read()
    await session.exec("rm -rf .venv data.txt data-link.txt")
    await session.hydrate_workspace(io.BytesIO(archive))
    assert (root / ".venv" / "bin" / "python").is_symlink()
    assert (root / ".venv" / "bin" / "python").read_bytes() == b"interp"
    # The absolute link inside the workspace comes back relative.
    python3 = root / ".venv" / "bin" / "python3"
    assert python3.is_symlink() and not str(python3.readlink()).startswith("/")
    assert python3.read_bytes() == b"interp"
    # The hard link comes back as a file with the same bytes.
    assert (root / "data-link.txt").read_bytes() == b"shared"
    assert not (root / "data-link.txt").is_symlink()
    await session.aclose()


async def test_an_unrestorable_snapshot_fails_and_the_computer_is_kept(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, tmp_path: Path
) -> None:
    snapshots = tmp_path / "snapshots"
    snapshots.mkdir()
    session = await client.create(
        manifest=manifest(root), snapshot=LocalSnapshotSpec(base_path=snapshots)
    )
    await session.start()
    await session.exec("ln -s /etc/hosts outside")
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await session.persist_workspace()
    assert caught.value.context["reason"] == "unrestorable_member"
    assert "outside" in str(caught.value.context["member"])
    # The runner's cleanup persists again, fails again, and shuts down anyway:
    # the computer holding the only copy is stopped, not deleted.
    with pytest.raises(WorkspaceArchiveReadError):
        await session.stop()
    await session.shutdown()
    await client.delete(session)
    assert fake.computers["vm-1"]["status"] == "stopped"
    assert not fake.calls("DELETE", "/vm-1")
    assert not list(snapshots.iterdir())


async def test_a_resume_that_fails_after_starting_stops_the_computer(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(
        manifest=manifest(root), options=MandalaSandboxClientOptions(on_shutdown="stop")
    )
    await session.aclose()
    assert fake.computers["vm-1"]["status"] == "stopped"
    payload = client.serialize_session_state(session.state)
    fake.fail[("PATCH", "vm-1")] = (500, {"error": "boom"})
    with pytest.raises(WorkspaceStartError):
        await client.resume(client.deserialize_session_state(payload))
    assert len(fake.calls("POST", "/vm-1/start")) == 1
    assert fake.computers["vm-1"]["status"] == "stopped"
    assert len(fake.calls("POST", "/vm-1/stop")) == 2
    fake.fail.clear()


async def test_a_resume_that_fails_on_a_running_computer_leaves_it_running(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    payload = client.serialize_session_state(session.state)
    fake.fail[("POST", "exec")] = (500, {"error": "boom"})
    with pytest.raises(WorkspaceStartError):
        await client.resume(
            client.deserialize_session_state(
                {**payload, "launch_timeout_s": 1}  # type: ignore[dict-item]
            )
        )
    # It was running before the resume touched it, so it is not stopped.
    assert fake.computers["vm-1"]["status"] == "running"
    assert not fake.calls("POST", "/vm-1/stop")
    fake.fail.clear()
    await session.aclose()


# --- review round 2 -------------------------------------------------------------


async def test_a_failing_snapshot_store_keeps_the_computer(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, tmp_path: Path
) -> None:
    # The store's directory sits under a regular file, so saving to it fails
    # after the archive itself was made without trouble.
    (tmp_path / "a-file").write_text("not a directory")
    session = await client.create(
        manifest=manifest(root),
        snapshot=LocalSnapshotSpec(base_path=tmp_path / "a-file" / "store"),
    )
    await session.start()
    await session.write(Path("work.txt"), io.BytesIO(b"only copy"))
    with pytest.raises(Exception):  # noqa: B017 - whatever the store raises
        await session.stop()
    await session.shutdown()
    await client.delete(session)
    assert fake.computers["vm-1"]["status"] == "stopped"
    assert not fake.calls("DELETE", "/vm-1")


async def test_a_confirmed_snapshot_lets_shutdown_delete(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, tmp_path: Path
) -> None:
    session = await client.create(
        manifest=manifest(root), snapshot=LocalSnapshotSpec(base_path=tmp_path)
    )
    await session.start()
    await session.stop()
    await session.shutdown()
    assert fake.computers == {}


async def test_a_snapshot_store_never_reached_keeps_the_computer(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path, tmp_path: Path
) -> None:
    session = await client.create(
        manifest=manifest(root), snapshot=LocalSnapshotSpec(base_path=tmp_path)
    )
    await session.start()
    await session.shutdown()  # no persist ran
    assert fake.computers["vm-1"]["status"] == "stopped"


async def test_an_absolute_symlink_with_dot_dot_is_refused(
    client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(manifest=manifest(root))
    await session.start()
    await session.exec(
        "mkdir -p sub/target && printf d > data && ln -s sub/target alias"
        f" && ln -s {root}/alias/../data tricky"
    )
    with pytest.raises(WorkspaceArchiveReadError) as caught:
        await session.persist_workspace()
    assert caught.value.context["reason"] == "unrestorable_member"
    assert "tricky" in str(caught.value.context["member"])
    await session.exec("rm tricky")
    assert (await session.persist_workspace()).read()
    await session.aclose()


async def test_a_reused_public_url_is_given_the_access_asked_for(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(
        manifest=manifest(root),
        options=MandalaSandboxClientOptions(exposed_ports=(8080,), public_url_access="private"),
    )
    fake.public_urls["vm-1"] = [
        {
            "id": "pu-existing",
            "url": "https://mine.mandala.dev",
            "hostname": "mine.mandala.dev",
            "label": "mine",
            "port": 8080,
            "access": "public",
            "disabled": False,
            "created_at": "2026-10-29T00:00:00.000Z",
            "updated_at": "2026-10-29T00:00:00.000Z",
        }
    ]
    endpoint = await session.resolve_exposed_port(8080)
    assert endpoint.host == "mine.mandala.dev"
    assert fake.public_urls["vm-1"][0]["access"] == "private"
    assert json.loads(fake.calls("PATCH", "/public-urls/pu-existing")[0].content) == {
        "access": "private"
    }
    assert not fake.calls("POST", "/public-urls")
    await session.aclose()


async def test_a_reused_public_url_that_will_not_change_access_is_refused(
    fake: FakePlatform, client: MandalaSandboxClient, root: Path
) -> None:
    session = await client.create(
        manifest=manifest(root),
        options=MandalaSandboxClientOptions(exposed_ports=(8080,), public_url_access="private"),
    )
    fake.public_urls["vm-1"] = [
        {
            "id": "pu-existing",
            "url": "https://mine.mandala.dev",
            "hostname": "mine.mandala.dev",
            "label": "mine",
            "port": 8080,
            "access": "public",
            "disabled": False,
            "created_at": "2026-10-29T00:00:00.000Z",
            "updated_at": "2026-10-29T00:00:00.000Z",
        }
    ]
    fake.fail[("PATCH", "pu-existing")] = (403, {"error": "no", "code": "PUBLIC_URLS_BLOCKED"})
    with pytest.raises(ExposedPortUnavailableError) as caught:
        await session.resolve_exposed_port(8080)
    assert caught.value.context["code"] == "PUBLIC_URLS_BLOCKED"
    fake.fail.clear()
    await session.aclose()
