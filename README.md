# mandala-computer-python

Python SDK for [Mandala Computer](https://mandala.computer) — cloud desktops for
AI agents.

> **Status: alpha, unpublished.** The API surface is settling; expect breaking
> changes before 1.0.

## Install

```sh
pip install mandala-computer       # not yet published
```

The install also puts a `mandala` command on your PATH — see
[The `mandala` CLI](#the-mandala-cli).

## Use

Authentication is an API key from the dashboard (Settings → API keys), read from
`MANDALA_API_KEY` unless you pass one.

```python
from mandala_computer import Client

client = Client()

with client.computers.ephemeral(template="base") as c:
    c.wait_for_guest()                      # desktop is up and answering
    c.open("https://example.com")           # on the screen, not as root
    c.click(640, 400)
    c.type("hello")
    png = c.screenshot()
# computer is destroyed here, even if the block raised
```

`open()` puts a URL on the screen; `exec()` without `desktop=True` runs as `root`
with no display, where nothing with a window can start. See [Launching GUI
applications](#launching-gui-applications).

For a computer that outlives the block, use `create()` — it never deletes:

```python
c = client.computers.create(name="dev", cpu=4, ram_mb=8192)
c.wait_until_running()
...
c.stop()
```

### Sizes

Rather than inventing numbers, name a size. `client.sizes.list()` is the
catalogue — each entry is a template plus a CPU/RAM/disk shape, and these are
the shapes the platform keeps pre-booted, so a create that names one is
typically answered from the warm pool in about a second where a custom shape
boots cold:

```python
for s in client.sizes.list():
    print(s.id, s.cpu, s.ram_mb, s.disk_gb, s.allowed)

c = client.computers.create(name="dev", size="large")
```

`size` sets the template and the three numbers together, so it cannot be
combined with `template`, `cpu`, `ram_mb` or `disk_gb` — that raises
`ValueError` before any request is made. Explicit numbers remain fully
supported, and `allowed=False` rows name the `cheapest_plan` that would take
them.

`ephemeral()` and `create()` are separate on purpose. Deleting a computer
destroys its disk, so tying that to a `with` block is only safe when the block is
unambiguously the machine's whole lifetime — which `ephemeral()` declares and
`create()` does not.

A create that builds a computer which then will not boot is not an error. The
machine exists and is billable, so it comes back — stopped, with the reason on
it — rather than being thrown away with an exception:

```python
c = client.computers.create(template="base")
if c.start_error:
    print(c.start_error)   # e.g. "no host had room to start it"
    c.start()              # often works on a second attempt
```

### Suspending

A suspend writes the guest's RAM to disk and gives the host its memory back.
It is a pause, not a stop: `start()` afterwards resumes the same session — same
processes, same open windows — in about a second, instead of booting.

```python
c.suspend()
c.is_suspended     # True
c.suspended_at     # when the session was saved
c.start()          # resumes it; ~1s, not a boot
c.stop()           # discards the session instead
```

`restart()` is refused with `ConflictError` while a session is saved, since it
would have to guess which of those two you meant. Start it or stop it first.

**A computer can suspend without you asking.** Its host puts down anything
nobody has used for the host's idle window — 30 minutes by default. Input,
`exec()` and file transfers all count as use and resume it automatically;
**`screenshot()` deliberately does not**, so a loop that only polls the screen
can watch its own machine go down under it. Drive the desktop, or accept the
resume.

`wait_until_running()` raises rather than spinning if it finds a suspended
computer, because that state does not resolve on its own — `start()` is the fix.

### Showing somebody the desktop

Every response that *is* one computer carries the credentials and URLs to open
its live desktop, so putting a screen on your own page costs no extra call:

```python
c = client.computers.get(computer_id)
c.vnc.embed_url    # watch-only, drop straight into an <iframe>
c.vnc.url          # full control: keyboard, pointer, clipboard
c.vnc.view_url     # watch only — the platform drops input on this socket
```

Two credentials, because they are not the same permission. `view_token` cannot
type even from a patched client; `token` is root-equivalent on that machine.
Neither is your API key — which is every computer on the account, forever, and
must never reach a browser. Both end when the computer restarts.

`vnc` is `None` on a computer that came from `list()`. That is deliberate on the
platform's side: a desktop credential in every list response is a credential in
every log line that ever captured one. Call `refresh()` to get one.

### Async

`AsyncClient` mirrors `Client` method for method — same names, same arguments,
same errors. Everything that performs IO is a coroutine.

```python
import asyncio
from mandala_computer import AsyncClient

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

Coordinates are in the computer's own screen space. That is a create-time choice
now rather than a fixed size, so read `c.resolution` (or `c.screen` for the two
numbers) instead of assuming — `mandala_computer.SCREEN_WIDTH` / `SCREEN_HEIGHT`
are the 1280×800 default, which is only what a computer that asked for nothing
else renders at.

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

The guest agent stops capturing a command's output at 16 MiB while the command
keeps producing it, so what comes back can be the first 16 MiB with nothing else
to say there was more. `res.truncated` is that signal, and it is worth checking
before parsing anything that could be large:

```python
res = c.exec("cat /var/log/syslog")
if res.truncated:
    ...     # redirect to a file inside the guest and fetch it instead
```

`res.ok` deliberately ignores truncation: a command that succeeded and produced
a lot of output still succeeded, and whether a short answer is acceptable depends
on what you were going to do with it.

### Launching GUI applications

By default `exec()` runs in the system context — as `root` on Linux, with no
`DISPLAY` and no `HOME` — which is right for installing packages and wrong for
anything with a window. The obvious call therefore does nothing:

```python
c.exec("firefox https://example.com")     # no DISPLAY — dies, and exec() reports it
```

`desktop=True` runs the command in the logged-in desktop session instead, as the
desktop user with `DISPLAY`, `HOME` and `XAUTHORITY` set:

```python
c.exec("nohup firefox https://example.com >/dev/null 2>&1 &", desktop=True)
```

The `nohup … &` is still yours to write. A GUI program does not exit on its own,
so a foreground launch blocks until `timeout_s` kills it and comes back as a
failure — having opened the window anyway, which is a confusing pair of outcomes.
Detach it and the call returns in well under a second.

### `open()`

Opening a URL is common enough, and has enough ways to get it subtly wrong, that
it has its own method:

```python
c.open("https://example.com")
```

That is `exec(desktop=True)` with the session, the detaching and the browser
already decided. The result describes the launch, not the page — a zero exit
means the shell started the browser, not that the URL resolved. Screenshot it to
see what loaded.

**Why it names a browser.** `xdg-open` is the portable way to want this and is
installed on the `base` template, along with `exo-open`, `sensible-browser` and
`x-www-browser` — and none of them work. Every one exits 0 and launches nothing,
because the image's default-browser association points at a desktop entry it does
not ship. Exit 0 and an unchanged screen is the worst shape a failure can take,
so `open()` asks for Firefox, which is the only browser on the image anyway;
there is no Chromium. When the platform fixes the association, `open()` changes
in one place and your code does not change at all — which is most of the reason
to call it rather than write the `exec()` yourself.

The URL is shell-quoted, so one containing `&` or `;` stays a URL. One starting
with `-` is refused rather than escaped: quoting stops the *shell* reading it as
a flag, nothing stops the *browser* doing so, and no real URL starts with a dash.

There is no `xdotool` or `wmctrl` on the image, which you should not need —
`click()`, `type()` and `key()` are that, and they work without anything
installed in the guest.

**Windows does not support this yet**, and neither does `open()`, which is a
`desktop=True` exec underneath. `exec()` there runs as `NT AUTHORITY\SYSTEM`
in session 0 while the desktop is session 1, and session 0 isolation means a GUI
process started that way never reaches the screen. `desktop=True` does not paper
over it — the API rejects it with a clear message rather than running the command
somewhere nobody can see:

```
APIError: session "desktop" is not supported on Windows guests yet
```

Until that lands, drive the Windows desktop through `click()`, `type()` and
`key()` — open the browser from the taskbar the way a person would.

### Readiness

`create()` returns as soon as the API does; the machine is starting, not ready.

- `wait_until_built()` — a cloned computer's disk has been copied. Only clones
  need this; it returns at once for anything else.
- `wait_until_running()` — the VM is up. The guest OS is still booting. Raises
  rather than waiting out the timeout on a failed build or a suspended session,
  neither of which becomes "running" on its own.
- `wait_for_guest()` — the guest agent answers. Linux and Windows both; the probe
  is `exit 0`, which bash and cmd.exe both have as a builtin.

The last of those is about the agent, not the desktop, and the agent answers
first — on Windows by a wide margin, since it runs in session 0 and replies
before anyone has logged in. To wait for a desktop somebody could use, poll
`screenshot()` until it looks right.

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

### Files

One file in or out of the guest, no shell involved — the way a credential
reaches a `.env` without echoing it through a command line:

```python
c.write_file("/home/user/app/.env", "API_TOKEN=hunter2\n")
report = c.read_file("/home/user/report.csv")
```

Guest paths are absolute; a relative path is refused before the request is
made, because nothing about a transfer runs in a shell with a working
directory. A transfer resumes a suspended computer, like any other use.

### Errors

Everything derives from `MandalaError`.

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
under way, a guest agent that has not finished coming up, or a suspend committed
to the computer a moment before your call. Waiting and retrying is the fix;
changing the request is not.

A retry loop on it terminates rather than spinning forever: a guest agent that
stays silent past its boot window stops being a conflict and becomes a 502
`APIError`, which is the platform saying the agent is broken rather than late.

```python
try:
    c.snapshot()
except mandala_computer.ConflictError:
    c.wait_until_built()   # or just try again shortly
    c.snapshot()
```

## The `mandala` CLI

Your own terminal against a computer, addressed by name or id. Authentication
is the SDK's: `MANDALA_API_KEY` in the environment.

```sh
mandala ssh dev                    # an interactive shell in the guest
mandala scp .env dev:/home/user/app/.env
mandala scp dev:/home/user/report.csv .
```

`ssh` opens the platform's terminal websocket: a PTY the platform keeps alive
server-side, running as the desktop user. Disconnecting *detaches* rather than
ends it — the shell and whatever it was running keep going, and running the
same command reattaches with recent output replayed. `--session <name>` keeps
several; the shell's exit code becomes the command's own. Inside, plain
`nano`/`vim`/`echo` work as they would over real ssh.

`scp` copies one file per invocation, the side spelled `<computer>:/path` being
the guest. It rides the files API rather than the terminal, so it works
without any shell in the guest at all.

Two answers worth recognizing: a computer that predates the terminal feature
answers 409 until it is stopped and started again (a restart is not enough,
deliberately — a resumed session must match its saved device topology), and
Windows guests have no terminal yet.

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

Drift has two directions, and that test pins both. `ALLOWED` mirrors the
platform's route table in full, and `UNIMPLEMENTED` names the part of it this
SDK cannot yet reach — currently the agent loop and its OpenAI-shaped door, file
transfer, and the guest window list. Without the second set the first proves
nothing over time: "every call lands on an allowlisted route" stays true no
matter how far behind the client falls.

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
