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
    c.wait_for_guest()  # guest agent is up and answering
    c.open("https://example.com")  # on the screen, not as root
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
    print(c.start_error)  # e.g. "no host had room to start it"
    c.start()  # often works on a second attempt
```

### Your own templates

A template is a `mandala/v1` document — the image family it resolves to, what it
is layered onto, and the shape a computer gets when the create names no numbers.
Publishing one gives it a ref you can launch by name.

```python
from pathlib import Path

doc = Path("devbox.yaml").read_text()

# Worth doing while you iterate: this reports EVERY problem at once, and claims
# no ref. It does not raise for an invalid document — that is the answer.
check = client.templates.validate(doc)
if not check.valid:
    raise SystemExit("\n".join(check.problems))

t = client.templates.publish(doc)
c = client.computers.create(template=t.ref)
```

**The namespace is your account.** `metadata.namespace` has to be your account
id — anything else is a `PermissionDeniedError`, `system` included — and this SDK
does not rewrite it, because publishing a ref that is not the one in your file
would be worse than refusing.

**A ref is immutable.** Publishing the identical document again succeeds and
changes nothing, so a pipeline that republishes on every commit is safe.
Publishing a *different* document under the same ref is a `ConflictError`; bump
`metadata.version`. What counts as different is the digest, so a changed label is
a change.

**Two digests, and one of them is sometimes a sentence instead.** `doc_digest`
covers the whole document and changes with any edit; `build_digest` covers only
what decides the image, so comparing it against a previous run is how you tell
whether an edit means a rebuild. A document naming a parent in `spec.from` gets
`build_digest_needs` *instead* of `build_digest` — the two are alternatives, not
a pair — because a layered document's build digest depends on the contents of
the base image, which only a host holding it can compute:

```python
if check.build_digest is None and check.build_digest_needs:
    print(check.build_digest_needs)
    # the contents of acme/base's image, which only a host holding it can
    # supply. Run `gorillad -build-template <file> -dry-run` there to see
    # this document's build digest
```

`check.canonical` is the document as `doc_digest` was taken over it — compact
JSON, key order and whitespace normalised — so you can check the binding rather
than trust it:

```python
import hashlib
mine = "sha256:" + hashlib.sha256(check.canonical.encode()).hexdigest()
assert mine == check.doc_digest
```

Read one back — yours or `system`, so you can see what you are layering onto:

```python
base = client.templates.get("system", "base")

# Your namespace is your account id — the one `metadata.namespace` carries, and
# the first half of any ref you published.
namespace = t.ref.split("/", 1)[0]
pinned = client.templates.get(namespace, "devbox", version="1.0.0")
```

Without `version` you get the newest, which is also what a create naming the
unpinned `namespace/name` resolves to.

#### Retiring one

```python
client.templates.retire(namespace, "devbox", version="1.0.4")  # one version
client.templates.retire(namespace, "devbox")  # every version
```

Omitting `version` retires the **whole name** — deliberately not `get()`'s "the
newest", which on a delete would let a loop walk backwards through a history it
never asked about. An empty string is refused before it is sent, for the same
reason.

**Computers are not affected.** A computer is built from the image the ref
resolved to and holds no reference to the document, so anything already running,
stopped or suspended is untouched. What a retire breaks is resolution: a *new*
create naming the ref is refused.

**The ref stays spoken for, and still counts once.** Publishing it again is a
`ConflictError`, identical bytes included, and `refs_claimed` on the result does
not go down — it is the count against a much larger, separate ceiling than
`templates`. A ref you retired is a `NotFoundError` whose message names the date
it went, rather than claiming the template never existed; read the message before
concluding you mistyped something.

### Building one

A document that declares `spec.build` steps has to be compiled into an image
before anything can launch it. That is minutes of work — an agent image is
roughly fifteen — so it never blocks:

```python
build = client.builds.start(doc)
out = client.builds.wait(build.id)

if out.status != "succeeded":
    # There may be no failed STEP: most of a build is copying the base image, and
    # a build that dies in `staging` or `copying` never reaches the first one.
    failed = next((s for s in out.steps if s.status == "failed"), None)
    where = f"step {failed.n} ({failed.kind} {failed.label})" if failed else f"phase {out.phase}"
    print(f"{where} failed: {out.error}")
```

`wait()` does **not** raise for a build that failed. `succeeded` and `failed` are
two situations with two remedies — one has an image, the other has a step to fix
— and an exception flattens them into "something went wrong". Read `status`.

For a terminal, stream it instead of polling:

```python
for p in client.builds.events(build.id):
    print(f"{p.phase} {p.step}/{p.of} {p.note}")
```

Each event is news — the platform sends one only when something moved — and the
last one is the `done`, **including for a build that failed**. An `error` event
means the *stream* could not go on and says nothing about the build; it raises,
and says so. An account may hold eight streams open at once.

**A build that declares its own family is not launchable yet.** The fleet does
not advertise a family it built rather than shipped, so a create naming such a
ref is refused with a `400` — a bare `APIError`, and a permanent answer: the
message says in words that retrying the create changes nothing and that what
would change it is publishing a new version. Deliberately not a `503`, which
arrives as `UnavailableError` and reads to a retry loop as an answer worth
waiting for. A `503` on this path still means the case that does come good: a
shipped family whose only holder is unreachable. Publishing the document is
worth doing anyway — it claims the ref, and it is what `builds.start()` takes.

Everything here has an async twin: `await client.templates.publish(doc)`,
`await client.builds.wait(build.id)`, and `async for p in client.builds.events(...)`.

### Suspending

A suspend writes the guest's RAM to disk and gives the host its memory back.
It is a pause, not a stop: `start()` afterwards resumes the same session — same
processes, same open windows — in about a second, instead of booting.

```python
c.suspend()
c.is_suspended  # True
c.suspended_at  # when the session was saved
c.start()  # resumes it; ~1s, not a boot
c.stop()  # discards the session instead
```

`stop()` asks the guest to shut down and gives it time to. `stop(force=True)`
skips the asking and pulls the power — the equivalent of holding the button in.
It is what to reach for when a guest will not come down on its own, and it loses
whatever had not been written to disk.

`restart()` is refused with `ConflictError` while a session is saved, since it
would have to guess which of those two you meant. Start it or stop it first.

**A computer can suspend without you asking.** Its host puts down anything
nobody has used for the host's idle window — 30 minutes by default. Input,
`exec()`, file transfers and `set_clipboard()` all count as use and resume it
automatically; **`screenshot()` and `clipboard()` deliberately do not**, so a
loop that only polls the screen — or waits for somebody to copy something — can
watch its own machine go down under it. The two reads differ in what happens
next: `screenshot()` keeps answering, while `clipboard()` starts raising
`ConflictError`, because a suspended computer has no clipboard to read. Drive
the desktop, or accept the resume.

`wait_until_running()` raises rather than spinning if it finds a suspended
computer, because that state does not resolve on its own — `start()` is the fix.

### Showing somebody the desktop

Every response that *is* one computer carries the credentials and URLs to open
its live desktop, so putting a screen on your own page costs no extra call:

```python
c = client.computers.get("vm-0a1b2c3d4e5f")
c.vnc.embed_url  # watch-only, drop straight into an <iframe>
c.vnc.url  # full control: keyboard and pointer (clipboard only on some guests — see below)
c.vnc.view_url  # watch only — the platform drops input on this socket
```

Two credentials, because they are not the same permission. `view_token` cannot
type even from a patched client, and the guest's clipboard does not come back
over it either — the daemon takes that capability out of the connection as it is
negotiated, so whatever the person at the desktop copies, a password included,
is not visible to whoever holds the watch-only link. `token` is root-equivalent
on that machine. Neither is your API key — which is every computer on the account, forever, and
must never reach a browser. Both end when the computer restarts.

Whether the clipboard crosses that socket is a property of the computer, and
`c.vnc.clipboard` is the field that answers it:

```python
if c.vnc.clipboard:
    ...  # the RFB clipboard path was provisioned on this computer
