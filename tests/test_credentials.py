"""Shared credential contract exercised through both public clients.

Every fixture is synthetic. Native tests use real files, with syscall wrappers
only to schedule mutations or observe IO; no fabricated stat results.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

import mandala_computer as mc
from mandala_computer import _credentials as credentials

CORPUS = json.loads((Path(__file__).parent / "fixtures/credentials-v1.json").read_bytes())
BASE_DOCUMENT = CORPUS["base_document"]
KEYS = tuple(CORPUS["synthetic_keys"].values())


def encode(document: Any) -> bytes:
    return json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def write_store(home: Path, document: Any = None, *, payload: bytes | None = None) -> Path:
    directory = home / ".mandala"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "credentials.json"
    path.write_bytes(
        payload if payload is not None else encode(BASE_DOCUMENT if document is None else document)
    )
    path.chmod(0o600)
    return path


def patched_file(spec: dict[str, Any]) -> Any:
    assert spec["base"] == "base"
    document = copy.deepcopy(BASE_DOCUMENT)
    for patch in spec["patch"]:
        parts = [
            part.replace("~1", "/").replace("~0", "~") for part in patch["path"].split("/")[1:]
        ]
        parent = document
        for part in parts[:-1]:
            parent = parent[part]
        if patch["op"] == "set":
            parent[parts[-1]] = copy.deepcopy(patch["value"])
        else:
            assert patch["op"] == "remove"
            del parent[parts[-1]]
    return document


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ("MANDALA_API_KEY", "MANDALA_PROFILE", "MANDALA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class Driver:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None
        self.response = httpx.Response(200, json=[])
        transport = httpx.MockTransport(self.respond)
        self.http = (
            httpx.Client(transport=transport, follow_redirects=True)
            if kind == "sync"
            else httpx.AsyncClient(transport=transport, follow_redirects=True)
        )

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        return httpx.Response(
            self.response.status_code, headers=self.response.headers, content=self.response.content
        )

    def construct(self, **options: Any) -> Any:
        constructor = mc.Client if self.kind == "sync" else mc.AsyncClient
        return constructor(http_client=self.http, **options)

    async def request(self, client: Any, method: str = "GET") -> Any:
        result = (
            client.computers.list()
            if method == "GET"
            else client.computers.create(name="fixture", template="base")
        )
        return result if self.kind == "sync" else await result

    async def close(self) -> None:
        if isinstance(self.http, httpx.Client):
            self.http.close()
        else:
            await self.http.aclose()


@pytest.fixture(params=["sync", "async"])
async def driver(request: pytest.FixtureRequest) -> Any:
    value = Driver(request.param)
    try:
        yield value
    finally:
        await value.close()


def assert_secret_safe(error: BaseException) -> None:
    diagnostic = str(error) + repr(error)
    for key in KEYS:
        assert key not in diagnostic
    assert "public_fixture_password" not in diagnostic
    assert "public_fixture_never_real" not in diagnostic


async def check_case(driver: Driver, case: dict[str, Any], options: dict[str, Any]) -> None:
    expected = case["expected"]
    if expected["outcome"] == "local_error":
        with pytest.raises(credentials.CredentialError) as caught:
            driver.construct(**options)
        assert caught.value.rule == expected["rule"]
        assert_secret_safe(caught.value)
        assert driver.requests == []
        return
    client = driver.construct(**options)
    assert client.base_url == expected["base_url"]
    assert client._t._credential_source == expected["source"]
    assert client._t._profile == expected.get("profile")
    await driver.request(client)
    assert len(driver.requests) == 1
    assert driver.requests[0].headers["Authorization"] == f"Bearer {expected['key']}"
    assert str(driver.requests[0].url) == expected["base_url"] + "/computers"


def observe_store_io(
    monkeypatch: pytest.MonkeyPatch, *, forbid_home: bool = False
) -> dict[str, Mock]:
    observed: dict[str, Mock] = {}
    for module, names in (
        (credentials.os, ("open", "read", "stat", "lstat", "fstat")),
        (credentials.json, ("loads",)),
        (credentials.Path, ("home",)),
    ):
        for name in names:
            original = getattr(module, name)
            wrapped = Mock(wraps=original)
            if name == "home" and forbid_home:
                wrapped.side_effect = AssertionError("home discovery is forbidden")
            monkeypatch.setattr(module, name, wrapped)
            observed[name] = wrapped
    return observed


@pytest.mark.parametrize("case", CORPUS["resolution_cases"], ids=lambda case: case["id"])
async def test_resolution(
    case: dict[str, Any], home: Path, driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    given = case["input"]
    for name, value in given.get("env", {}).items():
        monkeypatch.setenv(name, value)
    spec = given["file"]
    if isinstance(spec, dict):
        write_store(home, patched_file(spec))
    elif spec == "base":
        write_store(home)
    elif spec == "malformed-json":
        write_store(home, payload=b'{"version":')
    elif spec == "symlink-file":
        path = write_store(home)
        target = path.with_name("real.json")
        path.rename(target)
        path.symlink_to(target)
    else:
        assert spec == "missing"
    if given.get("platform") == "unsupported-file-protection":
        monkeypatch.setattr(credentials, "_supported", lambda: False)
    with monkeypatch.context() as observation:
        observed = observe_store_io(
            observation, forbid_home=given.get("home_discovery") == "throw_if_called"
        )
        # Construction is the measured operation; httpx's response JSON parser
        # runs afterwards and must not count as credential parsing.
        expected = case["expected"]
        if expected["outcome"] == "local_error":
            with pytest.raises(credentials.CredentialError) as caught:
                driver.construct(**given.get("options", {}))
            assert caught.value.rule == expected["rule"]
            assert_secret_safe(caught.value)
            client = None
        else:
            client = driver.construct(**given.get("options", {}))
        counts = {name: spy.call_count for name, spy in observed.items()}
    if expected["credential_file_io"] == "zero":
        assert not any(counts.values()), counts
    else:
        assert counts["home"] >= 1 and counts["open"] >= 1
    assert driver.requests == []
    if client is not None:
        assert client._t._credential_source == expected["source"]
        assert client._t._profile == expected.get("profile")
        assert client.base_url == expected["base_url"]
        await driver.request(client)
        assert driver.requests[0].headers["Authorization"] == f"Bearer {expected['key']}"
        assert str(driver.requests[0].url) == expected["base_url"] + "/computers"


@pytest.mark.parametrize("case", CORPUS["schema_cases"], ids=lambda case: case["id"])
async def test_schema(case: dict[str, Any], home: Path, driver: Driver) -> None:
    write_store(home, patched_file(case["file"]))
    await check_case(driver, case, case.get("options", {}))


@pytest.mark.parametrize("case", CORPUS["payload_cases"], ids=lambda case: case["id"])
async def test_payload(case: dict[str, Any], home: Path, driver: Driver) -> None:
    if "bytes_utf8" in case:
        payload = case["bytes_utf8"].encode("utf-8")
    elif "bytes_hex" in case:
        payload = bytes.fromhex(case["bytes_hex"])
    else:
        recipe = case["recipe"]
        if "profile_count" in recipe:
            document = {
                "version": 1,
                "default_profile": recipe["default_profile"],
                "profiles": {
                    f"p{index:03d}": recipe["profile_template"]
                    for index in range(recipe["profile_count"])
                },
            }
            payload = encode(document)
        else:
            assert recipe["base"] == "base"
            payload = encode(BASE_DOCUMENT)
            size = recipe["pad_json_trailing_ascii_spaces_to_bytes"]
            payload += b" " * (size - len(payload))
            assert len(payload) == size
    write_store(home, payload=payload)
    await check_case(driver, case, {})


@pytest.mark.parametrize("case", CORPUS["canonical_base_cases"], ids=lambda case: case["id"])
async def test_canonical_base(case: dict[str, Any], home: Path, driver: Driver) -> None:
    document = copy.deepcopy(BASE_DOCUMENT)
    expected = case["expected"]
    if expected["outcome"] == "canonical_url":
        base = expected["value"]
        document["profiles"]["default"]["base_url"] = base
        write_store(home, document)
        client = driver.construct(base_url=case["input"])
        assert client.base_url == base
        assert credentials.canonical_base(base) == base
        await driver.request(client)
        assert str(driver.requests[0].url) == base + "/computers"
        assert driver.requests[0].headers["Authorization"] == (
            "Bearer " + document["profiles"]["default"]["api_key"]
        )
    else:
        write_store(home)
        await check_case(driver, case, {"base_url": case["input"]})


@pytest.mark.parametrize("case", CORPUS["native_file_cases"], ids=lambda case: case["id"])
async def test_native_file(
    case: dict[str, Any], home: Path, driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not credentials._supported():
        pytest.skip("Native descriptor protection requires POSIX")
    number = case["id"].split("-")[0]
    if number in ("F04", "F05"):
        variable = (
            "MANDALA_TEST_FOREIGN_FILE_HOME"
            if number == "F04"
            else "MANDALA_TEST_FOREIGN_DIRECTORY_HOME"
        )
        foreign_home = os.environ.get(variable)
        if not foreign_home:
            pytest.skip(f"Native wrong-owner fixture unavailable; coordinator can set {variable}")
        foreign_directory = Path(foreign_home) / ".mandala"
        foreign_target = (
            foreign_directory / "credentials.json" if number == "F04" else foreign_directory
        )
        info = foreign_target.stat(follow_symlinks=False)
        assert info.st_uid != os.getuid(), "The supplied native fixture must have another owner"
        assert stat.S_IMODE(info.st_mode) == (0o600 if number == "F04" else 0o700)
        assert stat.S_ISREG(info.st_mode) if number == "F04" else stat.S_ISDIR(info.st_mode)
        monkeypatch.setenv("HOME", foreign_home)
    path = write_store(home)
    directory = path.parent
    listener = None
    if number in ("F02", "F15"):
        path.chmod(0o644 if number == "F02" else 0o400)
    elif number in ("F03", "F16"):
        directory.chmod(0o755 if number == "F03" else 0o500)
    elif number == "F06":
        target = path.with_name("real.json")
        path.rename(target)
        path.symlink_to(target)
    elif number == "F07":
        target = directory.with_name("real-directory")
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    elif number == "F08":
        os.link(path, path.with_name("second-link.json"))
    elif number == "F09":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif number == "F10":
        path.unlink()
        path.mkdir(mode=0o600)
    elif number == "F11":
        path.unlink()
        listener = socket.socket(socket.AF_UNIX)
        try:
            with monkeypatch.context() as local_cwd:
                local_cwd.chdir(directory)
                listener.bind("credentials.json")
        except PermissionError:
            listener.close()
            pytest.skip("Native Unix socket bind requires coordinator execution on this host")
    elif number in ("F12", "F13", "F14"):
        original_fstat = os.fstat
        mutated = False
        file_inode = path.stat().st_ino
        directory_inode = directory.stat().st_ino

        def mutate_after_fstat(descriptor: int) -> os.stat_result:
            nonlocal mutated
            info = original_fstat(descriptor)
            target_inode = directory_inode if number == "F14" else file_inode
            if not mutated and info.st_ino == target_inode:
                mutated = True
                if number == "F12":
                    with path.open("ab") as growing:
                        growing.write(b" " * 65_537)
                elif number == "F13":
                    replacement = path.with_name("replacement.json")
                    replacement.write_bytes(encode({**BASE_DOCUMENT, "default_profile": "Work"}))
                    replacement.chmod(0o600)
                    replacement.replace(path)
                else:
                    directory.rename(home / "original-directory")
                    write_store(home, {**BASE_DOCUMENT, "default_profile": "Work"})
            return info

        monkeypatch.setattr(credentials.os, "fstat", mutate_after_fstat)
    start = time.monotonic()
    try:
        expected = case["expected"]["result"]
        allowed_race = number in ("F13", "F14")
        try:
            client = driver.construct()
        except credentials.CredentialError as error:
            assert_secret_safe(error)
            if allowed_race:
                assert error.rule in ("unsafe_file", "unsafe_directory", "missing_credentials")
            else:
                assert error.rule == expected
            assert driver.requests == []
        else:
            assert expected == "credential" or allowed_race
            original = case.get(
                "original_identity",
                {
                    "key": BASE_DOCUMENT["profiles"]["default"]["api_key"],
                    "base_url": BASE_DOCUMENT["profiles"]["default"]["base_url"],
                    "profile": "default",
                },
            )
            assert client.base_url == original["base_url"]
            assert client._t._profile == original["profile"]
            await driver.request(client)
            assert driver.requests[0].headers["Authorization"] == "Bearer " + original["key"]
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < case["expected"]["completion_deadline_ms"]
        if number in ("F12", "F13", "F14"):
            assert mutated
    finally:
        if listener is not None:
            listener.close()
        if number == "F16":
            directory.chmod(0o700)


@pytest.mark.parametrize("case", CORPUS["instance_cases"], ids=lambda case: case["id"])
async def test_instance(
    case: dict[str, Any], home: Path, driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_store(home)
    selected = BASE_DOCUMENT["profiles"]["Work"]["api_key"]
    replacement = BASE_DOCUMENT["profiles"]["work"]["api_key"]
    client = driver.construct(profile="Work", retries={"idempotent": 3})
    number = case["id"].split("-")[0]
    modified = copy.deepcopy(BASE_DOCUMENT)
    if number == "I03":
        modified["profiles"]["Work"]["account"] = {"id": "another-account", "name": "another"}
        modified["profiles"]["Work"]["scope"] = {"type": "account"}
    else:
        modified["profiles"]["Work"]["api_key"] = replacement
    temporary = path.with_name("replacement.json")
    temporary.write_bytes(encode(modified))
    temporary.chmod(0o600)
    temporary.replace(path)
    newer = driver.construct(profile="Work") if number in ("I01", "I03") else None
    default = BASE_DOCUMENT["profiles"]["default"]["api_key"]

    def backend(request: httpx.Request) -> httpx.Response:
        bearer = request.headers["Authorization"]
        if number in ("I02", "I04") and bearer == "Bearer " + selected:
            return httpx.Response(
                401,
                json={
                    "error": "revoked fixture",
                    "reason": "revoked",
                    "request_id": "fixture-revoked",
                },
                headers={
                    "x-request-id": "fixture-revoked",
                    "retry-after": "7",
                    "www-authenticate": 'Bearer error="invalid_token"',
                },
            )
        identity = {
            default: "vm-default",
            selected: "vm-selected",
            replacement: "vm-replacement",
        }.get(bearer.removeprefix("Bearer "))
        if identity is None:
            return httpx.Response(401, json={"error": "unknown fixture identity"})
        return httpx.Response(
            200, json=[{"id": identity, "name": identity, "os": "linux", "status": "running"}]
        )

    driver.handler = backend
    # The default is independently usable at the fixture server. A refused
    # selected key must never acquire that authority through fallback.
    decoy = driver.construct(api_key=default, base_url=client.base_url)
    assert (await driver.request(decoy))[0].id == "vm-default"
    driver.requests.clear()
    with monkeypatch.context() as observation:
        observed = observe_store_io(observation, forbid_home=True)
        # Observe credential parsing separately from normal response decoding.
        observed.pop("loads")
        observation.setattr(credentials.json, "loads", json_loads)
        if number in ("I02", "I04"):
            with pytest.raises(mc.AuthenticationError) as caught:
                await driver.request(client, case["request_method"])
            assert caught.value.status == 401
            assert caught.value.reason == "revoked"
            assert caught.value.request_id == "fixture-revoked"
            assert caught.value.retry_after == 7
            assert caught.value.www_authenticate == 'Bearer error="invalid_token"'
            assert len(driver.requests) == 1
            assert driver.requests[0].method == case["request_method"]
        else:
            assert (await driver.request(client))[0].id == "vm-selected"
            assert (await driver.request(client))[0].id == "vm-selected"
            new_identity = (await driver.request(newer))[0].id
            assert new_identity == ("vm-replacement" if number == "I01" else "vm-selected")
            expected_new = replacement if number == "I01" else selected
            assert [r.headers["Authorization"] for r in driver.requests] == [
                "Bearer " + selected,
                "Bearer " + selected,
                "Bearer " + expected_new,
            ]
        assert not any(spy.called for spy in observed.values())
        assert driver.requests[0].headers["Authorization"] == "Bearer " + selected
        assert all(
            str(request.url).startswith(BASE_DOCUMENT["profiles"]["Work"]["base_url"] + "/")
            for request in driver.requests
        )


# Keep this real parser reference separate from the scoped IO observation.
json_loads = json.loads


async def test_none_is_absent(home: Path, driver: Driver) -> None:
    write_store(home)
    client = driver.construct(api_key=None, profile=None, base_url=None)
    await driver.request(client)
    assert client._t._profile == "default"
    assert (
        driver.requests[0].headers["Authorization"]
        == "Bearer " + BASE_DOCUMENT["profiles"]["default"]["api_key"]
    )


@pytest.mark.parametrize("path_kind", ["ordinary", "sse", "bounded"])
async def test_file_credentials_never_follow_redirects(
    home: Path, driver: Driver, path_kind: str
) -> None:
    write_store(home)
    client = driver.construct(retries={"idempotent": 2})
    # A same-origin redirect would retain Authorization in httpx.
    driver.response = httpx.Response(307, headers={"Location": "/another-account"})
    with pytest.raises(mc.MandalaError):
        if path_kind == "ordinary":
            await driver.request(client)
        elif path_kind == "bounded":
            result = client._t.bounded_binary(
                "GET", "/computers", max_bytes=100, status=200, media="application/json"
            )
            if driver.kind == "async":
                await result
        elif driver.kind == "sync":
            list(client._t.sse("POST", "/computers"))
        else:
            async for _ in client._t.sse("POST", "/computers"):
                pass
    assert len(driver.requests) == 1


@pytest.mark.parametrize("relative_home", [".", "relative"])
async def test_relative_home_never_searches_working_directory(
    home: Path, driver: Driver, monkeypatch: pytest.MonkeyPatch, relative_home: str
) -> None:
    write_store(home)
    monkeypatch.chdir(home)
    monkeypatch.setenv("HOME", relative_home)
    with pytest.raises(credentials.CredentialError) as caught:
        driver.construct()
    assert caught.value.rule == "unsafe_directory"
    assert driver.requests == []


async def test_read_deadline_refuses_before_opening_a_file(
    home: Path, driver: Driver, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_store(home)
    # No sleeping or fabricated stat: exhaust the clock budget during home
    # discovery, then prove the reader cannot proceed to opening the store.
    clock = Mock(side_effect=[0.0, 5.0])
    opener = Mock(side_effect=AssertionError("the read attempt already expired"))
    with monkeypatch.context() as read_attempt:
        read_attempt.setattr(credentials.time, "monotonic", clock)
        read_attempt.setattr(credentials.os, "open", opener)
        with pytest.raises(credentials.CredentialError) as caught:
            driver.construct()
    assert caught.value.rule == "read_timeout"
    assert not opener.called
    assert driver.requests == []
