# Changelog

Notable changes to `mandala-computer`. Dates are release dates; the format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project is pre-1.0, so a minor version may carry a behaviour change.

The reasoning behind a change lives in its commit message rather than here.
This is the summary you read to decide whether to upgrade.

## [Unreleased]

### Added

- **Create a computer in a workspace, and list one workspace's computers, with
  an account-wide key**, sync and async. `computers.create()`, `launch()` and
  `ephemeral()` take `workspace_id`, which also names the create's secrets from
  that workspace's scope; `computers.list()` takes `workspace_id`, a workspace
  id or `"unassigned"` for the computers in none. A workspace the key cannot
  reach raises `NotFoundError`. Needs a platform that accepts `workspace_id` on
  these two routes; an older one creates in no workspace and lists everything.

- **An input action's context carries the page in Chromium**, sync and async.
  `InputContext.dom` is a `PageContext` — the page's URL and title and the
  interactive elements visible in it (`PageElement`: tag, role, name, text,
  `href` on links, and a box in screen pixels to click) — when the focused
  window is Chromium, and `None` otherwise. `InputContext.error` can now be set
  beside `windows`, saying why `dom` is `None` (another window focused,
  Chromium not listening, an older computer image); with `windows` of `None`
  it still means the windows could not be read. `dom` is the last field, with
  a default, so a positional `InputContext(...)` from an earlier release still
  works, and a platform without page context decodes as before.

- **`SshKey.reach` says whether this account accepts a listed key**, sync and
  async: `"everywhere"` (added from the dashboard), `"this_account"` or
  `"another_account"` (added through an API key or connected app on that
  account), relative to the account the API key acts on and never naming
  another. `None` from a platform that does not report it. It is
  keyword-only, after `raw`, so a positional `SshKey(...)` call from an
  earlier release still binds its eighth argument to `raw`.

### Fixed

- **A setting change resets `operation_id`, and an answer that is not a
  computer is refused rather than wiping the handle**, sync and async.
  `set_idle_suspend()`, `set_browser_proxy()` and `set_egress_proxy()` now
  replace `Computer.operation_id` with what their answer carried, as
  `rename()` and `resize()` already did, so after `start()` and then a setting
  change it is `None` rather than the start's id, which
  `operations.wait(c.operation_id)` would have waited on. `computers.create()`,
  `launch()`, `ephemeral()`, `get()`, `Computer.clone()`, `snapshots.clone()`
  and `refresh()` raise `MandalaError("expected a computer from <METHOD>
  <path>")` for an answer with no `id`, and `computers.list()` raises for a
  row with none, naming the row, instead of returning a computer whose id is
  `""` and whose next call fails with a `ValueError` blaming the caller. A
  `rename()`, `resize()` or setting change answered without a computer reads
  the computer again rather than blanking the handle. The platform does not
  send such answers; this matches `mandala-computer` for TypeScript.

- **`mandala-py` shows an SSH key's reach, and stops before `ssh` when no key
  you hold is accepted here.** `mandala-py ssh-key list` has a REACH column
  (`every account`, `this account`, or `another account (refused here)`) and,
  for a key bound to another account, prints on stderr what to do about it.
  `mandala-py ssh-key rm` of a key this API key cannot remove says so and names
  both causes (a key added from the dashboard, or one bound to another
  account, is removed from the dashboard) instead of the platform's bare
  "ssh key not found"; it is still `not_found` with status 404 and exit 1.
  `mandala-py ssh <computer>` refuses, before running ssh, when every key you
  hold is bound to another account, rather than leaving ssh to fail
  authentication. `--json` output is unchanged. The same as `mandala`.

- **The `operations` docs no longer say dashboard calls record none.** The
  platform records the dashboard's lifecycle calls the same way and lists them
  in `operations.list()`, all but a move. The `Operations` docstring and the
  README now say so.

- **`mandala-py ssh --setup` no longer reports success with a key bound to
  another of your accounts.** It used to see the fingerprint in the listing,
  print "already registered" and switch SSH on, and the gateway then refused the
  key. It now exits 1 with `ssh_key_elsewhere`, leaves SSH as it was, and says
  what to do: remove the key and add it again from the dashboard to use it on
  every account, or pass `--key` with a separate key.

- **`mandala-py secrets set` refuses a value typed where the NAME goes, and no
  `secrets` command repeats one.** `mandala-py secrets set "$GITHUB_TOKEN"`
  used to store the token as the secret's name, where every member who can
  list the scope reads it, and print it back. A NAME that looks like a secret's
  value (a known token prefix, an AWS key id, a UUID, a random-looking run, or
  random base64) is now refused with `invalid_arguments` before the value is
  read and before any request, as the TypeScript CLI refuses it; a real name
  the check misreads goes through with the new `--no-value-check`. `secrets rm`
  no longer quotes such an operand in its not-found error ("no secret with
  that name or id"), and prints only the id when the secret it deleted has a
  name shaped like a value.
- **`mandala-py secrets rm` reads the secret again when its revision moved**
  between the read and the delete, up to three attempts in all, instead of
  failing on the first 409, as the TypeScript CLI does. A retry deletes only
  the secret the first read chose: if the name now resolves to another secret,
  it deletes nothing more and fails with `conflict`.
- **`mandala-py secrets list` prints each secret's revision**, in a REVISION
  column after SCOPE, as it was documented to.
- **Every `mandala-py` command accepts `--json`, and reports `mandala`'s error
  codes.** `webhooks create/get/update/delete/rotate/test`, `secrets rm`,
  `ssh-key rm` and `scp` refused the flag as a usage error. They take it now:
  the webhook verbs that already printed JSON print the same, `webhooks delete`
  prints `{"id", "deleted": true}`, `secrets rm` `{"id", "name", "deleted":
  true}` (no `name` when it looks like a value), `ssh-key rm` `{"id",
  "removed": true}` and `scp` `{"source", "destination", "bytes",
  "confirmed"}`; an upload whose reported count differs from what was sent
  is refused with `invalid_arguments` ("upload was incomplete"), with or
  without `--json`, as `mandala` refuses it, rather than reported as sent. A local credential refusal is reported under its own code
  (`missing_credentials`, `invalid_base_url`, ...) rather than `failed`;
  `terminal --json` is refused with `unsupported_mode` (exit 1) rather than as
  an unknown option, and `ssh --json <computer>` with an `unsupported_mode`
  JSON line (exit 2) rather than plain usage text. An `ssh` usage error is JSON
  whenever `--json` came before the computer, not only with `--setup`.
- **`mandala-py browser-proxy` and `egress-proxy` `set` and `clear` print the
  computer's record with `--json`**, as `mandala` does, without its `vnc`
  desktop URLs, instead of four of its fields. `get --json` still prints the
  setting with the computer's `id` and `name`.

### Changed

- **A request timeout now raises `RequestTimeoutError`, a
  `ConnectionInterruptedError` (as in the TypeScript SDK) that is still a
  `TimeoutError`**, sync and async. A deadline that fires says nothing about
  whether the request went out, so `except ConnectionInterruptedError` written
  to resend under the same `idempotency_key` now sees it; `except TimeoutError`
  and `except ConnectionError` both still catch it, `is_transient()` still
  answers `False`, and a keyed call's timeout still carries its key. A
  `wait_*` helper that gives up still raises a plain `TimeoutError`. Under
  `--json`, `mandala-py` reports a request timeout with the code
  `connection_interrupted` rather than `timeout`, as `mandala` does.

- **The SSH documentation says what the platform does**, sync and async.
  `ssh_keys.remove()` said a session already open with the removed key goes
  on until it disconnects; the platform closes it once each computer receives
  the new key list, within moments. `set_ssh_access()` and
  `SshAccess.key_count` said every owner's and member's key logs in; keys bound
  to another account and the keys of a member whose seat is suspended do not,
  and viewers' keys never do. `ssh_access()` said it is answered without
  waiting on the computer's host; it asks the host (after retrying any pending
  key push), so it can take a few seconds, though an unreachable host does not
  fail it. Documentation only.

