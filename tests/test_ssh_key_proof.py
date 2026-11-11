"""Proving an SSH key is yours: the challenge, the signed add, and each key's
``possession`` — on both clients and the CLI."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414

import mandala_computer as mc
from mandala_computer import _cli

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
