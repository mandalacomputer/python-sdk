"""Proving an SSH key is yours: the challenge, the signed add, and each key's
``possession`` — on both clients and the CLI."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414

import mandala_computer as mc
from mandala_computer import _api, _cli, _openssh

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGqsBlqrbipXh/7n81gKS46IyjJY7nVv8mGtIAE+v76w"
KEY: dict[str, Any] = {
    "id": "sshk-74025eba1b658b99",
    "name": "laptop",
    "public_key": PUBLIC_KEY,
    "fingerprint": "SHA256:7OR2azJrv1nm44ploDfzY03D/74wXZGn8Qj4fabJDXs",
    "key_type": "ssh-ed25519",
    "created_at": "2026-09-16T12:00:00Z",
    "last_used_at": None,
    "reach": "this_account",
    "possession": "proven",
}
CHALLENGE: dict[str, Any] = {
    "challenge": "sshkc-" + "ab" * 32,
    "namespace": "ssh-key-registration@mandala.computer",
    "expires_at": "2026-11-11T12:10:00.000Z",
    "sign_command": "printf '%s' 'sshkc-…' | ssh-keygen -Y sign -n mandala-ssh-key -f ~/.ssh/id_ed25519",
}
SIGNATURE = "-----BEGIN SSH SIGNATURE-----\nU1NIU0lHAAAAAQ==\n-----END SSH SIGNATURE-----\n"


@pytest.mark.parametrize("value", ["proven", "unproven", "legacy", "a-newer-word"])
def test_possession_is_read_as_the_platform_sent_it(value: str) -> None:
    assert mc.SshKey.from_api({**KEY, "possession": value}).possession == value


def test_possession_is_none_from_a_platform_that_does_not_send_it() -> None:
    assert (
        mc.SshKey.from_api({k: v for k, v in KEY.items() if k != "possession"}).possession is None
    )


def test_a_possession_that_is_not_a_string_is_refused() -> None:
    with pytest.raises(mc.MandalaError, match="possession"):
        mc.SshKey.from_api({**KEY, "possession": 1})


def test_possession_is_keyword_only_so_positional_construction_is_unchanged() -> None:
    key = mc.SshKey("sshk-1", "n", PUBLIC_KEY, "SHA256:x", "ssh-ed25519", "t", None, {})
    assert key.possession is None and key.reach is None


@respx.mock
async def test_challenge_posts_with_no_body_and_reads_the_challenge(client: Any) -> None:
    route = respx.post(f"{BASE}/ssh-keys/challenge").mock(
        return_value=httpx.Response(201, json=CHALLENGE)
    )
    issued = await resolved(client.ssh_keys.challenge())
    assert route.calls.last.request.content == b""
    assert issued == mc.SshKeyChallenge(
        CHALLENGE["challenge"],
        "ssh-key-registration@mandala.computer",
        CHALLENGE["expires_at"],
        CHALLENGE["sign_command"],
    )


@respx.mock
async def test_a_challenge_answer_without_its_challenge_is_refused(client: Any) -> None:
    respx.post(f"{BASE}/ssh-keys/challenge").mock(
        return_value=httpx.Response(201, json={**CHALLENGE, "challenge": ""})
    )
    with pytest.raises(mc.MandalaError, match="challenge"):
        await resolved(client.ssh_keys.challenge())


@respx.mock
async def test_add_sends_the_challenge_and_signature_as_given(client: Any) -> None:
    route = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    key = await resolved(
        client.ssh_keys.add(
            PUBLIC_KEY + "\n", name="laptop", challenge=CHALLENGE["challenge"], signature=SIGNATURE
        )
    )
    assert json.loads(route.calls.last.request.content) == {
        "public_key": PUBLIC_KEY,
        "name": "laptop",
        "challenge": CHALLENGE["challenge"],
        "signature": SIGNATURE,
    }
    assert key.possession == "proven"


@respx.mock
async def test_add_without_proof_sends_neither_field(client: Any) -> None:
    route = respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "possession": "unproven"})
    )
    key = await resolved(client.ssh_keys.add(PUBLIC_KEY))
    assert json.loads(route.calls.last.request.content) == {"public_key": PUBLIC_KEY}
    assert key.possession == "unproven"


@pytest.mark.parametrize(
    "proof", [{"challenge": "sshkc-x"}, {"signature": SIGNATURE}], ids=["challenge", "signature"]
)
@respx.mock
async def test_one_half_of_the_proof_is_refused_before_anything_is_sent(
    client: Any, proof: dict[str, str]
) -> None:
    route = respx.post(f"{BASE}/ssh-keys")
    with pytest.raises(ValueError, match="pass both or neither"):
        await resolved(client.ssh_keys.add(PUBLIC_KEY, **proof))
    assert not route.called


# --- CLI ----------------------------------------------------------------------


@pytest.fixture
def pub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    # No ssh-keygen unless a test asks for one: the real one is never run.
    monkeypatch.setattr(_cli.shutil, "which", lambda name: None)
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "id_ed25519.pub"
    path.write_text(f"{PUBLIC_KEY} me@laptop\n")
    return path


@respx.mock
def test_cli_challenge_prints_what_to_sign(pub: Path, capsys: pytest.CaptureFixture[str]) -> None:
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    assert _cli.main(["ssh-key", "challenge"]) == 0
    captured = capsys.readouterr()
    assert f"challenge   {CHALLENGE['challenge']}" in captured.out
    assert "namespace   ssh-key-registration@mandala.computer" in captured.out
    assert "--signature-file" in captured.err
    assert _cli.main(["ssh-key", "challenge", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == CHALLENGE


@respx.mock
def test_cli_add_sends_the_signature_from_a_file(
    pub: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    route = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    sig = tmp_path / "sig"
    sig.write_text(SIGNATURE)
    argv = ["ssh-key", "add", str(pub), "--challenge", CHALLENGE["challenge"]]
    assert _cli.main([*argv, "--signature-file", str(sig)]) == 0
    sent = json.loads(route.calls.last.request.content)
    assert (sent["challenge"], sent["signature"]) == (CHALLENGE["challenge"], SIGNATURE)
    assert capsys.readouterr().err == ""


@respx.mock
def test_cli_add_reads_the_signature_from_stdin(pub: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    monkeypatch.setattr("sys.stdin", io.StringIO(SIGNATURE))
    argv = ["ssh-key", "add", str(pub), "--challenge", CHALLENGE["challenge"]]
    assert _cli.main([*argv, "--signature-file", "-"]) == 0
    assert json.loads(route.calls.last.request.content)["signature"] == SIGNATURE


@respx.mock
def test_cli_add_of_an_unproven_key_says_what_it_cannot_reach(
    pub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "possession": "unproven"})
    )
    assert _cli.main(["ssh-key", "add", str(pub)]) == 0
    err = capsys.readouterr().err
    assert "unproven" in err and "by its id" in err and "ssh-key challenge" in err


@respx.mock
def test_cli_add_with_half_the_proof_sends_nothing(
    pub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    route = respx.post(f"{BASE}/ssh-keys")
    assert _cli.main(["ssh-key", "add", str(pub), "--challenge", "sshkc-x"]) != 0
    assert not route.called
    assert "--signature-file" in capsys.readouterr().err


# --- the CLI signs for itself -----------------------------------------------

KEYGEN = "/usr/bin/ssh-keygen"


class Signer:
    """Stands in for :func:`_openssh.sign_challenge`: records what it was asked."""

    def __init__(self, signature: str | None = SIGNATURE, reason: str = "") -> None:
        self.signature, self.reason = signature, reason
        self.calls: list[tuple[str, Path, str, str]] = []

    def __call__(
        self, keygen: str, public_key: Path, namespace: str, challenge: str, *, echo: Any
    ) -> tuple[str | None, str]:
        self.calls.append((keygen, public_key, namespace, challenge))
        self.echo = echo
        return self.signature, self.reason


@pytest.fixture
def keygen(monkeypatch: pytest.MonkeyPatch, pub: Path) -> Path:
    monkeypatch.setattr(_cli.shutil, "which", lambda name: KEYGEN if name == "ssh-keygen" else None)
    return pub


def sign_with(monkeypatch: pytest.MonkeyPatch, signer: Signer) -> Signer:
    monkeypatch.setattr(_cli._openssh, "sign_challenge", signer)
    return signer


def test_signing_keys_try_the_pub_then_the_private_file_beside_it(tmp_path: Path) -> None:
    public = tmp_path / "id_ed25519.pub"
    public.write_text(PUBLIC_KEY)
    assert _openssh.signing_keys(public) == [public]
    (tmp_path / "id_ed25519").write_text("private")
    assert _openssh.signing_keys(public) == [public, tmp_path / "id_ed25519"]
    other = tmp_path / "key.txt"
    assert _openssh.signing_keys(other) == [other]


def runner(*answers: tuple[int, str | None, str]) -> tuple[Any, list[Any]]:
    """A stand-in ssh-keygen: each answer is its exit status, the signature it
    writes beside the file it signs (or none), and its standard error."""
    seen: list[Any] = []
    queue = list(answers)

    def run(argv: list[str], echo: Any) -> tuple[int, list[str]]:
        signed = Path(argv[-1])
        seen.append((argv[:-1], signed.read_bytes(), oct(signed.stat().st_mode & 0o777)))
        code, signature, err = queue.pop(0)
        if signature is not None:
            Path(f"{signed}.sig").write_text(signature)
        return code, err.splitlines()

    return run, seen


def test_sign_challenge_signs_a_private_file_holding_the_challenge_exactly(
    tmp_path: Path,
) -> None:
    public = tmp_path / "id_ed25519.pub"
    run, seen = runner((0, SIGNATURE, "Signing file /tmp/x/challenge\n"))
    got = _openssh.sign_challenge(KEYGEN, public, "ns", "sshkc-1", run=run)
    assert got == (SIGNATURE, "")
    assert seen == [([KEYGEN, "-Y", "sign", "-n", "ns", "-f", str(public)], b"sshkc-1", "0o600")]


def test_sign_challenge_leaves_no_files_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_openssh.tempfile, "tempdir", str(tmp_path / "t"))
    (tmp_path / "t").mkdir()
    run, _ = runner((0, SIGNATURE, ""))
    _openssh.sign_challenge(KEYGEN, tmp_path / "k.pub", "ns", "c", run=run)
    run, _ = runner((1, None, "boom"))
    _openssh.sign_challenge(KEYGEN, tmp_path / "k.pub", "ns", "c", run=run)
    assert list((tmp_path / "t").iterdir()) == []


def test_sign_challenge_falls_back_to_the_private_file(tmp_path: Path) -> None:
    public = tmp_path / "id_ed25519.pub"
    (tmp_path / "id_ed25519").write_text("private")
    run, seen = runner((255, None, "Couldn't sign: agent refused operation\n"), (0, SIGNATURE, ""))
    assert _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=run) == (SIGNATURE, "")
    assert [argv[-1] for argv, _, _ in seen] == [str(public), str(tmp_path / "id_ed25519")]


def test_sign_challenge_says_why_it_could_not(tmp_path: Path) -> None:
    public = tmp_path / "id_ed25519.pub"
    run, _ = runner((255, None, "Signing file /tmp/x/challenge\nNo private key found\n\n"))
    assert _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=run) == (
        None,
        "ssh-keygen: No private key found",
    )
    run, _ = runner((0, "not a signature", ""))
    assert _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=run) == (
        None,
        "ssh-keygen exited 0 without a signature",
    )

    def broken(argv: list[str], echo: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    assert _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=broken) == (
        None,
        "could not run ssh-keygen: Permission denied",
    )


@respx.mock
def test_cli_add_signs_a_challenge_itself(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    route = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    signer = sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 0
    assert signer.calls == [
        (KEYGEN, keygen, "ssh-key-registration@mandala.computer", CHALLENGE["challenge"])
    ]
    sent = json.loads(route.calls.last.request.content)
    assert (sent["challenge"], sent["signature"]) == (CHALLENGE["challenge"], SIGNATURE)
    # On condition that no registration of the key landed meanwhile.
    assert sent["replaces"] is None
    assert capsys.readouterr().err == ""


@respx.mock
def test_cli_add_that_cannot_sign_adds_unproven_and_says_why(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    route = respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "possession": "unproven"})
    )
    sign_with(monkeypatch, Signer(None, "ssh-keygen: No private key found"))
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 0
    sent = json.loads(route.calls.last.request.content)
    assert "challenge" not in sent and "signature" not in sent
    err = capsys.readouterr().err
    assert "unproven (ssh-keygen: No private key found)" in err and "by its id" in err


@respx.mock
def test_cli_add_against_a_platform_without_challenges_adds_unproven(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(
        return_value=httpx.Response(404, json={"error": "Not found."})
    )
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "possession": "unproven"})
    )
    signer = sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 0
    assert signer.calls == []
    assert "does not issue challenges" in capsys.readouterr().err


@respx.mock
def test_cli_add_without_ssh_keygen_asks_for_no_challenge(
    pub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    challenge = respx.post(f"{BASE}/ssh-keys/challenge")
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "possession": "unproven"})
    )
    assert _cli.main(["ssh-key", "add", str(pub)]) == 0
    assert not challenge.called
    assert "no ssh-keygen on PATH" in capsys.readouterr().err


@respx.mock
def test_cli_add_no_proof_runs_nothing(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    challenge = respx.post(f"{BASE}/ssh-keys/challenge")
    route = respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(201, json={**KEY, "possession": "unproven"})
    )
    signer = sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen), "--no-proof"]) == 0
    assert not challenge.called and signer.calls == []
    assert json.loads(route.calls.last.request.content) == {"public_key": f"{PUBLIC_KEY} me@laptop"}
    assert "unproven (--no-proof)" in capsys.readouterr().err


@respx.mock
def test_cli_add_no_proof_with_a_signature_is_refused(
    keygen: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    route = respx.post(f"{BASE}/ssh-keys")
    sig = tmp_path / "sig"
    sig.write_text(SIGNATURE)
    argv = ["ssh-key", "add", str(keygen), "--no-proof", "--challenge", "sshkc-x"]
    assert _cli.main([*argv, "--signature-file", str(sig)]) != 0
    assert not route.called
    assert "--no-proof" in capsys.readouterr().err


@respx.mock
@pytest.mark.parametrize("as_json", [False, True])
def test_cli_add_conflict_without_proof_says_what_proof_would_change(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(409, json={"error": "That key is already registered."})
    )
    sign_with(monkeypatch, Signer(None, "ssh-keygen: No private key found"))
    argv = ["ssh-key", "add", str(keygen), *(["--json"] if as_json else [])]
    assert _cli.main(argv) == 1
    err = capsys.readouterr().err
    if as_json:
        assert err.count("\n") == 1 and "replaces that registration" not in err
    else:
        assert "replaces that registration; this add carried none (ssh-keygen: No private" in err


@respx.mock
def test_cli_add_conflict_with_proof_is_the_platforms_refusal_alone(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(409, json={"error": "That key is already registered."})
    )
    sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 1
    assert capsys.readouterr().err == "mandala-py: That key is already registered. (HTTP 409)\n"


def test_sign_challenge_interrupted_tries_no_other_key(tmp_path: Path) -> None:
    public = tmp_path / "id_ed25519.pub"
    (tmp_path / "id_ed25519").write_text("private")
    calls: list[Any] = []

    def stopped(argv: list[str], echo: Any) -> Any:
        calls.append(argv)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=stopped)
    assert len(calls) == 1


@respx.mock
def test_cli_add_interrupted_while_signing_adds_nothing(
    keygen: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    route = respx.post(f"{BASE}/ssh-keys")

    def stopped(*a: Any, **kw: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(_cli._openssh, "sign_challenge", stopped)
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 130
    assert not route.called


@respx.mock
@pytest.mark.parametrize(
    ("held", "advice"),
    [
        ({"possession": "proven", "reach": "this_account"}, None),
        ({"possession": "legacy", "reach": "this_account"}, None),
        ({"possession": "unproven", "reach": "everywhere"}, "add it again from the dashboard"),
        ({"possession": "unproven", "reach": "another_account"}, None),
    ],
)
def test_cli_add_of_a_held_key_that_a_signed_add_would_narrow_is_not_signed(
    keygen: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    held: dict[str, Any],
    advice: str | None,
) -> None:
    """Signing would replace the registration with one bound to this account:
    the add stays unsigned and is refused as a duplicate, with no hint."""
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[{**KEY, **held}]))
    challenge = respx.post(f"{BASE}/ssh-keys/challenge")
    route = respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(409, json={"error": "That key is already registered."})
    )
    signer = sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 1
    assert not challenge.called and signer.calls == []
    assert json.loads(route.calls.last.request.content) == {"public_key": f"{PUBLIC_KEY} me@laptop"}
    assert capsys.readouterr().err == "mandala-py: That key is already registered. (HTTP 409)\n"


@respx.mock
def test_cli_add_signs_a_held_unproven_key_of_this_account(
    keygen: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = {**KEY, "possession": "unproven", "reach": "this_account"}
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[held]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    route = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 0
    sent = json.loads(route.calls.last.request.content)
    assert sent["signature"] == SIGNATURE
    # Naming the unproven registration it saw, so only that one is replaced.
    assert sent["replaces"] == KEY["id"]


CHANGED = {"error": "Your registration of this key changed.", "code": "registration_changed"}


@respx.mock
def test_cli_add_registration_changed_relists_says_what_it_is_now_and_retries_nothing(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing: list[dict[str, Any]] = []
    respx.get(f"{BASE}/ssh-keys").mock(side_effect=lambda req: httpx.Response(200, json=listing))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    meanwhile = {
        **KEY,
        "id": "sshk-0000000000000099",
        "possession": "unproven",
        "reach": "another_account",
    }

    def refuse(req: httpx.Request) -> httpx.Response:
        listing.append(meanwhile)
        return httpx.Response(409, json=CHANGED)

    route = respx.post(f"{BASE}/ssh-keys").mock(side_effect=refuse)
    sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 1
    assert route.call_count == 1
    err = capsys.readouterr().err
    assert (
        "registered or removed elsewhere while it was being signed, so nothing was replaced" in err
    )
    assert "it is now sshk-0000000000000099 (unproven, reach another account" in err


@respx.mock
def test_cli_add_registration_changed_with_nothing_held_now_says_so(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(409, json=CHANGED))
    sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 1
    assert "you hold no registration of it now" in capsys.readouterr().err


@respx.mock
def test_cli_add_registration_changed_under_json_keeps_stderr_to_the_error(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(409, json=CHANGED))
    sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen), "--json"]) == 1
    assert "nothing was replaced" not in capsys.readouterr().err


def test_add_sends_replaces_null_included_and_leaves_it_out_when_not_given() -> None:
    body = _api.ssh_key_body
    assert body(PUBLIC_KEY, None, "c", "s", None)["replaces"] is None
    assert body(PUBLIC_KEY, None, "c", "s", "sshk-1")["replaces"] == "sshk-1"
    assert "replaces" not in body(PUBLIC_KEY, None, "c", "s")
    with pytest.raises(ValueError, match="replaces"):
        body(PUBLIC_KEY, None, "c", "s", " ")


@respx.mock
def test_cli_add_of_an_unproven_key_reaching_elsewhere_points_at_the_dashboard(
    pub: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(
            201, json={**KEY, "possession": "unproven", "reach": "everywhere"}
        )
    )
    assert _cli.main(["ssh-key", "add", str(pub)]) == 0
    err = capsys.readouterr().err
    assert err.rstrip().endswith("to prove it, remove it and add it again from the dashboard")


def test_run_keygen_passes_a_prompt_on_while_the_child_still_waits(tmp_path: Path) -> None:
    """A security key's touch prompt must show before the child exits."""
    go = tmp_path / "touched"
    script = (
        "import sys, time, pathlib\n"
        "sys.stderr.write('Signing file challenge\\n')\n"
        "sys.stderr.write('Confirm user presence for key ED25519-SK\\n'); sys.stderr.flush()\n"
        f"while not pathlib.Path({str(go)!r}).exists(): time.sleep(0.02)\n"
    )
    shown: list[str] = []
    result: list[Any] = []
    worker = threading.Thread(
        target=lambda: result.append(
            _openssh.run_keygen([sys.executable, "-c", script], shown.append)
        )
    )
    worker.start()
    deadline = time.monotonic() + 10
    while not shown and time.monotonic() < deadline:
        time.sleep(0.02)
    assert shown == ["Confirm user presence for key ED25519-SK"]
    assert worker.is_alive()
    go.write_text("")
    worker.join(10)
    code, said = result[0]
    assert code == 0
    assert said[-1] == "Confirm user presence for key ED25519-SK"