```

It reports *provisioning*, not live health — the vdagent channel QEMU was given
at cold boot, together with whether the image this computer was built from was
verified to ship `spice-vdagent`. Somebody with root in the guest can install,
remove or stop the agent afterwards and the field will not move. It is also
always `False` on `view_url`, where the `False` is about the credential rather
than the computer.

`True` means the transport is open, which is not the same as a copy or a paste
succeeding. The first paste of a session is often dropped, because the guest
*pulls* the text and vdagent may not own the selection yet, and a browser will
not hand over the guest's clipboard without focus and permission. A client of
your own also has to negotiate the extended-clipboard pseudo-encoding — that is
QEMU's only door to the guest's clipboard, so an RFB client that does not offer
it receives nothing however the guest is configured. `False` means a paste
reaches QEMU and stops, silently, with nothing to catch.

A `False` is sometimes fixable. The channel is hardware and comes from a *cold*
start: stop the computer and start it again. Restarting a *running* computer
does not do it — that resets the guest rather than rebuilding the machine QEMU
was given. The agent comes from the image, which a computer keeps for life, so
one built before the agent shipped needs the package installed in the guest —
you have root there — or replacing with a newly created one. Windows guests
never have it, whatever the hardware says. A resumed or snapshot-restored
session keeps the topology of the capture it came from, so a computer that had
the channel can come back without one and reacquires it on its next stop and
start; the field is computed per response rather than stored, so it follows that
rather than going stale. Keep the route below whichever you get.

[`clipboard()` and `set_clipboard()`](#the-clipboard) are the route to build on
— the reliable one, not merely the fallback — because they need nothing of the
*hardware*: no cold boot, no permission from a browser. They ask one thing of
the image (`xclip`, in every golden since August 2026) and say so in the answer
when it is missing, which is one condition stated instead of two inferred. Where
the socket *does* carry the clipboard the two do not fight over it: those
methods write the same X `CLIPBOARD` selection the agent then offers onward.

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
c.name  # "build box" — the handle is updated in place
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

### Resizing, and the idle window

```python
c.stop()
c.resize(cpu=4, ram_mb=8192)  # the computer must be stopped; disks grow only
c.set_idle_suspend(120)  # minutes untouched before the host suspends it
c.set_idle_suspend(None)  # back to the host's own sweep
```

Three methods rather than one `update()`, because the platform refuses these in
combination and is right to: a resize needs the computer stopped and the other
two do not, so one request could not honour both without applying half of it.

`c.idle_suspend_min` is `None` on a computer with no override of its own. That
is not the same as "never suspends" — it follows whatever its host is sweeping
at, 30 minutes at the time of writing. The host's number is deliberately not
reported in its place, because it is a property of the host and changes when an
operator changes it.

The screen is not part of this. `resolution` is fixed for the life of a
computer — see [Sizes](#sizes).

### Growing past the host

A resize is refused when the size asks for more RAM than the host the computer
happens to be on can run. That refusal is an offer rather than an ending: another
host in the same region may be able to run it, and the computer can be moved
there.

```python
import mandala_computer as mc

try:
    c.resize(ram_mb=32768)
except mc.MoveRequiredError as e:
    if not e.move_possible:
        raise  # nowhere in the region can run it
    c.relocate(ram_mb=32768)  # 202 — the copy runs behind it
    move = c.wait_for_move()
    if move.state != "done":
        print(move.state, move.detail)
```

**It is a separate method on purpose.** `relocate()` copies the computer's disk
to different hardware. A resize that did that without being asked is exactly what
neither this SDK nor the platform will do, so there is no keyword on `resize()`
that quietly relocates a machine.

**The computer must be stopped**, and suspended is not stopped here — unlike a
resize, which accepts it. A saved desktop only loads on the host that wrote it,
so it cannot travel: resume and stop the computer, or discard the session, first.

**`wait_for_move()` does not raise for a move that ended badly**, because the
ways it can end are not one thing:

| `state` | what happened |
|---|---|
| `done` | on the new host, at the new size |
| `moved` | on the new host, at its **old** size — the move landed and the resize did not. An ordinary `resize()` finishes it where it now is |
| `failed` | nothing happened; the computer is where it was, untouched |
| `lost` | we stopped watching. It may well have completed — read the computer |

`moved` is the one to read carefully: the computer really has changed hardware,
so treating it as "the move failed" sends you looking for a machine that is no
longer where it was.

One move runs per account at a time. `client.moves.list()` is the account-wide
view — where a move you did not start is found, and how an "another computer on
this account is being moved right now" refusal gets a name.

```python
for m in client.moves.list():
    print(m.computer_id, m.state, "running" if m.live else m.finished_at)
