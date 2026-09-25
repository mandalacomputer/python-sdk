"""Construction-time authentication from options, environment, or a private store.

The TypeScript CLI's ``mandala login`` adds to the store; the one write made
here is :func:`remove_profile` (``mandala-py logout``), under the same lock
file and the same checks. Paths and parsed values never appear in errors: both
can contain credentials supplied by an untrusted local file.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._exceptions import MandalaError

_MAX_BYTES = 65_536
_READ_SECONDS = 5.0
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_WHITESPACE = "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_RESERVED = frozenset(
    (
        "__proto__",
        "prototype",
        "constructor",
        "__defineGetter__",
        "__defineSetter__",
        "hasOwnProperty",
        "__lookupGetter__",
        "__lookupSetter__",
        "isPrototypeOf",
        "propertyIsEnumerable",
        "toLocaleString",
        "toString",
        "valueOf",
    )
)
_MESSAGES = {
    "invalid_explicit_key": "api_key must be a nonempty string.",
    "invalid_profile": "Credential profile names must be 1–64 allowed ASCII characters.",
    "missing_credentials": "No API key. Pass api_key=..., set MANDALA_API_KEY, or run `mandala login` (npm install -g mandala-computer).",
    "unsupported_file_protection": "Credential files require verified POSIX protection; pass api_key or set MANDALA_API_KEY on this system.",
    "unsafe_directory": "The credential directory must be a real current-owner directory with mode 0700.",
    "unsafe_file": "The credential file must be a current-owner regular file with mode 0600 and one link.",
    "file_too_large": "The credential file exceeds 65,536 bytes.",
    "read_timeout": "Reading the credential file exceeded five seconds.",
    "invalid_utf8": "The credential file must contain valid UTF-8.",
    "invalid_json": "The credential file must contain valid JSON without a BOM.",
    "invalid_schema": "The credential file does not match the complete V1 schema; repair it or log in again.",
    "unsupported_version": "Unsupported credential file version; update the SDK or log in again.",
    "missing_default_profile": "The credential file must name an existing default profile.",
    "missing_selected_profile": "The selected credential profile is missing; choose an existing profile or log in with that profile.",
    "too_many_profiles": "The credential file exceeds 100 profiles.",
    "invalid_base_url": "Credential base URLs must be canonical HTTPS URLs (or explicit HTTP loopback targets), without credentials, query, or fragment.",
    "base_binding_mismatch": "The selected credential belongs to a different base URL; remove the override or choose a matching profile.",
    "writer_lock_timeout": "Another process holds the credential file's lock (~/.mandala/.credentials.lock); try again when it finishes.",
    "credential_remove_failed": "Could not remove the profile; the credential file was not changed.",
    "credential_remove_unconfirmed": "The profile was removed, but durable persistence could not be confirmed. Check ~/.mandala/credentials.json.",
}


class CredentialError(MandalaError):
    """A local refusal, with a private diagnostic category and no file values."""

    def __init__(self, rule: str) -> None:
        self.rule = rule
        super().__init__(_MESSAGES[rule])


@dataclass(frozen=True)
class Credentials:
    key: str = field(repr=False)
    base_url: str
    source: str
    profile: str | None = None


def _trim(value: str) -> str:
    return value.strip(_WHITESPACE)


def _profile(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is None
        or value in _RESERVED
    ):
        raise CredentialError("invalid_profile")
    return value


def canonical_base(value: object) -> str:
    """The deliberately narrow, cross-language file URL grammar."""
    if not isinstance(value, str):
        raise CredentialError("invalid_base_url")
    value = _trim(value)
    if (
        not value
        or len(value) > 2048
        or not value.isascii()
        or re.search(r"[\x00-\x20\x7f\\%?#]", value)
    ):
        raise CredentialError("invalid_base_url")
    match = re.fullmatch(r"(https?)://([^/]+)(/.*)?", value, re.IGNORECASE)
    if match is None:
        raise CredentialError("invalid_base_url")
    scheme, authority, path = match.groups()
    scheme = scheme.lower()
    path = path or ""
    if "@" in authority or re.fullmatch(r"[A-Za-z0-9\-._~!$&'()*+,;=:@/]*", path) is None:
        raise CredentialError("invalid_base_url")
    if any(part in (".", "..") for part in path.split("/")):
        raise CredentialError("invalid_base_url")
    port_text: str | None = None
    loopback = False
    if authority.startswith("["):
        bracket = re.fullmatch(r"\[([^\[\]]+)\](?::([0-9]+))?", authority)
        if bracket is None:
            raise CredentialError("invalid_base_url")
        address, port_text = bracket.groups()
        try:
            ipv6 = ipaddress.IPv6Address(address)
        except ValueError:
            raise CredentialError("invalid_base_url") from None
        host = f"[{ipv6.compressed}]"
        loopback = int(ipv6) == 1
    else:
        parts = authority.split(":")
        if len(parts) > 2:
            raise CredentialError("invalid_base_url")
        host = parts[0].lower()
        if len(parts) == 2:
            port_text = parts[1]
        labels = host.split(".")
        if len(host) > 253 or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in labels
        ):
            raise CredentialError("invalid_base_url")
        if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", labels[-1]):
            if len(labels) != 4 or any(
                re.fullmatch(r"(?:0|[1-9][0-9]{0,2})", part) is None or int(part) > 255
                for part in labels
            ):
                raise CredentialError("invalid_base_url")
            loopback = labels[0] == "127"
        else:
            loopback = host == "localhost"
    port = ""
    if port_text is not None:
        if re.fullmatch(r"[0-9]+", port_text) is None or not 1 <= int(port_text) <= 65535:
            raise CredentialError("invalid_base_url")
        number = int(port_text)
        if number != (443 if scheme == "https" else 80):
            port = f":{number}"
    if scheme == "http" and not loopback:
        raise CredentialError("invalid_base_url")
    return f"{scheme}://{host}{port}{path.rstrip('/')}"


def _supported() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "getuid")
        and all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK"))
        and _OPEN_SUPPORTS_DIR_FD
    )


def _check_stat(info: os.stat_result, *, directory: bool) -> None:
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (
        not valid_type
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
        or (not directory and info.st_nlink != 1)
    ):
        raise CredentialError("unsafe_directory" if directory else "unsafe_file")
    if not directory and info.st_size > _MAX_BYTES:
        raise CredentialError("file_too_large")


def _read_store() -> bytes:
    deadline = time.monotonic() + _READ_SECONDS
    descriptors: list[int] = []
    stage = "unsafe_directory"

    def check_time() -> None:
        if time.monotonic() >= deadline:
            raise CredentialError("read_timeout")

    try:
        home = Path.home()
        if not home.is_absolute():
            raise CredentialError("unsafe_directory")
        check_time()
        # Anchor both lookups to open directories. A renamed/replaced .mandala
        # can never redirect the second open into an unvalidated directory.
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK)
        descriptors.append(home_fd)
        directory_fd = os.open(".mandala", flags | os.O_DIRECTORY, dir_fd=home_fd)
        descriptors.append(directory_fd)
        _check_stat(os.fstat(directory_fd), directory=True)
        check_time()
        stage = "unsafe_file"
        file_fd = os.open("credentials.json", flags, dir_fd=directory_fd)
        descriptors.append(file_fd)
        _check_stat(os.fstat(file_fd), directory=False)
        result = bytearray()
        while True:
            check_time()
            chunk = os.read(file_fd, min(8192, _MAX_BYTES + 1 - len(result)))
            result.extend(chunk)
            if len(result) > _MAX_BYTES:
                raise CredentialError("file_too_large")
            if not chunk:
                break
        _check_stat(os.fstat(file_fd), directory=False)
        _check_stat(os.fstat(directory_fd), directory=True)
        check_time()
        return bytes(result)
    except FileNotFoundError:
        raise CredentialError("missing_credentials") from None
    except (OSError, RuntimeError, ValueError):
        raise CredentialError(stage) from None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _object(value: Any, members: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != members:
        raise CredentialError("invalid_schema")
    return value


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


def _valid_strings(value: Any) -> None:
    # Iterative traversal also handles malicious nesting without recursion.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in item):
                raise CredentialError("invalid_schema")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _parse_store(payload: bytes) -> dict[str, Any]:
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
    _valid_strings(data)
    if not isinstance(data, dict):
        raise CredentialError("invalid_schema")
    version = data.get("version")
    if type(version) not in (int, float) or (
        isinstance(version, float) and (not math.isfinite(version) or not version.is_integer())
    ):
        raise CredentialError("invalid_schema")
    if version != 1:
        raise CredentialError("unsupported_version")
    if "default_profile" not in data:
        raise CredentialError("missing_default_profile")
    _object(data, {"version", "default_profile", "profiles"})
    if not isinstance(data["default_profile"], str):
        raise CredentialError("invalid_schema")
    default = _profile(data["default_profile"])
    profiles = data["profiles"]
    if not isinstance(profiles, dict) or not profiles:
        raise CredentialError("invalid_schema")
    if len(profiles) > 100:
        raise CredentialError("too_many_profiles")
    for name, entry in profiles.items():
        _profile(name)
        entry = _object(entry, {"api_key", "base_url", "key_id", "account", "scope"})
        if not isinstance(entry["api_key"], str) or not _trim(entry["api_key"]):
            raise CredentialError("invalid_schema")
        if not _nonempty(entry["key_id"]):
            raise CredentialError("invalid_schema")
        account = _object(entry["account"], {"id", "name"})
        if not _nonempty(account["id"]) or not (
            account["name"] is None or isinstance(account["name"], str)
        ):
            raise CredentialError("invalid_schema")
        scope = entry["scope"]
        if not isinstance(scope, dict):
            raise CredentialError("invalid_schema")
        if scope.get("type") == "account":
            _object(scope, {"type"})
        elif scope.get("type") == "workspace":
            _object(scope, {"type", "workspace_id", "workspace_name"})
            if not _nonempty(scope["workspace_id"]) or not _nonempty(scope["workspace_name"]):
                raise CredentialError("invalid_schema")
        else:
            raise CredentialError("invalid_schema")
        if canonical_base(entry["base_url"]) != entry["base_url"]:
            raise CredentialError("invalid_base_url")
    if default not in profiles:
        raise CredentialError("missing_default_profile")
    return data


def resolve_credentials(
    api_key: str | None, base_url: str | None, profile: str | None, default_base: str
) -> Credentials:
    """Resolve once; key winners never discover or inspect the store."""
    source = "explicit"
    if api_key is not None:
        if not isinstance(api_key, str) or not _trim(api_key):
            raise CredentialError("invalid_explicit_key")
        key = _trim(api_key)
    else:
        key = _trim(os.environ.get("MANDALA_API_KEY", ""))
        source = "environment"
    if key:
        # Preserve the SDK's existing base precedence and URL behavior here.
        base = (base_url or os.environ.get("MANDALA_BASE_URL") or default_base).rstrip("/")
        return Credentials(key, base, source)
    selected = (
        profile if profile is not None else _trim(os.environ.get("MANDALA_PROFILE", "")) or None
    )
    if selected is not None:
        selected = _profile(selected)
    if not _supported():
        raise CredentialError("unsupported_file_protection")
    data = _parse_store(_read_store())
    selected = selected if selected is not None else data["default_profile"]
    if selected not in data["profiles"]:
        raise CredentialError("missing_selected_profile")
    entry = data["profiles"][selected]
    override = (
        base_url if base_url is not None else _trim(os.environ.get("MANDALA_BASE_URL", "")) or None
    )
    if override is not None and canonical_base(override) != entry["base_url"]:
        raise CredentialError("base_binding_mismatch")
    return Credentials(_trim(entry["api_key"]), entry["base_url"], "file", selected)


_LOCK = ".credentials.lock"
_STORE = "credentials.json"


@dataclass(frozen=True)
class RemovedProfile:
    """What :func:`remove_profile` did."""

    #: The profile asked for: the named one, or the default.
    profile: str
    #: False when the store held no such profile; nothing was written then.
    removed: bool
    path: str
    #: The removed profile's key id, which still authenticates until revoked.
    key_id: str | None
    #: The default afterwards: unchanged, or — when the default itself was
    #: removed and others remain — the first of them by name. ``None`` when no
    #: profile remains and the file is gone.
    default_profile: str | None


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return a.st_dev == b.st_dev and a.st_ino == b.st_ino


def _read_in(directory_fd: int) -> bytes | None:
    """The store's bytes, read relative to an already-checked directory, or
    ``None`` when there is no store."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        fd = os.open(_STORE, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        _check_stat(os.fstat(fd), directory=False)
        result = bytearray()
        while True:
            chunk = os.read(fd, min(8192, _MAX_BYTES + 1 - len(result)))
            result.extend(chunk)
            if len(result) > _MAX_BYTES:
                raise CredentialError("file_too_large")
            if not chunk:
                break
        _check_stat(os.fstat(fd), directory=False)
        return bytes(result)
    finally:
        os.close(fd)


