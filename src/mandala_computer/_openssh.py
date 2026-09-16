"""The pieces of ``mandala ssh`` that do not talk to the API.

Everything here is pure or touches only local files: where the gateway is, the
known_hosts file the CLI manages, the ``ssh`` command line, the
``~/.ssh/config`` snippet, and finding and fingerprinting a public key. The
commands that use them live in ``_cli``.

The platform's SSH gateway is a jump host. A connection is two hops: OpenSSH to
the gateway on port 2222 as ``mandala``, and through it to the computer's own
sshd on port 22 as ``user``. The gateway's host key is pinned here, and the
computer's is trusted on first use, keyed by the computer's id so a rename does
not look like a different machine.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

#: The public gateway. ``MANDALA_SSH_GATEWAY`` overrides it (``host:port``).
GATEWAY_HOST = "ssh.mandala.computer"
GATEWAY_PORT = 2222
#: The gateway ignores the user name it is given; this is the one to give it.
GATEWAY_USER = "mandala"
#: The gateway's host key, pinned. ``MANDALA_SSH_GATEWAY_KNOWN_HOSTS`` overrides it.
GATEWAY_KNOWN_HOSTS = (
    "[ssh.mandala.computer]:2222 ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIJlZegWyY5KLksV9y22mZHnDI4qm++st9qZnbpSId1DR"
)
#: The account every computer's sshd logs you in as.
GUEST_USER = "user"
#: The ``Host`` name the ``ssh-config`` snippet gives the gateway.
GATEWAY_ALIAS = "mandala-gateway"

#: The public keys ``--setup`` and ``ssh-key add`` look for, in this order.
DEFAULT_KEYS = ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")

#: The CLI's own directory, shared with the saved credentials.
CONFIG_DIR = ".mandala"
#: The known_hosts file the CLI manages: the gateway's pin, and every computer's
#: key, trusted on first use.
KNOWN_HOSTS = "ssh_known_hosts"

_MARKER_BEGIN = "# >>> mandala {what} >>>"
_MARKER_END = "# <<< mandala {what} <<<"


@dataclass(frozen=True)
class Gateway:
    host: str
    port: int
    #: known_hosts lines pinning the gateway's key.
    known_hosts: tuple[str, ...]


def gateway(env: Mapping[str, str] | None = None) -> Gateway:
    """The gateway to use: the public one, or the environment's override.

    ``MANDALA_SSH_GATEWAY`` is ``host`` or ``host:port`` (``[v6]:port`` for an
    IPv6 address), port 2222 when none is given. ``MANDALA_SSH_GATEWAY_KNOWN_HOSTS``
    is a known_hosts line, or the path of a file of them, pinning its key.
    Without it, the public gateway's key is pinned under the override's name.
    """
    env = os.environ if env is None else env
    host, port = GATEWAY_HOST, GATEWAY_PORT
    spelled = env.get("MANDALA_SSH_GATEWAY", "").strip()
    if spelled:
        host, port = _host_port(spelled)
    pinned = env.get("MANDALA_SSH_GATEWAY_KNOWN_HOSTS", "").strip()
    if pinned:
        text = pinned
        if os.path.isfile(os.path.expanduser(pinned)):
            text = Path(os.path.expanduser(pinned)).read_text()
        lines = tuple(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if not lines:
            raise ValueError("MANDALA_SSH_GATEWAY_KNOWN_HOSTS holds no known_hosts line")
    else:
        # No pin of its own: the override is the same gateway at another
        # address, so the built-in key is pinned under the name ssh will look up.
        key = GATEWAY_KNOWN_HOSTS.split(None, 1)[1]
        lines = (f"{_known_hosts_name(host, port)} {key}",)
    for line in lines:
        if len(line.split()) < 3:
            raise ValueError(
                "MANDALA_SSH_GATEWAY_KNOWN_HOSTS must be known_hosts lines "
                "(<host> <key type> <base64>), or a file of them"
            )
    return Gateway(host, port, lines)


def _known_hosts_name(host: str, port: int) -> str:
    """How ssh names *host* in known_hosts: bracketed with the port unless it is 22."""
    return host if port == 22 else f"[{host}]:{port}"


def _host_port(spelled: str) -> tuple[str, int]:
    match = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?", spelled)
    if match:
        host, port_text = match.group(1), match.group(2)
    elif spelled.count(":") == 1:
        host, _, port_text = spelled.partition(":")
    else:
        host, port_text = spelled, None
    if not host or any(c.isspace() for c in host) or host.startswith("-"):
        raise ValueError(f"MANDALA_SSH_GATEWAY is not host:port: {spelled!r}")
    if port_text is None:
        return host, GATEWAY_PORT
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError(f"MANDALA_SSH_GATEWAY is not host:port: {spelled!r}")
    return host, int(port_text)


def known_hosts_path(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / CONFIG_DIR / KNOWN_HOSTS


def ensure_known_hosts(gw: Gateway, path: Path) -> None:
    """Make *path* pin the gateway, keeping every computer key already in it.

    The gateway's lines go first; any other line for the same host names is
    dropped, so a changed pin replaces the old one rather than sitting beside
    it. The directory is created 0700 and the file 0600, and the file is only
    rewritten when its content would change.
    """
    pinned_hosts = {line.split()[0] for line in gw.known_hosts}
    try:
        current = path.read_text()
    except FileNotFoundError:
        current = None
    kept = []
    for line in (current or "").splitlines():
        fields = line.split()
        if fields and fields[0] in pinned_hosts:
            continue
        kept.append(line)
    wanted = "\n".join([*gw.known_hosts, *[k for k in kept if k.strip()]]) + "\n"
    if wanted == current:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(path, wanted)


def _write_private(path: Path, text: str) -> None:
    """Replace *path* with *text*, mode 0600, never leaving a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _config_value(value: str) -> str:
    """One value for an ssh ``-o`` option or config line, quoted if it must be.

    ssh splits these values on whitespace — ``UserKnownHostsFile`` takes a list
    — so a path with a space in it is quoted, the one form OpenSSH reads back
    as a single word.
    """
    if value == "" or any(c.isspace() for c in value) or '"' in value:
        if '"' in value:
            raise ValueError(f"cannot pass a path containing a double quote to ssh: {value!r}")
        return f'"{value}"'
    return value


