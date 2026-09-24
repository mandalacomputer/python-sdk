# Changelog

Notable changes to `mandala-computer`. Dates are release dates; the format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project is pre-1.0, so a minor version may carry a behaviour change.

The reasoning behind a change lives in its commit message rather than here.
This is the summary you read to decide whether to upgrade.

## [0.5.1] — unreleased

### Added

- **Create-only uploads.** `computer.write_file(path, data, overwrite=False)`
  (sync and async) writes the file only if nothing is at `path`. A path that is
  taken raises the new `FileExistsError` — a `ConflictError` whose `reason` is
  `"exists"`, and Python's built-in `FileExistsError` too, which `is_transient`
  calls permanent — and that request writes nothing. A create-only upload's
  409 whose body could not be read is raised as `FileExistsError` too, never as
  a transient conflict. If an earlier attempt's outcome was unknown, the file
  may be yours: read it and compare before choosing another path or
  overwriting. The default is unchanged: a write replaces the file. Linux
  computers only. `mandala-py scp` gains `--no-overwrite` for uploads.

### Changed

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
  delivers each secret's latest value, and a secret bound as a file is replaced
  on a running computer as soon as its value is. On the async client too.
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

[0.5.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/mandalacomputer/python-sdk/compare/v0.3.0...v0.4.0
