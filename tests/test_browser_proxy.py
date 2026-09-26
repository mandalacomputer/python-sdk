"""A proxy for a computer's browsers: the bodies, the fields, the wait, launch and
the CLI (OPL-5144)."""

from __future__ import annotations

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

SERVER = "http://proxy.example.com:3128"
PROXY = {"server": SERVER, "bypass": ["<local>"]}


def proxied(pending, **extra):
    row = {**COMPUTER, "browser_proxy": PROXY, **extra}
    if pending is not None:
        row["browser_proxy_pending"] = pending
    return row


# --- bodies -------------------------------------------------------------------


def test_create_sends_the_setting_only_when_given():
    base = {"name": None, "template": None, "cpu": None, "ram_mb": None, "disk_gb": None}
    assert _api.create_body(**base, start=True, browser_proxy=PROXY)["browser_proxy"] == PROXY
    assert "browser_proxy" not in _api.create_body(**base, start=True)


def test_update_sends_null_to_remove_it():
    assert _api.browser_proxy_update_body(None) == {"browser_proxy": None}
    assert _api.browser_proxy_update_body({"server": SERVER, "bypass": []}) == {
        "browser_proxy": {"server": SERVER, "bypass": []}
    }


def test_a_proxy_read_off_a_computer_goes_back_as_it_is():
    read = mc.BrowserProxy(server=SERVER, bypass=("<local>", "*.example.com"))
    assert _api.browser_proxy_body(read) == {
        "server": SERVER,
        "bypass": ["<local>", "*.example.com"],
    }


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (SERVER, "must be a mapping"),
        ([SERVER], "must be a mapping"),
        ({}, "browser_proxy.server"),
        ({"server": "  "}, "server must not be empty"),
        ({"server": SERVER, "bypass": "example.com"}, "bypass must be a list"),
        ({"server": SERVER, "bypass": [1]}, r"bypass\[0\]"),
        ({"server": SERVER, "bypass": ["a.com", " "]}, r"bypass\[1\] must not be empty"),
        # A misspelt key would otherwise be dropped, and the browsers sent
        # through the proxy for every host the caller meant to exempt.
        ({"server": SERVER, "bypas": ["a.com"]}, "unknown keys"),
    ],
)
def test_the_shape_is_checked_here(value, message):
    with pytest.raises(ValueError, match=message):
        _api.browser_proxy_body(value)


def test_the_rules_on_values_are_left_to_the_platform():
    # Schemes, hosts and ports are the platform's to judge: the set it accepts
    # grows, and a copy here would refuse what it has since learned to take.
    for other in ("https://proxy.example.com:443", "ftp://x", "proxy.example.com"):
        assert _api.browser_proxy_body({"server": other}) == {"server": other}


# --- fields -------------------------------------------------------------------


def computer(**row):
    return mc.Computer(None, {"id": "launch-42", **row})  # type: ignore[arg-type]


def test_the_setting_reads_back_and_ignores_keys_it_does_not_know():
    c = computer(browser_proxy={**PROXY, "later": "field"}, browser_proxy_pending=True)
    assert c.browser_proxy == mc.BrowserProxy(server=SERVER, bypass=("<local>",))
    assert c.browser_proxy_pending is True
    bare = computer(browser_proxy={"server": SERVER})
    assert bare.browser_proxy == mc.BrowserProxy(server=SERVER)
    assert bare.browser_proxy_pending is False
    assert computer().browser_proxy is None


@pytest.mark.parametrize(
    "value",
    [
        SERVER,
        {"bypass": []},
        {"server": ""},
        {"server": SERVER, "bypass": "a.com"},
        {"server": SERVER, "bypass": ["a.com", 7]},
    ],
)
def test_a_value_it_cannot_read_raises_rather_than_being_dropped(value):
    # set_browser_proxy replaces the setting whole, so a bypass entry lost on
    # the read is one a caller's next change would remove without knowing.
    with pytest.raises(mc.MandalaError):
        _ = computer(browser_proxy=value).browser_proxy


def test_pending_that_is_not_a_boolean_raises():
    with pytest.raises(mc.MandalaError, match="browser_proxy_pending is not a boolean"):
        _ = computer(browser_proxy_pending="yes").browser_proxy_pending


# --- the wait -----------------------------------------------------------------


def wait_sync(monkeypatch, steps, **wait):
    scenario = Scenario(steps)
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.get("launch-42")
        return scenario, c.wait_for_browser_proxy(**wait)


def test_wait_polls_until_the_guest_has_it(monkeypatch):
    scenario, c = wait_sync(
        monkeypatch,
        [
            step("GET", "/launch-42", proxied(True)),
            step("GET", "/launch-42", proxied(True)),
            step("GET", "/launch-42", proxied(None)),
        ],
        poll=0.5,
    )
    assert not scenario.steps
    assert c.browser_proxy_pending is False