- **Deleting a workspace is documented as the platform does it: only an
  empty one.** `workspaces.delete()`, `WorkspaceDeleted`, the README and
  `mandala-py workspaces rm`'s help and confirmation said a deleted
  workspace's computers were kept. The platform refuses to delete a workspace
  that still holds computers, with `ConflictError` (409) saying how many, and
  deletes and revokes nothing; when it cannot confirm the workspace is empty it
  answers `UnavailableError` (503), also with nothing deleted. Documentation
  only.

- **The `ssh_keys` documentation says a key added through the API is bound to
  its credential**, sync and async. A key registered with
  `client.ssh_keys.add()` now reaches only the account the API key (or
  connected app) acts on, and is removed when that credential is revoked; a
  key added from the dashboard still reaches every account where you are an
  owner or member. No API change.

- **`ssh_keys.remove()` documents that it removes only a key bound to this
  account**, sync and async. With an API key or connected app, the platform now
  removes only a key whose `reach` is `this_account`. A key added from the
  dashboard, or with an API key before keys were bound to an account (`reach`
  `everywhere`), raises `NotFoundError` like a key bound to another account, so
  a leaked credential cannot take away SSH access you use on your other
  accounts; remove such a key from the dashboard. `mandala-py ssh-key rm`
  behaves the same way. Documentation only; no SDK change.

- **The `builds` and `templates` documentation says which methods need an
  account-wide key**, sync and async. The platform refuses an API key confined
  to a workspace with a 403 (`PermissionDeniedError`) on every build route and
  on publishing, reading the account's own template back and retiring one;
  such a key can still list templates, read `system` ones and launch a
  computer from a template by its ref. `builds.start()` no longer says build
  secrets resolve in the key's scope: they resolve among the account-wide
  secrets. `builds.list()` no longer describes what a workspace-scoped key
  sees in a build listing. Documentation only; no API change.

- **A bound `computers.launch()` returns within moments of its secrets
  landing**, rather than up to a whole poll interval later, sync and async.
  `wait_for_secrets`, `wait_for_browser_proxy`, `wait_for_egress_proxy` and
  `wait_for_desktop` (and the matching `launch()` stages) now poll 0.25s after
  their first read, doubling up to `poll`, which stays the ceiling and keeps
  its default. A poll that failed still waits `poll`, or the `Retry-After` the
  platform sent.

- **`Webhook.disabled_reason` documents `"plan"`.** When the account moves to
  a plan without webhooks the platform disables its subscriptions with
  `disabled_reason="plan"`, refuses `enabled=True` with a 402 meanwhile, and
  enables them again, oldest first up to the plan's allowance, when a plan with
  webhooks is chosen. `enabled` and `disabled_reason` used to name only
  `"customer"` and `"failing"`. The field stays a plain string, and
  `is_failing` stays false for `"plan"`. Documentation only; no API change.

## [0.9.0] — 2026-09-30

### Changed

- **`computers.launch()` now waits for the desktop session** on a Linux
  computer, sync and async, after its guest, secrets and proxy waits and
  inside the same readiness budget. The guest agent answers a few seconds
  before the desktop user is logged in, and an `exec(..., desktop=True)` sent
  in between was refused with a `ConflictError` "no active desktop session". A
  computer that never gets a desktop session now makes `launch()` raise a
  `TimeoutError` instead of returning.
- **`reason` words `name_taken` and `stale_revision` are permanent.** The
  secret store's 409s for a name the account already uses and for a stale
  `revision_id` now carry them, and `is_transient()` answers `False` for a 409
  carrying either rather than calling it a conflict worth sending again.
  `Secrets.set()` still reads again and retries as before.
- **413 stays `FileTooLargeError`** (the TypeScript SDK calls it
  `TooLargeError`); 502 stays a bare `APIError`, and 521-523 stay
  `OriginUnreachableError` with the SDK's own wording, as in the TypeScript
  SDK. Now pinned by tests.
- **`mandala-py logout` with no saved profile exits 0** and says `Not logged
  in; nothing to remove.` (with `--json`, `removed: false`). A named profile
  that is not saved while others are is still `not_logged_in`.
- **`mandala-py workspaces get` and `workspaces members` take a workspace
  name** as well as an id, resolved as `rename` and `rm` resolve one: an id is
  sent as it is, a name that fits one workspace becomes its id, and a name
  that fits more than one is refused (`ambiguous_workspace`) with their ids.

### Added

- **`mandala-py move COMPUTER --ram-mb N [--cpu N] [--disk-gb N] [--wait]` and
  `mandala-py moves list [--computer C]`**: move a stopped computer to another
  host in its region that can run a size its own host cannot (the step a
  resize refused with `move_required` offers), and list the moves running or
  finished in the last day. With `--wait`, `move` exits 0 only when the move's
  state is `done`; `moved`, `failed` and `lost` exit 1 with a note. `move` is a
  top-level verb, as `terminal` and `scp` are: this CLI has no `computers`
  group.
- **`mandala-py secrets get NAME|ID [--workspace ID]`**: one secret's metadata
  (id, name, scope, revision and dates, never its value), found by name or id
  as `secrets rm` finds one, in the saved default workspace when `--workspace`
  is not given.
- **`mandala-py workspaces use` and `workspaces current`**: save a default
  workspace, by name or id, for a saved profile; `secrets list`, `set` and
  `rm` and `api-keys create` then use it when `--workspace` is not given (an
  explicit `--workspace` wins; `workspaces use --clear` goes back to
  account-wide). `current` says which workspace applies and why: the key's own,
  the profile's default, or none. The default is kept in a new
  `~/.mandala/defaults.json`, keyed by profile and account and shared with the
  npm `mandala` CLI, so `credentials.json` and older readers of it are
  unaffected. `use` is refused with `MANDALA_API_KEY` set, and for another
  workspace when the profile's key is confined to one; `logout` removes the
  profile's default. A `defaults.json` that cannot be read is ignored with a
  note by `secrets list`, but `secrets set`, `secrets rm` and `api-keys create`
  refuse without sending anything rather than act account-wide (pass
  `--workspace`, or fix or delete the file).
- **Workspace create, rename and delete**: `client.workspaces.create(name)`,
  `rename(workspace_id, name)` and `delete(workspace_id)` (sync and async), and
  `mandala-py workspaces create`, `rename` and `rm --yes` (by name or id). They
  need an owner's account-wide key; a key confined to a workspace is refused
  with `PermissionDeniedError`. Deleting a workspace revokes every API key
  confined to it and returns a `WorkspaceDeleted` whose `revoked_keys` says how
  many; its computers are kept. `mandala-py workspaces rm` without `--yes` is
  refused before any request (`confirmation_required`). A `rename` or `rm`
  target shaped like a workspace id (`wsp-` and twelve hex) is always sent as
  that id, never matched against workspace names, so a retried `rm` of an
  already-deleted workspace answers not found instead of deleting one named
  like it. Needs a platform that has these routes; an older one answers 405.
- **A click repeat count**: `click`, `right_click` and `middle_click` (sync
  and async) take `count=`, 1 to 10, pressing the button that many times at
  double-click pacing. Needs a platform that accepts `count`; an older one
  answers 400.
- **Post-action window context on every input action** (sync and async): the
  clicks, `move`, `drag`, `mouse_down`, `mouse_up`, `scroll`, `paste`, `key`,
  `hold_key` and `wait` take `context=True` and then return an `InputContext`
  — the windows `windows()` lists by default and the `focused` one, as they
  stand just after the action — instead of `None`. `type(..., context=True)`
  returns a `TypeResult` with the `mechanism` and the `context`; without
  `context` it returns the mechanism as before. When the windows cannot be
  read, `windows` is `None` and `error` says why; the action still happened.
  New exports: `InputContext`, `TypeResult`.
- **A `User-Agent` naming this SDK and its version**:
  `mandala-computer-py/<version> python/<version> httpx/<version>`, in place of
  httpx's default, so the platform can tell which client and release sent a
  request. The new `user_agent=` option on `Client` and `AsyncClient` appends
  your own token, such as `my-app/1.2`.

- **`mandala-py operations list|get|wait`**, as `mandala operations` has them:
  `list` newest first with `--computer` (a name or an id), `--idempotency-key`,
  `--limit` and `--cursor`; `get ID`; and `wait ID` with `--timeout-ms` and
  `--poll-ms`, exiting 1 with `operation_failed` when the operation fails.
  Each takes `--json`.