def _proxy_word(word: str, windows: bool) -> str:
    """One word of the ProxyCommand: shell-quoted, with ``%`` doubled.

    ssh expands ``%`` tokens in a ProxyCommand and hands the result to a shell
    (``$SHELL -c``), so a literal ``%`` in a path must be written ``%%`` and
    every word quoted for that shell.
    """
    quoted = subprocess.list2cmdline([word]) if windows else shlex.quote(word)
    return quoted.replace("%", "%%")


#: ssh's options that take an argument (OpenSSH 10), so a scan of the words
#: after the destination knows where each option ends and the command begins.
_SSH_OPTS_WITH_ARG = frozenset("BbcDEeFIiJLlmOoPpQRSWw")

#: ``-o`` settings that choose the key offered. The gateway checks the same key
#: as the computer does, so these go to both hops.
_IDENTITY_SETTINGS = frozenset(
    {"identityfile", "identitiesonly", "identityagent", "certificatefile"}
)


def identity_options(extra: Sequence[str]) -> list[str]:
    """The key-choosing options among *extra*, spelled for the gateway hop.

    ``-i PATH`` (or ``-iPATH``) and ``-o IdentityFile=…``-style settings. The
    gateway checks the key you offer, and a ``ProxyCommand`` hop does not
    inherit the outer command line, so ``mandala ssh dev -i ~/.ssh/work`` would
    otherwise offer that key to the computer and not to the gateway in front of
    it. The scan stops where ssh's own does: at ``--`` or the first word that
    is not an option, which begins the remote command.
    """
    found: list[str] = []
    i = 0
    while i < len(extra):
        word = extra[i]
        if word == "--" or not word.startswith("-") or word == "-":
            break
        j = 1
        while j < len(word):
            letter = word[j]
            if letter in _SSH_OPTS_WITH_ARG:
                value = word[j + 1 :]
                if not value:
                    i += 1
                    if i >= len(extra):
                        return found
                    value = extra[i]
                if letter == "i":
                    found += ["-i", value]
                elif letter == "o":
                    key = re.split(r"[\s=]", value.strip(), maxsplit=1)[0].lower()
                    if key in _IDENTITY_SETTINGS:
                        found += ["-o", value]
                break
            j += 1
        i += 1
    return found


def proxy_command(
    ssh: str,
    gw: Gateway,
    known_hosts: Path,
    identity: Sequence[str] = (),
    *,
    windows: bool = False,
) -> str:
    """The command that carries the connection through the gateway.

    Not ``-J``: ssh does not apply ``-o`` options from its own command line to
    a ``-J`` jump, so the pinned key would never reach that hop and every
    connection would fail host key verification. An explicit ``ProxyCommand``
    takes the options with it. *identity* is :func:`identity_options`.
    """
    words = [
        ssh,
        "-o",
        f"UserKnownHostsFile={_config_value(str(known_hosts))}",
        "-o",
        "StrictHostKeyChecking=yes",
        *identity,
        "-p",
        str(gw.port),
    ]
    parts = [_proxy_word(w, windows) for w in words]
    parts += ["-W", "%h:%p", _proxy_word(f"{GATEWAY_USER}@{gw.host}", windows)]
    return " ".join(parts)


def ssh_argv(
    ssh: str,
    computer_id: str,
    gw: Gateway,
    known_hosts: Path,
    extra: Sequence[str] = (),
    *,
    windows: bool = False,
) -> list[str]:
    """The whole ``ssh`` command line for one computer.

    The computer's id is the destination and its host key alias, so its key is
    stored once however the computer is renamed. Everything in *extra* follows
    the destination unchanged: forwarding flags, ``--`` and a remote command.
    Any key it chooses is also offered to the gateway (:func:`identity_options`).
    """
    kh = _config_value(str(known_hosts))
    return [
        ssh,
        "-o",
        f"User={GUEST_USER}",
        "-o",
        f"HostKeyAlias={computer_id}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={kh}",
        "-o",
        "ProxyCommand="
        + proxy_command(ssh, gw, known_hosts, identity_options(extra), windows=windows),
        computer_id,
        *extra,
    ]