def test_wait_reads_again_even_when_the_handle_says_applied(monkeypatch):
    with pytest.raises(mc.TimeoutError) as caught:
        wait_sync(
            monkeypatch,
            [step("GET", "/launch-42", proxied(None))]
            + [step("GET", "/launch-42", proxied(True)) for _ in range(3)],
            timeout=2,
            poll=1,
        )
    assert str(caught.value) == "launch-42's browser proxy was still being applied after 2s"


def test_wait_waits_out_a_removal_whose_files_are_still_there(monkeypatch):
    removing = {**COMPUTER, "browser_proxy_pending": True}
    scenario, c = wait_sync(
        monkeypatch,
        [
            step("GET", "/launch-42", removing),
            step("GET", "/launch-42", removing),
            step("GET", "/launch-42", COMPUTER),
        ],
        poll=0.5,
    )
    assert c.browser_proxy is None
    assert not scenario.steps


def test_wait_answers_at_once_for_none(monkeypatch):
    scenario, _ = wait_sync(
        monkeypatch, [step("GET", "/launch-42", COMPUTER), step("GET", "/launch-42", COMPUTER)]
    )
    assert not scenario.steps


def test_wait_refuses_a_stopped_computer_nobody_is_starting(monkeypatch):
    stopped = proxied(None, status="stopped", running_ram_mb=0)
    with pytest.raises(mc.MandalaError) as caught:
        wait_sync(
            monkeypatch,
            [step("GET", "/launch-42", stopped), step("GET", "/launch-42", stopped)],
            timeout=60,
        )
    assert not isinstance(caught.value, mc.TimeoutError)
    assert str(caught.value) == (
        "launch-42 is 'stopped', and its browser proxy is applied only as it starts: call start()"
    )


def test_wait_rides_through_an_admitted_start(monkeypatch):
    # Never pending while not running, so the False on a stopped read is not an
    # answer; the admitted start is.
    admitted = proxied(None, status="stopped", running_ram_mb=1024)
    scenario, c = wait_sync(
        monkeypatch,
        [
            step("GET", "/launch-42", admitted),
            step("GET", "/launch-42", admitted),
            step("GET", "/launch-42", proxied(True)),
            step("GET", "/launch-42", proxied(False)),
        ],
        poll=0.5,
    )
    assert c.status == "running"
    assert not scenario.steps


def test_wait_names_a_create_whose_first_start_failed(monkeypatch):
    silent = proxied(None, status="stopped")
    del silent["running_ram_mb"]
    scenario = Scenario(
        [
            step("POST", "", {"computer": silent, "start_error": "no room"}),
            step("GET", "/launch-42", silent),
        ]
    )
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.create(browser_proxy=PROXY)
        with pytest.raises(mc.MandalaError) as caught:
            c.wait_for_browser_proxy(timeout=60)
    assert str(caught.value) == (
        "launch-42 is stopped after it failed to start, so its browser proxy was not applied: "
        "no room. Call start() to try again"
    )
    assert json.loads(scenario.requests[0].content)["browser_proxy"] == PROXY


def test_wait_rides_out_a_host_that_cannot_be_reached(monkeypatch):
    scenario, _ = wait_sync(
        monkeypatch,
        [
            step("GET", "/launch-42", proxied(True)),
            step("GET", "/launch-42", {"error": "host unreachable"}, status=503),
            step("GET", "/launch-42", proxied(False)),
        ],
        poll=0.5,
    )
    assert not scenario.steps


# --- set and launch -----------------------------------------------------------


def test_set_sends_the_change_alone_and_null_to_remove_it(monkeypatch):
    scenario = Scenario(
        [
            step("GET", "/launch-42", COMPUTER),
            step("PATCH", "/launch-42", proxied(True)),
            step("PATCH", "/launch-42", COMPUTER),
        ]
    )
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.get("launch-42")
        assert c.set_browser_proxy(PROXY) is c
        assert c.browser_proxy_pending is True
        c.set_browser_proxy(None)
        assert c.browser_proxy is None
    assert json.loads(scenario.requests[1].content) == {"browser_proxy": PROXY}
    assert json.loads(scenario.requests[2].content) == {"browser_proxy": None}


def launch_steps():
    return [
        step("POST", "", proxied(True)),
        step("GET", "/launch-42", proxied(True)),
        step("POST", "/launch-42/exec", GUEST),
        step("GET", "/launch-42", proxied(True)),
        step("GET", "/launch-42", proxied(False)),
    ]


def test_launch_waits_for_the_guest_to_have_it(monkeypatch):
    scenario = Scenario(launch_steps())
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = client.computers.launch(poll=0.5, browser_proxy=PROXY)
    assert not scenario.steps
    assert c.browser_proxy_pending is False
    assert json.loads(scenario.requests[0].content)["browser_proxy"] == PROXY


