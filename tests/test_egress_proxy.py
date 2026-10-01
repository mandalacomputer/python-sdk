"""A proxy for ALL of a computer's outbound TCP: the bodies, the fields, the
create and change on both clients, and the CLI (OPL-5246)."""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest
import respx
from tests.test_launch import BASE, COMPUTER, GUEST, Scenario, step

import mandala_computer as mc
import mandala_computer._async_computer as async_computers
import mandala_computer._async_resources as async_resources
import mandala_computer._computer as computers
import mandala_computer._resources as resources
from mandala_computer import _api, _cli

SERVER = "https://proxy.example.com:3128"
CREDS = "csec-0123456789abcdef"
PROXY = {"server": SERVER, "credentials_secret_id": CREDS}


# --- bodies -------------------------------------------------------------------


def test_create_sends_the_setting_only_when_given():
    base = {"name": None, "template": None, "cpu": None, "ram_mb": None, "disk_gb": None}
    assert _api.create_body(**base, start=True, egress_proxy=PROXY)["egress_proxy"] == PROXY
    assert "egress_proxy" not in _api.create_body(**base, start=True)


def test_update_sends_the_setting_alone_and_null_to_remove_it():
    assert _api.egress_proxy_update_body(None) == {"egress_proxy": None}
    assert _api.egress_proxy_update_body(PROXY) == {"egress_proxy": PROXY}
    assert _api.egress_proxy_update_body({"server": SERVER, "credentials_secret_id": None}) == {
        "egress_proxy": {"server": SERVER}
    }


def test_a_proxy_read_off_a_computer_goes_back_as_it_is():
    # Credentials included: the setting is replaced whole, so an id dropped on
    # the way back removes them and the upstream refuses every connection.
    assert _api.egress_proxy_body(mc.EgressProxy(server=SERVER, credentials_secret_id=CREDS)) == (
        PROXY
    )
    assert _api.egress_proxy_body(mc.EgressProxy(server=SERVER)) == {"server": SERVER}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        # A browser proxy's bypass list has no meaning here.
        ({"server": SERVER, "bypass": ["<local>"]}, "no bypass list"),
        ({"server": SERVER, "credentials": CREDS}, "unknown keys"),
        ({"server": SERVER, "credentials_secret_id": "csec-0123"}, "must be a secret's id"),
        ({"server": SERVER, "credentials_secret_id": 7}, "credentials_secret_id"),
        ({"server": ""}, "must not be empty"),
        ({"credentials_secret_id": CREDS}, "server"),
        (SERVER, "must be a mapping"),
    ],
)
def test_the_shape_is_checked_here(value, message):
    with pytest.raises(ValueError, match=message):
        _api.egress_proxy_body(value)


def test_the_rules_on_values_are_left_to_the_platform():
    for other in ("http://p.example:3128", "socks5://p.example:1080", "ftp://x", "p.example"):
        assert _api.egress_proxy_body({"server": other}) == {"server": other}


# --- fields -------------------------------------------------------------------


def computer(**row):
    return mc.Computer(None, {"id": "launch-42", **row})  # type: ignore[arg-type]


def test_the_setting_reads_back_with_and_without_credentials():
    c = computer(egress_proxy={**PROXY, "later": "field"}, egress_proxy_pending=True)
    assert c.egress_proxy == mc.EgressProxy(server=SERVER, credentials_secret_id=CREDS)
    assert c.egress_proxy_pending is True
    bare = computer(egress_proxy={"server": SERVER})
    assert bare.egress_proxy == mc.EgressProxy(server=SERVER)
    assert bare.egress_proxy_pending is False
    none = computer(egress_proxy={"server": SERVER, "credentials_secret_id": None})
    assert none.egress_proxy == mc.EgressProxy(server=SERVER)
    assert computer().egress_proxy is None
    assert computer(egress_proxy_pending=False).egress_proxy_pending is False


