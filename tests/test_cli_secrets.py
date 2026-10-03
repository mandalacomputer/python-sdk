"""``mandala-py secrets``: a value typed where the NAME goes is refused and never
repeated, ``rm`` reads again on a revision conflict, and ``list`` shows the
revision (OPL-5646).

The detector is a port of the TypeScript CLI's ``looksLikeSecretValue``; the
vectors below are copied from its tests (test/cli-secrets.test.ts), in both
directions, so the two CLIs refuse the same operands. Change them together.
"""

from __future__ import annotations

import io
import json
import math
import sys
from collections.abc import Callable

import httpx
import pytest
import respx

from mandala_computer import _cli

BASE = "https://api.test/api/v1"
ID = "csec-0123456789abcdef"
REV = "csr-0123456789abcdef01234567"
REV2 = "csr-0123456789abcdef01234568"
SECRET = {
    "id": ID,
    "name": "OPENAI_API_KEY",
    "workspace_id": None,
    "revision_id": REV,
    "created_at": "2026-09-20T12:00:00Z",
    "updated_at": "2026-09-20T12:00:00Z",
    "last_used_at": None,
}
LISTING = {"secrets": [SECRET], "delivery": True, "limits": {}}
STALE = {"error": "This secret changed since you read it.", "code": "stale_revision"}


# Token-shaped fixtures are assembled at run time, so no literal in this file
# reads as a leaked credential to a scanner. None of them is a real token.
def body(n: int, alphabet: str = "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1xY") -> str:
    return "".join(alphabet[(i * 7 + 3) % len(alphabet)] for i in range(n))


def tok(prefix: str, rest: str) -> str:
    return prefix + rest


def seeded(seed: int) -> Callable[[], float]:
    """The TypeScript tests' stream, bit for bit, so the shares match theirs."""
    state = seed & 0xFFFFFFFF

    def next_() -> float:
        nonlocal state
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        return state / 2**32

    return next_


