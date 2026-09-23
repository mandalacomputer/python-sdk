"""A computer's secret bindings: bound at create, read, and replaced (OPL-4974).

Every case runs on both clients. The PUT responder echoes what was sent, the way
the platform answers a replace, so the decode of a replace's answer is exercised
on the list this SDK actually put on the wire rather than on a constant.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"
A = "csec-0123456789abcdef"
B = "csec-0123456789abcde0"
REV_A = "csr-0123456789abcdef01234567"
REV_B = "csr-0123456789abcdef01234568"
COMPUTER = {"id": "vm-1", "name": "dev", "status": "running", "os": "linux"}
BINDINGS = {
    "secrets": [
        {"secret_id": A, "revision_id": REV_A, "env": "API_TOKEN"},
        {"secret_id": B, "revision_id": REV_B, "file": "kubeconfig"},
    ],
    "version": 3,
}

NINE_FILES = [{"secret_id": f"csec-{str(i) * 16}", "file": f"f{i}"} for i in range(9)]
#: Everything the platform would refuse, refused here before any request.
BAD: dict[str, Any] = {
    "both": [{"secret_id": A, "env": "X", "file": "x"}],
    "neither": [{"secret_id": A}],
    "a bad variable": [{"secret_id": A, "env": "1X"}],
    "a bad file name": [{"secret_id": A, "file": "Ca.pem"}],
    "one secret twice": [{"secret_id": A, "env": "X"}, {"secret_id": A, "env": "Y"}],
    "one variable twice": [{"secret_id": A, "env": "X"}, {"secret_id": B, "env": "X"}],
    "one file twice": [{"secret_id": A, "file": "x"}, {"secret_id": B, "file": "x"}],
    "nine files": NINE_FILES,
    "an empty secret id": [{"secret_id": "", "env": "X"}],
    "a blank secret id": [{"secret_id": "  ", "env": "X"}],
    "no secret id": [{"env": "X"}],
    "an empty revision id": [{"secret_id": A, "env": "X", "revision_id": ""}],
    "a blank revision id": [{"secret_id": A, "env": "X", "revision_id": " \t"}],
    "a padded secret id": [{"secret_id": f" {A}", "env": "X"}],
    "a padded revision id": [{"secret_id": A, "env": "X", "revision_id": f"{REV_A}\n"}],
    "an unknown key": [{"secret_id": A, "env": "X", "revision": REV_A}],
    "a mapping for a list": {"secret_id": A, "env": "X"},
    "thirty-three": [{"secret_id": f"csec-{i:016x}", "env": f"V{i}"} for i in range(33)],
}

#: Answers a read or a replace can come back with that are not a whole binding list.
MALFORMED: dict[str, Any] = {
    "not a list": {"error": "nope"},
    "no version": {"secrets": BINDINGS["secrets"]},
    "a negative version": {**BINDINGS, "version": -1},
    "a boolean version": {**BINDINGS, "version": True},
    "a fractional version": {**BINDINGS, "version": 3.5},
    "a row that is not an object": {"secrets": ["csec-1"], "version": 1},
    "an id-less row": {"secrets": [{"revision_id": REV_A, "env": "X"}], "version": 1},
    "an empty id": {"secrets": [{"secret_id": "", "revision_id": REV_A, "env": "X"}], "version": 1},
    "no revision": {"secrets": [{"secret_id": A, "env": "X"}], "version": 1},
    "neither name": {"secrets": [{"secret_id": A, "revision_id": REV_A}], "version": 1},
    "a padded id": {
        "secrets": [{"secret_id": f"{A} ", "revision_id": REV_A, "env": "X"}],
        "version": 1,
    },
    "a padded revision": {
        "secrets": [{"secret_id": A, "revision_id": f" {REV_A}", "env": "X"}],
        "version": 1,
    },
    "a bad variable": {
        "secrets": [{"secret_id": A, "revision_id": REV_A, "env": "1X"}],
        "version": 1,
    },
    "a bad file name": {
        "secrets": [{"secret_id": A, "revision_id": REV_A, "file": "Ca.pem"}],
        "version": 1,
    },
    "an empty variable beside a file": {
        "secrets": [{"secret_id": A, "revision_id": REV_A, "env": "", "file": "x"}],
        "version": 1,
    },
    "a variable that is not a string": {
        "secrets": [{"secret_id": A, "revision_id": REV_A, "env": 7}],
        "version": 1,
    },
    "both names": {
        "secrets": [{"secret_id": A, "revision_id": REV_A, "env": "X", "file": "x"}],
        "version": 1,
    },
}


def echo(request: httpx.Request) -> httpx.Response:
    """A replace, answered the way the platform answers one: what was stored."""
    sent = json.loads(request.content)
    rows = [
        {**b, "revision_id": b.get("revision_id", f"csr-latest-{b['secret_id']}")}
        for b in sent["secrets"]
    ]
    return httpx.Response(200, json={"secrets": rows, "version": sent.get("version", 0) + 1})


@pytest.fixture
def api() -> Any:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        router.post("/computers", name="create").mock(
            return_value=httpx.Response(201, json=COMPUTER)
        )
        router.get("/computers/vm-1", name="get").mock(
            return_value=httpx.Response(200, json=COMPUTER)
        )
        router.get("/computers/vm-1/secrets", name="read").mock(
            return_value=httpx.Response(200, json=BINDINGS)
        )
        router.put("/computers/vm-1/secrets", name="replace").mock(side_effect=echo)
        yield router


def sent(route: Any) -> Any:
    return json.loads(route.calls.last.request.content)


EXPECTED = [
    mc.SecretBinding(secret_id=A, revision_id=REV_A, env="API_TOKEN"),
    mc.SecretBinding(secret_id=B, revision_id=REV_B, file="kubeconfig"),
]
BOTH_KINDS: list[mc.SecretBindingArgs] = [
    {"secret_id": A, "env": "API_TOKEN"},
    {"secret_id": B, "file": "kubeconfig"},
]
KEPT: list[mc.SecretBindingArgs] = [
    {"secret_id": A, "env": "API_TOKEN", "revision_id": REV_A},
    {"secret_id": B, "file": "kubeconfig"},
]


# --- binding at create -------------------------------------------------------


def test_create_sends_each_binding_in_the_wire_spelling(api: Any) -> None:
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        client.computers.create(template="base", secrets=BOTH_KINDS)
    assert sent(api["create"]) == {"start": True, "template": "base", "secrets": BOTH_KINDS}


async def test_async_create_sends_each_binding_in_the_wire_spelling(api: Any) -> None:
    async with mc.AsyncClient(api_key="com_test", base_url=BASE) as client:
        await client.computers.create(template="base", secrets=BOTH_KINDS)
    assert sent(api["create"]) == {"start": True, "template": "base", "secrets": BOTH_KINDS}


def test_create_sends_no_secrets_key_when_none_are_bound(api: Any) -> None:
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        client.computers.create(template="base")
    assert sent(api["create"]) == {"start": True, "template": "base"}


async def test_async_create_sends_no_secrets_key_when_none_are_bound(api: Any) -> None:
    async with mc.AsyncClient(api_key="com_test", base_url=BASE) as client:
        await client.computers.create(template="base")
    assert sent(api["create"]) == {"start": True, "template": "base"}


def test_launch_carries_the_bindings_to_its_create(api: Any) -> None:
    api.post("/computers/vm-1/exec").mock(
        return_value=httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
    )
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        client.computers.launch(template="base", secrets=BOTH_KINDS, poll=0.01)
    assert sent(api["create"])["secrets"] == BOTH_KINDS


@pytest.mark.parametrize("case", sorted(BAD))
def test_create_refuses_before_sending(api: Any, case: str) -> None:
    with mc.Client(api_key="com_test", base_url=BASE) as client, pytest.raises(ValueError):
        client.computers.create(template="base", secrets=BAD[case])
    assert not api["create"].called


@pytest.mark.parametrize("case", sorted(BAD))
async def test_async_create_refuses_before_sending(api: Any, case: str) -> None:
    async with mc.AsyncClient(api_key="com_test", base_url=BASE) as client:
        with pytest.raises(ValueError):
            await client.computers.create(template="base", secrets=BAD[case])
    assert not api["create"].called


def test_a_variable_and_a_file_may_share_a_spelling(api: Any) -> None:
    same: list[mc.SecretBindingArgs] = [
        {"secret_id": A, "env": "ca"},
        {"secret_id": B, "file": "ca"},
    ]
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        client.computers.create(template="base", secrets=same)
    assert sent(api["create"]) == {"start": True, "template": "base", "secrets": same}


def test_the_limits_themselves_are_accepted(api: Any) -> None:
    """Thirty-two bindings, eight of them files: the edge is inside, not out."""
    edge: list[mc.SecretBindingArgs] = [
        {"secret_id": f"csec-{i:016x}", "file": f"f{i}"} for i in range(8)
    ] + [{"secret_id": f"csec-{i:016x}", "env": f"V{i}"} for i in range(8, 32)]
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        client.computers.create(template="base", secrets=edge)
    assert len(sent(api["create"])["secrets"]) == 32


# --- reading and replacing ---------------------------------------------------


def test_reads_them_typed_a_file_binding_and_all(api: Any) -> None:
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        got = client.computers.get("vm-1").secrets()
    assert got.version == 3
    assert got.secrets == EXPECTED
    assert got.raw == BINDINGS


async def test_async_reads_them_typed_a_file_binding_and_all(api: Any) -> None:
    async with mc.AsyncClient(api_key="com_test", base_url=BASE) as client:
        got = await (await client.computers.get("vm-1")).secrets()
    assert got.version == 3
    assert got.secrets == EXPECTED


def test_replaces_them_whole_with_the_version_and_a_kept_revision(api: Any) -> None:
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        vm = client.computers.get("vm-1")
        got = vm.set_secrets(KEPT, version=3)
        assert sent(api["replace"]) == {"secrets": KEPT, "version": 3}
        # The echo's answer, decoded: the kept revision stays, the other is new.
        assert got.version == 4
        assert got.secrets == [
            mc.SecretBinding(secret_id=A, revision_id=REV_A, env="API_TOKEN"),
            mc.SecretBinding(secret_id=B, revision_id=f"csr-latest-{B}", file="kubeconfig"),
        ]
        emptied = vm.set_secrets([])
        assert sent(api["replace"]) == {"secrets": []}
        assert emptied.secrets == []
        with pytest.raises(ValueError, match="version"):
            vm.set_secrets([], version=-1)
        with pytest.raises(ValueError, match="version"):
            vm.set_secrets([], version=True)
        with pytest.raises(ValueError, match="version"):
            vm.set_secrets([], version="3")  # type: ignore[arg-type]
    assert api["replace"].call_count == 2


async def test_async_replaces_them_whole_with_the_version_and_a_kept_revision(
    api: Any,
) -> None:
    async with mc.AsyncClient(api_key="com_test", base_url=BASE) as client:
        vm = await client.computers.get("vm-1")
        got = await vm.set_secrets(KEPT, version=3)
        assert sent(api["replace"]) == {"secrets": KEPT, "version": 3}
        assert got.version == 4
        assert got.secrets[0].revision_id == REV_A
        assert got.secrets[1].file == "kubeconfig"
        await vm.set_secrets([])
        assert sent(api["replace"]) == {"secrets": []}
        with pytest.raises(ValueError, match="version"):
            await vm.set_secrets([], version=-1)
    assert api["replace"].call_count == 2


@pytest.mark.parametrize("case", sorted(BAD))
def test_replace_refuses_before_sending(api: Any, case: str) -> None:
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        vm = client.computers.get("vm-1")
        with pytest.raises(ValueError):
            vm.set_secrets(BAD[case])
    assert not api["replace"].called


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_refuses_an_answer_that_is_not_a_whole_binding_list(api: Any, case: str) -> None:
    api["read"].mock(return_value=httpx.Response(200, json=MALFORMED[case]))
    api["replace"].mock(side_effect=None, return_value=httpx.Response(200, json=MALFORMED[case]))
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        vm = client.computers.get("vm-1")
        with pytest.raises(mc.MandalaError, match="GET computers/vm-1/secrets"):
            vm.secrets()
        with pytest.raises(mc.MandalaError, match="PUT computers/vm-1/secrets"):
            vm.set_secrets([])


@pytest.mark.parametrize("case", sorted(MALFORMED))
async def test_async_refuses_an_answer_that_is_not_a_whole_binding_list(
    api: Any, case: str
) -> None:
    api["read"].mock(return_value=httpx.Response(200, json=MALFORMED[case]))
    api["replace"].mock(side_effect=None, return_value=httpx.Response(200, json=MALFORMED[case]))
    async with mc.AsyncClient(api_key="com_test", base_url=BASE) as client:
        vm = await client.computers.get("vm-1")
        with pytest.raises(mc.MandalaError, match="GET computers/vm-1/secrets"):
            await vm.secrets()
        with pytest.raises(mc.MandalaError, match="PUT computers/vm-1/secrets"):
            await vm.set_secrets([])


def test_a_changed_list_is_a_conflict(api: Any) -> None:
    api["replace"].mock(
        side_effect=None,
        return_value=httpx.Response(
            409, json={"error": "the bindings changed since you read them"}
        ),
    )
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        vm = client.computers.get("vm-1")
        with pytest.raises(mc.ConflictError):
            vm.set_secrets(BOTH_KINDS, version=2)


def test_a_null_name_beside_the_other_reads_as_absent(api: Any) -> None:
    answer = {
        "secrets": [
            {"secret_id": A, "revision_id": REV_A, "env": None, "file": "kubeconfig"},
            {"secret_id": B, "revision_id": REV_B, "env": "API_TOKEN", "file": None},
        ],
        "version": 0,
    }
    api["read"].mock(return_value=httpx.Response(200, json=answer))
    with mc.Client(api_key="com_test", base_url=BASE) as client:
        got = client.computers.get("vm-1").secrets()
    assert got.version == 0
    assert got.secrets == [
        mc.SecretBinding(secret_id=A, revision_id=REV_A, file="kubeconfig"),
        mc.SecretBinding(secret_id=B, revision_id=REV_B, env="API_TOKEN"),
    ]
