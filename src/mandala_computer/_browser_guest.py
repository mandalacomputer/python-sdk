"""Version 1 guest file helper, mirrored byte-for-byte in the TypeScript SDK.

Executed with the Computer API's system identity, never with model shell text.
The only writable browser directory is a bounded noexec tmpfs. Its underlying
mountpoint is mode 000, so expiry/unmount cannot spill downloads onto /run.
"""

from __future__ import annotations

import base64
import ctypes
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import stat
import sys
import time
from typing import Any, cast

ROOT = "/run/mandala-browser-files"
NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC
DIRECTORY = NOFOLLOW | os.O_DIRECTORY
MAX_FILE = 1024 * 1024
MAX_TOTAL = 4 * MAX_FILE
GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def path_parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/") or len(path) > 4096:
        raise ValueError()
    parts = path.split("/")[1:]
    if (
        not parts
        or any(p in ("", ".", "..") for p in parts)
        or "\\" in path
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    ):
        raise ValueError()
    return parts


def open_directory(path: str) -> int:
    fd = os.open("/", DIRECTORY)
    try:
        for component in path_parts(path):
            child = os.open(component, DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(fd: int, maximum: int) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        raise ValueError()
    data = bytearray()
    while len(data) <= maximum:
        part = os.read(fd, min(65536, maximum + 1 - len(data)))
        if not part:
            break
        data.extend(part)
    after = os.fstat(fd)

    def identity(st: os.stat_result) -> tuple[int, ...]:
        return (
            st.st_dev,
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
            st.st_nlink,
        )

    if len(data) > maximum or len(data) != before.st_size or identity(before) != identity(after):
        raise ValueError()
    return bytes(data)


def read_upload(path: str, roots: list[str], maximum: int) -> bytes:
    path_parts(path)
    if not isinstance(roots, list) or not 1 <= len(roots) <= 16:
        raise ValueError()
    for allowed_root in roots:
        path_parts(allowed_root)
    root = next((r for r in roots if path.startswith(r + "/")), None)
    if root is None:
        raise ValueError()
    parent, name = path.rsplit("/", 1)
    directory = open_directory(parent)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW, dir_fd=directory)
        try:
            return read_regular(fd, maximum)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def payload(data: bytes) -> dict[str, Any]:
    return {
        "data": base64.b64encode(data).decode("ascii"),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def libc_call(name: str, *args: Any) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if getattr(libc, name)(*args) != 0:
        raise OSError(ctypes.get_errno())


def ensure_root() -> int:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError()
    run = open_directory("/run")
    try:
        st = os.fstat(run)
        if st.st_uid != 0 or st.st_mode & 0o022:
            raise ValueError()
        created = False
        try:
            os.mkdir("mandala-browser-files", 0o711, dir_fd=run)
            created = True
        except FileExistsError:
            pass
        fd = os.open("mandala-browser-files", DIRECTORY, dir_fd=run)
        if created:
            os.fchmod(fd, 0o711)
        st = os.fstat(fd)
        if st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o711:
            os.close(fd)
            raise ValueError()
        return fd
    finally:
        os.close(run)


def read_state(path: str) -> dict[str, Any]:
    fd = os.open(path + "/.state", os.O_RDONLY | NOFOLLOW)
    try:
        return cast("dict[str, Any]", json.loads(read_regular(fd, 4096)))
    finally:
        os.close(fd)


def write_state(path: str, state: dict[str, Any]) -> None:
    fd = os.open(path + "/.state.new", os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
    try:
        data = json.dumps(state, separators=(",", ":")).encode()
        if len(data) > 4096:
            raise ValueError()
        with os.fdopen(fd, "wb", closefd=False) as output:
            output.write(data)
    finally:
        os.close(fd)
    os.replace(path + "/.state.new", path + "/.state")


def chmod_directory(path: str, mode: int) -> None:
    fd = os.open(path, DIRECTORY)
    try:
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def destroy(path: str) -> None:
    # First deny traversal on the mounted root. The underlying inode has been
    # 000 since before mount. Lazy unmount keeps existing open fds quota-bound;
    # future browser opens can never recreate a writable unbounded directory.
    chmod_directory(path, 0)
    try:
        for name in ("incoming", "sealed", "approved"):
            shutil.rmtree(path + "/" + name)
    finally:
        libc_call("umount2", path.encode(), 2)
        os.rmdir(path)


def guard(path: str, ready: int) -> None:
    # A detached root guardian survives SDK failure. Heartbeats extend only the
    # five-minute idle bound, never the two-hour absolute retention deadline.
    # Do not inherit the create registry lock (or any API execution pipe).
    for entry in os.listdir("/proc/self/fd"):
        fd = int(entry)
        if fd > 2 and fd != ready:
            try:
                os.close(fd)
            except OSError:
                pass
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    if null > 2:
        os.close(null)
    os.write(ready, b"1")
    os.close(ready)
    try:
        while True:
            time.sleep(5)
            try:
                fd = os.open(path + "/.lock", os.O_RDWR | NOFOLLOW)
            except FileNotFoundError:
                return
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                state = read_state(path)
                if state["closed"] or time.monotonic() >= min(
                    state["heartbeat"] + 300, state["deadline"]
                ):
                    destroy(path)
                    return
            finally:
                os.close(fd)
    finally:
        os._exit(0)


def create(path: str, request: dict[str, Any]) -> dict[str, Any]:
    uid, gid = request["uid"], request["gid"]
    if (
        type(uid) is not int
        or type(gid) is not int
        or not 1 <= uid <= 2147483647
        or not 1 <= gid <= 2147483647
    ):
        raise ValueError()
    total = request["total"]
    if type(total) is not int or not 1 <= total <= MAX_TOTAL:
        raise ValueError()
    # The global lock is metadata, not one of the 32 quarantine scopes.
    if sum(name != ".registry" for name in os.listdir(ROOT)) >= 32:
        raise ValueError()
    os.mkdir(path, 0)
    mounted = False
    try:
        options = f"size={2 * total + 65536},nr_inodes=128,mode=0711,uid=0,gid=0".encode()
        libc_call("mount", b"tmpfs", path.encode(), b"tmpfs", 2 | 4 | 8, options)
        mounted = True
        for name, mode in (("incoming", 0o700), ("sealed", 0o700), ("approved", 0o755)):
            os.mkdir(path + "/" + name, mode)
            chmod_directory(path + "/" + name, mode)
        os.chown(path + "/incoming", uid, gid)
        fd = os.open(path + "/.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
        os.close(fd)
        now = time.monotonic()
        write_state(
            path,
            {
                "context": request["context"],
                "task": request["task"],
                "heartbeat": now,
                "deadline": now + 7200,
                "closed": False,
                "published": 0,
                "count": 0,
                "total": total,
            },
        )
        ready, started = os.pipe()
        child = os.fork()
        if child == 0:
            os.close(ready)
            guard(path, started)
        os.close(started)
        try:
            if not select.select([ready], [], [], 5)[0] or os.read(ready, 1) != b"1":
                raise ValueError()
        finally:
            os.close(ready)
        return {"path": path + "/incoming"}
    except BaseException:
        if mounted:
            try:
                chmod_directory(path, 0)
                libc_call("umount2", path.encode(), 2)
            except OSError:
                pass
        try:
            os.rmdir(path)
        except OSError:
            pass
        raise


def quarantine(request: dict[str, Any]) -> dict[str, Any]:
    scope = request["scope"]
    if not isinstance(scope, str) or re.fullmatch(r"[0-9a-f]{32}", scope) is None:
        raise ValueError()
    for key in ("context", "task"):
        if not isinstance(request[key], str) or not 1 <= len(request[key]) <= 128:
            raise ValueError()
    rootfd = ensure_root()
    path = ROOT + "/" + scope
    op = request["op"]
    if op == "create":
        registry = os.open(".registry", os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600, dir_fd=rootfd)
        os.close(rootfd)
        try:
            fcntl.flock(registry, fcntl.LOCK_EX)
            return create(path, request)
        finally:
            os.close(registry)
    os.close(rootfd)
    try:
        lock = os.open(path + "/.lock", os.O_RDWR | NOFOLLOW)
    except FileNotFoundError:
        if op == "close":
            return {"removed": False}
        raise
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path)
        if state["context"] != request["context"] or state["task"] != request["task"]:
            raise ValueError()
        if op == "close":
            # Destruction must work even when the browser exhausted the tmpfs.
            # The held lock and removed mount are the terminal state; no allocation.
            destroy(path)
            return {"removed": True}
        if state["closed"] or time.monotonic() >= min(state["deadline"], state["heartbeat"] + 300):
            raise ValueError()
        if op == "heartbeat":
            state["heartbeat"] = time.monotonic()
            write_state(path, state)
            return {}
        guid = request["guid"]
        if not isinstance(guid, str) or not GUID.fullmatch(guid):
            raise ValueError()
        if op == "discard":
            for suffix in (
                "incoming/" + guid,
                "incoming/" + guid + ".crdownload",
                "sealed/" + guid,
            ):
                try:
                    os.unlink(path + "/" + suffix)
                except FileNotFoundError:
                    pass
            return {}
        if op == "seal":
            fd = os.open(path + "/incoming/" + guid, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW)
            try:
                data = read_regular(fd, request["maximum"])
            finally:
                os.close(fd)
            # Never publish a renamed writable inode: an old browser fd could
            # keep changing it after chmod/chown. This new inode is root-only.
            fd = os.open(
                path + "/sealed/" + guid, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600
            )
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            os.unlink(path + "/incoming/" + guid)
            return payload(data)
        if op == "publish":
            name = request["name"]
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,119}", name) is None
            ):
                raise ValueError()
            fd = os.open(path + "/sealed/" + guid, os.O_RDONLY | NOFOLLOW)
            try:
                data = read_regular(fd, request["maximum"])
                if hashlib.sha256(data).hexdigest() != request["sha256"]:
                    raise ValueError()
                if state["published"] + len(data) > state["total"] or state["count"] >= 8:
                    raise ValueError()
                os.fchmod(fd, 0o444)
            finally:
                os.close(fd)
            target = path + "/approved/" + guid + "-" + name
            os.link(path + "/sealed/" + guid, target, follow_symlinks=False)
            os.unlink(path + "/sealed/" + guid)
            state["published"] += len(data)
            state["count"] += 1
            write_state(path, state)
            return {"path": target}
        raise ValueError()
    finally:
        os.close(lock)


def main(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("version") != 1:
        raise ValueError()
    maximum = request.get("maximum", MAX_FILE)
    if type(maximum) is not int or not 1 <= maximum <= MAX_FILE:
        raise ValueError()
    if request["op"] == "read":
        return payload(read_upload(request["path"], request["roots"], maximum))
    return quarantine(request)


if __name__ == "__main__":
    try:
        answer = main(json.loads(base64.b64decode(sys.argv[1], validate=True)))
        print(json.dumps(answer, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - helper errors must not disclose file contents or paths
        # Caller-supplied paths, file bytes and helper authority never become
        # exception text, even in the SDK host's logs.
        print('{"error":"refused"}')
        sys.exit(1)
