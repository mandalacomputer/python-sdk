"""Proving an SSH key is yours: the challenge, the signed add, and each key's
``possession`` — on both clients and the CLI."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414

import mandala_computer as mc
from mandala_computer import _cli, _openssh

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
    "namespace": "mandala-ssh-key",
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
        "mandala-ssh-key",
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
    assert "namespace   mandala-ssh-key" in captured.out
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
        self, keygen: str, public_key: Path, namespace: str, challenge: str
    ) -> tuple[str | None, str]:
        self.calls.append((keygen, public_key, namespace, challenge))
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


def runner(*answers: tuple[int, bytes, bytes]) -> tuple[Any, list[Any]]:
    seen: list[Any] = []
    queue = list(answers)

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append((argv, kw["input"]))
        code, out, err = queue.pop(0)
        return subprocess.CompletedProcess(argv, code, out, err)

    return run, seen


def test_sign_challenge_sends_the_challenge_exactly_and_keeps_the_signature(
    tmp_path: Path,
) -> None:
    public = tmp_path / "id_ed25519.pub"
    run, seen = runner((0, SIGNATURE.encode(), b"Signing data on standard input\n"))
    got = _openssh.sign_challenge(KEYGEN, public, "ns", "sshkc-1", run=run)
    assert got == (SIGNATURE, "")
    assert seen == [([KEYGEN, "-Y", "sign", "-n", "ns", "-f", str(public)], b"sshkc-1")]


def test_sign_challenge_falls_back_to_the_private_file(tmp_path: Path) -> None:
    public = tmp_path / "id_ed25519.pub"
    (tmp_path / "id_ed25519").write_text("private")
    run, seen = runner(
        (255, b"", b"Couldn't sign: agent refused operation\n"), (0, SIGNATURE.encode(), b"")
    )
    assert _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=run) == (SIGNATURE, "")
    assert [argv[-1] for argv, _ in seen] == [str(public), str(tmp_path / "id_ed25519")]


def test_sign_challenge_says_why_it_could_not(tmp_path: Path) -> None:
    public = tmp_path / "id_ed25519.pub"
    run, _ = runner((255, b"", b"Signing data on standard input\nNo private key found\n\n"))
    assert _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=run) == (
        None,
        "ssh-keygen: No private key found",
    )
    run, _ = runner((0, b"not a signature", b""))
    assert _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=run) == (
        None,
        "ssh-keygen exited 0 without a signature",
    )

    def broken(argv: list[str], **kw: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    assert _openssh.sign_challenge(KEYGEN, public, "ns", "c", run=broken) == (
        None,
        "could not run ssh-keygen: Permission denied",
    )


@respx.mock
def test_cli_add_signs_a_challenge_itself(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    route = respx.post(f"{BASE}/ssh-keys").mock(return_value=httpx.Response(201, json=KEY))
    signer = sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 0
    assert signer.calls == [(KEYGEN, keygen, "mandala-ssh-key", CHALLENGE["challenge"])]
    sent = json.loads(route.calls.last.request.content)
    assert (sent["challenge"], sent["signature"]) == (CHALLENGE["challenge"], SIGNATURE)
    assert capsys.readouterr().err == ""


@respx.mock
def test_cli_add_that_cannot_sign_adds_unproven_and_says_why(
    keygen: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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
    respx.post(f"{BASE}/ssh-keys/challenge").mock(return_value=httpx.Response(201, json=CHALLENGE))
    respx.post(f"{BASE}/ssh-keys").mock(
        return_value=httpx.Response(409, json={"error": "That key is already registered."})
    )
    sign_with(monkeypatch, Signer())
    assert _cli.main(["ssh-key", "add", str(keygen)]) == 1
    assert capsys.readouterr().err == "mandala-py: That key is already registered. (HTTP 409)\n"
