"""Finite artifact metadata and local verification of retained bytes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from . import _api
from ._results import digest, identity, integer, invalid, record, scope, timestamp

__all__ = ["Artifact", "ArtifactAssociation"]


@dataclass(frozen=True)
class ArtifactAssociation:
    kind: Literal["caller_selected"]
    execution_id: str
    verified_at: str


@dataclass(frozen=True)
class Artifact:
    """One nominated immutable object; association does not prove authorship."""

    artifact_id: str
    kind: Literal["artifact"]
    state: Literal["ready"]
    computer_id: str
    workspace_id: str | None
    created_at: str
    expires_at: str
    size: int
    sha256: str
    execution_association: ArtifactAssociation | None


def decode_artifact(
    data: Mapping[str, Any], *, computer_id: str, artifact_id: str | None = None
) -> Artifact:
    aid = identity(data.get("artifact_id"), artifact_id, "artifact_id")
    if data.get("kind") != "artifact" or data.get("state") != "ready":
        raise invalid("artifact kind or state")
    computer = scope(data.get("computer_id"), "computer_id")
    if computer != computer_id:
        raise invalid("computer_id")
    workspace = (
        None
        if data.get("workspace_id") is None and "workspace_id" in data
        else scope(data.get("workspace_id"), "workspace_id")
    )
    created, created_ns = timestamp(data.get("created_at"), "created_at")
    expires, expires_ns = timestamp(data.get("expires_at"), "expires_at")
    if not 0 < expires_ns - created_ns <= _api.RESULT_RETENTION_MAX * 1_000_000_000:
        raise invalid("artifact chronology")
    association = None
    if "execution_association" not in data:
        raise invalid("execution_association")
    if data["execution_association"] is not None:
        item = record(data["execution_association"], "execution_association")
        if item.get("kind") != "caller_selected":
            raise invalid("association kind")
        execution = identity(item.get("execution_id"), None, "execution_id")
        verified, verified_ns = timestamp(item.get("verified_at"), "verified_at")
        if verified_ns > created_ns:
            raise invalid("association chronology")
        association = ArtifactAssociation("caller_selected", execution, verified)
    return Artifact(
        aid,
        "artifact",
        "ready",
        computer,
        workspace,
        created,
        expires,
        integer(data.get("size"), "size", high=_api.ARTIFACT_MAX_BYTES),
        digest(data.get("sha256")),
        association,
    )


def verify_nomination(artifact: Artifact, body: Mapping[str, Any]) -> Artifact:
    association = artifact.execution_association
    if (
        artifact.size != body["expected_size"]
        or artifact.sha256 != body["expected_sha256"]
        or (association.execution_id if association else None) != body.get("execution_id")
    ):
        raise invalid("artifact nomination")
    return artifact


def verify_download(artifact: Artifact, data: bytes) -> bytes:
    if len(data) != artifact.size or hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise invalid("artifact size or SHA-256")
    return data
