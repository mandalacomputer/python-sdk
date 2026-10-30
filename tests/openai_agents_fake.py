"""A stand-in Mandala API for the OpenAI Agents sandbox tests.

It answers the routes the sandbox client uses, through ``httpx.MockTransport``,
so the real SDK builds and parses every request. The "guest" is this machine:
commands run in a local shell and files land on the local disk, so tar,
the Agents SDK's helper scripts and its fingerprinting really run. Tests point
the manifest root at a temporary directory, and ``wipe`` names the paths a
deleted computer takes with it, so a replacement starts from an empty disk.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
_PREFIX = "/api/v1/"
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")
#: The most output one poll returns.
_WINDOW = 4096


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


@dataclass
class _Job:
    pid: int
    command: str
    proc: subprocess.Popen[bytes]
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    sent_out: int = 0
    sent_err: int = 0
    killed: bool = False
    readers: list[threading.Thread] = field(default_factory=list)

    def status(self) -> dict[str, Any]:
        finished = self.proc.poll() is not None and not any(t.is_alive() for t in self.readers)
        # Like the platform, one read carries a bounded window and says whether
        # more is waiting.
        out = bytes(self.stdout[self.sent_out : self.sent_out + _WINDOW])
        err = bytes(self.stderr[self.sent_err : self.sent_err + _WINDOW])
        self.sent_out += len(out)
        self.sent_err += len(err)
        more = self.sent_out < len(self.stdout) or self.sent_err < len(self.stderr)
        return {
            "pid": self.pid,
            "command": self.command,
            "running": not finished,
            "exited": finished,
            "exit_code": self.proc.returncode if finished and not self.killed else None,
            "stdout_b64": _b64(out),
            "stderr_b64": _b64(err),
            "stdout_offset": self.sent_out,
            "stderr_offset": self.sent_err,
            "more": more,
            "killed": self.killed,
        }


class FakePlatform:
    """The API, one computer table and a local "guest"."""

    def __init__(self, *, wipe: list[Path] | None = None) -> None:
        self.computers: dict[str, dict[str, Any]] = {}
        self.jobs: dict[int, _Job] = {}
        self.requests: list[httpx.Request] = []
        self.public_urls: dict[str, list[dict[str, Any]]] = {}
        self.uploads: dict[str, bytearray] = {}
        self.wipe = list(wipe or [])
        self.public_urls_enabled = True
        #: Status code to answer a route with instead of serving it, by
        #: ``(method, last path segment)``.
        self.fail: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        #: The same, answered once and then served normally.
        self.fail_once: dict[tuple[str, str], tuple[int, dict[str, Any]]] = {}
        self.create_status = "running"
        self._ids = itertools.count(1)
        self._pids = itertools.count(1000)

    # --- plumbing -----------------------------------------------------------

    def client(self) -> mc.AsyncClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return mc.AsyncClient("gck_secret_test_key", base_url=BASE, http_client=http)

    def calls(self, method: str | None = None, suffix: str = "") -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if (method is None or r.method == method) and r.url.path.endswith(suffix)
        ]

    def close(self) -> None:
        for job in self.jobs.values():
            if job.proc.poll() is None:
                _kill_group(job.proc)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path[len(_PREFIX) :].split("/")
        key = (request.method, parts[-1])
        failure = self.fail_once.pop(key, None) or self.fail.get(key)
        if failure is not None:
            return httpx.Response(failure[0], json=failure[1])
        if parts == ["computers"] and request.method == "POST":
            return self._create(json.loads(request.content))
        if parts[0] != "computers" or len(parts) < 2:
            return httpx.Response(404, json={"error": "no such route"})
        record = self.computers.get(parts[1])
        if record is None:
            return httpx.Response(404, json={"error": "computer not found"})
        rest = parts[2:]
        if not rest:
            return self._computer(request, record)
        if rest in (["start"], ["stop"], ["suspend"]) and request.method == "POST":
            record["status"] = {"start": "running", "stop": "stopped", "suspend": "suspended"}[
                rest[0]
            ]
            return httpx.Response(200, json={"ok": True})
        if rest[0] == "exec":
            return self._exec(request, rest[1:])
        if rest[0] == "files":
            return self._files(request, rest[1:])
        if rest[0] == "public-urls":
            return self._public_urls(request, record["id"], rest[1:])
        return httpx.Response(404, json={"error": "no such route"})

    # --- computers ------------------------------------------------------------

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        computer_id = f"vm-{next(self._ids)}"
        record: dict[str, Any] = {
            "id": computer_id,
            "name": body.get("name") or computer_id,
            "status": self.create_status,
            "os": "linux",
            "template": body.get("template") or "base",
            "running_ram_mb": 1024,
            "created_at": "2026-10-29T00:00:00Z",
            "sent": body,
        }
        if body.get("ends_in_hours") is not None:
            record["ends_at"] = _hours_ahead(body["ends_in_hours"])
        self.computers[computer_id] = record
        return httpx.Response(201, json=record)

    def _computer(self, request: httpx.Request, record: dict[str, Any]) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=record)
        if request.method == "DELETE":
            del self.computers[record["id"]]
            for path in self.wipe:
                shutil.rmtree(path, ignore_errors=True)
            return httpx.Response(200, json={"ok": True, "snapshots_deleted": 0})
        if request.method == "PATCH":
            body = json.loads(request.content)
            if body.get("ends_in_hours") is not None:
                record["ends_at"] = _hours_ahead(body["ends_in_hours"])
            return httpx.Response(200, json=record)
        return httpx.Response(405, json={"error": "method"})

    # --- commands -------------------------------------------------------------

    def _exec(self, request: httpx.Request, rest: list[str]) -> httpx.Response:
        if not rest and request.method == "POST":
            body = json.loads(request.content)
            if not body.get("background"):
                # The readiness probe: a foreground `exit 0`.
                return httpx.Response(
                    200, json={"exit_code": 0, "stdout_b64": "", "stderr_b64": ""}
                )
            return self._start(body)
        job = self.jobs.get(int(rest[0])) if rest else None
        if job is None:
            return httpx.Response(404, json={"error": "no such process"})
        if request.method == "DELETE":
            if job.proc.poll() is None:
                job.killed = True
                _kill_group(job.proc)
                job.proc.wait()
            for reader in job.readers:
                reader.join()
            return httpx.Response(200, json=job.status())
        return httpx.Response(200, json=job.status())

    def _start(self, body: dict[str, Any]) -> httpx.Response:
        env = {**os.environ, **(body.get("env") or {})}
        shell = shutil.which("bash") or "/bin/sh"
        proc = subprocess.Popen(
            [shell, "-c", body["command"]],
            cwd=body.get("cwd") or "/",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        job = _Job(pid=next(self._pids), command=body["command"], proc=proc)
        for stream, sink in ((proc.stdout, job.stdout), (proc.stderr, job.stderr)):
            reader = threading.Thread(target=_drain, args=(stream, sink), daemon=True)
            reader.start()
            job.readers.append(reader)
        self.jobs[job.pid] = job
        return httpx.Response(200, json={"pid": job.pid, "command": job.command, "running": True})

    # --- files ----------------------------------------------------------------

    def _files(self, request: httpx.Request, rest: list[str]) -> httpx.Response:
        params = request.url.params
        path = Path(params["path"])
        if rest == ["uploads"]:
            return self._upload(request, path)
        if request.method == "PUT":
            path.write_bytes(request.content)
            return httpx.Response(200, json={"bytes": len(request.content)})
        if not path.is_file():
            return httpx.Response(404, json={"error": "file not found"})
        data = path.read_bytes()
        wanted = request.headers.get("range")
        if wanted is None:
            return httpx.Response(
                200, content=data, headers={"content-type": "application/octet-stream"}
            )
        if not data:
            return httpx.Response(
                416, json={"error": "empty"}, headers={"content-range": "bytes */0"}
            )
        m = _RANGE.fullmatch(wanted)
        assert m is not None, wanted
        first = int(m.group(1) or 0)
        last = min(int(m.group(2)) if m.group(2) else len(data) - 1, len(data) - 1)
        if first >= len(data):
            return httpx.Response(
                416, json={"error": "range"}, headers={"content-range": f"bytes */{len(data)}"}
            )
        return httpx.Response(
            206,
            content=data[first : last + 1],
            headers={
                "content-type": "application/octet-stream",
                "content-range": f"bytes {first}-{last}/{len(data)}",
            },
        )

    def _upload(self, request: httpx.Request, path: Path) -> httpx.Response:
        params = request.url.params
        key = f"{params['path']}:{params['upload_id']}"
        if request.method == "DELETE":
            self.uploads.pop(key, None)
            return httpx.Response(200, json={"ok": True})
        staged = self.uploads.setdefault(key, bytearray())
        size = int(params["size"])
        if request.method == "PUT":
            assert int(params["offset"]) == len(staged)
            staged += request.content
        complete = len(staged) == size
        if complete and request.method == "PUT":
            path.write_bytes(bytes(staged))
        return httpx.Response(
            200,
            json={
                "path": params["path"],
                "upload_id": params["upload_id"],
                "offset": len(staged),
                "size": size,
                "complete": complete,
            },
        )

    # --- public URLs ----------------------------------------------------------

    def _public_urls(
        self, request: httpx.Request, computer_id: str, rest: list[str]
    ) -> httpx.Response:
        urls = self.public_urls.setdefault(computer_id, [])
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "enabled": self.public_urls_enabled,
                    "reason": None if self.public_urls_enabled else "Public URLs are off here.",
                    "base_domain": "mandala.dev",
                    "limit": 5,
                    "public_urls": urls,
                },
            )
        if request.method == "POST":
            body = json.loads(request.content)
            label = f"agent-{body['port']}-{len(urls)}"
            url = {
                "id": f"pu-{computer_id}-{body['port']}",
                "url": f"https://{label}.mandala.dev",
                "hostname": f"{label}.mandala.dev",
                "label": label,
                "port": body["port"],
                "access": body.get("access") or "public",
                "disabled": False,
                "created_at": "2026-10-29T00:00:00.000Z",
                "updated_at": "2026-10-29T00:00:00.000Z",
            }
            urls.append(url)
            return httpx.Response(201, json=url)
        if request.method == "PATCH" and rest:
            body = json.loads(request.content)
            for url in urls:
                if url["id"] == rest[0]:
                    url.update({k: v for k, v in body.items() if k in ("port", "access")})
                    return httpx.Response(200, json=url)
            return httpx.Response(404, json={"error": "no such URL"})
        if request.method == "DELETE" and rest:
            self.public_urls[computer_id] = [u for u in urls if u["id"] != rest[0]]
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(405, json={"error": "method"})


def _drain(stream: Any, sink: bytearray) -> None:
    for chunk in iter(lambda: stream.read1(65536), b""):
        sink += chunk
    stream.close()


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _hours_ahead(hours: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(hours=hours)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")
