"""``~/.mandala/defaults.json``: the default workspace ``mandala-py workspaces
use`` saves for a profile (OPL-5499).

It is a file of its own, beside ``credentials.json``, because every released
reader of ``credentials.json`` has a closed profile schema: a new field there,
or a new file version, would lock older installs out of every profile. Older
clients never open this file. It is read and replaced with the same checks as
``credentials.json`` (a private 0700 directory, a 0600 regular file owned by
this user, its own sibling lock, a flushed temporary file renamed into place),
under ``.defaults.lock``. The npm ``mandala`` CLI reads and writes the same file
the same way::

    {"version":1,"profiles":{"work":{"account_id":"acc-…","workspace":{"id":"wsp-…","name":"…"}}}}

``account_id`` is the profile's account when the default was saved: a profile
logged in again to another account does not carry the default over.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from ._credentials import (
    _MAX_BYTES,
    KEEP,
    CredentialError,
    _parse_store,
    _profile,
    _read_store,
    _supported,
    _trim,
    rewrite_locked,
)
from ._exceptions import MandalaError

#: The file's place, as the CLI's sentences name it.
DEFAULTS_PATH = "~/.mandala/defaults.json"
_STORE = "defaults.json"
_T = TypeVar("_T")
_LOCK = ".defaults.lock"

_REASONS = {
    "invalid_utf8": "it is not UTF-8 text",
    "invalid_json": "it is not valid JSON",
    "invalid_schema": "its contents are not in the expected form",
    "invalid_profile": "it names a profile that is not a valid profile name",
    "unsupported_version": "it is a version this CLI does not read",
    "too_many_profiles": "it holds more than 100 profiles",
    "file_too_large": "it is larger than 64 KiB",
    "unsafe_file": "it must be a regular file with mode 0600, owned by you",
    "unsafe_directory": "~/.mandala must be a directory with mode 0700, owned by you",
    "unsupported_file_protection": "this platform cannot protect it",
    "read_timeout": "reading it timed out",
    "writer_lock_timeout": "another process held its lock",
    "credential_remove_failed": "it could not be changed",
    "credential_remove_unconfirmed": "the change was made, but not confirmed durable",
}
#: The failures of a file that is there but cannot be read: never overwritten.
_UNREADABLE = frozenset(
    (
        "invalid_utf8",
        "invalid_json",
        "invalid_schema",
        "invalid_profile",
        "unsupported_version",
        "too_many_profiles",
        "file_too_large",
        "unsafe_file",
    )
)


class DefaultsError(MandalaError):
    """A ``defaults.json`` that cannot be used, or changed.

    ``code`` is the failed stage, as :class:`CredentialError` names it;
    ``reason`` says why in a few words, for
    ``ignoring ~/.mandala/defaults.json: <reason>``.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.reason = _REASONS.get(code, code)
        super().__init__(message or f"{DEFAULTS_PATH} cannot be used: {self.reason}")


@dataclass(frozen=True)
class WorkspaceDefault:
    """One profile's saved default: the workspace, and the account it was saved for."""

    account_id: str
    workspace_id: str
    workspace_name: str