- **`Computer.wait_for_desktop()`** (and the `AsyncComputer` twin) polls a
  no-output `true` in the desktop session until it finishes with exit 0; a
  probe that times out inside the guest is polled through, not taken as a
  session. It returns at once for a Windows guest or a computer whose `os` is not reported. An absent
  `desktop` field is an X11 desktop and is waited on.

- **`Computer.delete(delete_snapshots=...)`** (and the async twin), the name
  the other clients use for `purge_snapshots=`, which still works and is
  deprecated in the docstring only. Both with different values raise
  `TypeError`.
- **`usage.read(from_=..., to=...)`**, the wire's names for `since=`/`until=`.
  Naming one bound both ways raises `TypeError`.

### Fixed

- **`computers.launch()` resent under its key no longer trusts a replayed
  create answer.** A create answered with `Idempotent-Replayed: true` is the
  first call's, up to 24 hours old, so launch now reads the computer afresh
  before acting on it: a start that attempt reported failed is sent again
  instead of raising "did not start" at once, and a computer stopped or
  suspended since is started instead of waited on until the budget ran out.
- **A failed `launch()` carries the create's `idempotency_key`**, not the key
  of the later stage that failed (such as the start's). Resending `launch()`
  with `idempotency_key=err.idempotency_key` returns the same computer; the
  start's key, sent on a create, would have built a second one.
- **`ephemeral()` whose block deleted the computer itself** no longer raises
  `NotFoundError` on the way out, and no longer warns that the computer is
  "still running and still billable" when the block raised. A 404 from the
  cleanup delete is success.
- **A failed read after `start()`, `stop()`, `suspend()` or `restart()`** now
  raises a `MandalaError` saying the action succeeded and must not be sent
  again, with the read's error as its cause, instead of a `ConnectionError`
  that `is_transient()` called worth retrying (a retried restart resets the
  computer twice).
- **Error messages from RFC 9457 bodies**: a `{title, detail}` body, which an
  edge in front of the platform answers with, now gives `detail` (or `title`)
  as the message instead of `HTTP <status>`. Statuses this SDK words itself
  keep that wording.
- **A base URL with a query or a fragment is refused**, given to the
  constructor or in `MANDALA_BASE_URL`, with the same `CredentialError` a
  credential file's base URL gets. Every path is joined onto it as a string,
  so a query swallowed the path after it.
- **`scroll()` `amount` is capped at 50 and `wait()`/`hold_key()` at 30
  seconds**, the platform's own limits, and refused locally with `ValueError`
  before a request is made.
- **A `write_file()` body over 64 MiB raises `FileTooLargeError`**, the class
  the platform's 413 arrives as, instead of a bare `ValueError`. It is also a
  `ValueError`, so existing handlers still catch it.

### Documentation

- The `api-keys` CLI help, the README and the `ApiKeys` and
  `PermissionDeniedError` docstrings name the real dashboard control that
  allows a key to manage keys: Settings → Credentials → API keys → the key's
  menu → **Allow managing keys**. They used to point at a "Manage keys"
  checkbox that does not exist.
- The `secrets` argument's docstring on `computers.create()` (sync and async)
  now points to Settings → Credentials → Secrets, where the account's secrets
  are kept, instead of a Secrets tab under Settings that no longer exists.

## [0.8.0] — 2026-09-29

### Added

- **`computers.launch()` takes `idempotency_key=`**, sync and async, sent on
  its create only. After a dropped connection or a timeout on that create,
  launching again with the same arguments and the error's `idempotency_key`
  answers the first create's computer, and launch carries on waiting for it.
  A start launch sends itself and the waits after it are other calls: their
  failures carry the start's key or none, so recover from them through the
  computer's id, which the error's message names. This makes the 0.7.0
  entry's "`computers.create` (and so `launch`)" true of the keyword too.
- **`ModelProviderError`**, an `APIError` raised by `agent()` and
  `agent_once()` (sync and async) for a 402. On the agent routes a 402 is the
  model API's `billing_error` for the account behind `model_key`, relayed as
  it came, not the Mandala plan; see Changed. It is also raised for a 404 or
  413 the model API answered; see Fixed.
- **`screenshot_info()`**, sync and async: `screenshot()`'s request, answered
  as `ScreenshotInfo(data, content_type, suspended)`. `suspended` is the
  platform's `X-GC-Frame: suspended` marker for a suspended computer's saved
  frame, which the bytes cannot show when `width` is set.
- **`AgentResult.steps_taken`**: the steps an `agent_once()` run took, from the
  `steps_taken` its body carries. Empty on a streamed result.
- **`RateLimitError.limit`, `.remaining` and `.reset`**, from the
  `RateLimit-Limit`, `RateLimit-Remaining` and `RateLimit-Reset` headers; each
  `None` when absent, or when the value is not a plain whole number of at most
  18 digits. The README shows how to read the same headers on a successful
  answer with an `httpx` event hook, and says that such a hook turns off the
  SDK's opt-in retries of connection failures seen before a response.

### Changed

- **`mandala-py` spells an escaped character as the TypeScript CLI does**:
  `\uXXXX` with four lowercase hex digits, so a newline in a printed name or
  error message now reads `\u000a` rather than `\x0a`, and ESC `\u001b`
  rather than `\x1b`. It also escapes the three bidi marks it missed, U+061C
  (Arabic letter mark), U+200E and U+200F (left-to-right and right-to-left
  marks), so it now escapes everything the TypeScript CLI does. One
  difference remains: `mandala-py` also escapes the Unicode line and
  paragraph separators, as `\u2028` and `\u2029`, which the TypeScript CLI
  prints as they are. `--json` output is unchanged.
- **A 402 from `agent()` or `agent_once()` raises `ModelProviderError`, not
  `PlanLimitError`.** Nothing inside an agent run consults the plan: the
  platform's mid-run recheck covers the credential, the role and the account's
  standing and answers only 401 or 403 with reason `revoked`, and a 402, 504
  or 529 there is the model API's own status for the account behind
  `model_key`. The docs no longer say a plan downgraded mid-run stops a run.
  `is_transient()` still answers `False`.
- **A model API 504 on `agent_once()` raises a plain `APIError`, not
  `GatewayTimeoutError`.** The platform relays the model's `timeout_error`
  with the run's usage and steps in the body (on `e.agent`), so the connection
  was not cut; `agent()` already reported the same 504 this way. A body-less
  504 or 524 is still `GatewayTimeoutError`.
- **`open()` picks a browser the image has, and raises when none starts**
  (OPL-3705). It used to run `nohup firefox <url> &`, which exits 0 whether
  or not a `firefox` exists, so on the Omarchy image — Chromium and no Firefox
  — it opened nothing and reported success. It now uses the first of
  `firefox-esr`, `firefox` and `chromium` that `command -v` finds, looked up
  before the launch is detached, and raises `MandalaError` on a non-zero exit
  (127 with `no browser (firefox-esr, firefox or chromium) on this image` when
  there is none) or a timed-out wait.
- **Template `publish()` 409s are permanent.** The four publish refusals — a
  different document under a published ref, a retired ref, and the two
  per-account ceilings — clear by nothing but a changed request, and came with
  no `reason`, so `is_transient()` called them worth retrying. `publish()`
  now raises them as a `ConflictError` whose `reason` is the platform's own
  permanent word, or `"exists"` where it sent none or sent a word this version
  does not recognise. A clearing word (`contention` or `starting`) is kept as
  the platform sent it, and `is_transient()` answers `True` for it.
- **`wait_until_running()`, `wait_for_guest()`, `wait_for_secrets()`,
  `wait_for_browser_proxy()`, `wait_for_egress_proxy()` and `launch()` raise at
  once on a `half-removed` computer** — one whose deletion stopped partway and
  took its disk — instead of polling to their timeout or telling the caller to
  call `start()`. Deleting it again is what clears it.

### Fixed

- **A 404 or 413 the model API answered on `agent()` or `agent_once()` is
  raised as `ModelProviderError`** (sync and async), not `NotFoundError` or
  `FileTooLargeError`. The platform relays the model API's own failures with
  the message prefixed `model API: `; a 404 there is usually a model name the
  provider does not know, and a 413 a request it found too large, not a
  missing computer or a transfer to page. The platform's own 404 and 413 on
  these routes carry no prefix and keep their classes. `agent_stream()` still
  yields the failure as an `AgentFailed` event.
- **`is_transient()` answers `False` for a 429 the model API answered an
  agent run with after the run had already taken steps** (`agent()` and
  `agent_once()`, sync and async). The wait is the model provider's, but the
  steps are on the desktop, and sending the same prompt again repeats them;
  read `e.agent` before running again. Steps count as taken when the error
  body lists them, or, on `agent()`, when the stream delivered a step before
  the failure. The same relayed 429 before any step, and the platform's own
  429 on the agent routes, are still `True`.
- **A 429 the model API answered on `agent_once()` has `limit`, `remaining`
  and `reset` set to `None`** (sync and async), as a streamed one already
  did. They came from the platform's `RateLimit-*` headers, the caller's
  Mandala budget, which did not refuse the call. `retry_after` stays: it is
  the model API's own wait, forwarded. The platform's own 429 on the agent
  routes keeps all three.
- **`mandala-py` escapes control and bidirectional characters in every name,
  id, timestamp and error text it prints, not only in `whoami`.** Every
  listing (secrets, webhooks and their deliveries, SSH keys, API keys,
  workspaces, members) escapes each cell; the lines `secrets set`/`rm`,
  `ssh-key add`, `ssh-access`, `ssh --setup`, `browser-proxy` and
  `egress-proxy` print escape the values they quote; and so does every
  refusal that names a computer, a secret or the platform's reason, such as
  the list of computers after an unknown name. A computer, secret or key
  named with a right-to-left override (U+202E), an escape sequence or a
  newline now prints as `\u202e`, `\u001b` or `\u000a` rather than
  reversing, driving or forging part of the terminal's output. `--json`
  output is unchanged.

- **`mandala-py ssh`, `ssh --setup` and `ssh-config` refuse a computer id
  OpenSSH could misread**, rather than handing it to `ssh` or writing it into
  `~/.ssh/config`. The id is ssh's destination and `HostKeyAlias` and the
  block's `HostName`, so one holding a newline could have added a directive
  such as `ProxyCommand`, and one starting with `-` would have been read as
  an option. The refusal names the id escaped, under the `--json` error code
  `invalid_response`, before anything is registered, printed, written or run.
  Every id the platform issues is accepted as before.

- **`mandala-py ssh-config` no longer writes a computer's name as its `Host`
  when ssh would also read that name as another destination**: a dotted
  name shaped like a hostname (one ending in a dot, as `github.com.` does, or
  in an all-letter or `xn--` label, as `github.com` and `corp.internal` do),
  an IPv4 address in any form the resolver reads (`10.5` is `10.0.0.5`), a
  bare decimal or `0x` number, `localhost`, the gateway's alias
  `mandala-gateway`, or another computer's id, compared without regard to
  case as OpenSSH does. A block under such a name took over every connection
  made there, so a computer a teammate named `github.com` received the
  pushes meant for GitHub. The block uses the computer's id instead, and a
  note on stderr says so, with the name escaped; for such a name the
  `--json` output's `host` and `config` carry the id rather than the name,
  and `--write` replaces a block written earlier under the name with one
  under the id, so `ssh <name>` stops reaching that computer and
  `ssh <id>` does. Other names, dotted ones such as `ubuntu-24.04` or
  `py3.12` included, are used as before.
  Upgrading changes nothing in `~/.ssh/config` by itself: `--write`
  replaces only the block of the computer it is run for, so a block an
  earlier version wrote under such a name stays until you run
  `mandala-py ssh-config <computer> --write` again for that computer. To
  find one, read the `Host` line after each
  `# >>> mandala computer <id> >>>` marker in `~/.ssh/config`, and run
  `--write` again for any whose `Host` is a hostname, an IP address, a bare
  number, `localhost`, `mandala-gateway`, another computer's id, or a name
  another computer also has (for a computer that no longer exists, delete
  its block, markers included).

- **`mandala-py ssh-config` refuses as a `Host` every IPv4 form macOS
  reads**, not only those `inet_aton` takes: a part with a leading zero and
  an `8` or `9` (`08.0.0.1` is `8.0.0.1` there, and `192.168.1.09` an
  address), an empty `0x` part (`0x.1` is `0.0.0.1`), and a part too big for
  its bytes. A block under such a name took over the connections macOS's ssh
  made to that address. The check no longer asks the local `inet_aton`, so
  the same names are refused on every platform. The block uses the
  computer's id instead, with the same stderr note as for the other refused
  names.

- **`mandala-py ssh-config` uses the computer's id as its `Host` when a
  block already in `~/.ssh/config` uses the name for another computer**, as
  its `Host` or as its id, compared without regard to case: one written with
  another account's API key, say, which the listing the name was checked
  against does not show. `ssh <name>` went to whichever of the two blocks
  came first. A note on stderr says so, with the name escaped, and `--json`'s
  `host` and `config` carry the id. The computer's own earlier block never
  counts, and `--write` still replaces it where it stands. The file is only
  read, in print mode too; one that is missing or cannot be read holds no
  blocks. When the id itself is already another computer's `Host` there (one
  named after this computer's id, say), there is no other name to use, so
  `ssh-config` refuses, in every mode, with a `conflict` error naming that
  computer: it prints and writes nothing, since a second block under the id
  would never be reached and `ssh <id>` would go to the other computer.
  Remove that block, then run again.

- **`mandala-py whoami` escapes every field it prints**, not only the names
  and email: the user, account, workspace and key ids, the account's status
  and the role are the platform's text too, and a control character in one
  of them reached the terminal raw. Each line is now escaped whole.

- **`mandala-py` names a failed call's ids in text mode too**, as `--json`
  already did: after the error line, a second stderr line gives the operation
  id, idempotency key and request id the failure carries (each only when
  present), escaped as printed names are. The error line itself is escaped
  the same way, since its text can come from the response: a newline or
  escape sequence in it can no longer forge a line or drive the terminal.

- **`Computer.schedule()` / `AsyncComputer.schedule()` no longer overwrite
  `snapshot_schedule`** with their answer, which reported a computer with no
  schedule as a disabled 00:00 UTC window. The property is the computer
  record's field: `refresh()`, `set_schedule()` and `clear_schedule()` update
  it.

- **`drag()` and `scroll()` refuse `modifiers` given as a single string**,
  sync and async, with a `ValueError` before any request, instead of holding
  each of its letters: `modifiers="shift"` was sent as `s+h+i+f+t`. Pass a
  tuple such as `("shift",)`. The click methods take their modifiers as
  separate arguments and are unchanged.

- **`mandala-py ssh-config` reads a `~/.ssh/config` that holds a byte that
  is not UTF-8**, such as a Latin-1 comment. Such a file read as holding no
  blocks at all, so a name or id another computer's block already used went
  unnoticed in print and `--json` modes. The file is read in the system's
  text encoding, as `--write` reads it, so a block `--write` moves keeps its
  bytes; only when that fails is the byte read as a replacement character,
  and the blocks around it are found. `--write` still fails on such a file,
  as before.

- **`mandala-py ssh-config` counts every alias of a hand-edited block's
  `Host` lines**, read the way OpenSSH reads them: `Host dev # mine`,
  `Host other dev`, `  host=dev`, `Host "dev"`, `Host 'dev'` and a second
  `Host` line all put `dev` in that block. Only spaces and tabs separate
  aliases, as for ssh: a no-break space does not, so the `#` after one starts
  no comment. Only the whole first `Host` line counted before, so a name
  such a block used could still be written under, and `ssh <name>` went to
  whichever block came first. Negated patterns (`!dev`) do not count, and a
  wildcard pattern is compared as written, not expanded.

- **`mandala-py ssh-config` no longer refuses forever when two computers
  are named after each other's ids** (say `vm-1` named `vm-other` and
  `vm-other` named `vm-1`, in two accounts). The one written second fell
  back to its id, which the first one's block held, and removing that block
  only moved the refusal to the other computer. `--write` now puts both
  under their ids in one write: it changes only the other block's `Host`
  line, to its id, and a note on stderr says so. Without `--write` it still
  refuses, and the `conflict` error says that `--write` moves both. A block
  with more than the one alias the CLI writes, or whose id another block
  uses as a `Host`, is refused as before, and so is a config holding a byte
  that is not valid in the system's text encoding, which `--write` cannot
  rewrite: that error says to fix the byte first.