def random(next_: Callable[[], float], alphabet: str, n: int) -> str:
    return "".join(alphabet[math.floor(next_() * len(alphabet))] for _ in range(n))


ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
BASE64 = ALNUM + "+/"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: Names people really bind as — none of which may be refused.
NAMES = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DATABASE_URL",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_ACCESS_KEY_ID",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_STORAGE_CONNECTION_STRING",
    "NEXT_PUBLIC_SUPABASE_ANON_KEY",
    "CLOUDFLARE_API_TOKEN_2024",
    "POSTGRES_PASSWORD_PROD_EU_WEST_1",
    "E2E_TEST_USER_PASSWORD_V2",
    "ID_RSA_4096_PRIVATE_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "HF_TOKEN",
    "NPM_TOKEN",
    "STRIPE_SECRET_KEY",
    "SLACK_BOT_TOKEN",
    "myServiceAccountKey2024",
    "ServiceAccountKeyProd2025V2",
    "AppConfigValueForTesting123",
    "K8sClusterAdminToken2024",
    "ApiKeyV2ForS3BackupJob",
    "S3BucketAccessKeyIdProd",
    "OAuth2ClientSecretForGitHubApp",
    "X509CertificateChainPem",
    "Base64EncodedServiceAccountJSON",
    "IPv4AddressForHost01",
    "MyApp2FASecretV3Backup",
    "GPGKeyFingerprint2024Q3",
    "SSHHostKeyEd25519Pub",
    "K8sSvcAcctJWTPubKeyV1",
    "JWTRSAPublicKeyPEMBase64",
    "AWSIAMRoleARNForCIDeployer",
    "GCPServiceAccountJSONKeyB64",
    "TLSCertAndKeyPEMForMTLSProxy",
    "DBURLForETLJobInUSEast",
    "DatabaseURLProdEUWest1",
    "SSHPrivateKeyED25519ForCI",
    "kubeconfig",
    "gh",
    "id_ed25519",
    "hf_token",
    "npm_token",
    "npm_package_devDependencies",
    "sk_test_integrationTestKey",
    "hf_hubTokenReadOnly2024",
    "ghp_personalAccessTokenForCI",
    "sk-prod-signing-key",
    "sk_live_mode_config",
    "xoxb-bot-token-for-alerts",
    "gcloud-service-account-key",
    "db_backup_2024_01_15",
    "prod-eu-west-1-kubeconfig",
    "aws-credentials-2025-q3",
    "stripe_webhook_signing_secret",
    "my-app-v2-config-prod-01",
    "terraform-cloud-token-2024",
    "x509-client-cert-prod-2025",
    "kubeconfig20240115backup",
    "sha256sumsforrelease2024",
    "user1password2024prod",
    "mysql8rootpassword2024",
    "/home/user/.config/gcloud/key.json",
    "/etc/ssl/private/server-2025.key",
    "/run/mandala-secrets/user/files/openai",
    "db-password",
    "SLACK_WEBHOOK_URL",
    "/etc/app/key",
    "config/prod/DatabasePassword2024",
    "secrets/prod/StripeSecretKeyLive",
    "team/ServiceAccountKeyProd2025V2/config1",
    "apps/prod/OAuth2ClientSecretForGitHubApp",
    "k8s/ClusterAdminToken2024/v2",
    "services/payments/prod/DB/password/v2024",
    "infra/terraform/AWS/state/bucket/key2024",
    "ci/github/actions/deploy/SSH/key/ed25519",
    "myapp/prod/DATABASE/URL",
    "github/org/repo/NPMTOKEN",
    "prod/GCP/SA/JSON/key/2024",
    "prod/APIKEY/STRIPE/live2",
    "OPENAI+ANTHROPIC/keys/prod",
    "k8s/v2/db1/s3/prod/x509/certs",
    "myapp/staging/REDIS/URL/v1beta1",
    "myapp/prod/DATABASE/URL/v2beta",
    "myapp/s3cache/DATABASE/URL",
    "acme/prod2eu/SMTP/PASSWORD",
    "myapp/prod/OAUTH2CLIENTSECRET",
    "myapp/prod/S3BUCKETKEY",
    "myapp/DATABASE/DbPassword",
    "myapp/DATABASE/JSONWebKey",
    "prod/DATABASE/DbPassword",
    "acme/prod/DATABASE/APIKey",
    "myapp/staging/REDIS/HMACKey",
    "myapp/DATABASE/TLSCert",
    "myapp/prod/DATABASE/iOS",
    "mobile/prod/DATABASE/tvOS",
    "RedisURL",
    "MongoURI",
    "NeonDBURL",
    "ZoomJWT",
    "SSHKeyEd25519",
    "Ed25519Key",
    "PyPIToken",
    "myapp/DATABASE/RedisURL",
    "myapp/DATABASE/MongoURI",
    "myapp/DATABASE/NeonDBURL",
    "myapp/DATABASE/ZoomJWT",
    "myapp/DATABASE/SSHKeyEd25519",
    "myapp/DATABASE/Ed25519Key",
    "myapp/DATABASE/PyPIToken",
    "team/prod/SSHKeyEd25519",
    "ci/PyPIToken",
    "MySQLURL",
    "MySQLDSN",
    "MyDBURL",
    "myapp/DATABASE/MySQLURL",
    "myapp/DATABASE/MySQLDSN",
    "myapp/DATABASE/MyDBURL",
    "/srv/myapp/secrets/DatabasePassword",
    "/srv/app/config/StripeSecretKeyLive",
]

#: An AWS secret access key's documented example, which ``/`` cuts into short runs.
AWS_SECRET_PARTS = ("wJalrXUtnFEMI", "K7MDENG", "bPxRfiCYEXAMPLEKEY")
AWS_SECRET_EXAMPLE = "/".join(AWS_SECRET_PARTS)

TOKENS = [
    tok("ghp_", body(36)),  # a valid variable name
    tok("gho_", body(36)),
    tok("ghs_", body(36)),
    tok("github_pat_", f"{body(22)}_{body(59)}"),
    tok("sk-", body(48)),
    tok("sk-proj-", f"{body(40)}-{body(20)}"),
    tok("sk-ant-api03-", body(40)),
    tok("sk_live_", body(24)),
    tok("sk_test_", body(24)),
    tok("rk_live_", body(24)),
    tok("xoxb-", f"123456789012-1234567890123-{body(24)}"),
    tok("glpat-", body(20)),
    tok("AIza", body(35)),
    tok("hf_", body(34)),
    tok("npm_", body(36)),
    tok("pypi-", body(60)),
    tok("SG.", f"{body(22)}.{body(43)}"),
    tok("AKIA", "Q2W3E4R5T6Y7U8I9"),
    tok("ASIA", "Q2W3E4R5T6Y7U8I9"),
    tok("dop_v1_", body(64, "0123456789abcdef")),
    tok("eyJ", f"{body(30)}.{body(40)}.{body(43)}"),
    "9f86d081-884c-4d63-a6c5-2a1f0e8b7c3d",
    "a3f9c0e17b2d48a6e5f1c9b3d7a0e4f8",
    "f1e2d3c4b5a697887766554433221100aabbccdd",
    "Xk9fQ2mZr7Lp4Tw8Bv3Nc6Hd",
    tok("sk_test_", "QxZrTpLmWvNbKjHg"),
    tok("hf_", "QxZrTpLmWvNbKjHg2024"),
    "QZXkTRmWPbNVcJHLdGFsYK",
]