```

The target is ours to choose and is never in the request: you are told a host in
this region, not which one.

Not called `move()`, which on a computer is the mouse pointer and has been since
before there was anything else to move. The TypeScript SDK made the same choice
for the same reason.

### Driving the desktop

Coordinates are in the computer's own screen space. That is a create-time choice
now rather than a fixed size, so read `c.resolution` (or `c.screen` for the two
numbers) instead of assuming — `mandala_computer.SCREEN_WIDTH` / `SCREEN_HEIGHT`
are the 1280×800 default, which is only what a computer that asked for nothing
else renders at.

```python
x, y = 640, 480

c.move(x, y)
c.click(x, y)
c.right_click(x, y)
c.double_click(x, y)
c.scroll(x, y, direction="up", amount=3)
c.type("some text")
c.key("ctrl", "c")

png = c.screenshot()  # full-resolution PNG
jpg = c.screenshot(width=320)  # downscaled JPEG — cheap enough to poll
now = c.screenshot(fresh=True)  # skip the cache; what a drive loop wants

res = c.exec("ls /tmp")  # native shell: bash on Linux, cmd.exe on Windows
res.ok, res.exit_code, res.stdout, res.stderr
```

**Pass `fresh=True` whenever the image is feeding a decision.** A bare
`screenshot()` may be answered from a frame up to 1.5 seconds old. That is the
right trade for a thumbnail and the wrong one for a loop: a model shown the
screen from before its own click concludes the click missed and clicks again,
and the second one lands on whatever the first one opened.

A non-zero exit is returned, not raised — check `res.ok`.

The guest agent stops capturing a command's output at 16 MiB while the command
keeps producing it, so what comes back can be the first 16 MiB with nothing else
to say there was more. `res.truncated` is that signal, and it is worth checking
before parsing anything that could be large:

```python
res = c.exec("cat /var/log/syslog")
if res.truncated:
    ...  # redirect to a file inside the guest and fetch it instead
```

`res.ok` deliberately ignores truncation: a command that succeeded and produced
a lot of output still succeeded, and whether a short answer is acceptable depends
on what you were going to do with it.

### Launching GUI applications

By default `exec()` runs in the system context — as `root` on Linux, with no
`DISPLAY` and no `HOME` — which is right for installing packages and wrong for
anything with a window. The obvious call therefore does nothing:

```python
c.exec("firefox https://example.com")  # no DISPLAY — dies, and exec() reports it
```

`desktop=True` runs the command in the logged-in desktop session instead, as the
desktop user with `DISPLAY`, `HOME` and `XAUTHORITY` set:

```python
c.exec("nohup firefox https://example.com >/dev/null 2>&1 &", desktop=True)
```

The `nohup … &` is still yours to write. A GUI program does not exit on its own,
so a foreground launch blocks until `timeout` kills it and comes back as a
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

**Why it names a browser.** `open()` asks for Firefox by name rather than going
through `xdg-open` or one of the other portable wrappers. Naming it puts the
choice in one place: this method is the only thing that decides which browser the
guest opens, so if that ever needs to be a different one, it changes here and
your code does not change at all. Which is most of the reason to call it rather
than write the `exec()` yourself.

The URL is shell-quoted, so one containing `&` or `;` stays a URL. One starting
with `-` is refused rather than escaped: quoting stops the *shell* reading it as
a flag, nothing stops the *browser* doing so, and no real URL starts with a dash.

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

### Long-running commands

`exec()` waits, and `timeout` passing means *you* stopped waiting — the
command keeps running inside the guest, and its output and exit code are lost
with the request.

**`exec()` has a ceiling of about two minutes, and it is not `timeout`'s.** The
HTTP budget is derived from `timeout` and the platform stretches its own
deadline to match, so neither this client nor the platform is what stops a long
command. A proxy in front of the platform is: it abandons a request that has
produced no response for about two minutes and answers 524, which arrives as
`GatewayTimeoutError`. Measured against `app.mandala.computer`:

| command | `timeout` | result | wall clock |
|---|---|---|---|
| `sleep 110` | 230 | ok | 110.6s |
| `sleep 130` | 300 | `GatewayTimeoutError` | 125.2s |
| `sleep 130` | 3600 | `GatewayTimeoutError` | 125.3s |

The last two rows are the whole point: `timeout` differs by an order of
magnitude and the failure lands in the same place, because the ceiling belongs
to a hop that never saw it. Raising `timeout` cannot buy time from it.

The command also survives the request that abandoned it, so the call *after* a
`GatewayTimeoutError` commonly raises `ConflictError` — the guest agent is still
busy with the command that timed out. That is the first failure still happening,
not a second one.

So `exec()` is for commands that finish in well under two minutes. For anything
slower — and for anything slower than a few seconds, which is a lower bar —
start it instead:

```python
import time

job = c.start_exec("apt-get install -y build-essential", cwd="/root")

while True:
    status = job.poll()
    print(status.stdout, end="")
    if status.drained:
        break
    if not status.more:
        time.sleep(2)

print(status.exit_code)
```

Strictly better than backgrounding with `&`, which throws away both the exit
code and the output.

The read is a **cursor, not a buffer**: each `poll()` returns what has arrived
since the last one and advances the daemon's own offset. Output you receive and
drop is gone, and two pollers on one pid split the stream between them rather
than each seeing all of it — so keep one handle per command. `status.more`
means there is output waiting right now, which is why the loop above only sleeps
when it is clear.

`job.kill()` stops the command and everything it started, and answers with its
final state including whatever it printed that you had not read — so it collects
the tail as well as ending the job. `job.pid` survives the process: a later run
can pick the command back up with `c.background_command(pid)`, which makes no
request until you poll it.

### What is on the desktop

A screenshot says what the desktop looks like; `windows()` says what any of it
*is*. That is how a browser that failed to launch is told apart from one that
has not painted yet, without asking a model to find it in a PNG.

```python
for w in c.windows():
    print(w.id, w.wm_class, w.title, w.width, w.height, w.focused, w.visible)