- **`mandala-py ssh-config --write` removes a second copy of the computer's
  block or the gateway's block**, one an earlier `mandala` CLI appended to a
  `~/.ssh/config` saved with CRLF line endings, instead of leaving it routing
  its old `Host` alias. A copy followed by a line of your own before the next
  `Host` or `Match` line is left in place, since that line belongs to the
  copy's `Host` and removing the copy would apply it to other hosts. A later
  copy whose `# <<< mandala … <<<` line was deleted is left in place, with
  every copy after it.

### Documentation

- **Idempotency after a `5xx` (corrects the 0.7.0 entry).** 0.7.0 said a key
  never answers the result after any `5xx`. That holds only for a `5xx` that
  names an `operation_id`: the key is spent, and resends answer `409
  idempotency_outcome_unknown`. A `5xx` naming none may have been refused
  before the call was sent anywhere, which releases the key, and a resend is
  carried out. Resending under the same key is safe after any `5xx`. Keys are
  kept per credential scope, so `operations.list(idempotency_key=...)` finds
  only operations a credential of the same scope reserved.
- A replayed create, clone or rename answer has `vnc` `None`: the platform
  strips desktop credentials from stored answers; `refresh()` fetches them.
- `agent_once()` on the hosted API is cut at about two minutes with a
  body-less 524; the run is stopped and its usage and steps are lost, so the
  "work usually carries on" advice for `GatewayTimeoutError` does not apply.
