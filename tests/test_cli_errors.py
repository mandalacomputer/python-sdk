"""One error vocabulary, and a mistyped command's whole usage (OPL-5048).

The words are the ones the ``mandala`` CLI reports, so a script reading
``error.code`` does not care which of the two it ran.
"""

from __future__ import annotations

import json
import re

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
        "mandala-py webhooks list: error: 1 argument too many "
        "(quote a value that has spaces in it)\n"
    )
    assert "extra-arg" not in err
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


TOKEN = "sk-live-0123456789-do-not-echo"


@pytest.mark.parametrize("as_json", [False, True])
def test_a_secret_typed_where_secrets_set_reads_stdin_is_never_echoed(
    capsys: pytest.CaptureFixture[str], as_json: bool
) -> None:
    # The value belongs on stdin or at the prompt; typed as an operand it is
    # counted, not quoted, so it does not reach a log that captures stderr.
    argv = ["secrets", "set", "OPENAI_API_KEY", TOKEN, *(["--json"] if as_json else [])]
    with pytest.raises(SystemExit) as caught:
        _cli.main(argv)
    assert caught.value.code == 2
    out, err = capsys.readouterr()
    assert TOKEN not in out + err
    assert "1 argument too many" in err


def test_a_dashed_value_is_described_not_echoed(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["secrets", "set", "OPENAI_API_KEY", f"-{TOKEN}", "--json"])
    error = failure(capsys)
    assert TOKEN not in json.dumps(error)
    assert error["message"] == "1 argument too many (quote a value that has spaces in it)"


SECRET = "sk-demo-123"
UNNAMED = (
    "1 unrecognized option, not repeated here, as under secrets it may be a secret value; "
    "secrets set reads the value from stdin or a prompt"
)
UNREAD = (
    "an argument could not be read, and is not repeated here, "
    "as under secrets it may be a secret value"
)


@pytest.mark.parametrize(
    ("tail", "message"),
    [
        # After --, how a value that starts with a dash is passed: an operand
        # however it is spelled, so counted, and not mislabelled an option.
        (["--", f"--{SECRET}"], "1 argument too many (quote a value that has spaces in it)"),
        (
            ["--", SECRET, f"-{SECRET}"],
            "2 arguments too many (quote a value that has spaces in it)",
        ),
        # Shaped exactly like an option name, which is named anywhere else.
        ([f"--{SECRET}"], UNNAMED),
        # Both at once, and the separator is not counted as an operand.
        (
            [f"--{SECRET}", "--", f"--{SECRET}"],
            f"{UNNAMED}; 1 argument too many (quote a value that has spaces in it)",
        ),
        # argparse's own diagnostics, which quote the value.
        ([f"--keep-newline={SECRET}"], UNREAD),
        ([f"--k={SECRET}"], UNREAD),
    ],
)
@pytest.mark.parametrize("as_json", [False, True])
def test_no_diagnostic_under_secrets_set_repeats_what_was_typed(
    capsys: pytest.CaptureFixture[str], tail: list[str], message: str, as_json: bool
) -> None:
    # --json goes first: after -- it would be one more operand.
    argv = ["secrets", "set", "OPENAI_API_KEY", *(["--json"] if as_json else []), *tail]
    with pytest.raises(SystemExit) as caught:
        _cli.main(argv)
    assert caught.value.code == 2
    if as_json:
        error = failure(capsys)
        assert SECRET not in json.dumps(error)
        assert error["message"] == message
    else:
        out, err = capsys.readouterr()
        assert SECRET not in out + err
        assert err.startswith(f"mandala-py secrets set: error: {message}\n")


@pytest.mark.parametrize("verb", [SECRET, "ls", "sk(choose from 'x', 'y')"])
def test_an_unknown_verb_under_secrets_is_not_repeated(
    capsys: pytest.CaptureFixture[str], verb: str
) -> None:
    with pytest.raises(SystemExit) as caught:
        _cli.main(["secrets", verb])
    assert caught.value.code == 2
    out, err = capsys.readouterr()
    first = err.splitlines()[0]
    assert verb not in first
    if verb != "ls":
        assert verb not in out + err
    # The verbs it could have been are declared names, so they are listed.
    assert first.startswith("mandala-py secrets: error: unknown command; choose from ")
    assert first.endswith(
        " (the word typed is not repeated here, as under secrets it may be a secret value)"
    )
    # Some Python versions quote the choices and some do not.
    assert re.search(r"choose from '?list'?, '?set'?, '?rm'? \(", first)
    assert "'x'" not in err


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["--json", "webhooks", "list"], "webhooks list"),
        (["webhooks", "--json", "list"], "webhooks list"),
        (["--json", "secrets", "list"], "secrets list"),
        (["secrets", "--json", "list"], "secrets list"),
        (["secrets", "--json", "set", "A"], "secrets set"),
        (["--workspace", "ws_1", "secrets", "list"], "secrets list"),
        (["--workspace=ws_1", "secrets", "list"], "secrets list"),
        (["secrets", "--workspace", "ws_1", "list"], "secrets list"),
        (["secrets", "--workspace=ws_1", "list"], "secrets list"),
    ],
)
def test_an_option_typed_before_its_command_is_said_to_be(
    capsys: pytest.CaptureFixture[str], argv: list[str], command: str
) -> None:
    # Only the command declares it, so the parsers above it cannot take it.
    # It is not an extra argument, and naming it quotes only a declared name.
    with pytest.raises(SystemExit) as caught:
        _cli.main(argv)
    assert caught.value.code == 2
    out, err = capsys.readouterr()
    assert "too many" not in out + err
    option = (argv[0] if argv[0].startswith("-") else argv[1]).split("=")[0]
    assert f"{option} must come after the command, " in out + err
    assert f"mandala-py {command}" in out + err
    # The value typed with it is neither quoted nor read as the command.
    assert "ws_1" not in out + err
    assert "invalid choice" not in out + err