def test_run_keygen_kills_the_child_when_interrupted() -> None:
    """Ctrl-C while ssh-keygen waits (for a touch, say) ends it, promptly."""
    started: list[subprocess.Popen[bytes]] = []
    real = subprocess.Popen

    def spy(*a: Any, **kw: Any) -> subprocess.Popen[bytes]:
        child = real(*a, **kw)
        started.append(child)
        threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGINT)).start()
        return child

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_openssh.subprocess, "Popen", spy)
        began = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            _openssh.run_keygen(
                [sys.executable, "-c", "import time; time.sleep(60)"], lambda line: None
            )
    assert time.monotonic() - began < 10
    assert started[0].poll() is not None


@respx.mock
@pytest.mark.parametrize("as_json", [False, True])
def test_cli_add_keeps_ssh_keygens_words_off_json_stderr(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    """Under --json a failed add's stderr is the one error object; what
    ssh-keygen said goes to the terminal instead."""
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(409, json={"error": "That key is already registered."})
    )
    on_tty: list[str] = []
    monkeypatch.setattr(_cli._openssh, "echo_terminal", on_tty.append)
    real = _openssh.sign_challenge

    def refusing(argv: list[str], echo: Any) -> tuple[int, list[str]]:
        echo("Couldn't sign: agent refused operation")
        return 255, ["Couldn't sign: agent refused operation"]

    monkeypatch.setattr(
        _cli._openssh, "sign_challenge", lambda *a, **kw: real(*a, run=refusing, **kw)
    )
    argv = ["ssh-key", "add", str(keygen), *(["--json"] if as_json else [])]
    assert _cli.main(argv) == 1
    err = capsys.readouterr().err
    if as_json:
        assert err.count("\n") == 1 and json.loads(err)["error"]
        assert on_tty == ["Couldn't sign: agent refused operation"]
    else:
        assert err.startswith("Couldn't sign: agent refused operation\n")
        assert on_tty == []