- `snapshot()` documents the admission 503: manual captures on one account are
  admitted one at a time, and after a 503 look for a `capturing` row before
  retrying.
- `builds.start()` and the README document `spec.secrets` (by id and `as`,
  resolved in the key's scope and frozen at submit, at most 32) and which of
  its refusals are worth retrying; `spec.from` must be a `system/...`
  template; a document with `spec.env` counts as one to build.
- `build_digest_needs` quotes the platform's current sentence; the SDK no
  longer claims it says how or where to compute the digest.
- `set_idle_suspend(0)` is documented: never suspend, capped per plan, with
  `PlanLimitError` past the cap; the maximum is 10080 minutes.
- `Computer.status` documents `half-removed` and the closed set of six.
- `Snapshot.state` no longer claims a listing puts `capturing` rows first;
  `ApiKeys.create` and `mandala-py api-keys create --name` state which
  characters a key name may not contain.

## [0.7.0] — 2026-09-27

### Added

- **An egress proxy for all of a computer's outbound TCP:** `egress_proxy=`
  on `computers.create` and `launch` (an `EgressProxyArgs`: `server`, and
  optionally `credentials_secret_id`), `Computer.set_egress_proxy(proxy |
  None)` to replace or remove it, and the `egress_proxy` / `egress_proxy_pending`
  properties, sync and async. The server is `http://`, `https://` or
  `socks5://` with an explicit port; `credentials_secret_id` names a secret
  holding `user:password` that the computer's host signs in with and the
  computer never receives. It fails closed, drops UDP to the internet and
  ICMP, and does not proxy DNS. A key an egress proxy does not have (such as
  `bypass`) is a `ValueError` before a request is sent. The `mandala-py` CLI
  gains `egress-proxy get|set|clear`, whose `set` keeps the current
  credentials for an unchanged server as `browser-proxy set` does. New
  exports: `EgressProxy`, `EgressProxyArgs`.

- **`Idempotency-Key` on every lifecycle call:** `computers.create` (and so
  `launch`), `clone`, `start`, `stop`, `suspend`, `restart`, `rename`,
  `resize`, `set_idle_suspend`, `set_browser_proxy`, `relocate`, `delete`,
  `snapshots.restore` and `snapshots.clone` send one, sync and async — a fresh
  random key per call, or your own through the new `idempotency_key=` keyword
  (1 to 255 printable ASCII characters, no spaces; anything else is a
  `ValueError` before a request is sent). An exception that leaves the outcome
  unknown — a dropped connection or timeout after the request went out, a
  `5xx`, or the platform's `409` `idempotency_in_progress` /
  `idempotency_outcome_unknown` — carries the key as `idempotency_key`. After
  a dropped connection, a timeout or `idempotency_in_progress`, calling the
  same method again with it answers the first call's result instead of doing
  it twice. After a `5xx` it never does: every resend under that key raises
  `ConflictError` with code `idempotency_outcome_unknown`, so read the
  computer, or the operation the error names, instead of resending — and do
  not resend under a new key, which may do the call a second time.
  `operations.list` takes `idempotency_key=`,
  `Operation.idempotency_key` says which key started one (`None` when none, or
  on an older platform), `delete` is a documented kind, and `is_transient` is
  `False` for `idempotency_outcome_unknown`.

- **Lifecycle operations:** `client.operations.get(operation_id)`,
  `list(computer_id=, limit=, cursor=)` and `wait(operation_or_id, timeout=,
  poll=)`, sync and async, over the platform's `GET operations` and
  `GET operations/{id}`. `wait` returns on `succeeded` and raises the new
  `OperationFailedError` (with the platform's `code` and `detail`) on
  `failed`. `succeeded` means the platform finished its step, not that the
  desktop has booted: keep `wait_for_guest()` for that. `kind` and `state` are
  open strings, since the platform adds kinds. The id is surfaced as
  `computer.operation_id` (after a create, a clone, and each start, stop,
  suspend, restart, rename or resize through the handle; a refresh keeps it),
  `move.operation_id` on what `relocate` accepted, and on
  `snapshots.restore()`, which now returns a `LifecycleAck` rather than
  `None`. New exports: `Operation`, `OperationError`, `OperationPage`,
  `LifecycleAck` and `OperationFailedError`.
- **A proxy for a computer's browsers:** `browser_proxy={"server": ..., "bypass":
  [...]}` on `computers.create()` and `launch()`, and
  `computer.set_browser_proxy(proxy)` where `None` removes it (sync and
  async); `computer.browser_proxy` (a `BrowserProxy`) and
  `computer.browser_proxy_pending` read it back, and
  `computer.wait_for_browser_proxy()` waits until the guest has it (its
  `expect_browser_proxy` option waits past a read that leaves the setting out,
  and a computer with none whose start is admitted is waited on until it runs,
  so a proxy removed while it was stopped is gone first). `launch()` waits for
  it when the create carried one. Which proxies are accepted is the platform's
  rule, so a refused value is its 400, not a check here. The CLI takes
  `mandala-py browser-proxy get | set COMPUTER URL [--bypass LIST] [--wait] |
  clear COMPUTER [--wait]`; `--wait` on a stopped computer with no start under
  way says the change is stored rather than failing, and an empty bypass entry
  is refused. New exports: `BrowserProxy` and `BrowserProxyArgs`.
- **Screenshot shaping: `region`, `scale`, `format` and `quality`** on
  `computer.screenshot()` (sync and async), over the platform's new query
  parameters — a crop `(x, y, width, height)` in screen pixels, a shrink factor
  in (0, 1], `"png"` or `"jpeg"`, and a JPEG quality of 1-100 — for a cheaper
  frame to hand a model. A scale beside a width, a quality on a PNG and
  malformed values raise `ValueError` before anything is sent. A suspended
  computer refuses a crop, a scale, a PNG or a quality with `ConflictError`
  whose `reason` is `"unavailable"`.
- **`client.api_keys.list()`, `create(name=, workspace_id=)` and
  `revoke(key_id)`**, and **`client.account.whoami()`** (sync and async), over
  the platform's new `GET whoami` and `GET|POST api-keys`,
  `DELETE api-keys/{id}`. The key routes need the calling key's opt-in "Manage
  keys" permission, granted only from a dashboard session; without it they
  raise `PermissionDeniedError` carrying the platform's sentence. A minted key
  is answered once, as `ApiKeyCreated.key`, and never has the permission
  itself.
- **`mandala-py whoami`, `api-keys list | create | revoke`, `logout` and
  `--version`.** `logout` forgets one profile saved by `mandala login`, under
  the same lock file, and prints the id of the key it held, which stays valid
  until revoked; `api-keys create` prints only the new key on stdout. For a
  workspace-scoped key, `whoami` names what the platform withholds from it
  (the user's name and email, the account's name and plan) instead of
  printing them empty: `User usr-1 (name and email withheld from a
  workspace-scoped key)` rather than `<> (usr-1)`, and `Account: acc-1,
  active (name and plan withheld …)` rather than `(unnamed)` and `plan ,`.
- **`computer.wait_for_secrets()`** and **`computer.secrets_delivering`** (sync
  and async). A computer comes back `running`, and its guest answers, a few
  seconds before its secrets land. The wait polls until the platform's
  `secrets_delivering` is false (or, on a platform that predates it, until the
  receipt names the latest delivering start), and raises instead of waiting out
  its timeout for a delivery that failed, a stopped computer the platform says
  has no start admitted, or a create's computer whose first start failed
  (`start_error`, kept past the refresh that clears it); a host that does not
  say is waited on. `expect_secrets=True` tells it secrets are bound, so a read
  that leaves the bindings out is waited past rather than taken for "nothing
  bound" — unless that read says outright that nothing is starting, which is
  refused as above. A restart delivers bound secrets again and reads `running`
  before they land. Called after `restart()`, it waits for them on a platform
  that reports that redelivery as `secrets_delivering`; on one that does not,
  `secrets_delivering` may read false before the values land, and on a
  platform that predates the field the wait falls back to the receipt as
  above. Either way the wait can return before the values land.
- **`mandala-py --json` failures carry an error code.** A failure under `--json`
  writes `{"error": {"code", "message", "status"?, "reason"?}}` as one line on
  stderr, with nothing on stdout. `code` is one snake_case word, the same
  vocabulary as the npm `mandala` CLI (`not_found`, `unauthenticated`,
  `conflict`, `invalid_arguments`, …). A usage error is one too, `ssh --setup`
  included, and `--json` counts however argparse lets it be abbreviated.
- **A browser proxy's credentials:** `credentials_secret_id` on `BrowserProxy`
  and `BrowserProxyArgs`, the id of a secret whose value is `user:password`
  for an upstream that asks for one. It is read back and sent on, so
  `set_browser_proxy(computer.browser_proxy)`, or a `dataclasses.replace()` of
  it, keeps the credentials; before this the read dropped the id and a
  mapping carrying it was refused, so that read-modify-write removed them and
  every browser on the computer was then answered 407 by its upstream. The
  secret must be bound to the computer as a file, and a change that leaves the
  id out removes the credentials, since the setting is replaced whole. A value
  that is not a secret's id raises `ValueError` before any request.
  `mandala-py browser-proxy set` keeps the proxy's current credentials when
  the server is unchanged, unless given `--credentials SECRET_ID` or
  `--no-credentials`; a set that names a different server with neither is
  refused before any change, since the credentials are sent to the proxy on
  every request and belong to the server they were set for. `get` and `set`
  print which secret it uses.

- **Workspaces:** `client.workspaces.list()`, `get(workspace_id)` and
  `members(workspace_id)`, sync and async, over the platform's
  `GET workspaces`, `GET workspaces/{id}` and `GET workspaces/{id}/members`.
  `get` of an id the key cannot see raises `NotFoundError`; `members` raises
  `PermissionDeniedError` for a key confined to a workspace. New exports:
  `Workspace`, `WorkspaceMember`.
- **`mandala-py workspaces list | get ID | members ID`:** the same three
  reads from a shell, as tables (`--json` for the platform's rows), with
  names and emails escaped as `api-keys list` escapes them. `list` gives the
  id that `secrets --workspace` and `api-keys create --workspace` take;
  `members` needs an account-wide key (`--json`: `code` `permission_denied`,
  `status` 403), and `get` of a workspace the key cannot see is `not_found`.
- **`NO_BROWSER_PROXY`:** `browser_proxy=NO_BROWSER_PROXY` on
  `computers.create()` and `launch()` (sync and async) sends
  `"browser_proxy": null`, creating the computer with no browser proxy even
  when its template carries a default (`spec.browser_proxy`). Leaving
  `browser_proxy` at `None` still omits the key, so the template's default is
  inherited. New exports: `NO_BROWSER_PROXY` and its type, `NoBrowserProxy`.
- **`computer.wait_for_egress_proxy()`** (sync and async) waits until the
  computer's host holds its egress proxy's credentials
  (`egress_proxy_pending` false on a running computer), and `launch()` calls
  it when the create's `egress_proxy`, or the computer's, names
  `credentials_secret_id`: until then every connection the computer opens is
  closed.
- **`modifiers=` on `drag()`** (sync and async), keys held for the whole
  drag, sent as `click()` and `scroll()` send theirs.
- **`ApiKey.minted_by_key_id`** (and so on `ApiKeyCreated` and
  `Whoami.key`): the id of the key that minted this one over the API, kept
  after that key is revoked; `None` for a dashboard key. Revoking a key does
  not revoke the keys it minted, so this is how to find them.
  `mandala-py api-keys list` prints it as `MINTED BY`. Keyword-only, at the
  end of the constructor.
- **`APIError.code` and `APIError.operation_id`**, read-only, from the error
  body's `code` (such as `idempotency_in_progress` or
  `idempotency_outcome_unknown`) and `operation_id`; `None` when absent.
  `OperationFailedError.code` is unchanged.
- **`ComputerDeletion.operation_id`**: the delete's lifecycle operation, from
  `delete(detailed=True)`. Keyword-only, at the end of the constructor.
- **`mandala-py --json` failures carry the call's ids:** `request_id`,
  `idempotency_key` and `operation_id` in the `error` object when the failure
  has them.

### Changed

- **`set_schedule()` keeps the window it is not told about** (sync and
  async). `hour`, `minute` and `tz` now default to `None`, meaning "keep the
  current schedule's value": it reads the computer record first and sends
  its `snapshot_schedule` back, falling back to 04:00 UTC only for a computer
  with none. The read does not refresh the handle, so a create's
  `start_error` survives it and `wait_until_running()` still fails fast. (It reads the record, not `schedule()`, because that route
  answers a computer with no schedule as a disabled 00:00 UTC one.) The
  platform stores the window whole, so `set_schedule(enabled=False)` used to
  move a 23:30 America/Chicago window to 04:00 UTC while switching it off.
  Passing all three sends no read, as before.
- **`WhoamiUser.email` and `WhoamiAccount.plan` are `str | None`.** The
  platform sends `null` in both for a key confined to a workspace, and they
  were decoded as `""`. A value that is neither a string nor `null` now
  raises `MandalaError`.
- **`computers.launch()` waits for bound secrets** (sync and async). With
  secrets bound it now returns only once they have reached the desktop, inside
  the same readiness budget, so the first command on the returned computer sees
  them. A delivery that failed raises, naming why. A launch with nothing bound
  makes no extra request.
- **`computer.write_file()` returns the byte count** the platform reports, or
  `None` if it did not say (sync and async), as the TypeScript SDK's
  `writeFile` does. It returned `None` always.
- **A mistyped `mandala-py` command prints that command's whole help** under the
  message. An extra argument was reported against the top-level parser, whose
  one-line usage said nothing about the command typed. Extra arguments are now
  counted, never quoted, and an unknown option is named only when it is shaped
  like one: either could be a secret typed where `secrets set` reads stdin.
  A word after `--` is an operand however it is spelled, counted and never
  named as an option, and an option typed before the command that declares it
  (`mandala-py --json secrets list`) is said to belong after it, with its value
  too, however that is typed (`--workspace ws_1`, `--workspace=ws_1`,
  `--workspace='a b'`, `-ss1`, an abbreviation such as `--work`), naming the
  option in full and never the value. With the verb left off or mistyped
  (`--workspace ws_1 secrets lis`), the value is dropped too and the verb is
  what is said to be missing or unknown. A value typed as a separate word is
  still read as the command name, and named as an unknown one, when no
  command follows it at all (`mandala-py --workspace ws_1`) or when the
  command after it is mistyped (`mandala-py --workspace ws_1 secretz list`),
  since there a value and a mistyped command cannot be told apart; typed
  joined (`--workspace=ws_1 secretz list`), it is dropped and the mistyped
  command is what is named. Under `secrets`, no usage error repeats what was
  typed: not an option-shaped word such as `--sk-live-0123`, and not
  argparse's own messages that quote a value (`--keep-newline=VALUE`, an
  ambiguous abbreviation, an unknown verb, which now lists the verbs instead).

### Fixed

- **Documentation corrected against the platform:** `BrowserProxyArgs` and
  `EgressProxyArgs` named a `BadRequestError` that does not exist (a refused
  value is an `APIError` with `status == 400`); revoking a manage-keys key
  does NOT revoke the plain keys it minted; a memory clone gets its own
  network identity before its network comes up and runs beside its source;
  an `egress_proxy` create is never answered from the warm pool, not "always a
  cold boot"; `Computer.clone()` needs the source stopped or suspended; a
  suspended computer's `screenshot()` is its saved JPEG, at most 640 pixels
  wide; `publish()`'s 400 carries every problem in `body["problems"]`;
  `templates.schema()`'s URL needs a key, so save it to a file for an editor;
  `doc_digest` does not change with comments, key order or whitespace;
  `BuildStep.status` can be `unknown`; `PermissionDeniedError` covers a
  missing role, membership or permission; `OriginUnreachableError` leaves the
  outcome unknown; and a 409 reason `running` means stop the computer.
- **`api_keys.revoke()` refuses an API key passed where its id goes** (sync
  and async, and `mandala-py api-keys revoke`). Revoking the key you hold by
  pasting it (`com_...`) put the live key into the request path, where access
  logs record it, and the platform answered 404 and left the key valid. Any
  value starting `com_`, or holding a full key anywhere (behind a byte-order
  mark or zero-width space, in quotes, after `Bearer`), now raises
  `ValueError` ("that is an API key, not a key id; run api-keys list to find
  its id (key-...)") before any request, and neither the error nor the CLI
  repeats the value.
- **A 409 whose `reason` is `running` is permanent.** It is the platform's
  refusal of something only a stopped computer can have, a resize today, and
  nothing clears it by waiting: stop the computer, then send it again.
  `is_transient` called it worth sending again, as it does any other
  `ConflictError`, so a caller looping on it resent the same resize until it
  gave up. It now answers `False`.
- **The CLI escapes control characters in the names it prints.**
  `mandala-py api-keys list`, `api-keys create` and `whoami` printed key,
  workspace, user and account names (and a user's email) as they were stored,
  so a name holding a newline could forge a row and one holding an escape
  sequence could drive the terminal. Each control character (C0, DEL, C1,
  U+2028, U+2029) now prints as a visible escape such as `\x0a`, `\x1b` or
  `\u2028`, and so does each bidi embedding, override and isolate
  (U+202A-U+202E, U+2066-U+2069), which could visually reverse the rest of a
  row. `--json` output is unchanged. The platform now refuses such
  characters in a new key's name; keys named before that keep their names.

## [0.6.0] — 2026-09-25

0.5.1 was never released; everything listed for it is here. Three behaviour
changes to read before upgrading, all under **Changed**: the command is now
`mandala-py` (the package no longer installs `mandala`), a 503 on a change is
no longer transient, and `Computer.type()` now returns the platform's
`mechanism`. A create-only upload's refusals arrive as two new `ConflictError`
subclasses, `FileExistsError` and `CreateOnlyConflictError`; `no_wake`'s
refusal as `ComputerNotRunningError`.

### Added

- **Create-only uploads.** `computer.write_file(path, data, overwrite=False)`
  (sync and async) writes the file only if nothing is at `path`. A path that is
  taken raises the new `FileExistsError` — a `ConflictError` whose `reason` is
  `"exists"`, and Python's built-in `FileExistsError` too, which `is_transient`
  calls permanent — and that request writes nothing. A create-only upload's
  409 with no usable reason (a body that could not be read, or JSON without a
  string `reason`) raises the new `CreateOnlyConflictError`, a `ConflictError`
  that `is_transient` calls permanent and that claims nothing about the path;
  `FileExistsError` is only the platform's explicit `exists`. If an earlier
  attempt's outcome was unknown, the file may be yours: read it and compare
  before choosing another path or overwriting. The default is unchanged: a
  write replaces the file. Linux computers only. `mandala-py scp` gains
  `--no-overwrite` for uploads.
- **The account's secret store.** `client.secrets.list/get/create/replace/delete`
  (sync and async) over `GET/POST /secrets` and `GET/PUT/DELETE /secrets/{id}`,
  each taking `workspace_id` for a workspace's scope. Values are write-only: a
  `Secret` carries its name, id, scope and `revision_id`, never a value. A
  replace and a delete send the `revision_id` a read answered, and a delete
  requires it. `client.secrets.set(name, value)` creates the name or replaces
  its value, matching names ignoring ASCII case as the platform does, and reads
  again up to three times on a conflict. `SecretList` carries the store's
  `limits` and whether `delivery` is available.
- **`mandala-py secrets list | set NAME | rm NAME`**, with `--workspace`. `set`
  reads the value from stdin (one trailing `\n` or `\r\n` dropped; `--keep-newline`
  keeps it) or a prompt that does not echo — never from the command line.
- **A computer's secret state.** `Computer.secret_bindings`,
  `secrets_generation`, `secrets_applied` (a `SecretsReceipt`),
  `secrets_error`, and `secrets_pending`, which is `True`, `False` or `None`
  when the platform could not check — never folded into `False`.
- **`no_wake=True`** on `read_file`, `read_text_file`, `read_file_part`,
  `download_file` and `write_file`: require a running computer rather than
  resume one. The refusal is the new `ComputerNotRunningError`, a
  `ConflictError` that `is_transient` calls permanent.
- **`Computer.list_directory(path)`** lists a guest directory (a bounded
  `GuestDirectory`, with `truncated` and `skipped`).
- **Passive history.** `Computer.signals()` reads the host's passive signals
  from a cursor; `activities()`, `activity()` and `activity_results()` read the
  computer's API activity history. None of them wakes the guest.
- **`Computer.paste(text, shift=False)`**, the input `paste` action.
- **`Computer.delete(..., detailed=True)`** returns a `ComputerDeletion` with
  `ok`, `computer_deleted`, `error` and the per-copy `purge` tally; a queued
  purge answers 202 with `ok` false. The plain form is unchanged.
- `Snapshot.restore_available` and `computer_unreachable`;
  `SnapshotHoldings.computer_present`, `capturing` and `deleting`;
  `Computer.desktop` and `running_ram_mb`; `APIError.method`. A clone's
  `memory_dropped_reason` documents `"capture unrecorded"` and is an open set.

### Changed

- **`is_transient` no longer calls a 503 on a change worth sending again.** The
  platform documents that a change answered 503 may or may not have happened,
  so an `UnavailableError` is transient only when its request was a `GET` or a
  `HEAD` — and one built without a `method` is treated as a change. A create,
  command, delete or secret change answered 503 needs a look at the current
  state first. The SDK's own `retries` already never replayed one.
- **`Computer.type()` returns the platform's `mechanism`** (`"physical"`,
  `"unicode"` or `"mixed"`, or `None`), and waits long enough for a Unicode
  request, which can take over a minute. Its docs used to say unmappable
  characters were skipped; the platform types Unicode text where it is
  supported and refuses it before typing anything where it is not.
- **Refusal words, documented as the platform has them.** `APIError.reason` is
  an open set: `contention` and `starting` are transient; `unavailable`,
  `unsupported`, `exists` and `revoked` are not; an unknown word falls back to
  the exception type. The `ConflictError` and `UnavailableError` docs no longer
  say nearly every 409 clears or that a 503 is simply worth retrying.
- **The command is `mandala-py`.** The package no longer installs `mandala`,
  which is the npm package's full CLI (`npm install -g mandala-computer`), with
  `login`, `computers`, `snapshots`, `templates` and `--json`. Both installed a
  `mandala`, and whichever came first on PATH won. `mandala-py` has the same
  commands as before: `terminal`, `scp`, `ssh`, `ssh-key`, `ssh-access`,
  `ssh-config` and `webhooks`. SSH config blocks written by either keep working.

## [0.5.0] — 2026-09-23

### Added

- **Secret bindings.** `computers.create(secrets=[...])` binds secrets from the
  account at create, each as an environment variable (`env`) or as a file under
  `/run/mandala-secrets/user/files` (`file`). `computer.secrets()` reads a
  computer's bindings with the `version` to send back, and
  `computer.set_secrets(bindings, version=...)` replaces them. A binding's
  `revision_id` is the revision last delivered: every start and restart
  delivers each secret's latest value, and a replaced value is also sent to a
  running computer bound to it as a file, asynchronously and on a best-effort
  basis. On the async client too.
- **Memory snapshot clone options.** `snapshots.clone(id, memory=False)` builds a
  memory snapshot's clone from its disk alone, as a fresh boot with its own
  network identity. `inherit_secrets=True` consents to resuming a memory
  snapshot of a computer that held secrets: the copy holds the same credentials.
  Both are keyword-only, and on the async client too.
- **`Computer.memory_dropped` and `memory_dropped_reason`.** On the computer a
  snapshot clone returns, they say when the session you asked for was not
  resumed and the computer was built from the disk instead (`"secrets"` or
  `"bindings unrecorded"`). Kept through `wait_until_built()`.
- **`client.computers.launch()`** creates a computer, starts it if needed and
  waits for its guest agent, in one call. It takes the create options, shares a
  180-second readiness budget after the create (`timeout=` to change it), never
  replays an admitted start, and leaves the computer in place on failure: the
  error keeps its type and carries the created computer's id.
- **SSH.** `client.ssh_keys.list()`, `.add(public_key, name=...)` and
  `.remove(key_id)` manage the account's keys, and `computer.ssh_access()` /
  `computer.set_ssh_access(enabled)` read and switch a computer's SSH, with new
  `SshKey` and `SshAccess` models. The CLI gains `mandala ssh --setup`,
  `mandala ssh-key`, `mandala ssh-access` and `mandala ssh-config`. On the async
  client too.
- **Saved credential profiles.** With no `api_key` and no `MANDALA_API_KEY`, a
  client now reads the profile the CLI's browser login saved in
  `~/.mandala/credentials.json`: `profile=`, then `MANDALA_PROFILE`, then the
  file's default. A saved key is bound to its stored base URL, and an invalid,
  unsafe or mismatched store fails locally. An explicit or environment key never
  touches the file.
- **Opt-in retries for safe reads.** `Client(retries={"idempotent": N})` retries
  GET and HEAD reads on connection failures and 502/503/504, with backoff and
  `Retry-After`. Off by default; mutations and the consuming output poll are
  always one attempt.
- **`client.account.read()`** returns a typed `AccountQuota`: the plan's
  instantaneous limits, usage and remaining room, keeping an unknown value
  distinct from zero.
- **Background executions by stable id.** A background handle carries an
  `execution_id` (`None` from an older daemon), and `computer.execution(id)` and
  `computer.execution_output(id, ...)` read its state and its output from
  offsets the caller owns, so several readers can follow one run without
  consuming each other's output or trusting a reusable PID.
- **Retained output and artifacts.** `exec(..., retain_output=True)` keeps a
  synchronous run's output and returns its `result_id`;
  `retain_execution_output(execution_id)` captures a background run's output.
  `result()`, `result_output()` and `delete_result()` read and remove it, and
  `publish_artifact()`, `artifact()`, `download_artifact()` (SHA-256 verified)
  and `delete_artifact()` handle files you choose to keep. Default `exec`
  behaviour is unchanged.
- **Error metadata.** API errors carry `request_id`, `allow` and
  `www_authenticate` when the response had them, and a 405 raises the new
  `MethodNotAllowedError` (an `APIError`).

### Changed

- **`mandala ssh` is real OpenSSH now; the old shell is `mandala terminal`.** In
  0.4.0 `mandala ssh <computer>` opened the websocket shell. That command is now
  `mandala terminal <computer> [--session NAME]`, unchanged, and `mandala ssh`
  execs the system `ssh` through the SSH gateway, which needs a registered key
  and SSH switched on for the computer (`mandala ssh --setup` does both). Scripts
  that called `mandala ssh` for a shell should call `mandala terminal`.

### Fixed

- **An error nested in a response body is classified by the real HTTP status**,
  keeping its message, reason and full body, and a failed agent run keeps the
  original error frame and its request id, on both clients.

### Documentation

- **Driving the computer agent from the OpenAI client.** The README shows the
  OpenAI-compatible `chat/completions` endpoint with the `openai` library,
  separate Mandala and Anthropic keys, and why its automatic retries should be
  off. The example is executed by the test suite.

### Internal

- The surface mirror tracks the platform's new routes and parameters (file
  browser, activity history, signals, secret bindings, snapshot clone options)
  as each lands, marked not yet wrapped until a client method exists.

## [0.4.0] — 2026-09-14

### Added

- **`Computer.read_text_file()` and `AsyncComputer.read_text_file()`** — the
  same request `read_file()` makes, decoded as UTF-8. `read_file()` stays the
  primitive and stays `bytes`, because a guest file is not promised to be text
  and decoding one that is not would put replacement characters at the SDK
  boundary for every caller. The decode is `errors="replace"`, on the same
  terms as `ExecResult.stdout_text`: the caller asked for text, and the bytes
  are one call away.

### Changed

- **An empty clipboard write now clears the selection** instead of being
  refused locally. `set_clipboard("")` is a thing the platform accepts and a
  thing callers want; refusing it here was this SDK inventing a rule.
- **`reason: "revoked"` is classified as permanent.** The platform added a
  fifth refusal word for the case where the authority a request arrived with no
  longer holds — suspended, demoted, removed, or a retired session — and it is
  the first of these words about the *caller* rather than about a computer.
  Nothing was broken before this: a 401 or 403 is none of the four transient
  classes, so an unrecognised word already answered `False` from
  `is_transient()`. What changes is that it is answered on purpose, so a future
  status for this refusal cannot quietly make a permission failure look
  replayable. The status still says what to do: 401 means present a credential
  again, 403 means the role changed and signing in again will not help.

### Fixed

- **A mid-run refusal no longer discards the partial agent run.** Authorization
  is rechecked before each further piece of work, so a non-streaming
  `agent()` can be stopped after steps have already run on the desktop and
  already cost model tokens — and the refusal body is the only place either is
  ever reported. Those steps and that usage now survive the error.
- **The documented install works on a Homebrew or distribution Python.** The
  README opened with `pip install mandala-computer`, which answers
  `externally-managed-environment` and installs nothing on the ordinary
  developer Mac (PEP 668). The install guidance now leads with a virtual
  environment rather than with a command that fails.

### Documentation

- **A run's steps are not its model calls.** Three places said they were and
  offered `max_steps` as a way to size Anthropic spend on that basis. The
  platform counts a step per *tool call* and encourages a model to ask for
  several actions in one reply, so one model call can spend several steps; a
  paused turn is resubmitted for tokens and no step; and a bash call or a
  cursor read takes no screenshot. A caller budgeting from the old sentence was
  wrong whichever way their run went. The README also now states the default,
  which nothing here did: omit `max_steps` and you get 20, and the ceiling
  is 100.
- Webhook idempotency and API usage guidance clarified, and the template
  section restored the two refusals on the custom-build path that never clear.

### Internal

No effect on the published surface, listed because it is most of the window.

- The drift check reads the platform's published **surface manifest** instead
  of scanning its TypeScript and Go as text. Eleven hundred lines of
  hand-written reader are gone, along with the class of defect they kept
  producing: a false all-clear, reporting the mirror in step because the scan
  had silently read nothing. The new reader fails closed on a manifest that is
  missing, unparseable, of an unknown version, or that names a key twice.
- Surface inventory parsing hardened in the same direction, before the scanner
  was retired.

[0.9.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.3.0...v0.4.0