def remove_profile(profile: str | None = None, *, lock_timeout: float = 5.0) -> RemovedProfile:
    """Forget one saved profile — ``mandala-py logout``.

    ``profile`` defaults to ``MANDALA_PROFILE``, then the store's default. The
    key it held stays valid on the platform: this changes only this machine.

    Held to the TypeScript CLI's writer: the same exclusive lock file, never
    stolen from another writer; the store re-read under it and checked unchanged
    just before the swap; the new store written to a private temporary file,
    flushed, and renamed over the old one. A default removed while others
    remain is replaced by the first of them by name, because the store has to
    name one; the last profile removed takes the file with it, because its
    schema has no spelling for an empty store.
    """
    selected = (
        profile if profile is not None else _trim(os.environ.get("MANDALA_PROFILE", "")) or None
    )
    if selected is not None:
        selected = _profile(selected)
    if not _supported():
        raise CredentialError("unsupported_file_protection")
    home = Path.home()
    if not home.is_absolute():
        raise CredentialError("unsafe_directory")
    path = str(home / ".mandala" / _STORE)
    descriptors: list[int] = []
    lock_info: os.stat_result | None = None
    temp: str | None = None
    committed = False
    directory_fd = -1
    try:
        try:
            home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK)
            descriptors.append(home_fd)
            directory_fd = os.open(
                ".mandala",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=home_fd,
            )
        except FileNotFoundError:
            return RemovedProfile(selected or "default", False, path, None, None)
        descriptors.append(directory_fd)
        _check_stat(os.fstat(directory_fd), directory=True)

        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                lock_fd = os.open(
                    _LOCK,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                # Never removed on a timeout: the lock may be a live writer's.
                if time.monotonic() >= deadline:
                    raise CredentialError("writer_lock_timeout") from None
                time.sleep(0.05)
        descriptors.append(lock_fd)
        os.fchmod(lock_fd, 0o600)
        lock_info = os.fstat(lock_fd)
        _check_stat(lock_info, directory=False)

        old = _read_in(directory_fd)
        data = None if old is None else _parse_store(old)
        name = selected or (data["default_profile"] if data else "default")
        if data is None or name not in data["profiles"]:
            return RemovedProfile(
                name, False, path, None, data["default_profile"] if data else None
            )
        key_id = str(data["profiles"][name]["key_id"])
        profiles = {k: v for k, v in data["profiles"].items() if k != name}
        left = sorted(profiles)
        default = None
        if left:
            default = left[0] if data["default_profile"] == name else data["default_profile"]
            payload = (
                json.dumps(
                    {"version": 1, "default_profile": default, "profiles": profiles},
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            _parse_store(payload)
            if len(payload) > _MAX_BYTES:
                raise CredentialError("file_too_large")
            temp = f".credentials-{os.urandom(16).hex()}.tmp"
            temp_fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            descriptors.append(temp_fd)
            os.fchmod(temp_fd, 0o600)
            view = memoryview(payload)
            while view:
                view = view[os.write(temp_fd, view) :]
            os.fsync(temp_fd)
        # The store was read under the lock; it must still be exactly that.
        if _read_in(directory_fd) != old:
            raise CredentialError("unsafe_file")
        if not _same(os.stat(_LOCK, dir_fd=directory_fd, follow_symlinks=False), lock_info):
            raise CredentialError("unsafe_file")
        if temp is not None:
            os.replace(temp, _STORE, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            temp = None
        else:
            os.unlink(_STORE, dir_fd=directory_fd)
        committed = True
        try:
            os.fsync(directory_fd)
        except OSError:
            raise CredentialError("credential_remove_unconfirmed") from None
        return RemovedProfile(name, True, path, key_id, default)
    except CredentialError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise CredentialError(
            "credential_remove_unconfirmed" if committed else "credential_remove_failed"
        ) from None
    finally:
        if directory_fd >= 0:
            if temp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(temp, dir_fd=directory_fd)
            if lock_info is not None:
                with contextlib.suppress(OSError):
                    if _same(os.stat(_LOCK, dir_fd=directory_fd, follow_symlinks=False), lock_info):
                        os.unlink(_LOCK, dir_fd=directory_fd)
        for descriptor in reversed(descriptors):
            with contextlib.suppress(OSError):
                os.close(descriptor)
