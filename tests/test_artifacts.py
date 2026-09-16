"""Nominated artifacts are downloaded whole, bounded, and verified locally."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any

import httpx
import pytest
import respx
from tests.test_api_contract import BASE, computer, resolved
from tests.test_api_contract import client as client  # noqa: PLC0414
from tests.test_retained_results import EXECUTION_ID, STAMP

import mandala_computer as mc

ARTIFACT_ID = "art_" + "d" * 32


def artifact_manifest(content: bytes = b"abc", **extra: Any) -> dict[str, Any]:
    return {
        "artifact_id": ARTIFACT_ID,
        "kind": "artifact",
        "state": "ready",
        "computer_id": "vm-1",
        "workspace_id": None,
        "created_at": STAMP,
        "expires_at": "2026-09-17T12:00:00.123456789Z",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "execution_association": None,
        **extra,
    }


def download(content: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        content=content,
        headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(content)),
            "Content-Disposition": 'attachment; filename="ignored.html"',
        },
    )


@pytest.mark.parametrize(
    "path", ["/tmp/ log ", "/tmp/a\\b", "C:\\tmp\\你好.bin", "D:/tmp/a", "\\\\host\\share\\a"]
)
@respx.mock
async def test_publish_preserves_exact_nomination_without_guest_preflight(
    client: Any, path: str
) -> None:
    association = {"kind": "caller_selected", "execution_id": EXECUTION_ID, "verified_at": STAMP}
    route = respx.post(f"{BASE}/computers/vm-1/artifacts").mock(
        httpx.Response(
            201, json=artifact_manifest(execution_association=association, private_account="omit")
        )
    )
    got = await resolved(
        computer(client).publish_artifact(
            path,
            expected_size=3,
            expected_sha256=hashlib.sha256(b"abc").hexdigest(),
            execution_id=EXECUTION_ID,
            max_bytes=10,
            retention_seconds=600,
        )
    )
    assert got.execution_association.execution_id == EXECUTION_ID
    assert "private_account" not in dataclasses.asdict(got) and not hasattr(got, "account_id")
    assert not hasattr(got, "version")
    assert json.loads(route.calls[0].request.content) == {
        "path": path,
        "expected_size": 3,
        "expected_sha256": got.sha256,
        "execution_id": EXECUTION_ID,
        "max_bytes": 10,
        "retention_seconds": 600,
    }
    assert route.call_count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"path": "relative"},
        {"path": "/x\x00"},
        {"path": "/x\x7f"},
        {"path": "/\ud800"},
        {"path": "/" + "é" * 2048},
        {"expected_size": True},
        {"expected_size": -1},
        {"expected_sha256": "A" * 64},
        {"max_bytes": 2},
        {"max_bytes": 67108865},
        {"execution_id": "../exec"},
        {"retention_seconds": 0},
    ],
)
@respx.mock
async def test_invalid_nomination_is_refused_before_any_io(
    client: Any, overrides: dict[str, Any]
) -> None:
    args = {
        "path": "/tmp/a",
        "expected_size": 3,
        "expected_sha256": hashlib.sha256(b"abc").hexdigest(),
        **overrides,
    }
    with pytest.raises((TypeError, ValueError)):
        await resolved(computer(client).publish_artifact(**args))
    assert not respx.calls


@pytest.mark.parametrize("content", [b"", bytes(range(256)), b"\xef\xbb\xbf\xff\xe2\x82\xac\x00"])
@respx.mock
async def test_whole_download_verifies_exact_bytes_and_ignores_locations(
    client: Any, content: bytes
) -> None:
    meta = respx.get(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}").mock(
        httpx.Response(
            200, json=artifact_manifest(content), headers={"Location": "https://untrusted.test/x"}
        )
    )
    binary = respx.get(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}/download").mock(
        download(content)
    )
    assert await resolved(computer(client).download_artifact(ARTIFACT_ID)) == content
    assert meta.call_count == binary.call_count == 1
    req = binary.calls[0].request
    assert "range" not in req.headers and not req.url.query
    assert req.headers["accept-encoding"] == "identity"


@respx.mock
async def test_download_cap_refuses_before_transfer(client: Any) -> None:
    meta = respx.get(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}").mock(
        httpx.Response(200, json=artifact_manifest())
    )
    with pytest.raises(mc.MandalaError, match="download max_bytes"):
        await resolved(computer(client).download_artifact(ARTIFACT_ID, max_bytes=2))
    assert meta.call_count == len(respx.calls) == 1


@pytest.mark.parametrize("failure", ["hash", "short", "long", "expired", "partial"])
@respx.mock
async def test_no_partial_success_or_guest_fallback(client: Any, failure: str) -> None:
    respx.get(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}").mock(
        httpx.Response(200, json=artifact_manifest())
    )
    response = {
        "hash": download(b"abd"),
        "short": download(b"ab"),
        "long": download(b"abcd"),
        "expired": httpx.Response(404),
        "partial": httpx.Response(206, content=b"abc"),
    }[failure]
    binary = respx.get(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}/download").mock(response)
    with pytest.raises(mc.MandalaError):
        await resolved(computer(client).download_artifact(ARTIFACT_ID))
    assert binary.call_count == 1 and len(respx.calls) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_id", "art_" + "a" * 32),
        ("computer_id", "vm-2"),
        ("size", True),
        ("sha256", "bad"),
        ("state", "staging"),
        ("execution_association", {}),
        ("created_at", "2026-02-30T12:00:00Z"),
        ("expires_at", STAMP),
    ],
)
@respx.mock
async def test_invalid_metadata_never_starts_binary_io(client: Any, field: str, value: Any) -> None:
    meta = respx.get(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}").mock(
        httpx.Response(200, json=artifact_manifest(**{field: value}))
    )
    with pytest.raises(mc.MandalaError):
        await resolved(computer(client).download_artifact(ARTIFACT_ID))
    assert meta.call_count == len(respx.calls) == 1


@respx.mock
async def test_artifact_read_and_both_delete_outcomes_are_explicit(client: Any) -> None:
    respx.get(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}").mock(
        httpx.Response(200, json=artifact_manifest())
    )
    route = respx.delete(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}").mock(
        side_effect=[
            httpx.Response(204),
            httpx.Response(404, json={"error": "artifact unavailable"}),
        ]
    )
    c = computer(client)
    assert (await resolved(c.artifact(ARTIFACT_ID))).size == 3
    assert await resolved(c.delete_artifact(ARTIFACT_ID)) is None
    with pytest.raises(mc.NotFoundError):
        await resolved(c.delete_artifact(ARTIFACT_ID))
    assert route.call_count == 2


@respx.mock
async def test_distinct_versions_are_not_cached_or_replayed(client: Any) -> None:
    other = "art_" + "e" * 32
    for aid, content in [(ARTIFACT_ID, b"abc"), (other, b"def")]:
        respx.get(f"{BASE}/computers/vm-1/artifacts/{aid}").mock(
            httpx.Response(200, json=artifact_manifest(content, artifact_id=aid))
        )
        respx.get(f"{BASE}/computers/vm-1/artifacts/{aid}/download").mock(download(content))
    c = computer(client)
    assert await resolved(c.download_artifact(ARTIFACT_ID)) == b"abc"
    assert await resolved(c.download_artifact(other)) == b"def"
    assert len(respx.calls) == 4


@pytest.mark.parametrize("digits", range(10))
@respx.mock
async def test_artifact_timestamps_keep_all_fraction_lengths(client: Any, digits: int) -> None:
    stamp = "2024-02-29T12:00:00" + ("." + "123456789"[:digits] if digits else "") + "Z"
    meta = artifact_manifest(
        created_at=stamp,
        expires_at="2024-03-01T12:00:00Z",
        execution_association={
            "kind": "caller_selected",
            "execution_id": EXECUTION_ID,
            "verified_at": stamp,
        },
    )
    respx.get(f"{BASE}/computers/vm-1/artifacts/{ARTIFACT_ID}").mock(httpx.Response(200, json=meta))
    got = await resolved(computer(client).artifact(ARTIFACT_ID))
    assert got.created_at == got.execution_association.verified_at == stamp


@pytest.mark.parametrize(
    "overrides",
    [
        {"artifact_id": "invalid"},
        {"size": 2},
        {"sha256": "0" * 64},
        {
            "execution_association": {
                "kind": "caller_selected",
                "execution_id": EXECUTION_ID,
                "verified_at": STAMP,
            }
        },
    ],
)
@respx.mock
async def test_accepted_but_unconfirmed_publication_is_never_repeated(
    client: Any, overrides: dict
) -> None:
    route = respx.post(f"{BASE}/computers/vm-1/artifacts").mock(
        httpx.Response(201, json=artifact_manifest(**overrides))
    )
    with pytest.raises(mc.MandalaError):
        await resolved(
            computer(client).publish_artifact(
                "/tmp/a", expected_size=3, expected_sha256=artifact_manifest()["sha256"]
            )
        )
    assert route.call_count == 1
    assert set(json.loads(route.calls[0].request.content)) == {
        "path",
        "expected_size",
        "expected_sha256",
    }


@pytest.mark.parametrize("value", [True, 0, -1, 67108865, 1.5])
@respx.mock
async def test_invalid_download_caps_dispatch_nothing(client: Any, value: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        await resolved(computer(client).download_artifact(ARTIFACT_ID, max_bytes=value))
    assert not respx.calls
