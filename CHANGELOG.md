# Changelog

Notable changes to `mandala-computer`. Dates are release dates; the format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project is pre-1.0, so a minor version may carry a behaviour change.

The reasoning behind a change lives in its commit message rather than here.
This is the summary you read to decide whether to upgrade.

## [Unreleased]

### Added

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
  until revoked; `api-keys create` prints only the new key on stdout.
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
  `secrets_delivering` may read false (or be absent on an older platform)
  before the values land, and the wait can return before they do.
- **`mandala-py --json` failures carry an error code.** A failure under `--json`
  writes `{"error": {"code", "message", "status"?, "reason"?}}` as one line on
  stderr, with nothing on stdout. `code` is one snake_case word, the same
  vocabulary as the npm `mandala` CLI (`not_found`, `unauthenticated`,
  `conflict`, `invalid_arguments`, …). A usage error is one too, `ssh --setup`
  included, and `--json` counts however argparse lets it be abbreviated.

### Changed

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

[0.6.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.3.0...v0.4.0