#: The token the CLI cases type as the NAME.
TOKEN = tok("ghp_", body(36))

PREFIXED_WORDS = [
    "integrationTestKey",
    "hubTokenReadOnly",
    "personalAccessTokenForCI",
    "devDependencies",
    "JWTRSAPublicKeyPEMBase64",
    "AWSIAMRoleARNForCIDeployer",
    "GCPServiceAccountJSONKeyB64",
    "TLSCertAndKeyPEMForMTLSProxy",
    "DBURLForETLJobInUSEast",
    "DatabaseURLProdEUWest1",
    "OAuthClientSecretForGitHubApp",
    "XMLHttpRequestToken",
    "signingKeyForJWTs",
    "StripeWebhookSigningSecret",
    "ScriptsForStrings",
    "CloudflareDNSEditToken",
    "KubernetesClusterAdmin",
    "PostgresReplicaPassword",
    "WebhookSecretForGitLabCI",
    "SentryDsnForFrontend",
    "FirebaseAdminCredentials",
    "ServiceAccountKeyProdV2",
    "readOnlyTokenForMyApp",
    "nightlyBackupSigningKey",
]

SPELLED_WORDS = [
    "SslCertPassword",
    "NpmPublishToken",
    "GcpServiceKey",
    "GrpcAuthToken",
    "CdnPurgeToken",
    "KmsKeyForBackups",
    "VpnSharedSecret",
    "PgpPrivateKeyArmored",
    "RdsMasterPassword",
    "StsAssumeRoleSecret",
    "McpServerToken",
    "KafkaConsumerSecret",
    "LangchainApiKeyProd",
    "myHuggingfaceToken",
    "BlockchainNodeKey",
    "WebflowApiToken",
    "BuildkiteAgentToken",
    "FirmwareSigningKey",
    "MemcachedAuthSecret",
    "DeepgramApiKeyProd",
    "InfluxdbWriteToken",
    "ZipkinCollectorToken",
    "HetznerCloudToken",
    "NextjsPreviewSecret",
    "EcdsaSigningKey",
    "strengthLengthWidth",
    "LlamaApiKey",
    "nginxConfigSecret",
    "EtcdClientCert",
    "JfrogArtifactToken",
    "GroqApiKeyForBot",
    "RabbitmqAdminPassword",
    "MysqlReplicaPassword",
    "PostgresqlAdminPassword",
    "SqliteEncryptionKey",
    "GraphqlGatewaySecret",
    "BigqueryServiceAccount",
    "CiCdDeployToken",
    "PgBouncerPassword",
    "TfCloudApiToken",
    "PyPackageIndexToken",
    "GhActionsDeployKey",
    "TsNodeSigningKey",
    "iPhoneBackupKey",
    "iOSSigningCertificate",
    "eSignatureToken",
    "aTokenForMyApp",
]

looks = _cli._looks_like_secret_value


# --- the detector, against the TypeScript CLI's vectors -----------------------


@pytest.mark.parametrize("name", NAMES)
def test_passes_every_realistic_variable_name_file_name_and_path(name: str) -> None:
    assert looks(name) is False


@pytest.mark.parametrize(
    "value",
    ["AsBeByDoGo/InIsOnUpTo", "OnUpSQL/AsByURL/DoGoDSN", "SQLMy/URLAs/DSNGo/JWTUp"]
    + ["MyDbUpIs+SQLOnAs+GoURLs"],
)
def test_reads_a_base64_piece_of_two_letter_words_alone_as_a_value(value: str) -> None:
    assert looks(value) is True


@pytest.mark.parametrize("token", TOKENS)
def test_catches_the_token_formats_issuers_hand_out(token: str) -> None:
    assert looks(token) is True