@pytest.mark.parametrize(
    "value",
    [
        SERVER,
        {},
        {"server": ""},
        {"server": 7},
        {"server": SERVER, "credentials_secret_id": ""},
        {"server": SERVER, "credentials_secret_id": 7},
    ],
)
def test_a_value_it_cannot_read_raises_rather_than_being_dropped(value):
    with pytest.raises(mc.MandalaError, match="egress_proxy"):
        _ = computer(egress_proxy=value).egress_proxy


def test_pending_that_is_not_a_boolean_raises():
    with pytest.raises(mc.MandalaError, match="egress_proxy_pending is not a boolean"):
        _ = computer(egress_proxy_pending="yes").egress_proxy_pending


# --- on the wire, both clients ------------------------------------------------


@respx.mock
def test_create_and_change_on_the_wire():
    post = respx.post(f"{BASE}/computers").mock(
        return_value=httpx.Response(201, json={**COMPUTER, "egress_proxy": PROXY})
    )
    patch = respx.patch(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(200, json=COMPUTER)
    )
    with mc.Client("com_test", base_url=BASE) as client:
        c = client.computers.create(
            template="base", egress_proxy={"server": SERVER, "credentials_secret_id": CREDS}
        )
        assert json.loads(post.calls.last.request.content)["egress_proxy"] == PROXY
        # A proxy read off the computer, edited, keeps its credentials.
        c.set_egress_proxy(dataclasses.replace(c.egress_proxy, server="https://q.example:443"))
        assert json.loads(patch.calls.last.request.content) == {
            "egress_proxy": {"server": "https://q.example:443", "credentials_secret_id": CREDS}
        }
        c.set_egress_proxy(None)
        assert json.loads(patch.calls.last.request.content) == {"egress_proxy": None}
        with pytest.raises(ValueError, match="no bypass list"):
            c.set_egress_proxy({"server": SERVER, "bypass": ["a.com"]})  # type: ignore[typeddict-unknown-key]
    assert patch.call_count == 2


@respx.mock
async def test_async_create_and_change_on_the_wire():
    post = respx.post(f"{BASE}/computers").mock(
        return_value=httpx.Response(201, json={**COMPUTER, "egress_proxy": PROXY})
    )
    patch = respx.patch(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(200, json=COMPUTER)
    )
    async with mc.AsyncClient("com_test", base_url=BASE) as client:
        c = await client.computers.create(template="base", egress_proxy=PROXY)
        assert json.loads(post.calls.last.request.content)["egress_proxy"] == PROXY
        assert c.egress_proxy == mc.EgressProxy(server=SERVER, credentials_secret_id=CREDS)
        await c.set_egress_proxy({"server": "socks5://p.example:1080"})
        assert json.loads(patch.calls.last.request.content) == {
            "egress_proxy": {"server": "socks5://p.example:1080"}
        }
        await c.set_egress_proxy(None)
        assert json.loads(patch.calls.last.request.content) == {"egress_proxy": None}
        with pytest.raises(ValueError, match="unknown keys"):
            await c.set_egress_proxy({"server": SERVER, "credentials": CREDS})  # type: ignore[typeddict-unknown-key]
    assert patch.call_count == 2


# --- the wait, and launch (OPL-5322) ------------------------------------------


def egressed(pending, **extra):
    row = {**COMPUTER, "egress_proxy": PROXY, **extra}
    if pending is not None:
        row["egress_proxy_pending"] = pending
    return row


def run_sync(monkeypatch, steps, act):
    scenario = Scenario(steps)
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        return scenario, act(client)


async def run_async(monkeypatch, steps, act):
    scenario = Scenario(steps)
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        return scenario, await act(client)


def pending_then_held():
    return [
        step("GET", "/launch-42", egressed(True)),
        step("GET", "/launch-42", egressed(True)),
        step("GET", "/launch-42", egressed(False)),
    ]