firefox = next(w for w in c.windows() if w.wm_class == "Navigator" and w.visible)
c.window_action(firefox.id, "focus")
c.window_action(firefox.id, "move", x=0, y=0)
c.window_action(firefox.id, "resize", width=1280, height=760)
```

Match on `wm_class`, not `title`: the class is the application and is stable,
the title is whatever page it happens to be showing.

Check `visible` before treating `x`/`y` as somewhere to click. It is the only
thing that separates a minimised window from one on the screen: a minimised
window stays on the list, keeps the coordinates it had and can still be the
`focused` one, so clicking where it says it is sends the click to whatever is
actually in front. An answer the client cannot read counts as not visible —
a window skipped, rather than a click somewhere nobody asked for.

`pid` is the guest process that owns the window, or `None` where the window did
not say — `None` rather than `0`, because a guest may legitimately advertise
`_NET_WM_PID` 0. It does *not* identify the window: `xfce4-terminal` and every
browser back several windows with one process, so killing this pid takes windows
you never asked about.

Prefer `focus` over `raise`. Raising without focusing gives a window that is
visibly in front and silently not receiving keystrokes — which in a screenshot
looks exactly like one that is.

The result is the window *as it now is*, not an acknowledgement. Believe it
rather than the request: the window manager places the frame and applications
snap to their own increments, so a move to (300, 200) routinely lands at
(305, 229). After a `close` there is no window to describe, and `res.gone` is
what separates that from an action the guest simply could not report on.

`include_all=True` keeps the desktop's own furniture — panels, docks, the
wallpaper window. Off by default because a stock guest showing one terminal has
five windows, four of which are not applications. Linux only.

### The clipboard

The desktop's `CLIPBOARD` selection — what Ctrl-C writes and Ctrl-V pastes —
read and written from outside the guest. Linux only, and it needs nothing of
the *hardware*: no cold boot, no permission from a browser. What it does need is
`xclip` in the guest, which every golden built since August 2026 carries — so in
practice this is the road that works on every computer, and where it is not, the
refusal says so. (The other road is RFB extended cut text over the desktop
socket, which is live and conditional; see
[Showing somebody the desktop](#showing-somebody-the-desktop).)

```python
c.set_clipboard("https://mandala.computer")
c.key("ctrl", "v")  # into whatever has focus

on_clipboard = c.clipboard()  # "" is an empty clipboard
```

`set_clipboard()` takes at most 64 KiB of UTF-8; `clipboard()` returns at most
128 KiB. They are different bounds on different channels, and the read is
**refused rather than truncated** past its own — half a password is not less of
an answer, it is a wrong one that looks completely normal. Empty text and a NUL
are refused here, before the request goes out.

The platform confirms the write by reading the selection back before it
answers, so `set_clipboard()` returning means the desktop is *holding* the text
rather than that a command ran.

**Not every `ConflictError` here is worth retrying.** Classified refusals carry
an `APIError.reason`: `contention` and `starting` clear on their own, while
`unavailable` and `unsupported` require a different action. `is_transient()`
therefore answers `True` for the first pair and `False` for the second. A
stopped or suspended computer is `unavailable`; start it instead of retrying
the clipboard request. If an older platform response has no recognised reason,
the SDK preserves the historical `ConflictError` fallback of `True`, so code
that must support unclassified responses should verify the computer state and
keep its retry loop bounded.

Two others worth knowing. A **400** never clears: a computer built from a golden
that predates `xclip` is refused permanently — install `xclip` in the guest, or
create a new computer. And an over-cap read raises `FileTooLargeError`, whose
usual remedy does not apply: there is no `Range` on a selection, so the text is
either under 128 KiB or out of reach.

The two differ on one thing worth knowing: `set_clipboard()` **resumes a
suspended computer**, because putting text on a clipboard is the first half of
pasting it and that is somebody working on the machine. `clipboard()` does not
— what somebody copied is not worth waking a machine for — so reading a
suspended computer is a 409 rather than a start you did not ask for.

A read failure raises; an empty clipboard is `""`. That is the distinction the
`exec` recipe these replace could not make.

#### What these replace

Until platform OPL-3768 the only public road was a recipe over `exec` with
`desktop=True`, documented at length in this README. Do not go back to it.
`exec` runs a **login shell**, so the desktop user's profile is sourced and
anything it prints lands on the same stdout as your command's output, ahead of
it. That is wanted when you asked to run a command the way the user would, and
fatal when you are reading a value: an `echo` in the guest's `.profile`
corrupts the answer and a deliberate one forges it. No framing you add fixes
that — a profile that prints your frame owns everything after it. The clipboard
endpoints do not share that stream.

The write was worse. An X selection belongs to a live process, so the holder
had to outlive the exec under `setsid` and have its output redirected, or the
resident `xclip` held the pipe the guest agent reads and the call ran to its
full timeout before answering. The text had to travel base64 and quoted, since
an apostrophe would otherwise end the shell word. And because being granted a
selection is asynchronous, the result had to be polled for in a loop bounded in
*attempts* — each one a billable exec — rather than trusted. `set_clipboard()`
does all of it in one call.

### Letting the platform drive

`agent()` hands the whole loop to the platform: it screenshots, asks a model
what to do, does it, and repeats, until the task is done or it runs out of
steps. The point is that ten clicks stop being ten images in your context.

```python
import os

key = os.environ["ANTHROPIC_API_KEY"]  # your own; see below

result = c.agent("Open the settings and turn on dark mode.", model_key=key)
print(result.text)
if not result.finished:
    print(f"did not finish: {result.stop}")
```

**It runs on your own Anthropic key**, passed as `model_key` and sent on that
one request as `X-Model-Key`. The platform never stores one, never bills you for
it, and will not fall back to anything — so the key is a per-call argument
rather than something the client holds. Every step is a model call plus a
screenshot on that key, which is why `max_steps` bounds spending as much as it
bounds the loop. It is a whole number from 1 to 100 — the platform's ceiling,
which the SDK refuses past rather than spending a round trip to be told.

**The computer must already be running.** This route will not start one for you:
starting is billable, and it is not a decision to make on your behalf because
you sent a prompt. A stopped or suspended computer is a `ConflictError`, and so
is one another run is already driving.

A run is minutes of clicking, so `agent_stream()` reports as it goes — something
that says nothing until it is over cannot be told from a hang:

```python
import mandala_computer as mc

for event in c.agent_stream("Find the cheapest flight to Lisbon", model_key=key):
    match event:
        case mc.AgentStepEvent(step):
            print(f"{step.n}. {step.detail}")
        case mc.AgentText(text):
            print(text)
        case mc.AgentDone(result):
            print(f"{result.stop} after {result.steps} steps")