def test_catches_most_random_tokens_and_every_hex_one_of_24_or_more() -> None:
    next_ = seeded(5076)

    def share(alphabet: str, n: int, runs: int = 400) -> float:
        return sum(looks(random(next_, alphabet, n)) for _ in range(runs)) / runs

    for n in (24, 32, 40):
        assert share("0123456789abcdef", n) > 0.98, f"hex {n}"
    for n in (24, 32, 40):
        assert share(ALNUM, n) > 0.85, f"alnum {n}"
    assert share("abcdefghijklmnopqrstuvwxyz0123456789", 32) > 0.6


def test_catches_a_base64_secret_that_slash_or_plus_cut_into_short_runs() -> None:
    assert looks(AWS_SECRET_EXAMPLE) is True
    next_ = seeded(5076)
    hit = cut = cut_hit = 0
    for _ in range(2000):
        key = random(next_, BASE64, 40)
        flagged = looks(key)
        hit += flagged
        if "+" in key or "/" in key:
            cut += 1
            cut_hit += flagged
    assert cut > 1000
    assert cut_hit / cut > 0.93
    assert hit / 2000 > 0.93


def test_catches_a_base64_secret_whose_first_character_is_a_slash() -> None:
    assert looks(f"/{AWS_SECRET_EXAMPLE[1:]}") is True
    next_ = seeded(5076)
    hit = sum(looks(f"/{random(next_, BASE64, 39)}") for _ in range(2000))
    assert hit / 2000 > 0.93


def test_catches_a_base64_secret_quoted_after_bearer_or_name_equals() -> None:
    padded = f"{AWS_SECRET_EXAMPLE}Xw=="
    for key in (AWS_SECRET_EXAMPLE, padded):
        for wrapped in (
            f'"{key}"',
            f"'{key}'",
            f"Bearer {key}",
            f"AWS_SECRET_ACCESS_KEY={key}",
            f"{key},",
            f"{key};",
        ):
            assert looks(wrapped) is True, wrapped
    next_ = seeded(5076)
    cut = cut_hit = 0
    for _ in range(2000):
        key = random(next_, BASE64, 40)
        if "+" not in key and "/" not in key:
            continue
        cut += 1
        cut_hit += looks(f'"{key}"') and looks(f"KEY={key};")
    assert cut_hit / cut > 0.93


def test_catches_a_padded_base64_secret() -> None:
    next_ = seeded(5076)
    cut = cut_hit = 0
    for _ in range(2000):
        key = f"{random(next_, BASE64, 43)}="
        if "+" not in key and "/" not in key:
            continue
        cut += 1
        cut_hit += looks(key)
    assert cut > 1000
    assert cut_hit / cut > 0.93
    assert looks(f"{AWS_SECRET_EXAMPLE}Xw==") is True


def test_catches_a_known_prefix_ahead_of_random_letters_from_twelve_on() -> None:
    next_ = seeded(5128)
    for prefix, n in [
        ("sk_test_", 12),
        ("sk_test_", 24),
        ("hf_", 16),
        ("hf_", 34),
        ("ghp_", 36),
        ("xoxb-", 24),
        ("sk-proj-", 20),
        ("AIza", 20),
        ("glpat-", 20),
    ]:
        hit = sum(looks(tok(prefix, random(next_, LETTERS, n))) for _ in range(10_000))
        assert hit / 10_000 >= 0.99, f"{prefix} + {n} letters"


@pytest.mark.parametrize("prefix", ["hf_", "sk_test_", "ghp_", "npm_"])
def test_passes_a_known_prefix_ahead_of_camel_case_words(prefix: str) -> None:
    for word in PREFIXED_WORDS:
        assert looks(tok(prefix, word)) is False, prefix + word


@pytest.mark.parametrize("prefix", ["hf_", "sk-", "sk_test_", "npm_"])
def test_passes_names_the_spelling_rules_alone_would_refuse(prefix: str) -> None:
    for word in SPELLED_WORDS:
        assert looks(tok(prefix, word)) is False, prefix + word


@pytest.mark.parametrize(
    "word",
    [
        "WalGEncryptionKey",
        "QsPublishingToken",
        "ZqPublishingToken",
        "PublishingXzvToken",
        "PublishingTokenXavkzq",
    ],
)
def test_refuses_the_names_it_is_known_to(word: str) -> None:
    assert looks(tok("hf_", word)) is True


def test_never_flags_something_short() -> None:
    next_ = seeded(1)
    for _ in range(200):
        assert looks(random(next_, "ABCDEFabcdef0123456789", 19)) is False


