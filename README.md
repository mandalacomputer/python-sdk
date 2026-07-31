# gorillacloud-python

Python SDK for [GorillaCloud](https://gorillacloud.ai) — cloud desktops for AI agents.

> **Status: alpha, unpublished.** The API surface is settling; expect breaking
> changes before 1.0.

## Install

```sh
pip install gorillacloud       # not yet published
```

## Use

Authentication is an API key from the dashboard (Settings → API keys), read from
`GORILLACLOUD_API_KEY` unless you pass one.

```python
from gorillacloud import Client

client = Client()

with client.computers.ephemeral(template="base") as c:
    c.wait_for_guest()                      # desktop is up and answering
    c.exec("xdg-open https://example.com")
    c.click(640, 400)
    c.type("hello")
    png = c.screenshot()
# computer is destroyed here, even if the block raised
```

For a computer that outlives the block, use `create()` — it never deletes:

```python
c = client.computers.create(name="dev", cpu=4, ram_mb=8192)
c.wait_until_running()
...
c.stop()
```

`ephemeral()` and `create()` are separate on purpose. Deleting a computer
destroys its disk, so tying that to a `with` block is only safe when the block is
unambiguously the machine's whole lifetime — which `ephemeral()` declares and
`create()` does not.

### Driving the desktop

Coordinates are in the guest's fixed 1280×800 space (`gorillacloud.SCREEN_WIDTH`
/ `SCREEN_HEIGHT`).

```python
c.move(x, y); c.click(x, y); c.right_click(x, y); c.double_click(x, y)
c.scroll(x, y, direction="up", amount=3)
c.type("some text")
c.key("ctrl", "c")

png  = c.screenshot()             # full-resolution PNG
jpg  = c.screenshot(width=320)    # downscaled JPEG — cheap enough to poll

res = c.exec("ls /tmp")           # native shell: bash on Linux, cmd.exe on Windows
res.ok, res.exit_code, res.stdout, res.stderr
```

A non-zero exit is returned, not raised — check `res.ok`.

### Readiness

`create()` returns as soon as the API does; the machine is starting, not ready.

- `wait_until_running()` — the VM is up. The guest OS is still booting.
- `wait_for_guest()` — something inside the guest answers. Linux only; it uses
  the guest agent, which Windows images do not ship yet.

### Snapshots

```python
snap = c.snapshot()                     # disk, works while running
snap = c.snapshot(memory=True)          # + live RAM, resumes without booting
client.snapshots.restore(snap.id)
twin = client.snapshots.clone(snap.id)  # a fork, for memory snapshots
c.set_schedule(enabled=True, hour=4, tz="America/Chicago")
```

### Errors

Everything derives from `GorillaCloudError`.

| Exception | When |
|---|---|
| `AuthenticationError` | 401 — key missing, malformed, or revoked |
| `PlanLimitError` | 402 — plan caps: count, size, RAM/disk pools, OS |
| `PermissionDeniedError` | 403 — suspended or unverified account |
| `NotFoundError` | 404 — no such resource (also another tenant's) |
| `APIError` | any other unsuccessful response |
| `TimeoutError` | a `wait_*` helper gave up |

`PlanLimitError`'s message names the limit that was hit.

## Design notes

**This SDK binds only to the curated `/api/v1` surface, never to the hypervisor
daemon's own routes.** That boundary is deliberate and load-bearing:

- The daemon's VM representation carries fields that are nobody's business
  outside the platform — owner ids, VNC tokens and ports, MAC addresses, host
  image paths. The public surface exposes a deliberately narrower object.
- The daemon's auth model treats an absent owner header as *administrative*
  scope. Nothing client-facing should be one bug away from that.
- Multi-host is on the platform roadmap. The daemon's routes will change shape
  when the control plane splits from the hypervisor agents; the point of the v1
  surface is that this SDK does not have to care.

Practically: if something the SDK needs isn't in `/api/v1`, the fix is to add it
there, not to reach past it. `tests/test_surface.py` enforces this — it exercises
every method that makes a request and asserts each one lands on an allowlisted
route, so drift fails here rather than in a user's hands.

Response objects keep the raw payload in `.raw`, so a server that starts
returning more fields does not break older clients.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

Not yet implemented: an async client. The transport is separated from the
resource classes so it can be added without restating auth, URL, and error rules.

## License

MIT — see [LICENSE](LICENSE).