```

`agent()` is that loop waited out, and it streams underneath for the same
reason: it is the same request either way, and the streaming one is the request
a proxy between you and the platform will not close for being quiet. Use
`agent_once()` — one non-streaming request — only if you cannot use a stream at
all.

**A run that ends unfinished does not raise.** `max_steps`, `rate_limited` and
`refusal` all leave real work on the desktop, and raising would throw away the
only account of what was done to the machine. Check `result.finished`, which is
`stop == "end_turn"` and nothing else — including for a stop reason added after
this SDK was written. What *does* raise is a failure the platform reports
mid-run, as whatever class its status deserves: a bad model key comes back as an
`AuthenticationError`, not as something your handler cannot classify.

That raise carries the run with it. `e.agent` holds what the loop had already
spent on your model key and the steps it had already taken, so a failure at step
eight stays an account of eight steps rather than only a message — the spend is
on a key the platform never meters, and the clicks are still on the desktop.
`agent_stream()` hands the same record over as an `AgentFailed` event.

```python
import mandala_computer as mc

try:
    c.agent("Book the flight", model_key=key)
except mc.MandalaError as e:
    if e.agent:
        print(f"{len(e.agent.steps)} steps, {e.agent.usage.input_tokens} tokens in")
```

Every step spends your Mandala rate budget too — the same budget your own calls
draw on, at the same price, because a click through here costs what a click plus
a screenshot costs anywhere. A run that exhausts it stops where it is and ends
`rate_limited` rather than failing.

Events this SDK does not model are skipped rather than raised on, so the
platform adding an event type does not break your loop. A bare `break` does not
close an async iterator, and immediate cleanup of a sync generator is not
portable across Python implementations. To stop a run early, wrap the iterator
in `contextlib.closing()` (sync) or `contextlib.aclosing()` (async); leaving that
context closes the HTTP stream and stops the run.

The platform also exposes the same engine behind an OpenAI-shaped door at
`POST /chat/completions`. This SDK deliberately does not wrap it: if you want
that, you already have an OpenAI client — point its `base_url` here, which is
the whole reason the door is there.

### Readiness

`create()` returns as soon as the API does; the machine is starting, not ready.

- `wait_until_built()` — a cloned computer's disk has been copied. Only clones
  need this; it returns at once for anything else.
- `wait_until_running()` — the VM is up. The guest OS is still booting. Raises
  rather than waiting out the timeout on a failed build or a suspended session,
  neither of which becomes "running" on its own.
- `wait_for_guest()` — the guest agent answers. Linux and Windows both; the probe
  is `exit 0`, which bash and cmd.exe both have as a builtin. A failed start or
  stopped computer is reported immediately. A suspended computer is different:
  the probe is an `exec()`, so it resumes the session as use normally does.

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
snap = c.snapshot()

c = client.snapshots.clone(snap.id)
c.is_building  # True
c.wait_until_built()  # minutes, for a large disk
c.start().wait_for_guest()
```

If the copy fails the computer stays, so you can see it and reclaim the space it
took. It never becomes usable — delete it and clone again.

```python
if c.build_failed:
    print(c.build_error)  # e.g. "no space left on device"
    c.delete()
```

`wait_until_built()` raises rather than waiting out the timeout if the build
failed, and its `TimeoutError` means only that the wait stopped — the copy is
still going.

### Snapshots

```python
snap = c.snapshot()  # disk, works while running
snap = c.snapshot(memory=True, name="before-upgrade")  # + live RAM, resumes without booting
client.snapshots.restore(snap.id)
twin = client.snapshots.clone(snap.id)  # a fork, for memory snapshots
twin.wait_until_built()  # the disk is copied out of backup first
c.set_schedule(enabled=True, hour=4, tz="America/Chicago")
c.set_schedule(enabled=False, hour=4, tz="America/Chicago")  # off, keeps the time
c.clear_schedule()  # removed entirely
```

A snapshot carries the shape it was captured at — `snap.os`, `snap.template`,
`snap.cpu`, `snap.ram_mb`, `snap.disk_gb`, `snap.resolution` — which is what a
`clone()` of it comes up as. That is the capture's shape and not the source
computer's current one, so a computer resized after the snapshot was taken
clones back to what it was. Read it before cloning if the size matters.

`c.snapshot_schedule` is the same window carried on the computer itself, for a
caller that already holds one and would rather not spend a second call on
`c.schedule()`. It is `None` on a computer that has no schedule, which is not
the same as one whose schedule is switched off.

Disabling and clearing differ. `set_schedule(enabled=False)` is deliberately
non-destructive — it keeps the chosen time so toggling back on restores it.
`clear_schedule()` returns the computer to never having had a schedule.

The schedule describes the *window* and nothing else — there is no `last_run`.
For "when did my backups last run", read the snapshots, which carry real capture
times; `auto` marks the ones the scheduler took:

```python
backups = [s for s in c.snapshots() if s.auto]  # or s.is_scheduled
last = max((s.created_at for s in backups), default=None)
if last is None:
    print("no automatic backup has ever run")
```

`auto` also marks the only snapshots retention will age out — ones you take
yourself are never removed automatically.

#### How long they are kept

A schedule says when snapshots are taken and not how long they survive. That is
your plan's, account-wide, and read-only:

```python
r = client.snapshots.retention()
print(f"keeps {r.daily} daily, {r.weekly} weekly, {r.monthly} monthly")
```

What survives is the newest automatic snapshot in each of the last `daily` days
**that have one**, and likewise for ISO weeks and calendar months — periods that
contain a capture, not periods on the calendar, so a computer switched off for a
month still has the history it had. Boundaries are cut in UTC whatever timezone
the schedule runs in. A zero turns that tier off.

The window belongs to the account and is applied per computer: two computers on
`7/4/12` keep up to twenty-three snapshots each, not twenty-three between them.
Taking one by hand is how you keep something past it.

#### Orphans

Snapshots outlive the computers they came from, so an ordinary account's listing
contains rows whose `computer_id` resolves to nothing. Those carry
`orphaned=True`, and it decides which of the two operations still works: `clone`
builds a new computer out of the snapshot alone and is fine, while `restore`
puts the disk back on a source that no longer exists.

`include_unfinished=True` widens a listing to deletions that began and did not
finish. Nothing can be restored or cloned from one, but they still hold objects
and are still billed — so it is the flag for a question about storage rather
than about what you can act on.

#### Deleting a computer, and its snapshots

Deleting a computer keeps its snapshots by default; they become the orphans
above. Destroying them with it is opt-in, and bound to what you were shown:

```python
held = c.snapshot_holdings()
print(held.count, held.size_bytes)

if held.count == 2:  # you looked, and decided
    c.delete(purge_snapshots=True, expect=held.fingerprint)
```

`snapshot_holdings()` is not a listing — the snapshots themselves come from
`c.snapshots()`, and the two routes answer different shapes deliberately. What
it has that a listing cannot give you is `fingerprint`, which names that exact
set and cannot be computed from the rows. It is the only interlock on an
irreversible operation: the daemon refuses the sweep if a capture has landed
since you read it.

Which is why the fingerprint must not be fetched on the line above the delete.
That binds the purge to whatever the set is *now* rather than to what anybody
agreed to, and the race it exists for is precisely a capture that finishes
between the decision and the call. A stale one raises `ConflictError` and
destroys nothing. Purging without one raises `ValueError` before any request is
made — the platform itself allows an unguarded purge, for callers that have no
way to read the holdings, and this SDK has one call away.

`delete()` returns how many snapshots went with the computer, or `None` when the
platform did not say. `None` rather than `0`: reporting "nothing was destroyed"
because the server was quiet is the one wrong answer worth going out of the way
to avoid.

### Usage

What the account has spent, in the same figures the dashboard shows and the
invoice bills on. This is the read to build a spend check around: a loop that
launches computers is the caller that can run up a bill without noticing.

```python
u = client.usage.read()

print(f"{u.usage.vcpu_hours} vCPU-hours since {u.from_}")
for c in u.usage.computers:
    print(f"  {c.name or c.id}{' (deleted)' if c.gone else ''}  {c.run_hours}h")
```

With no arguments the window is the account's **current billing period**, which
is what makes the numbers comparable with an invoice. Name a window for one that
has closed — the billing period is always the current one, and by the time an
invoice arrives the period it covers is not:

```python
from datetime import datetime, timezone

client.usage.read(
    since=datetime(2026, 7, 1, tzinfo=timezone.utc),
    until=datetime(2026, 8, 1, tzinfo=timezone.utc),
)
```

One window at a time, and at most 62 days of it: every hypervisor replays its
ledger a day at a time to answer, so a longer span is refused rather than quietly
shortened. Records reach back 399 days, so an older period is read by naming both
bounds rather than by widening one. And send `since` **with** `until` when the
period has closed — `until` on its own is measured from the current period's
start, which is after it.

`since` and `until` are sent as `from` and `to`; the other spelling exists
because `from` is a Python keyword. Both take an **aware** `datetime` or an RFC
3339 string carrying a zone — `"2026-08-01T00:00:00Z"`, not
`"2026-08-01T00:00:00"`. A naive datetime is refused rather than rendered,
because the zone that would have to be assumed is not necessarily yours, and a
window silently shifted by a few hours is the worst possible failure on the one
call whose output somebody checks against a bill.

**Read `degraded` and `unmetered` before you use the numbers.** Every figure is a
sum across the hypervisors your computers are on, so a host that did not
contribute does not leave a hole you could notice — it leaves a total that is
quietly too small.

```python
if u.degraded or u.unmetered:
    # Short, and saying so. `degraded` clears when the host comes back;
    # `unmetered` is a host running a daemon older than the meter and never does.
    print("these totals may be low — do not reconcile them against an invoice")
```

This is why the call returns rather than raising, unlike a partial listing below:
the caveat travels on the same object, so it cannot be missed the way a missing
row can — and one of the two shortfalls would never clear by retrying.

Two more fields worth knowing:

- `reported_through` — the last UTC day whose usage has settled for billing, as a
  contiguous prefix. Not a caveat on the totals, which are live and true through
  `to`; it is the boundary to check before comparing anything with an invoice.
  `None` while none of the window has settled.
- `breakdown` — `False` when the API key is scoped to a workspace. Usage is
  metered and billed per **account**, so `usage.computers` would name computers
  outside such a key's scope and the platform withholds it; the account-wide
  totals still arrive. The tuple is empty either way, and this flag is what tells
  "no computers ran" from "this key may not see which did".

The async client reads it the same way:

```python
u = await client.usage.read()
```

### Partial listings

`client.computers.list()`, `client.snapshots.list()` and `client.builds.list()`
fan out across every hypervisor holding something of yours, so one that cannot
be reached makes the answer incomplete. By default the platform refuses to send
it and this raises `UnavailableError` — because a short list is not a smaller
truth. It reads exactly like the missing computers were deleted, and the obvious
next thing a script does with a computer that has disappeared is tidy up after
it.

```python
computers = client.computers.list(allow_partial=True)
if not computers.is_complete:
    ...  # do not treat anything absent from this as deleted
```

The return is a `Listing`, which is a `list` — everything written against the
old return type still works. What it adds is `is_complete`, and `incomplete` for
the count. Branch on `is_complete`, never on the number: `incomplete` is what
the platform's placement cache could account for, and it is legitimately `0`,
because a computer created during the outage was never cached against the host
now holding it.

Rows the platform could not read come back marked rather than omitted —
`c.unreachable` on a computer, `s.unreachable` on a snapshot — carrying an id
and nothing else. Everything else on such a row is absent, so `status` reads
`""` rather than anything true. `c.snapshots()` keeps them for that reason even
though they cannot be attributed to a computer: dropping them would remove
precisely the markers saying the answer is short, and then report a confident
count.

Builds are the exception, and the reason to read `is_complete` there rather than
the rows. A short build listing has no marked rows at all — the platform keeps
no record of which hypervisor ran which build, so the missing ones are simply
absent and `incomplete` is `0` rather than a count. An outage and an account
that has never built anything are the same rows; only the `Listing` tells them
apart.

The marked rows above are also an account-wide key's alone. A key scoped to one
workspace gets none, on any of the three listings: naming the missing ids means
reading them out of a placement cache with no workspace column, which would hand
a confined credential ids from the workspaces it is confined away from. With
such a key, `is_complete` is the only signal everywhere.

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

**One request moves at most 64 MiB**, and that is a limit on the request rather
than on the file. An oversized write is refused locally, before anything is
sent; an oversized `read_file()` raises `FileTooLargeError`, which is a signpost
rather than a dead end.

#### A window of a file

`download_file()` is how a file of any size comes off a computer, and
`read_file_part()` is the single window underneath it:

