"""Linux-root integration probe. Run only inside a private mount namespace.

unshare --mount --propagation private sh -c \
  'mount -t tmpfs -o mode=0755,size=32m,nr_inodes=1024 tmpfs /run; python3 tests/browser_guest_probe.py'
"""

import base64
import os
import runpy
import subprocess
import time
import uuid
from pathlib import Path

helper = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "src/mandala_computer/_browser_guest.py")
)
main = helper["main"]
root = Path("/run/mandala-browser-files")
scopes = []


def request(scope, op, **kw):
    return main(
        dict(version=1, scope=scope, context="context", task="task", maximum=1024, op=op, **kw)
    )


def create():
    scope = uuid.uuid4().hex
    scopes.append(scope)
    request(scope, "create", uid=65534, gid=65534, total=2048)
    return scope, root / scope


def denied_read(path):
    result = subprocess.run(
        ["python3", "-c", 'import sys;open(sys.argv[1],"rb").read()', str(path)],
        preexec_fn=lambda: os.setuid(65534),
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


try:
    one, path = create()
    two, other = create()  # The detached guardian must not retain the global create lock.
    guid = str(uuid.uuid4())
    incoming = path / "incoming" / guid
    original = open(incoming, "w+b")  # noqa: SIM115 - retain writable fd across sealing
    original.write(b"approved")
    original.flush()
    sealed = request(one, "seal", guid=guid)
    assert base64.b64decode(sealed["data"]) == b"approved"
    original.seek(0)
    original.write(b"attacked")
    original.flush()
    denied_read(path / "sealed" / guid)
    assert not list((path / "approved").iterdir())
    try:
        request(one, "publish", guid=guid, name="../../escape.txt", sha256=sealed["sha256"])
        raise AssertionError("traversal accepted")
    except ValueError:
        pass
    try:
        main(
            {
                "version": 1,
                "scope": one,
                "context": "wrong-context",
                "task": "task",
                "maximum": 1024,
                "op": "publish",
                "guid": guid,
                "name": "safe.txt",
                "sha256": sealed["sha256"],
            }
        )
        raise AssertionError("foreign context accepted")
    except ValueError:
        pass
    result = request(one, "publish", guid=guid, name="safe.txt", sha256=sealed["sha256"])
    published = Path(result["path"])
    assert published.read_bytes() == b"approved"
    assert published.stat().st_uid == 0 and published.stat().st_mode & 0o777 == 0o444
    denied_read(path / "sealed" / guid)
    # The original writable descriptor cannot alter the protected approved inode.
    original.seek(0)
    original.write(b"replaced")
    original.flush()
    assert published.read_bytes() == b"approved"
    original.close()
    oversized = path / "incoming" / str(uuid.uuid4())
    oversized.write_bytes(b"x" * 1025)
    try:
        request(one, "seal", guid=oversized.name)
        raise AssertionError("oversized accepted")
    except ValueError:
        pass
    oversized.unlink()
    # A browser cannot grow the quarantine without bound even before progress events.
    try:
        with open(path / "incoming" / "full", "wb") as output:
            output.writelines(b"x" * 4096 for _ in range(1024))
        raise AssertionError("quota not enforced")
    except OSError as error:
        assert error.errno == 28
    (path / "incoming" / "full").unlink()
    # There is also a hard inode cap, independent of file byte count.
    try:
        for i in range(512):
            (path / "incoming" / str(i)).touch()
        raise AssertionError("inode quota not enforced")
    except OSError as error:
        assert error.errno == 28
    request(one, "close")
    assert not path.exists() and not published.exists()
    request(one, "close")
    # The mounted root disappears under guardian expiry without help from the SDK.
    state = helper["read_state"](str(other))
    state["heartbeat"] = time.monotonic() - 301
    helper["write_state"](str(other), state)
    limit = time.monotonic() + 10
    while other.exists() and time.monotonic() < limit:
        time.sleep(0.1)
    assert not other.exists(), "guardian did not expire quarantine"
    # The registry lock must not consume one of the 32 quarantine slots.
    for _ in range(32):
        create()
    assert len([p for p in root.iterdir() if p.name != ".registry"]) == 32
    try:
        create()
        raise AssertionError("33rd quarantine scope accepted")
    except ValueError:
        pass
    print(
        "PASS: concurrent guardians, protected snapshot, wrong context/traversal, byte/inode quotas, close, expiry and 32-scope boundary"
    )
finally:
    for scope in scopes:
        request(scope, "close")