def test_launch_adds_no_request_for_none(monkeypatch):
    scenario = Scenario(
        [
            step("POST", "", COMPUTER),
            step("GET", "/launch-42", COMPUTER),
            step("POST", "/launch-42/exec", GUEST),
        ]
    )
    scenario.install(monkeypatch, resources, computers)
    with (
        httpx.Client(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.Client("com_test", base_url=BASE, http_client=http) as client,
    ):
        client.computers.launch(poll=0.5)
    assert not scenario.steps


# --- async --------------------------------------------------------------------


async def test_async_launch_waits_for_the_guest_to_have_it(monkeypatch):
    scenario = Scenario(launch_steps())
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.launch(poll=0.5, browser_proxy=PROXY)
    assert not scenario.steps
    assert c.browser_proxy_pending is False


async def test_async_set_and_wait(monkeypatch):
    scenario = Scenario(
        [
            step("GET", "/launch-42", COMPUTER),
            step("PATCH", "/launch-42", proxied(True)),
            step("GET", "/launch-42", proxied(True)),
            step("GET", "/launch-42", proxied(False)),
            step("PATCH", "/launch-42", COMPUTER),
        ]
    )
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.get("launch-42")
        await c.set_browser_proxy(PROXY)
        await c.wait_for_browser_proxy(poll=0.5)
        await c.set_browser_proxy(None)
    assert not scenario.steps
    assert json.loads(scenario.requests[4].content) == {"browser_proxy": None}


async def test_async_wait_refuses_a_stopped_computer_nobody_is_starting(monkeypatch):
    stopped = proxied(None, status="stopped", running_ram_mb=0)
    scenario = Scenario([step("GET", "/launch-42", stopped), step("GET", "/launch-42", stopped)])
    scenario.install(monkeypatch, async_resources, async_computers)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(scenario.handle)) as http,
        mc.AsyncClient("com_test", base_url=BASE, http_client=http) as client,
    ):
        c = await client.computers.get("launch-42")
        with pytest.raises(mc.MandalaError, match="applied only as it starts"):
            await c.wait_for_browser_proxy(timeout=60)


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
def test_cli_sets_a_proxy_by_name(cli_env, capsys):
    listing()
    patch = respx.patch(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(200, json=proxied(True, name="dev"))
    )
    argv = [
        "browser-proxy",
        "set",
        "dev",
        SERVER,
        "--bypass",
        "<local>, a.com",
        "--bypass",
        "b.com",
    ]
    assert _cli.main(argv) == 0
    assert json.loads(patch.calls.last.request.content) == {
        "browser_proxy": {"server": SERVER, "bypass": ["<local>", "a.com", "b.com"]}
    }
    out = capsys.readouterr().out
    assert f"dev: browsers through {SERVER}" in out
    assert "pending" in out


@respx.mock
def test_cli_clears_and_waits(cli_env, capsys, monkeypatch):
    monkeypatch.setattr(computers.time, "sleep", lambda _: None)
    listing()
    patch = respx.patch(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(200, json={**COMPUTER, "browser_proxy_pending": True})
    )
    reads = respx.get(f"{BASE}/computers/launch-42").mock(
        side_effect=[
            httpx.Response(200, json={**COMPUTER, "browser_proxy_pending": True}),
            httpx.Response(200, json={**COMPUTER, "name": "dev"}),
        ]
    )
    assert _cli.main(["browser-proxy", "clear", "dev", "--wait", "--json"]) == 0
    assert json.loads(patch.calls.last.request.content) == {"browser_proxy": None}
    assert reads.call_count == 2
    assert json.loads(capsys.readouterr().out) == {
        "id": "launch-42",
        "name": "dev",
        "browser_proxy": None,
        "browser_proxy_pending": False,
    }


@respx.mock
def test_cli_gets_the_setting(cli_env, capsys):
    listing()
    respx.get(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(200, json=proxied(None, name="dev"))
    )
    assert _cli.main(["browser-proxy", "get", "dev"]) == 0
    assert capsys.readouterr().out == f"dev: browsers through {SERVER}\n  bypass: <local>\n"


@respx.mock
def test_cli_prints_the_platforms_refusal_as_it_is(cli_env, capsys):
    # The rules on a proxy URL are the platform's, and they grow; the CLI sends
    # what was typed and passes the sentence back.
    sentence = "browser_proxy.server: https:// is not supported yet"
    listing()
    respx.patch(f"{BASE}/computers/launch-42").mock(
        return_value=httpx.Response(400, json={"error": sentence})
    )
    assert _cli.main(["browser-proxy", "set", "dev", "https://p.example.com:443", "--json"]) == 1
    assert sentence in json.loads(capsys.readouterr().err)["error"]["message"]


def test_cli_refuses_an_empty_url_before_any_request(cli_env, monkeypatch):
    monkeypatch.setattr(_cli, "_client", lambda: pytest.fail("must not make an API request"))
    assert _cli.main(["browser-proxy", "set", "dev", " "]) == 1
