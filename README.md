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

### Async

`AsyncClient` mirrors `Client` method for method — same names, same arguments,
same errors. Everything that performs IO is a coroutine.

```python
import asyncio
from gorillacloud import AsyncClient

async def main():
    async with AsyncClient() as client:
        async with client.computers.ephemeral(template="base") as c:
            await c.wait_for_guest()
            png = await c.screenshot()
            await c.type("hello")

asyncio.run(main())
```

Independent calls can overlap:

```python
templates, computers, snapshots = await asyncio.gather(
    client.templates.list(), client.computers.list(), client.snapshots.list()
)
```

One caveat worth knowing: the platform serialises QMP access **per computer**, so
concurrent screenshots or input against the *same* machine queue server-side.
Concurrency pays off across different computers, and for overlapping the waiting
rather than the work.

### Renaming

```python
c.rename("build box")
c.name                  # "build box" — the handle is updated in place
```

The name is a label; nothing is derived from it, and it need not be unique. The
id is what identifies a computer, so renaming moves nothing and invalidates no
handle, id or snapshot anyone is holding.

The server trims surrounding whitespace and control characters and caps the
result at 64 characters, so read `c.name` back rather than assuming it kept what
you sent. An empty name raises `ValueError` before the request goes out.

Snapshots already taken keep the name they were captured under. While the
computer exists they are listed under its current name; once it is deleted they
fall back to what it was called at the time, which is then the only thing left
identifying where those bytes came from.

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

- `wait_until_built()` — a cloned computer's disk has been copied. Only clones
  need this; it returns at once for anything else.
- `wait_until_running()` — the VM is up. The guest OS is still booting.
- `wait_for_guest()` — something inside the guest answers. Linux only; it uses
  the guest agent, which Windows images do not ship yet.

### Computers that are still being built

`create()` is instant, because a new computer's disk is an overlay on the golden
image — nothing is copied. A **clone** is not: cloning a computer copies its
whole disk, and cloning a snapshot copies it out of backup storage, collapsing a
whole incremental chain on the way. That runs for minutes.

So both clone calls return before the disk exists, with the computer in
`building`. It is listed and has an id you can navigate to, but there is nothing
to boot yet — starting, stopping, snapshotting or cloning it raises
`ConflictError` until the copy lands.

```python
c = client.snapshots.clone(snap.id)
c.is_building          # True
c.wait_until_built()   # minutes, for a large disk
c.start().wait_for_guest()
```

If the copy fails the computer stays, so you can see it and reclaim the space it
took. It never becomes usable — delete it and clone again.

```python
if c.build_failed:
    print(c.build_error)   # e.g. "no space left on device"
    c.delete()
```

`wait_until_built()` raises rather than waiting out the timeout if the build
failed, and its `TimeoutError` means only that the wait stopped — the copy is
still going.

### Snapshots

```python
snap = c.snapshot()                     # disk, works while running
snap = c.snapshot(memory=True)          # + live RAM, resumes without booting
client.snapshots.restore(snap.id)
twin = client.snapshots.clone(snap.id)  # a fork, for memory snapshots
twin.wait_until_built()                 # the disk is copied out of backup first
c.set_schedule(enabled=True, hour=4, tz="America/Chicago")
c.set_schedule(enabled=False, hour=4, tz="America/Chicago")  # off, keeps the time
c.clear_schedule()                                           # removed entirely
```

Disabling and clearing differ. `set_schedule(enabled=False)` is deliberately
non-destructive — it keeps the chosen time so toggling back on restores it.
`clear_schedule()` returns the computer to never having had a schedule.

The schedule describes the *window* and nothing else — there is no `last_run`.
For "when did my backups last run", read the snapshots, which carry real capture
times; `auto` marks the ones the scheduler took:

```python
backups = [s for s in c.snapshots() if s.auto]      # or s.is_scheduled
last = max((s.created_at for s in backups), default=None)
if last is None:
    print("no automatic backup has ever run")
```

`auto` also marks the only snapshots retention will age out — ones you take
yourself are never removed automatically.

### Errors

Everything derives from `GorillaCloudError`.

| Exception | When |
|---|---|
| `AuthenticationError` | 401 — key missing, malformed, or revoked |
| `PlanLimitError` | 402 — plan caps: count, size, RAM/disk pools, OS |
| `PermissionDeniedError` | 403 — suspended or unverified account |
| `NotFoundError` | 404 — no such resource (also another tenant's) |
| `ConflictError` | 409 — right request, wrong moment; retry |
| `APIError` | any other unsuccessful response |
| `TimeoutError` | a `wait_*` helper gave up |

`PlanLimitError`'s message names the limit that was hit.

`ConflictError` is the one worth catching separately, because it is the only one
that clears itself: something is in flight that the operation cannot run
alongside — a disk still being copied, a snapshot being taken, a delete already
under way. Waiting and retrying is the fix; changing the request is not.

```python
try:
    c.snapshot()
except gorillacloud.ConflictError:
    c.wait_until_built()   # or just try again shortly
    c.snapshot()
```

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
every method that makes a request, for **both** clients, and asserts each call
lands on an allowlisted route, so drift fails here rather than in a user's hands.

Response objects keep the raw payload in `.raw`, so a server that starts
returning more fields does not break older clients.

### Keeping sync and async honest

Two implementations of one API drift. The defence is structural rather than
diligent:

- Paths, request bodies, and argument validation live in `_api.py`; both clients
  call the same functions, so neither can invent its own URL or payload.
- Field accessors live in one `ComputerFields` base shared by both handles.
- Auth, URL building, and status→exception mapping live on one transport base.
- `tests/test_parity.py` asserts the two expose the same method names with the
  same signatures, that every async IO method is a coroutine, and that the field
  accessors are literally the same objects.
- `tests/test_surface.py` asserts both clients hit *exactly the same routes* —
  not merely that each stays inside the allowlist, which a client that silently
  skipped a call would also satisfy.

What is left duplicated is the awaits, which is the irreducible part.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

## License

MIT — see [LICENSE](LICENSE).