@pytest.mark.parametrize(
    "argv",
    [
        ["--workspace", SECRET, "secrets", "list"],
        [f"--workspace={SECRET}", "secrets", "list"],
        ["--json", "--workspace", SECRET, "secrets", "list"],
    ],
)
def test_an_option_typed_before_its_command_names_only_itself(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as caught:
        _cli.main(argv)
    assert caught.value.code == 2
    out, err = capsys.readouterr()
    assert SECRET not in out + err
    early = " ".join(w.split("=")[0] for w in argv if w.startswith("--"))
    message = f"{early} must come after the command, mandala-py secrets list"
    if "--json" in argv:
        assert json.loads(err)["error"]["message"] == message
    else:
        assert err.startswith(f"mandala-py secrets list: error: {message}\n")


def test_a_value_is_dropped_only_for_an_option_the_command_declares_with_one(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --json takes no value, so the word after it is still read as a command
    # name, and argparse says it is none.
    with pytest.raises(SystemExit):
        _cli.main(["--json", "nosuch", "secrets", "list"])
    assert "invalid choice" in json.dumps(failure(capsys))
    # Nor is a value dropped when no command is named after it.
    with pytest.raises(SystemExit):
        _cli.main(["--workspace", "ws_1"])
    assert "invalid choice" in capsys.readouterr().err
    # A flag joined to a value is not one the command would take there either.
    with pytest.raises(SystemExit):
        _cli.main(["--json=yes", "webhooks", "list"])
    assert "must come after" not in capsys.readouterr().err


def test_an_option_before_the_command_is_still_not_named_under_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["--json", f"--{SECRET}", "secrets", "list"])
    error = failure(capsys)
    assert SECRET not in json.dumps(error)
    assert error["message"] == (
        "--json must come after the command, mandala-py secrets list; "
        "1 unrecognized option, not repeated here, as under secrets it may be a secret value"
    )


def test_only_secrets_set_points_at_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    # list and rm read no value, so the stdin hint would be beside the point.
    for argv in (["secrets", "list", f"--{SECRET}"], ["secrets", "rm", "A", f"--{SECRET}"]):
        with pytest.raises(SystemExit):
            _cli.main(argv)
        out, err = capsys.readouterr()
        assert SECRET not in out + err
        assert err.startswith(
            f"mandala-py secrets {argv[1]}: error: 1 unrecognized option, not repeated here, "
            "as under secrets it may be a secret value\n"
        )


def test_a_diagnostic_that_quotes_only_declared_names_still_reads_under_secrets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["secrets", "set", "A", "--workspace"])
    assert "argument --workspace: expected one argument" in capsys.readouterr().err


def test_an_option_is_named_only_when_typed_before_the_separator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Outside secrets a mistyped flag is still named; the same spelling after
    # -- is an operand, counted.
    with pytest.raises(SystemExit):
        _cli.main(["webhooks", "list", "--jsno", "--", "--jsno"])
    assert capsys.readouterr().err.startswith(
        "mandala-py webhooks list: error: unrecognized option: --jsno; "
        "1 argument too many (quote a value that has spaces in it)\n"
    )
    # Nor is one spelled like an option the command took before the --.
    with pytest.raises(SystemExit):
        _cli.main(["webhooks", "list", "--json", "--", "--json"])
    assert failure(capsys)["message"] == (
        "1 argument too many (quote a value that has spaces in it)"
    )
    # Nor one the command took as an abbreviation.
    with pytest.raises(SystemExit):
        _cli.main(["webhooks", "list", "--js", "--", "--js"])
    assert failure(capsys)["message"] == (
        "1 argument too many (quote a value that has spaces in it)"
    )


def test_a_mistyped_option_is_still_named(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _cli.main(["webhooks", "list", "--jsno"])
    assert "unrecognized option: --jsno" in capsys.readouterr().err


def test_an_abbreviated_json_flag_reports_a_usage_error_as_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # argparse reads --j as --json; the usage error has to agree with it.
    with pytest.raises(SystemExit) as caught:
        _cli.main(["webhooks", "list", "extra", "--j"])
    assert caught.value.code == 2
    assert failure(capsys)["code"] == "invalid_arguments"


def test_ssh_setup_usage_errors_are_json_under_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cli, "_client", lambda: pytest.fail("must not make an API request"))
    # --json after the mistake counts too: the whole of ssh's own words is read.
    assert _cli.main(["ssh", "--setup", "dev", "extra", "--json"]) == 2
    error = failure(capsys)
    assert error["code"] == "invalid_arguments"
    assert error["message"] == "--setup takes one computer and nothing more"
    assert "mandala-py ssh <computer>" in str(error["usage"])


def test_flags_that_belong_to_the_remote_command_do_not_make_a_failure_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Everything after a plain ssh's computer is the remote command's, so its
    # --setup --json say nothing about how this CLI reports its own failure.
    seen: list[list[str]] = []

    def connect(target: str, rest: list[str]) -> int:
        seen.append(rest)
        _cli._die("no computer named 'dev'", "not_found")

    monkeypatch.setattr(_cli, "_ssh_connect", connect)
    with pytest.raises(SystemExit) as caught:
        _cli.main(["ssh", "dev", "remote-tool", "--setup", "--json"])
    assert caught.value.code == "mandala-py: no computer named 'dev'"
    assert seen == [["remote-tool", "--setup", "--json"]]
    assert capsys.readouterr().err == ""


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