@respx.mock
def test_cli_add_sends_unsigned_a_key_registered_elsewhere_while_it_was_signing(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    listing: list[dict[str, Any]] = []
    respx.get(f"{BASE}/ssh-keys").mock(side_effect=lambda req: httpx.Response(200, json=listing))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    route = respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(409, json={"error": "That key is already registered."})
    )

    def sign(*a: Any, **kw: Any) -> tuple[str, str]:
        listing.append({**KEY, "reach": "another_account", "possession": "unproven"})
        return SIGNATURE, ""

    monkeypatch.setattr(_cli._openssh, "sign_challenge", sign)
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 1
    assert json.loads(route.calls.last.request.content) == {"public_key": f"{PUBLIC_KEY} me@laptop"}
    assert capsys.readouterr().err == "mandala-py: That key is already registered. (HTTP 409)\n"


@respx.mock
@pytest.mark.parametrize(
    "issued",
    [
        {**CHALLENGE, "namespace": "git"},
        {**CHALLENGE, "challenge": "tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904"},
        {**CHALLENGE, "challenge": "sshkc-" + "AB" * 32},
        {**CHALLENGE, "challenge": "sshkc-" + "ab" * 32 + "\n"},
    ],
)
@pytest.mark.parametrize("command", [["ssh-key", "add"], ["ssh", "--setup", "dev"]])
def test_an_unexpected_challenge_is_never_signed(
    keygen: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    issued: dict[str, Any],
    command: list[str],
) -> None:
    """A signature is good in its namespace: an endpoint naming another one,
    or handing over something that is not a registration challenge, could
    collect a signature usable elsewhere."""
    respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(
            200, json=[{"id": "vm-9", "name": "dev", "status": "running", "os": "linux"}]
        )
    )
    respx.get(f"{BASE}/computers/vm-9/ssh").mock(
        return_value=httpx.Response(200, json={"computer": "vm-9", "enabled": False})
    )
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=issued))
    add = respx.post(f"{BASE}/ssh-keys")
    signer = sign_with(monkeypatch, Signer())
    argv = [*command, *([str(keygen)] if command[0] == "ssh-key" else ["--key", str(keygen)])]
    assert _cli.main([*argv, "--json"]) == 1
    assert signer.calls == [] and not add.called
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "unexpected_challenge"
    assert error["message"].startswith("refusing to sign: ")


@respx.mock
def test_cli_shows_ssh_keygens_lines_terminal_safe(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    real = _openssh.sign_challenge

    def touch(argv: list[str], echo: Any) -> tuple[int, list[str]]:
        echo("Confirm user presence for key ED25519-SK \x1b]0;owned\x07")
        Path(f"{argv[-1]}.sig").write_text(SIGNATURE)
        return 0, []

    monkeypatch.setattr(_cli._openssh, "sign_challenge", lambda *a, **kw: real(*a, run=touch, **kw))
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 0
    err = capsys.readouterr().err
    assert "Confirm user presence for key ED25519-SK" in err and "\x1b" not in err