def test_wait_polls_until_the_host_holds_the_credentials(monkeypatch):
    scenario, c = run_sync(
        monkeypatch,
        pending_then_held(),
        lambda client: mc.Computer(client._t, egressed(True)).wait_for_egress_proxy(poll=0.5),
    )
    assert not scenario.steps
    assert c.egress_proxy_pending is False


async def test_async_wait_polls_until_the_host_holds_the_credentials(monkeypatch):
    async def act(client):
        return await mc.AsyncComputer(client._t, egressed(True)).wait_for_egress_proxy(poll=0.5)

    scenario, c = await run_async(monkeypatch, pending_then_held(), act)
    assert not scenario.steps
    assert c.egress_proxy_pending is False


def test_wait_answers_at_once_for_a_proxy_with_no_credentials(monkeypatch):
    plain = {**COMPUTER, "egress_proxy": {"server": SERVER}}
    scenario, _ = run_sync(
        monkeypatch,
        [step("GET", "/launch-42", plain)],
        lambda client: mc.Computer(client._t, plain).wait_for_egress_proxy(),
    )
    assert not scenario.steps


def test_wait_times_out_naming_the_credentials(monkeypatch):
    with pytest.raises(mc.TimeoutError) as caught:
        run_sync(
            monkeypatch,
            # Reads at 0, 0.25, 0.75 and 1.75s: the sleep ramps to the poll.
            [step("GET", "/launch-42", egressed(True)) for _ in range(4)],
            lambda client: mc.Computer(client._t, egressed(True)).wait_for_egress_proxy(
                timeout=2, poll=1
            ),
        )
    assert str(caught.value) == (
        "launch-42's host still did not hold its egress proxy's credentials after 2s, so its "
        "connections are still being closed"
    )


def test_wait_refuses_a_stopped_computer_nobody_is_starting(monkeypatch):
    stopped = egressed(None, status="stopped", running_ram_mb=0)
    with pytest.raises(mc.MandalaError) as caught:
        run_sync(
            monkeypatch,
            [step("GET", "/launch-42", stopped)],
            lambda client: mc.Computer(client._t, stopped).wait_for_egress_proxy(timeout=60),
        )
    assert not isinstance(caught.value, mc.TimeoutError)
    assert "call start()" in str(caught.value)


def launch_steps(created):
    return [
        step("POST", "", created),
        step("GET", "/launch-42", created),
        step("POST", "/launch-42/exec", GUEST),
        *pending_then_held(),
    ]


@pytest.mark.parametrize(
    ("argument", "created"),
    [
        # The create named credentials, and the reads before the wait left the
        # setting out...
        (PROXY, COMPUTER),
        # ...or the create did not, and the computer's record does.
        (None, egressed(True)),
    ],
)
def test_launch_waits_for_the_credentials(monkeypatch, argument, created):
    scenario, c = run_sync(
        monkeypatch,
        launch_steps(created),
        lambda client: client.computers.launch(poll=0.5, egress_proxy=argument),
    )
    assert not scenario.steps
    assert c.egress_proxy_pending is False


async def test_async_launch_waits_for_the_credentials(monkeypatch):
    async def act(client):
        return await client.computers.launch(poll=0.5, egress_proxy=PROXY)

    scenario, c = await run_async(monkeypatch, launch_steps(egressed(True)), act)
    assert not scenario.steps
    assert c.egress_proxy_pending is False


def test_launch_adds_no_request_for_a_proxy_without_credentials(monkeypatch):
    plain = {**COMPUTER, "egress_proxy": {"server": SERVER}}
    scenario, _ = run_sync(
        monkeypatch,
        [
            step("POST", "", plain),
            step("GET", "/launch-42", plain),
            step("POST", "/launch-42/exec", GUEST),
        ],
        lambda client: client.computers.launch(poll=0.5, egress_proxy={"server": SERVER}),
    )
    assert not scenario.steps


# --- CLI ----------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch):
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    monkeypatch.setenv("MANDALA_BASE_URL", BASE)


def listing():
    return respx.get(f"{BASE}/computers").mock(
        return_value=httpx.Response(200, json=[{**COMPUTER, "name": "dev"}])
    )