```python
c.download_file("/home/user/out.tar", "out.tar")  # 2 GB, a part at a time

# Or into anything writable. A handle you opened is a handle you close —
# `download_file` deliberately does not close one it did not open.
with open("out.tar", "wb") as open_handle:
    c.download_file("/home/user/out.tar", open_handle)

tail = c.read_file_part("/var/log/build.log", offset=-4096)  # the last 4 KiB
head = c.read_file_part("/home/user/out.tar", length=512)  # the first 512 B
```

`offset` counts from the start of the file, or from its end when it is
negative — the reading Python gives an index, and the one `Range: bytes=-N`
has. A tail takes no `length`: it is already anchored at both ends.

**Asking for more than one request moves is not an error, and you can get fewer
bytes than you asked for on a success.** The platform trims the window rather
than refusing it, precisely because a caller cannot know the ceiling before
asking — so the `FilePart` that comes back is the authority on what arrived and
where to ask from next, never the numbers passed in:

```python
path = "/home/user/out.tar"
part = c.read_file_part(path, offset=0, length=1 << 20)
part.data  # the bytes
part.offset  # where they start in the file
part.total  # the file's length
part.end  # the offset to ask from next
part.at_end  # whether there is anything left to ask for
```

Which end gets trimmed follows the end you anchored: a window counted from the
start keeps its start, a tail keeps its end. An over-long tail is still the tail
of the file, never the middle of it.

Two answers worth recognizing. A window naming no byte the file has raises
`RangeNotSatisfiableError`, whose `size` is the file's real length so the retry
does not have to guess — and it is how an *empty* file answers every window,
which is why `download_file()` reads a zero there as an empty file rather than a
failure. A file whose length the guest cannot report — a `/proc` entry, where
the seek says 0 and the bytes are there anyway — has no positions to name, so
the platform sends the whole thing and ignores the range; `partial` is `False`,
`total` is `None`, and `at_end` is `True`, because everything there was arrived.

`download_file()` does not open a local path until the first window has landed
*and been checked*, so a download that is refused leaves whatever was there
alone — opening for write is destructive on its own, and "nothing was written"
is no comfort to a file that was truncated on the way to an exception.

A file that **grows** while it is read is followed to its new end: appending
leaves the windows already read where they were, so that is still one file. A
file that **shrinks** raises, because it is not. Either the next window falls
off the new end — a `RangeNotSatisfiableError` — or it lands inside it and the
length it reports has dropped, which is a `MandalaError` naming both. The
alternative is two files spliced at whatever offset the change landed on, under
a byte count that looks perfectly reasonable.

### Errors

Everything derives from `MandalaError`.

