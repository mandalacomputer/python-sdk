"""SSH access: the SDK methods, and ``mandala ssh`` / ``ssh-key`` /
``ssh-access`` / ``ssh-config``.

``ssh`` itself is never run: ``_cli._exec`` is replaced, and what it would have
run is compared word for word. Everything the CLI writes goes under a
temporary HOME.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc
from mandala_computer import _cli, _openssh

BASE = "https://api.test/api/v1"
SSH = "/usr/bin/ssh"

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGqsBlqrbipXh/7n81gKS46IyjJY7nVv8mGtIAE+v76w"
#: What `ssh-keygen -lf` printed for PUBLIC_KEY.
FINGERPRINT = "SHA256:7OR2azJrv1nm44ploDfzY03D/74wXZGn8Qj4fabJDXs"
KEY = {
    "id": "sshk-74025eba1b658b99",
    "name": "laptop",
    "public_key": PUBLIC_KEY,
    "fingerprint": FINGERPRINT,
    "key_type": "ssh-ed25519",
    "created_at": "2026-09-16T12:00:00Z",
    "last_used_at": None,
}
OTHER_KEY = {**KEY, "id": "sshk-0000000000000001", "fingerprint": "SHA256:other", "name": "work"}
ON = {
    "computer": "vm-9",
    "enabled": True,
    "available": True,
    "pending": False,
    "key_count": 1,
    "keys_pushed": 1,
    "error": None,
}
OFF = {**ON, "enabled": False, "key_count": 0, "keys_pushed": 0}
PIN = _openssh.GATEWAY_KNOWN_HOSTS


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MANDALA_SSH_GATEWAY", raising=False)
    monkeypatch.delenv("MANDALA_SSH_GATEWAY_KNOWN_HOSTS", raising=False)
    monkeypatch.setattr(_cli, "LOCAL_WINDOWS", False)
    monkeypatch.setattr(_cli.shutil, "which", lambda name: SSH if name == "ssh" else None)
    return tmp_path


@pytest.fixture
def execs(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    ran: list[list[str]] = []

    def fake(argv: list[str]) -> int:
        ran.append(argv)
        return 0

    monkeypatch.setattr(_cli, "_exec", fake)
    return ran


def computers(name: str = "dev") -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(
            200, json=[{"id": "vm-9", "name": name, "status": "running", "os": "linux"}]
        )
    )


def kh(home: Path) -> Path:
    return home / ".mandala" / "ssh_known_hosts"


def expected_argv(home: Path, *extra: str, identity: str = "") -> list[str]:
    known = kh(home)
    return [
        SSH,
        "-o",
        "User=user",
        "-o",
        "HostKeyAlias=vm-9",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known}",
        "-o",
        (
            f"ProxyCommand={SSH} -o UserKnownHostsFile={known} -o StrictHostKeyChecking=yes "
            f"{identity}-p 2222 -W %h:%p mandala@ssh.mandala.computer"
        ),
        "vm-9",
        *extra,
    ]


# --- argument building -----------------------------------------------------


def test_argv_is_exact_with_the_public_gateway() -> None:
    gw = _openssh.gateway({})
    argv = _openssh.ssh_argv(SSH, "vm-9", gw, Path("/h/.mandala/ssh_known_hosts"))
    assert argv == [
        "/usr/bin/ssh",
        "-o",
        "User=user",
        "-o",
        "HostKeyAlias=vm-9",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "UserKnownHostsFile=/h/.mandala/ssh_known_hosts",
        "-o",
        (
            "ProxyCommand=/usr/bin/ssh -o UserKnownHostsFile=/h/.mandala/ssh_known_hosts "
            "-o StrictHostKeyChecking=yes -p 2222 -W %h:%p mandala@ssh.mandala.computer"
        ),
        "vm-9",
    ]


def test_argv_quotes_a_path_with_spaces_and_a_percent() -> None:
    """ssh splits UserKnownHostsFile on whitespace and expands % in a
    ProxyCommand before a shell reads it, so each needs its own escaping."""
    gw = _openssh.gateway({})
    argv = _openssh.ssh_argv(
        "/opt/my tools/ssh", "vm-9", gw, Path("/Users/a b/100%/.mandala/ssh_known_hosts")
    )
    assert argv[8] == 'UserKnownHostsFile="/Users/a b/100%/.mandala/ssh_known_hosts"'
    assert argv[10] == (
        "ProxyCommand='/opt/my tools/ssh' "
        "-o 'UserKnownHostsFile=\"/Users/a b/100%%/.mandala/ssh_known_hosts\"' "
        "-o StrictHostKeyChecking=yes -p 2222 -W %h:%p mandala@ssh.mandala.computer"
    )


def test_argv_passes_extra_words_through_verbatim_after_the_destination() -> None:
    gw = _openssh.gateway({})
    extra = ["-L", "8080:localhost:8080", "--", "echo", "two words", "'quoted'"]
    argv = _openssh.ssh_argv(SSH, "vm-9", gw, Path("/k"), extra)
    assert argv[argv.index("vm-9", 5) :] == ["vm-9", *extra]


@pytest.mark.parametrize(
    ("extra", "identity"),
    [
        (["-i", "/keys/work key"], ["-i", "/keys/work key"]),
        (["-i/keys/work"], ["-i", "/keys/work"]),
        (
            ["-v", "-o", "IdentitiesOnly=yes", "-i", "/k1"],
            ["-o", "IdentitiesOnly=yes", "-i", "/k1"],
        ),
        (["-o", "IdentityFile /k2"], ["-o", "IdentityFile /k2"]),
        (["-o", "ForwardAgent=yes", "-L", "1:h:2"], []),
        # The value of an option that takes one is not an option of its own.
        (["-L", "-i", "-p", "22"], []),
        # Past `--`, or past the first word of the command, it is the command's.
        (["--", "cmd", "-i", "/k"], []),
        (["uname", "-i", "/k"], []),
        (["-vi", "/k"], ["-i", "/k"]),
        (["-i"], []),
    ],
)
def test_the_key_you_choose_is_offered_to_the_gateway_too(
    extra: list[str], identity: list[str]
) -> None:
    assert _openssh.identity_options(extra) == identity


def test_a_chosen_key_lands_in_the_proxy_command_quoted() -> None:
    gw = _openssh.gateway({})
    argv = _openssh.ssh_argv(SSH, "vm-9", gw, Path("/k"), ["-i", "/my keys/id"])
    assert argv[10] == (
        "ProxyCommand=/usr/bin/ssh -o UserKnownHostsFile=/k -o StrictHostKeyChecking=yes "
        "-i '/my keys/id' -p 2222 -W %h:%p mandala@ssh.mandala.computer"
    )
    assert argv[-3:] == ["vm-9", "-i", "/my keys/id"]


def test_a_windows_proxy_command_is_quoted_for_windows() -> None:
    gw = _openssh.gateway({})
    command = _openssh.proxy_command(
        r"C:\Program Files\OpenSSH\ssh.exe", gw, Path("/k"), windows=True
    )
    assert command.startswith(r'"C:\Program Files\OpenSSH\ssh.exe" -o ')


# --- the gateway and its pin -----------------------------------------------


@pytest.mark.parametrize(
    ("spelled", "host", "port"),
    [
        ("gw.example.com:2200", "gw.example.com", 2200),
        ("gw.example.com", "gw.example.com", 2222),
        ("[2001:db8::1]:22", "2001:db8::1", 22),
        ("[2001:db8::1]", "2001:db8::1", 2222),
    ],
)
def test_the_gateway_override(spelled: str, host: str, port: int) -> None:
    gw = _openssh.gateway({"MANDALA_SSH_GATEWAY": spelled})
    assert (gw.host, gw.port) == (host, port)
    # The same gateway at another address: the built-in key, under the name
    # ssh looks up for that address.
    name = host if port == 22 else f"[{host}]:{port}"
    assert gw.known_hosts == (f"{name} {PIN.split(None, 1)[1]}",)


@respx.mock
def test_an_address_override_alone_pins_the_public_key_there(
    env: Path, execs: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MANDALA_SSH_GATEWAY", "gw.example.com:2200")
    mock_connect(ON, [KEY])
    assert _cli.main(["ssh", "dev"]) == 0
    assert kh(env).read_text() == (
        "[gw.example.com]:2200 ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIJlZegWyY5KLksV9y22mZHnDI4qm++st9qZnbpSId1DR\n"
    )
    assert execs[0][10].endswith("-p 2200 -W %h:%p mandala@gw.example.com")


@pytest.mark.parametrize("spelled", ["gw:0", "gw:99999", "gw:x", "-oProxy=x", "a b:22", "gw:"])
def test_a_malformed_gateway_override_is_refused(spelled: str) -> None:
    with pytest.raises(ValueError, match="MANDALA_SSH_GATEWAY"):
        _openssh.gateway({"MANDALA_SSH_GATEWAY": spelled})


def test_the_pin_override_is_a_line_or_a_file(tmp_path: Path) -> None:
    line = "[gw.example.com]:2200 ssh-ed25519 AAAAkey"
    assert _openssh.gateway({"MANDALA_SSH_GATEWAY_KNOWN_HOSTS": line}).known_hosts == (line,)
    pins = tmp_path / "pins"
    pins.write_text(f"# the test gateway\n{line}\n\n{line}2\n")
    assert _openssh.gateway({"MANDALA_SSH_GATEWAY_KNOWN_HOSTS": str(pins)}).known_hosts == (
        line,
        f"{line}2",
    )
    with pytest.raises(ValueError, match="known_hosts lines"):
        _openssh.gateway({"MANDALA_SSH_GATEWAY_KNOWN_HOSTS": "ssh-ed25519 AAAAkey"})


def test_known_hosts_is_created_private(tmp_path: Path) -> None:
    path = tmp_path / ".mandala" / "ssh_known_hosts"
    _openssh.ensure_known_hosts(_openssh.gateway({}), path)
    assert path.read_text() == PIN + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_known_hosts_keeps_computer_keys_and_replaces_a_stale_pin(tmp_path: Path) -> None:
    path = tmp_path / "ssh_known_hosts"
    path.write_text("[ssh.mandala.computer]:2222 ssh-ed25519 OLDKEY\nvm-9 ssh-ed25519 GUESTKEY\n")
    _openssh.ensure_known_hosts(_openssh.gateway({}), path)
    assert path.read_text() == f"{PIN}\nvm-9 ssh-ed25519 GUESTKEY\n"


def test_known_hosts_is_left_alone_when_it_is_already_right(tmp_path: Path) -> None:
    path = tmp_path / "ssh_known_hosts"
    path.write_text(f"{PIN}\nvm-9 ssh-ed25519 GUESTKEY\n")
    path.chmod(0o600)
    before = path.stat().st_ino
    _openssh.ensure_known_hosts(_openssh.gateway({}), path)
    assert path.stat().st_ino == before


# --- the config writer -----------------------------------------------------


def snippet(name: str = "dev", home: Path = Path("/h")) -> str:
    return _openssh.config_snippet(name, "vm-9", _openssh.gateway({}), kh(home))


def test_the_snippet_is_exact() -> None:
    assert snippet() == (
        "# >>> mandala gateway >>>\n"
        "Host mandala-gateway\n"
        "  HostName ssh.mandala.computer\n"
        "  Port 2222\n"
        "  User mandala\n"
        "  UserKnownHostsFile /h/.mandala/ssh_known_hosts\n"
        "  StrictHostKeyChecking yes\n"
        "# <<< mandala gateway <<<\n"
        "\n"
        "# >>> mandala computer vm-9 >>>\n"
        "Host dev\n"
        "  HostName vm-9\n"
        "  User user\n"
        "  ProxyJump mandala-gateway\n"
        "  HostKeyAlias vm-9\n"
        "  UserKnownHostsFile /h/.mandala/ssh_known_hosts\n"
        "  StrictHostKeyChecking accept-new\n"
        "# <<< mandala computer vm-9 <<<\n"
    )


@pytest.mark.parametrize(("name", "host"), [("dev", "dev"), ("my box", "vm-9"), ("", "vm-9")])
def test_a_name_that_cannot_be_a_host_falls_back_to_the_id(name: str, host: str) -> None:
    assert _openssh.host_alias(name, "vm-9") == host


def test_write_config_creates_a_private_file(tmp_path: Path) -> None:
    path = tmp_path / ".ssh" / "config"
    assert _openssh.write_config(path, snippet())
    assert path.read_text() == snippet()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_write_config_preserves_other_content_and_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text("Host work\n  User me")
    path.chmod(0o644)
    assert _openssh.write_config(path, snippet())
    assert path.read_text() == "Host work\n  User me\n\n" + snippet()
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_write_config_replaces_the_block_in_place_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "config"
    path.write_text(f"Host first\n\n{snippet('dev')}\nHost last\n  User me\n")
    assert _openssh.write_config(path, snippet("renamed"))
    text = path.read_text()
    assert text == f"Host first\n\n{snippet('renamed')}\nHost last\n  User me\n"
    assert "Host dev\n" not in text
    assert text.count("# >>> mandala gateway >>>") == 1
    assert not _openssh.write_config(path, snippet("renamed"))
    assert path.read_text() == text


def test_a_second_computer_adds_its_block_and_shares_the_gateway(tmp_path: Path) -> None:
    path = tmp_path / "config"
    _openssh.write_config(path, snippet())
    other = _openssh.config_snippet("ci", "vm-7", _openssh.gateway({}), kh(Path("/h")))
    _openssh.write_config(path, other)
    text = path.read_text()
    assert text.count("Host mandala-gateway") == 1
    assert "Host dev\n" in text
    assert "Host ci\n" in text


# --- keys on disk ----------------------------------------------------------


def test_key_discovery_order(tmp_path: Path) -> None:
    ssh_dir = tmp_path / ".ssh"
    assert _openssh.find_default_key(tmp_path) is None
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa.pub").write_text("rsa")
    assert _openssh.find_default_key(tmp_path) == ssh_dir / "id_rsa.pub"
    (ssh_dir / "id_ecdsa.pub").write_text("ecdsa")
    assert _openssh.find_default_key(tmp_path) == ssh_dir / "id_ecdsa.pub"
    (ssh_dir / "id_ed25519.pub").write_text("ed")
    assert _openssh.find_default_key(tmp_path) == ssh_dir / "id_ed25519.pub"


def test_fingerprint_matches_ssh_keygen() -> None:
    assert _openssh.fingerprint(PUBLIC_KEY + " me@laptop") == FINGERPRINT
    with pytest.raises(ValueError):
        _openssh.fingerprint("ssh-ed25519 !!!notbase64")


def test_read_public_key_refuses_what_is_not_one(tmp_path: Path) -> None:
    good = tmp_path / "good.pub"
    good.write_text(f"\n{PUBLIC_KEY} me@laptop\n")
    assert _openssh.read_public_key(good) == f"{PUBLIC_KEY} me@laptop"
    private = tmp_path / "id"
    private.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n")
    with pytest.raises(ValueError, match="private key"):
        _openssh.read_public_key(private)
    two = tmp_path / "two.pub"
    two.write_text(f"{PUBLIC_KEY}\n{PUBLIC_KEY}\n")
    with pytest.raises(ValueError, match="exactly one"):
        _openssh.read_public_key(two)


# --- SDK methods -----------------------------------------------------------


@respx.mock
def test_ssh_keys_and_access_decode() -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[KEY]))
    add = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    rm = respx.delete(f"{BASE}/ssh-keys/sshk-1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    respx.get(f"{BASE}/computers/vm-9").mock(
        return_value=httpx.Response(200, json={"id": "vm-9", "status": "running"})
    )
    put = respx.put(f"{BASE}/computers/vm-9/ssh").mock(
        return_value=httpx.Response(200, json={**ON, "available": None, "pending": True})
    )
    with mc.Client() as client:
        [key] = client.ssh_keys.list()
        assert key == mc.SshKey(
            id=KEY["id"],
            name="laptop",
            public_key=PUBLIC_KEY,
            fingerprint=FINGERPRINT,
            key_type="ssh-ed25519",
            created_at="2026-09-16T12:00:00Z",
            last_used_at=None,
        )
        client.ssh_keys.add(f"  {PUBLIC_KEY} me@laptop\n")
        assert json.loads(add.calls.last.request.content) == {
            "public_key": f"{PUBLIC_KEY} me@laptop"
        }
        client.ssh_keys.add(PUBLIC_KEY, name="laptop")
        assert json.loads(add.calls.last.request.content) == {
            "public_key": PUBLIC_KEY,
            "name": "laptop",
        }
        client.ssh_keys.remove("sshk-1")
        assert rm.called
        access = client.computers.get("vm-9").set_ssh_access(True)
        assert json.loads(put.calls.last.request.content) == {"enabled": True}
        assert access.enabled is True
        assert access.available is None
        assert access.pending is True
        assert access.key_count == 1


def test_access_reads_a_missing_enabled_as_off() -> None:
    access = mc.SshAccess.from_api({"computer": "vm-9", "available": False})
    assert access.enabled is False
    assert access.available is False
    assert access.error is None


@respx.mock
def test_ssh_refusals_map_to_the_usual_errors() -> None:
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(409, json={"error": "That key is already registered."})
    )
    respx.delete(f"{BASE}/ssh-keys/sshk-1").mock(
        return_value=httpx.Response(404, json={"error": "ssh key not found"})
    )
    with mc.Client() as client:
        with pytest.raises(mc.ConflictError, match="already registered"):
            client.ssh_keys.add(PUBLIC_KEY)
        with pytest.raises(mc.NotFoundError, match="not found"):
            client.ssh_keys.remove("sshk-1")


def test_bad_arguments_are_refused_before_a_request() -> None:
    with mc.Client() as client:
        with pytest.raises(ValueError, match="empty"):
            client.ssh_keys.add("  \n")
        with pytest.raises(ValueError, match="must be a string"):
            client.ssh_keys.add(PUBLIC_KEY, name=7)  # type: ignore[arg-type]
        computer = mc.Computer(client._t, {"id": "vm-9"})
        with pytest.raises(ValueError, match="True or False"):
            computer.set_ssh_access("true")  # type: ignore[arg-type]


@respx.mock
async def test_async_ssh_methods() -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[KEY]))
    respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    respx.delete(f"{BASE}/ssh-keys/sshk-1").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{BASE}/computers/vm-9/ssh").mock(return_value=httpx.Response(200, json=OFF))
    respx.put(f"{BASE}/computers/vm-9/ssh").mock(return_value=httpx.Response(200, json=ON))
    async with mc.AsyncClient() as client:
        assert [k.id for k in await client.ssh_keys.list()] == [KEY["id"]]
        assert (await client.ssh_keys.add(PUBLIC_KEY)).fingerprint == FINGERPRINT
        await client.ssh_keys.remove("sshk-1")
        computer = mc.AsyncComputer(client._t, {"id": "vm-9"})
        assert (await computer.ssh_access()).enabled is False
        assert (await computer.set_ssh_access(True)).enabled is True


# --- mandala ssh -----------------------------------------------------------


def mock_connect(access: dict[str, Any], keys: list[dict[str, Any]]) -> None:
    computers()
    respx.get(f"{BASE}/computers/vm-9/ssh").mock(return_value=httpx.Response(200, json=access))
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=keys))


@respx.mock
def test_ssh_execs_openssh_with_the_computer_id(env: Path, execs: list[list[str]]) -> None:
    mock_connect(ON, [KEY])
    assert _cli.main(["ssh", "dev", "-L", "8080:localhost:8080", "--", "uname", "-a"]) == 0
    assert execs == [expected_argv(env, "-L", "8080:localhost:8080", "--", "uname", "-a")]
    assert kh(env).read_text() == PIN + "\n"


@respx.mock
def test_ssh_offers_a_chosen_key_to_both_hops(env: Path, execs: list[list[str]]) -> None:
    mock_connect(ON, [KEY])
    assert _cli.main(["ssh", "vm-9", "-i", "/k/id", "hostname"]) == 0
    assert execs == [expected_argv(env, "-i", "/k/id", "hostname", identity="-i /k/id ")]


@respx.mock
def test_ssh_honours_the_gateway_override(
    env: Path, execs: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MANDALA_SSH_GATEWAY", "gw.example.com:2200")
    monkeypatch.setenv("MANDALA_SSH_GATEWAY_KNOWN_HOSTS", "[gw.example.com]:2200 ssh-ed25519 K")
    mock_connect(ON, [KEY])
    assert _cli.main(["ssh", "dev"]) == 0
    assert execs[0][10].endswith("-p 2200 -W %h:%p mandala@gw.example.com")
    assert kh(env).read_text() == "[gw.example.com]:2200 ssh-ed25519 K\n"


@pytest.mark.parametrize(
    ("access", "keys", "message"),
    [
        (
            OFF,
            [KEY],
            (
                'mandala-py: SSH is off for dev; run "mandala-py ssh --setup dev" to turn it on, '
                'or use "mandala-py terminal dev" for a shell without a key'
            ),
        ),
        (
            {**ON, "available": False},
            [KEY],
            (
                "mandala-py: dev was made from a template that predates SSH; create a new computer "
                'to use SSH, or use "mandala-py terminal dev" for a shell without a key'
            ),
        ),
        (
            ON,
            [],
            (
                'mandala-py: you have no SSH keys registered; run "mandala-py ssh --setup dev" to add '
                'one, or use "mandala-py terminal dev" for a shell without a key'
            ),
        ),
    ],
)
@respx.mock
def test_ssh_refuses_and_never_falls_back_to_the_terminal(
    access: dict[str, Any],
    keys: list[dict[str, Any]],
    message: str,
    execs: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_connect(access, keys)
    monkeypatch.setattr(_cli, "_interact", lambda url: pytest.fail("must not open a terminal"))
    with pytest.raises(SystemExit) as caught:
        _cli.main(["ssh", "dev"])
    assert caught.value.code == message
    assert execs == []


def test_ssh_without_an_ssh_binary_exits_127(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], execs: list[list[str]]
) -> None:
    monkeypatch.setattr(_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(_cli, "_client", lambda: pytest.fail("must not make an API request"))
    with pytest.raises(SystemExit) as caught:
        _cli.main(["ssh", "dev"])
    assert caught.value.code == 127
    assert capsys.readouterr().err == (
        "mandala-py: no ssh command found on PATH; install OpenSSH, "
        'or use "mandala-py terminal <computer>" for a shell without it\n'
    )
    assert execs == []


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["ssh"], "name a computer"),
        (["ssh", "--json", "dev"], "ssh is interactive and has no --json output"),
        (["ssh", "--key", "k.pub", "dev"], "--key goes with --setup"),
        (["ssh", "-L", "1:h:2", "dev"], "unrecognized option -L before the computer"),
        (["ssh", "--setup", "dev", "-i", "k"], "unrecognized option -i with --setup"),
        (["ssh", "--setup", "dev", "other"], "--setup takes one computer"),
        (["ssh", "--setup", "dev", "--key"], "--key needs a PATH"),
    ],
)
def test_ssh_usage_errors_exit_2(
    argv: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    execs: list[list[str]],
) -> None:
    monkeypatch.setattr(_cli, "_client", lambda: pytest.fail("must not make an API request"))
    assert _cli.main(argv) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert f"mandala-py ssh: error: {message}" in err
    assert execs == []


def test_ssh_help(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, "_client", lambda: pytest.fail("must not make an API request"))
    assert _cli.main(["ssh", "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: mandala-py ssh <computer> [ssh-args ...]")
    assert "MANDALA_SSH_GATEWAY" in out


def test_windows_runs_ssh_and_returns_its_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_cli, "LOCAL_WINDOWS", True)
    monkeypatch.setattr(_cli.subprocess, "call", lambda argv: 42)
    monkeypatch.setattr(_cli.os, "execv", lambda *a: pytest.fail("must not exec on Windows"))
    assert _cli._exec(["ssh", "vm-9"]) == 42


def test_posix_replaces_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, list[str]]] = []

    def execv(path: str, argv: list[str]) -> None:
        seen.append((path, argv))
        raise SystemExit(0)

    monkeypatch.setattr(_cli.os, "execv", execv)
    with pytest.raises(SystemExit):
        _cli._exec([SSH, "vm-9"])
    assert seen == [(SSH, [SSH, "vm-9"])]


# --- mandala ssh --setup ---------------------------------------------------


@pytest.fixture
def pub(env: Path) -> Path:
    ssh_dir = env / ".ssh"
    ssh_dir.mkdir()
    path = ssh_dir / "id_ed25519.pub"
    path.write_text(f"{PUBLIC_KEY} me@laptop\n")
    return path


#: What a computer that has never been asked answers before setup.
UNASKED = {**OFF, "available": None}


def mock_setup(
    keys: list[dict[str, Any]],
    access: dict[str, Any] = ON,
    before: dict[str, Any] = UNASKED,
) -> tuple[Any, Any]:
    computers()
    respx.get(f"{BASE}/computers/vm-9/ssh").mock(return_value=httpx.Response(200, json=before))
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=keys))
    add = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    put = respx.put(f"{BASE}/computers/vm-9/ssh").mock(
        return_value=httpx.Response(200, json=access)
    )
    return add, put


@respx.mock
def test_setup_registers_the_default_key_and_switches_ssh_on(
    pub: Path, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add, put = mock_setup([OTHER_KEY])
    assert _cli.main(["ssh", "--setup", "dev"]) == 0
    assert json.loads(add.calls.last.request.content) == {"public_key": f"{PUBLIC_KEY} me@laptop"}
    assert json.loads(put.calls.last.request.content) == {"enabled": True}
    assert capsys.readouterr().out == (
        f"key {FINGERPRINT} (laptop) registered\nSSH is on for dev\nconnect with: mandala-py ssh dev\n"
    )
    assert kh(env).read_text() == PIN + "\n"


@respx.mock
def test_setup_is_idempotent(pub: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add, put = mock_setup([OTHER_KEY, KEY])
    assert _cli.main(["ssh", "--setup", "dev"]) == 0
    assert _cli.main(["ssh", "--setup", "dev"]) == 0
    assert not add.called
    assert put.call_count == 2
    out = capsys.readouterr().out
    assert out.count(f"key {FINGERPRINT} (laptop) already registered\n") == 2


@respx.mock
def test_setup_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = tmp_path / "chosen.pub"
    key.write_text(PUBLIC_KEY)
    mock_setup([])
    assert _cli.main(["ssh", "--setup", "dev", "--key", str(key), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "computer": "vm-9",
        "name": "dev",
        "key": KEY,
        "key_added": True,
        "ssh": ON,
        "command": "mandala-py ssh dev",
    }


@respx.mock
def test_setup_without_a_key_says_how_to_make_one(env: Path) -> None:
    mock_setup([])
    with pytest.raises(SystemExit, match="no SSH public key found .*ssh-keygen -t ed25519"):
        _cli.main(["ssh", "--setup", "dev"])


@respx.mock
@pytest.mark.parametrize(
    ("access", "message"),
    [
        (
            {**ON, "available": False},
            (
                "mandala-py: dev was made from a template that predates SSH; "
                "create a new computer to use SSH"
            ),
        ),
        (
            {**ON, "error": "no room"},
            "mandala-py: the computer's host refused the SSH setting: no room",
        ),
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_setup_that_cannot_work_prints_no_success(
    pub: Path,
    env: Path,
    capsys: pytest.CaptureFixture[str],
    access: dict[str, Any],
    message: str,
    as_json: bool,
) -> None:
    """A failed setup looks like every other CLI failure, --json included:
    one line on stderr, exit 1, and nothing on stdout a script could parse
    as success."""
    with respx.mock:
        mock_setup([KEY], access)
        argv = ["ssh", "--setup", "dev", *(["--json"] if as_json else [])]
        with pytest.raises(SystemExit) as caught:
            _cli.main(argv)
    assert caught.value.code == message
    assert capsys.readouterr().out == ""
    assert not kh(env).exists()


@respx.mock
def test_setup_refuses_a_computer_that_predates_ssh_before_changing_anything(
    pub: Path, env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    add, put = mock_setup([], before={**OFF, "available": False})
    with pytest.raises(SystemExit) as caught:
        _cli.main(["ssh", "--setup", "dev", "--json"])
    assert caught.value.code == (
        "mandala-py: dev was made from a template that predates SSH; create a new computer to use SSH"
    )
    assert not add.called
    assert not put.called
    assert capsys.readouterr().out == ""
    assert not kh(env).exists()


@respx.mock
def test_setup_survives_a_racing_setup_of_its_own(
    pub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Listed before a concurrent run added the key: the add is a 409, and a
    second listing shows the key is ours, so setup carries on."""
    computers()
    respx.get(f"{BASE}/computers/vm-9/ssh").mock(return_value=httpx.Response(200, json=UNASKED))
    respx.get(f"{BASE}/ssh-keys").mock(
        side_effect=[httpx.Response(200, json=[]), httpx.Response(200, json=[KEY])]
    )
    add = respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(409, json={"error": "That key is already registered."})
    )
    put = respx.put(f"{BASE}/computers/vm-9/ssh").mock(return_value=httpx.Response(200, json=ON))
    assert _cli.main(["ssh", "--setup", "dev", "--json"]) == 0
    assert add.call_count == 1
    assert put.called
    printed = json.loads(capsys.readouterr().out)
    assert printed["key_added"] is False
    assert printed["key"] == KEY