def _object(value: Any, members: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != members:
        raise CredentialError("invalid_schema")
    return value


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise CredentialError("invalid_schema")
    return value


def parse_defaults(payload: bytes) -> dict[str, WorkspaceDefault]:
    """Decode and check ``defaults.json``'s bytes: each profile's default.

    A :class:`CredentialError` names what is wrong.
    """
    if len(payload) > _MAX_BYTES:
        raise CredentialError("file_too_large")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeError:
        raise CredentialError("invalid_utf8") from None

    def bad_constant(value: str) -> Any:
        raise ValueError("non-JSON number")

    try:
        data = json.loads(text, parse_constant=bad_constant)
    except (ValueError, RecursionError):
        raise CredentialError("invalid_json") from None
    if not isinstance(data, dict):
        raise CredentialError("invalid_schema")
    version = data.get("version")
    # bool is an int in Python, and true is not a version.
    if type(version) not in (int, float) or (
        isinstance(version, float) and (not math.isfinite(version) or not version.is_integer())
    ):
        raise CredentialError("invalid_schema")
    if version != 1:
        raise CredentialError("unsupported_version")
    _object(data, {"version", "profiles"})
    profiles = data["profiles"]
    if not isinstance(profiles, dict):
        raise CredentialError("invalid_schema")
    if len(profiles) > 100:
        raise CredentialError("too_many_profiles")
    result: dict[str, WorkspaceDefault] = {}
    for name, entry in profiles.items():
        _profile(name)
        entry = _object(entry, {"account_id", "workspace"})
        workspace = _object(entry["workspace"], {"id", "name"})
        result[name] = WorkspaceDefault(
            _text(entry["account_id"]), _text(workspace["id"]), _text(workspace["name"])
        )
    return result


def _encode(profiles: dict[str, WorkspaceDefault]) -> bytes:
    payload = (
        json.dumps(
            {
                "version": 1,
                "profiles": {
                    name: {
                        "account_id": d.account_id,
                        "workspace": {"id": d.workspace_id, "name": d.workspace_name},
                    }
                    for name, d in profiles.items()
                },
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    parse_defaults(payload)
    return payload


def read_defaults() -> dict[str, WorkspaceDefault]:
    """Every profile's default; empty when the file (or ``~/.mandala``) is missing.

    One that cannot be used raises :class:`DefaultsError`; a command that only
    reads it reports that and goes on without a default.
    """
    if not _supported():
        raise DefaultsError("unsupported_file_protection")
    try:
        return parse_defaults(_read_store(_STORE))
    except CredentialError as e:
        if e.rule == "missing_credentials":
            return {}
        raise DefaultsError(e.rule) from None


def workspace_default(
    defaults: dict[str, WorkspaceDefault], profile: str, account_id: str
) -> tuple[WorkspaceDefault | None, WorkspaceDefault | None]:
    """``(entry, ignored)``: the default a profile's commands use, when it was
    saved for the account the profile is logged in to now; else the one saved
    for another account, which is not used."""
    entry = defaults.get(profile)
    if entry is None:
        return None, None
    return (entry, None) if entry.account_id == account_id else (None, entry)


def _rewrite(
    compute: Callable[[bytes | None, str], tuple[bytes | None | object, _T]], lock_timeout: float
) -> _T:
    try:
        return rewrite_locked(_STORE, _LOCK, ".defaults-", compute, lock_timeout=lock_timeout)
    except CredentialError as e:
        reason = _REASONS.get(e.rule, e.rule)
        if e.rule in _UNREADABLE:
            message = (
                f"{DEFAULTS_PATH} cannot be read ({reason}), so it was not changed. "
                "Delete or fix it, then run the command again."
            )
        elif e.rule == "credential_remove_unconfirmed":
            message = (
                f"{DEFAULTS_PATH} was changed, but durable persistence could not be confirmed."
            )
        else:
            message = f"{DEFAULTS_PATH} was not changed: {reason}."
        raise DefaultsError(e.rule, message) from None


def save_workspace_default(
    profile: str, entry: WorkspaceDefault, *, lock_timeout: float = 5.0
) -> None:
    """Save ``entry`` as ``profile``'s default, keeping every other profile's.

    A file that is there but cannot be read is never overwritten.
    """
    _profile(profile)

    def compute(old: bytes | None, path: str) -> tuple[bytes, None]:
        profiles = {} if old is None else parse_defaults(old)
        profiles[profile] = entry
        return _encode(profiles), None

    _rewrite(compute, lock_timeout)


def remove_workspace_default(profile: str, *, lock_timeout: float = 5.0) -> bool:
    """Remove ``profile``'s default; False when it had none, and nothing was
    written. The last one removed takes the file with it."""
    _profile(profile)

    def compute(old: bytes | None, path: str) -> tuple[bytes | None | object, bool]:
        profiles = {} if old is None else parse_defaults(old)
        if profile not in profiles:
            return KEEP, False
        del profiles[profile]
        return (_encode(profiles) if profiles else None), True

    return _rewrite(compute, lock_timeout)


def saved_profile(profile: str | None) -> tuple[str, dict[str, Any]]:
    """The saved profile in use, as the client resolves it: ``profile``, else
    ``MANDALA_PROFILE``, else the store's default; and its entry.

    Raises :class:`CredentialError` as the client does when there is no store
    or no such profile.
    """
    selected = (
        profile if profile is not None else _trim(os.environ.get("MANDALA_PROFILE", "")) or None
    )
    if selected is not None:
        selected = _profile(selected)
    if not _supported():
        raise CredentialError("unsupported_file_protection")
    data = _parse_store(_read_store())
    name = selected if selected is not None else data["default_profile"]
    if name not in data["profiles"]:
        raise CredentialError("missing_selected_profile")
    return name, data["profiles"][name]