@respx.mock
def test_cli_gets_the_setting(cli_env, capsys):
    listing()
    respx.get(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(
            200,
            json={**COMPUTER, "name": "dev", "egress_proxy": PROXY, "egress_proxy_pending": True},
        )
    )
    assert _cli.main(["egress-proxy", "get", "dev"]) == 0
    out = capsys.readouterr().out
    assert f"dev: all outbound TCP through {SERVER}" in out
    assert f"  credentials: secret {CREDS}" in out
    assert "pending" in out
    assert _cli.main(["egress-proxy", "get", "dev", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "id": "launch-42",
        "name": "dev",
        "egress_proxy": PROXY,
        "egress_proxy_pending": True,
    }


@respx.mock
def test_cli_set_keeps_replaces_or_removes_the_credentials(cli_env):
    # The setting is replaced whole, so a set that named no credentials would
    # remove the proxy's and leave every connection refused by its upstream.
    listing()
    current = {**COMPUTER, "name": "dev", "egress_proxy": PROXY}
    respx.get(f"{BASE}/computers/launch-42").mock(return_value=httpx.Response(200, json=current))
    patch = respx.patch(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(200, json=current)
    )
    # The same server, written with other case: kept.
    same = "HTTPS://Proxy.Example.com:3128"
    assert _cli.main(["egress-proxy", "set", "dev", same]) == 0
    assert json.loads(patch.calls.last.request.content) == {
        "egress_proxy": {"server": same, "credentials_secret_id": CREDS}
    }
    other = "csec-fedcba9876543210"
    assert _cli.main(["egress-proxy", "set", "dev", SERVER, "--credentials", other]) == 0
    assert json.loads(patch.calls.last.request.content) == {
        "egress_proxy": {"server": SERVER, "credentials_secret_id": other}
    }
    assert _cli.main(["egress-proxy", "set", "dev", SERVER, "--no-credentials"]) == 0
    assert json.loads(patch.calls.last.request.content) == {"egress_proxy": {"server": SERVER}}


@respx.mock
def test_cli_set_refuses_a_new_server_without_a_credentials_flag(cli_env, capsys):
    listing()
    current = {**COMPUTER, "name": "dev", "egress_proxy": PROXY}
    respx.get(f"{BASE}/computers/launch-42").mock(return_value=httpx.Response(200, json=current))
    patch = respx.patch(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(200, json=current)
    )
    for url in (
        "https://other.example:3128",
        "https://proxy.example.com:8443",
        "http://proxy.example.com:3128",
    ):
        assert _cli.main(["egress-proxy", "set", "dev", url, "--json"]) == 1, url
        error = json.loads(capsys.readouterr().err)["error"]
        assert error["code"] == "invalid_arguments", url
        assert CREDS in error["message"], url
        assert "--no-credentials" in error["message"], url
    assert patch.call_count == 0


@respx.mock
def test_cli_clears_it(cli_env, capsys):
    listing()
    patch = respx.patch(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(200, json={**COMPUTER, "name": "dev"})
    )
    assert _cli.main(["egress-proxy", "clear", "dev"]) == 0
    assert json.loads(patch.calls.last.request.content) == {"egress_proxy": None}
    assert "dev: no egress proxy; its traffic goes out directly" in capsys.readouterr().out


def test_cli_refuses_a_malformed_or_doubled_credentials_flag_before_any_request(
    cli_env, monkeypatch
):
    monkeypatch.setattr(_cli, "_client", lambda: pytest.fail("must not make an API request"))
    assert _cli.main(["egress-proxy", "set", "dev", SERVER, "--credentials", "csec-01"]) == 1
    assert _cli.main(["egress-proxy", "set", "dev", " "]) == 1
    with pytest.raises(SystemExit):
        _cli.main(
            ["egress-proxy", "set", "dev", SERVER, "--credentials", CREDS, "--no-credentials"]
        )