def test_quoted_operand_quotes_ids_and_names_and_nothing_else() -> None:
    assert _cli._quoted_operand(ID) == f"'{ID}'"
    assert _cli._quoted_operand("OPENAI_API_KEY") == "'OPENAI_API_KEY'"
    assert _cli._quoted_operand(TOKEN) is None
    assert _cli._quoted_operand(AWS_SECRET_EXAMPLE) is None
    # A name the platform would refuse is not quoted either.
    assert _cli._quoted_operand("a" * 61) is None
    assert _cli._quoted_operand("a\x01b") is None


# --- the commands -------------------------------------------------------------


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


class _Stdin(io.TextIOWrapper):
    def isatty(self) -> bool:
        return False


def _pipe(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", _Stdin(io.BytesIO(data), encoding="utf-8"))


@pytest.mark.parametrize("typed", [TOKEN, AWS_SECRET_EXAMPLE, f"  {TOKEN}  "])
def test_set_refuses_a_value_typed_as_the_name_and_sends_nothing(
    typed: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pipe(monkeypatch, b"the-real-value\n")
    with respx.mock(assert_all_called=False) as router, pytest.raises(_cli._Failure) as caught:
        every = router.route()
        _cli.main(["secrets", "set", typed])
    assert caught.value.reason == "invalid_arguments"
    assert "looks like a secret's value, not a name; nothing was sent" in caught.value.message
    assert "--no-value-check" in caught.value.message
    assert typed.strip() not in str(caught.value.code)
    assert not every.called
    out, err = capsys.readouterr()
    assert typed.strip() not in out and typed.strip() not in err


def test_set_refuses_it_under_json_too_without_repeating_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pipe(monkeypatch, b"the-real-value\n")
    with respx.mock(assert_all_called=False) as router:
        every = router.route()
        assert _cli.main(["secrets", "set", TOKEN, "--json"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert json.loads(err)["error"]["code"] == "invalid_arguments"
    assert TOKEN not in out and TOKEN not in err
    assert not every.called


@respx.mock
def test_set_sends_it_as_typed_with_no_value_check(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json={**SECRET, "name": TOKEN}))
    _pipe(monkeypatch, b"the-real-value\n")
    assert _cli.main(["secrets", "set", TOKEN, "--no-value-check"]) == 0
    assert json.loads(made.calls.last.request.content)["name"] == TOKEN


@respx.mock
def test_set_still_takes_a_real_name(monkeypatch: pytest.MonkeyPatch) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": []}))
    made = respx.post(f"{BASE}/secrets").mock(httpx.Response(201, json=SECRET))
    _pipe(monkeypatch, b"the-real-value\n")
    assert _cli.main(["secrets", "set", "OPENAI_API_KEY"]) == 0
    assert json.loads(made.calls.last.request.content)["name"] == "OPENAI_API_KEY"


@pytest.mark.parametrize("typed", [TOKEN, AWS_SECRET_EXAMPLE])
@respx.mock
def test_rm_does_not_repeat_a_value_typed_as_the_name(
    typed: str, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    with pytest.raises(_cli._Failure) as caught:
        _cli.main(["secrets", "rm", typed])
    assert caught.value.reason == "not_found"
    assert "no secret with that name or id in the account-wide scope" in caught.value.message
    assert typed not in str(caught.value.code) and typed not in caught.value.message
    out, err = capsys.readouterr()
    assert typed not in out and typed not in err


@respx.mock
def test_rm_still_names_a_plain_name_it_did_not_find() -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    with pytest.raises(_cli._Failure) as caught:
        _cli.main(["secrets", "rm", "NOPE"])
    assert caught.value.message == "no secret named 'NOPE' in the account-wide scope"


@respx.mock
def test_rm_does_not_repeat_a_value_shaped_name_it_deleted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stored = {**SECRET, "name": TOKEN}
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json={**LISTING, "secrets": [stored]}))
    respx.delete(f"{BASE}/secrets/{ID}").mock(httpx.Response(204))
    assert _cli.main(["secrets", "rm", ID]) == 0
    out, err = capsys.readouterr()
    assert out == f"deleted {ID}\n"
    assert TOKEN not in out and TOKEN not in err


@respx.mock
def test_rm_reads_again_when_the_revision_moved(capsys: pytest.CaptureFixture[str]) -> None:
    listing = respx.get(f"{BASE}/secrets").mock(
        side_effect=[
            httpx.Response(200, json=LISTING),
            httpx.Response(200, json={**LISTING, "secrets": [{**SECRET, "revision_id": REV2}]}),
        ]
    )
    delete = respx.delete(f"{BASE}/secrets/{ID}").mock(
        side_effect=[httpx.Response(409, json=STALE), httpx.Response(204)]
    )
    assert _cli.main(["secrets", "rm", "OPENAI_API_KEY"]) == 0
    assert listing.call_count == 2
    assert [c.request.url.params["revision_id"] for c in delete.calls] == [REV, REV2]
    assert capsys.readouterr().out == f"deleted OPENAI_API_KEY  {ID}\n"


@respx.mock
def test_rm_surfaces_the_conflict_after_three_attempts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    listing = respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    delete = respx.delete(f"{BASE}/secrets/{ID}").mock(httpx.Response(409, json=STALE))
    assert _cli.main(["secrets", "rm", "OPENAI_API_KEY"]) == 1
    assert listing.call_count == 3 and delete.call_count == 3
    assert "changed since you read it" in capsys.readouterr().err


@respx.mock
def test_rm_never_deletes_a_different_secret_on_the_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Between the first read and the retry, somebody creates a secret NAMED
    # like the chosen secret's id. An exact name wins a fresh resolution, so
    # a retry that resolved again would delete that one instead.
    other_id = "csec-fedcba9876543210"
    other = {**SECRET, "id": other_id, "name": ID}
    respx.get(f"{BASE}/secrets").mock(
        side_effect=[
            httpx.Response(200, json=LISTING),
            httpx.Response(
                200, json={**LISTING, "secrets": [{**SECRET, "revision_id": REV2}, other]}
            ),
        ]
    )
    mine = respx.delete(f"{BASE}/secrets/{ID}").mock(httpx.Response(409, json=STALE))
    theirs = respx.delete(f"{BASE}/secrets/{other_id}").mock(httpx.Response(204))
    with pytest.raises(_cli._Failure) as caught:
        _cli.main(["secrets", "rm", ID])
    assert caught.value.reason == "conflict"
    assert other_id in caught.value.message and "nothing was deleted" in caught.value.message
    assert mine.call_count == 1
    assert theirs.call_count == 0
    assert "deleted" not in capsys.readouterr().out


@respx.mock
def test_rm_does_not_repeat_a_value_typed_as_the_name_when_it_moves_on_the_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A secret whose NAME is value-shaped is chosen; before the retry it is
    # renamed and another secret takes that name. The conflict refusal must
    # name both ids, never the operand.
    typed = tok("ghp_", body(36))
    other_id = "csec-fedcba9876543210"
    respx.get(f"{BASE}/secrets").mock(
        side_effect=[
            httpx.Response(200, json={**LISTING, "secrets": [{**SECRET, "name": typed}]}),
            httpx.Response(
                200,
                json={
                    **LISTING,
                    "secrets": [
                        {**SECRET, "name": "x", "revision_id": REV2},
                        {**SECRET, "id": other_id, "name": typed},
                    ],
                },
            ),
        ]
    )
    mine = respx.delete(f"{BASE}/secrets/{ID}").mock(httpx.Response(409, json=STALE))
    theirs = respx.delete(f"{BASE}/secrets/{other_id}").mock(httpx.Response(204))
    with pytest.raises(_cli._Failure) as caught:
        _cli.main(["secrets", "rm", typed])
    failure = caught.value
    assert failure.reason == "conflict"
    assert failure.message.startswith("that name or id changed while it was being removed")
    assert ID in failure.message and other_id in failure.message
    assert "nothing was deleted" in failure.message
    assert typed not in failure.message and typed not in str(failure)
    assert typed not in str(failure.code)
    assert mine.call_count == 1 and theirs.call_count == 0
    out, err = capsys.readouterr()
    assert typed not in out and typed not in err


@respx.mock
def test_list_shows_each_secrets_revision(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/secrets").mock(httpx.Response(200, json=LISTING))
    assert _cli.main(["secrets", "list"]) == 0
    header, row = capsys.readouterr().out.splitlines()
    assert header.split() == ["ID", "NAME", "SCOPE", "REVISION", "LAST", "USED", "UPDATED"]
    assert row.split() == [ID, "OPENAI_API_KEY", "account", REV, "never", SECRET["updated_at"]]