| Exception | When |
|---|---|
| `AuthenticationError` | 401 — key missing, malformed, or revoked |
| `PlanLimitError` | 402 — plan caps: count, size, RAM/disk pools, OS |
| `PermissionDeniedError` | 403 — suspended or unverified account |
| `NotFoundError` | 404 — no such resource (also another tenant's) |
| `ConflictError` | 409 — right request, wrong moment; retry, except the two cases below |
| `MoveRequiredError` | 409 — …except this one: the size needs a host that can run it |
| `FileTooLargeError` | 413 — past what one request carries. A file over 64 MiB: ask for a window. A clipboard over 128 KiB: there is no window to ask for |
| `RangeNotSatisfiableError` | 416 — that window names no byte the file has; `size` says how long it is |
| `RateLimitError` | 429 — too many requests; retry after `retry_after` |
| `UnavailableError` | 503 — a hypervisor could not be reached; retry |
| `GatewayTimeoutError` | 504/524 — a proxy gave up waiting; the work usually carries on |
| `OriginResponseError` | 520 — it was reached; the exchange broke on the way back |
| `OriginUnreachableError` | 521-523 — a proxy could not reach it; retry |
| `OriginTLSError` | 525/526 — a certificate the two cannot agree on; report it |
| `APIError` | any other unsuccessful response |
| `ConnectionError` | the request never completed: DNS, refused socket, broken TLS — except the case below |
| `ConnectionInterruptedError` | the request was dispatched and the answer was lost; do not replay a create |
| `TimeoutError` | a `wait_*` helper gave up, or a request outran its budget |

`PlanLimitError`'s message names the limit that was hit.

`MoveRequiredError` is a 409 that does **not** clear, and it is a subclass
of `ConflictError` so that an `except ConflictError` written before it existed
still catches it. It means the size asked for is more RAM than the host this
computer is on can run — and the host will not grow, so the same request answers
the same way for as long as the computer is where it is. `move_possible` is the
branch: `True` means somewhere else in the region can run that size and
`relocate()` takes the offer up, `False` means nowhere can and the size is the
thing to change. See [Growing past the host](#growing-past-the-host).

`ConnectionInterruptedError` is a `ConnectionError` that does **not** mean the
request never left, and it is a subclass of `ConnectionError` so that an
`except ConnectionError` written before it existed still catches it. It means
the request was dispatched and the answer was lost — a socket reset while the
body was being read, a protocol error on the way back. The platform may have
acted. Do not replay a create on the strength of it; check whether the first
attempt took effect. `is_transient` says no, matching `MoveRequiredError` under
`ConflictError`.

`FileTooLargeError` and `RangeNotSatisfiableError` are the two size statuses,
and each has a next move attached, which is why neither is a bare `APIError`.
A 413 on a **file** means the ceiling applies to what one request moves, so ask
for part of it — `download_file()` is that loop already written. A 413 on the
**clipboard** has no such remedy: there is no `Range` on a selection, so a
clipboard past 128 KiB is out of reach rather than something to page through,
and `download_file()` is not a method it has. A 416 carries the file's real
length in `size`, which is the whole point of the status: you asked about a file
whose length you did not know, and the number comes back with the refusal
instead of behind another request. See [Files](#files).

`RateLimitError` is the only refusal that says how long to wait:
`retry_after` carries the `Retry-After` header in seconds. Every route on this
surface is metered, including ones that go on to answer 404 — the meter runs
before the routing does — so a burst of anything counts against the same budget.
That budget is generous, in the low thousands of requests a minute even at the
bottom of the range, so hitting it usually means a poll loop with no sleep in it
rather than real load.

`UnavailableError` is not only about listings. Every route on this surface ends
at a hypervisor, so any call naming a computer on a host that cannot be reached
raises it — `start()`, `exec()`, `screenshot()` — rather than a `NotFoundError`,
because the computer has not gone anywhere. Creates and resizes raise it when
the fan-out that checks your plan comes back short, and so does a host with no
room left for another guest. Retrying is the fix; `allow_partial=True` applies
only to the fan-out listings, which are the one case where a partial answer
exists (see [Partial listings](#partial-listings)).

These four are the edge failing rather than the platform refusing, and they are
four classes rather than one because a caller asking *did my work happen* needs
four different answers.

`GatewayTimeoutError` is a hop that stopped waiting. Usually the platform has
the request and is still working on it — that is what a 524 is — so retrying the
same call unchanged reproduces it exactly, and after one on an `exec()` the next
call may report the guest agent busy. A strong default rather than a guarantee,
though: a 504 can come from a hop that never reached the platform, and a 524 can
end an upload whose body had not finished arriving. `str(e)` carries the
platform's own message where it sent one, and the SDK's explanation otherwise.
See [Long-running commands](#long-running-commands) for the ceiling and for
`start_exec()`, which is the shape that does not meet it.

`OriginUnreachableError` is its near-opposite: 521-523, a proxy that could not
reach the platform at all. Almost always the request was never sent, so nothing
was started — *almost*, because a connection can also time out after it was
established, and bytes already on the wire are not unsent because no answer came
back. Usually the platform restarting, and it clears on its own.

`OriginTLSError` is 525 and 526, and it is the one edge failure with no waiting
in it. An expired or mismatched certificate fails identically on every retry, so
the `wait_*` helpers raise it immediately instead of spending their timeout on
it. It is a deployment somebody has to fix.

`OriginResponseError` is 520 alone, and it is the trap in that range. Despite
the neighbouring number it does **not** mean the platform was never reached: it
means the platform *was* reached and the exchange broke on the way back. So the
work may have happened in full, in part, or not at all. Retrying a read costs
nothing; before retrying anything that *creates* something, check whether the
first attempt took effect — the alternative is two computers where you meant
one, both billable, on the strength of an error that looked like nothing
happened.

The rule of thumb across all four: reads are always safe to retry, and anything
that creates deserves a look first.

`is_transient(err)` is that rule as a function, and it answers for the riskiest
caller — code wrapping an arbitrary call, possibly a `create`. It says yes to
`ConflictError` (minus `MoveRequiredError`), `RateLimitError`, `UnavailableError`
and `ConnectionError` (minus `ConnectionInterruptedError`), and no to everything
above whose outcome is unknown, 502 and 504 included. The same four classes, and
only those, answer yes in mandala-computer-typescript and mandala-computer-mcp.

The `wait_*` helpers do not ask it. They replay idempotent reads under a
deadline you set, so they ride out every 5xx — a hypervisor briefly away during
a boot is what a poll loop is *for* — and give up only on a failure describing
the **request**: a 4xx other than 408, 409 and 429, a 3xx, a certificate, or a
524. Two audiences, two answers.

One caveat the table cannot show: these classes are for failures that arrive as
an HTTP status. The agent loop reports its own failures as events inside a
successful response, and a gateway or origin status relayed that way comes back
as a plain `APIError` — the stream having been delivered is proof no proxy
abandoned anything. So `except GatewayTimeoutError` around `agent()` will not
catch a 504 the platform is *reporting*; catch `APIError` and read `.status`.

`ConflictError` is the one worth catching separately, because nearly every one
clears itself: something is in flight that the operation cannot run alongside —
a disk still being copied, a snapshot being taken, a delete already under way, a
guest agent that has not finished coming up, or a suspend committed to the
computer a moment before your call. Waiting and retrying is the fix; changing
the request is not.

Two do not clear. `MoveRequiredError` is one, and it has a class you can catch.
The other is a clipboard refusal on a stopped or suspended computer: waiting
will not start it, so `start()` is the fix. Current platform responses classify
that refusal with `APIError.reason == "unavailable"`, and `is_transient()`
answers `False`; `contention` and `starting` answer `True`. An absent or unknown
reason is deliberately treated as unclassified and falls back to the exception
type, so a legacy `ConflictError` still answers `True`. If you support such
responses, check the computer state and keep the retry loop bounded.

A retry loop on it terminates because it has a deadline, not because of any
status: a guest agent that stays silent past its boot window does stop being a
conflict and become a 502, but `wait_for_guest` polls through a 502 as well —
an agent that is merely slow answers one for its first seconds too.

```python
import mandala_computer as mc

try:
    c.snapshot()
except mc.ConflictError:
    c.wait_until_built()  # or just try again shortly
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

The interactive `ssh` command currently requires a Unix-like local terminal.
`scp` remains available on Windows, including with drive-letter paths.

`scp` copies one file per invocation, the side spelled `<computer>:/path` being
the guest. It rides the files API rather than the terminal, so it works
without any shell in the guest at all. A download is paged, so a file larger
than the 64 MiB one request moves copies like any other, and a copy that is
refused leaves the local file alone. An upload is one request, and one larger
than the limit is refused before it is read.

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
platform's `V1_ROUTES` table in full, and `UNIMPLEMENTED` names the part of it
this SDK does not reach — currently just `POST chat/completions`, and that one
by choice rather than by lag: a caller who wants an OpenAI-shaped door already
has an OpenAI client to point at it. Without the second set the first proves
nothing over time: "every call lands on an allowlisted route" stays true no
matter how far behind the client falls, so closing a gap means deleting a line
from `UNIMPLEMENTED` rather than nobody noticing.

A mirror nobody compares is a comment, though, and that is not hypothetical:
background exec and the snapshot holdings landed upstream and stayed invisible
here for exactly as long as there was nothing doing the comparison, with the
whole suite green throughout — neither of those tests can see a route the
mirror has never heard of. `scripts/check_surface.py` closes that hole. It
parses the real table out of the platform repo whenever it happens to be checked
out next door, or wherever `MANDALA_PLATFORM_REPO` points, and says so and exits
0 when it is not — the ordinary case in CI here, and failing over it would make
this a check people learn to ignore. It recognizes that checkout by asking git
where it was cloned from rather than by which of its files are present, because
the second answer is fail-open in the one case that matters: a checkout missing
the parameter table is drift to report, not evidence that there is no checkout
to compare against.

The suite runs it too, and that is the part that matters: a script somebody has
to remember is the same hole one step further back. `pytest` skips it where the
platform is not checked out and fails on drift where it is, which covers every
machine this mirror actually gets edited on.

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
.venv/bin/python scripts/check_surface.py   # the drift check on its own, with output
```

## License

MIT — see [LICENSE](LICENSE).