@respx.mock
def test_setup_refuses_a_key_somebody_else_owns(
    pub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    computers()
    respx.get(f"{BASE}/computers/vm-9/ssh").mock(return_value=httpx.Response(200, json=UNASKED))
    keys = respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(
            409,
            json={"error": "That key is already registered. A key can belong to one person only."},
        )
    )
    put = respx.put(f"{BASE}/computers/vm-9/ssh").mock(return_value=httpx.Response(200, json=ON))
    assert _cli.main(["ssh", "--setup", "dev"]) == 1
    assert keys.call_count == 2
    assert not put.called
    out, err = capsys.readouterr()
    assert out == ""
    assert err == (
        "mandala-py: That key is already registered. A key can belong to one person only.\n"
    )


@respx.mock
def test_setup_reports_a_pending_setting(pub: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mock_setup([KEY], {**ON, "pending": True})
    assert _cli.main(["ssh", "--setup", "dev"]) == 0
    assert "has not received the setting yet" in capsys.readouterr().err


# --- ssh-key, ssh-access, ssh-config ---------------------------------------


@respx.mock
def test_ssh_key_list(capsys: pytest.CaptureFixture[str]) -> None:
    route = respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[KEY]))
    assert _cli.main(["ssh-key", "list"]) == 0
    assert capsys.readouterr().out == (
        "ID                     TYPE         FINGERPRINT"
        "                                         LAST USED  NAME\n"
        f"sshk-74025eba1b658b99  ssh-ed25519  {FINGERPRINT}  never      laptop\n"
    )
    assert _cli.main(["ssh-key", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [KEY]
    route.mock(return_value=httpx.Response(200, json=[]))
    assert _cli.main(["ssh-key", "list"]) == 0
    assert capsys.readouterr() == ("", "no SSH keys\n")


@respx.mock
def test_ssh_key_add_and_rm(pub: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    rm = respx.delete(f"{BASE}/ssh-keys/{KEY['id']}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert _cli.main(["ssh-key", "add", "--name", "laptop"]) == 0
    assert json.loads(add.calls.last.request.content) == {
        "public_key": f"{PUBLIC_KEY} me@laptop",
        "name": "laptop",
    }
    assert capsys.readouterr().out == f"added {KEY['id']}  {FINGERPRINT}  laptop\n"
    assert _cli.main(["ssh-key", "add", str(pub), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == KEY
    assert _cli.main(["ssh-key", "rm", KEY["id"]]) == 0
    assert rm.called
    assert capsys.readouterr().out == f"removed {KEY['id']}\n"


@respx.mock
def test_ssh_key_rm_of_an_unknown_key_fails(capsys: pytest.CaptureFixture[str]) -> None:
    respx.delete(f"{BASE}/ssh-keys/sshk-x").mock(
        return_value=httpx.Response(404, json={"error": "ssh key not found"})
    )
    assert _cli.main(["ssh-key", "rm", "sshk-x"]) == 1
    assert capsys.readouterr().err == "mandala-py: ssh key not found\n"


@respx.mock
def test_ssh_access_shows_and_switches(capsys: pytest.CaptureFixture[str]) -> None:
    computers()
    respx.get(f"{BASE}/computers/vm-9/ssh").mock(
        return_value=httpx.Response(200, json={**OFF, "available": None})
    )
    put = respx.put(f"{BASE}/computers/vm-9/ssh").mock(
        return_value=httpx.Response(200, json={**ON, "pending": True, "error": "refused"})
    )
    assert _cli.main(["ssh-access", "dev"]) == 0
    assert capsys.readouterr().out == (
        "SSH is off for dev\n"
        "  whether dev can run SSH is not known yet; it is checked at its next start\n"
    )
    assert not put.called
    assert _cli.main(["ssh-access", "dev", "on"]) == 0
    assert json.loads(put.calls.last.request.content) == {"enabled": True}
    assert capsys.readouterr().out == (
        "SSH is on for dev\n"
        "  keys: 1 of 1 delivered\n"
        "  pending: the computer's host has not received the current setting yet\n"
        "  error: refused\n"
    )
    assert _cli.main(["ssh-access", "dev", "off", "--json"]) == 0
    assert json.loads(put.calls.last.request.content) == {"enabled": False}
    assert json.loads(capsys.readouterr().out)["pending"] is True


def test_ssh_access_refuses_an_unknown_state(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        _cli.main(["ssh-access", "dev", "yes"])
    assert caught.value.code == 2


@respx.mock
def test_ssh_config_prints(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    computers()
    assert _cli.main(["ssh-config", "dev"]) == 0
    assert capsys.readouterr().out == snippet(home=env)
    assert kh(env).read_text() == PIN + "\n"
    assert not (env / ".ssh" / "config").exists()


@respx.mock
def test_ssh_config_writes(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    computers()
    config = env / ".ssh" / "config"
    assert _cli.main(["ssh-config", "dev", "--write"]) == 0
    assert capsys.readouterr().out == (f"wrote Host dev in {config}\nconnect with: ssh dev\n")
    assert config.read_text() == snippet(home=env)
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert _cli.main(["ssh-config", "dev", "--write", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "computer": "vm-9",
        "name": "dev",
        "host": "dev",
        "config": snippet(home=env),
        "path": str(config),
        "changed": False,
    }


@respx.mock
def test_ssh_config_uses_the_id_when_another_computer_shares_the_name(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "vm-9", "name": "dev", "status": "running", "os": "linux"},
                {"id": "vm-7", "name": "dev", "status": "running", "os": "linux"},
            ],
        )
    )
    assert _cli.main(["ssh-config", "vm-9", "--write"]) == 0
    out, err = capsys.readouterr()
    assert err == "mandala-py: another computer is also named dev; using Host vm-9 instead\n"
    config = env / ".ssh" / "config"
    assert out == f"wrote Host vm-9 in {config}\nconnect with: ssh vm-9\n"
    assert config.read_text() == snippet("vm-9", home=env)
    assert "Host dev\n" not in config.read_text()
    assert _cli.main(["ssh-config", "vm-7", "--json"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["host"] == "vm-7"
    assert "using Host vm-7 instead" in err


@respx.mock
def test_ssh_config_uses_the_id_when_the_listing_is_incomplete(
    env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "vm-9", "name": "dev", "status": "running", "os": "linux"}],
            headers={"X-GC-Incomplete": "1"},
        )
    )
    assert _cli.main(["ssh-config", "vm-9", "--json"]) == 0
    out, err = capsys.readouterr()
    assert err == "mandala-py: could not check other computers' names; using Host vm-9 instead\n"
    printed = json.loads(out)
    assert printed["host"] == "vm-9"
    assert printed["config"] == snippet("vm-9", home=env)


@respx.mock
def test_ssh_config_json_without_write(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    computers("my box")
    assert _cli.main(["ssh-config", "vm-9", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["host"] == "vm-9"
    assert printed["path"] is None
    assert printed["changed"] is None
    assert "Host vm-9\n" in printed["config"]