def host_alias(name: str, computer_id: str) -> str:
    """The ``Host`` a config block is written under: the name, where it can be one."""
    if name and re.fullmatch(r"[A-Za-z0-9._-]+", name) and not name.startswith("-"):
        return name
    return computer_id


def config_snippet(name: str, computer_id: str, gw: Gateway, known_hosts: Path) -> str:
    """The ``~/.ssh/config`` blocks for one computer, markers included.

    Two blocks: the gateway, which ``ProxyJump`` does honour options for, and
    the computer, jumping through it. ``ssh <name>``, ``scp``, ``sftp`` and VS
    Code's Remote-SSH all read them.
    """
    kh = _config_value(str(known_hosts))
    gateway_block = "\n".join(
        [
            _MARKER_BEGIN.format(what="gateway"),
            f"Host {GATEWAY_ALIAS}",
            f"  HostName {gw.host}",
            f"  Port {gw.port}",
            f"  User {GATEWAY_USER}",
            f"  UserKnownHostsFile {kh}",
            "  StrictHostKeyChecking yes",
            _MARKER_END.format(what="gateway"),
        ]
    )
    computer_block = "\n".join(
        [
            _MARKER_BEGIN.format(what=f"computer {computer_id}"),
            f"Host {host_alias(name, computer_id)}",
            f"  HostName {computer_id}",
            f"  User {GUEST_USER}",
            f"  ProxyJump {GATEWAY_ALIAS}",
            f"  HostKeyAlias {computer_id}",
            f"  UserKnownHostsFile {kh}",
            "  StrictHostKeyChecking accept-new",
            _MARKER_END.format(what=f"computer {computer_id}"),
        ]
    )
    return f"{gateway_block}\n\n{computer_block}\n"


def _block_pattern(label: str) -> re.Pattern[str]:
    """One marked block, begin marker to end marker, for the label given."""
    return re.compile(
        rf"^{re.escape(_MARKER_BEGIN.format(what=label))}\n.*?"
        rf"^{re.escape(_MARKER_END.format(what=label))}$",
        re.MULTILINE | re.DOTALL,
    )


def merge_config(current: str, snippet: str) -> str:
    """*current* with each marked block of *snippet* replaced, or appended.

    Everything outside the markers is kept byte for byte. A block already
    there is replaced where it stands, so writing twice changes nothing.
    """
    text = current
    labels = re.findall(r"^# >>> mandala (.+?) >>>$", snippet, re.MULTILINE)
    for label in labels:
        block_match = _block_pattern(label).search(snippet)
        if block_match is None:  # pragma: no cover - the snippet is ours
            continue
        block = block_match.group(0)
        found = _block_pattern(label).search(text)
        if found is not None:
            text = text[: found.start()] + block + text[found.end() :]
            continue
        if text and not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        text += block + "\n"
    return text


def write_config(path: Path, snippet: str) -> bool:
    """Merge *snippet* into the ssh config at *path*. Whether it changed.

    A missing file is created 0600 (and ``~/.ssh`` 0700); an existing one keeps
    its mode.
    """
    try:
        current = path.read_text()
        mode = os.stat(path).st_mode & 0o7777
    except FileNotFoundError:
        current, mode = "", None
    merged = merge_config(current, snippet)
    if merged == current and mode is not None:
        return False
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(path, merged)
    if mode is not None:
        os.chmod(path, mode)
    return True


def find_default_key(home: Path | None = None) -> Path | None:
    """The first of :data:`DEFAULT_KEYS` in ``~/.ssh`` that exists."""
    ssh_dir = (Path.home() if home is None else home) / ".ssh"
    for name in DEFAULT_KEYS:
        candidate = ssh_dir / name
        if candidate.is_file():
            return candidate
    return None


def no_key_message() -> str:
    looked = ", ".join(f"~/.ssh/{n}" for n in DEFAULT_KEYS)
    return (
        f"no SSH public key found (looked for {looked}); "
        "create one with ssh-keygen -t ed25519, or pass --key PATH"
    )


def read_public_key(path: Path) -> str:
    """The one key line in a ``.pub`` file, or a ``ValueError`` saying what is wrong."""
    try:
        text = path.read_text()
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not an OpenSSH public key") from None
    if "PRIVATE KEY" in text:
        raise ValueError(f"{path} is a private key; pass the .pub file beside it")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(f"{path} must hold exactly one public key line")
    return lines[0]


def fingerprint(public_key: str) -> str:
    """``SHA256:…`` for a key line, exactly as ``ssh-keygen -l`` prints it."""
    fields = public_key.split()
    if len(fields) < 2:
        raise ValueError("not an OpenSSH public key line")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("not an OpenSSH public key line") from None
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"
