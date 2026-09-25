"""One error vocabulary, and a mistyped command's whole usage (OPL-5048).

The words are the ones the ``mandala`` CLI reports, so a script reading
``error.code`` does not care which of the two it ran.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from mandala_computer import _cli

BASE = "https://api.test/api/v1"
SECRETS = {
    "secrets": [],
    "delivery": True,
    "limits": {
        "name_max_chars": 60,
        "value_max_bytes": 4096,
        "active_per_account": 100,
        "created_per_account": 1000,
    },
}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


def failure(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out, err = capsys.readouterr()
    assert out == ""
    assert err.count("\n") == 1
    return json.loads(err)["error"]


def test_an_extra_argument_prints_the_commands_whole_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        _cli.main(["webhooks", "list", "extra-arg"])
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith(
        "mandala-py webhooks list: error: unrecognized argument: extra-arg "
        "(quote a value that has spaces in it)\n"
    )
    # The command's own usage and options, not the top parser's one line.
    assert "usage: mandala-py webhooks list [-h] [--json]" in err
    assert "--json      the rows as JSON" in err


def test_a_missing_argument_prints_the_commands_whole_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["secrets", "set"])
    err = capsys.readouterr().err
    assert "the following arguments are required: NAME" in err
    assert "--keep-newline" in err


def test_a_usage_error_under_json_is_the_error_object(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        _cli.main(["webhooks", "list", "extra", "--json"])
    assert caught.value.code == 2
    error = failure(capsys)
    assert error["code"] == "invalid_arguments"
    assert "usage: mandala-py webhooks list" in str(error["usage"])


@respx.mock
@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {"error": "bad key"}, "unauthenticated"),
        (403, {"error": "owners only"}, "permission_denied"),
        (404, {"error": "no such thing"}, "not_found"),
        (409, {"error": "busy", "reason": "contention"}, "conflict"),
        (429, {"error": "slow down"}, "rate_limited"),
        (418, {"error": "teapot"}, "api_error"),
    ],
)
def test_an_api_refusal_is_named_by_what_went_wrong(
    capsys: pytest.CaptureFixture[str], status: int, body: dict[str, str], code: str
) -> None:
    respx.get(f"{BASE}/secrets").mock(return_value=httpx.Response(status, json=body))
    assert _cli.main(["secrets", "list", "--json"]) == 1
    error = failure(capsys)
    assert error["code"] == code
    assert error["status"] == status
    assert error.get("reason") == body.get("reason")


@respx.mock
def test_a_refusal_of_the_clis_own_carries_its_word(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[]))
    assert _cli.main(["ssh-access", "nope", "--json"]) == 1
    assert failure(capsys) == {
        "code": "not_found",
        "message": "no computer named 'nope'; the account has no computers",
    }


@respx.mock
def test_a_command_with_no_json_flag_fails_in_text(capsys: pytest.CaptureFixture[str]) -> None:
    respx.get(f"{BASE}/secrets").mock(return_value=httpx.Response(200, json=SECRETS))
    with pytest.raises(SystemExit) as caught:
        _cli.main(["secrets", "rm", "NOPE"])
    assert caught.value.code == "mandala-py: no secret named 'NOPE' in the account-wide scope"


@respx.mock
def test_without_json_a_failure_is_still_one_line_of_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    respx.get(f"{BASE}/secrets").mock(return_value=httpx.Response(404, json={"error": "gone"}))
    assert _cli.main(["secrets", "list"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err == "mandala-py: gone\n"


def test_a_local_file_error_keeps_its_errno_as_a_detail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli._error_info(FileNotFoundError(2, "No such file", "x")) == {
        "code": "io_error",
        "message": "[Errno 2] No such file: 'x'",
        "details": {"errno": "ENOENT"},
    }
