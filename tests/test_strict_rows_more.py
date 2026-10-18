"""Rows and fields that used to decode to a quiet default now fail the read.

A row without its id, a flag that is neither true nor false, and a list entry
that is not an object were read as ``""``, ``False`` and "not there". Each is a
wrong answer a caller acts on — a delete sent to an empty id, a snapshot read as
not orphaned, a directory listing one entry short — so each is refused, as the
other listings already refuse them (OPL-6080).
"""

from __future__ import annotations

from typing import Any

import pytest

import mandala_computer as mc

SNAPSHOT: dict[str, Any] = {"id": "snap-1", "computer_id": "vm-1", "state": "durable"}


def test_a_snapshot_row_without_an_id_is_refused() -> None:
    with pytest.raises(mc.MandalaError, match="no usable id"):
        mc.Snapshot.from_api({**SNAPSHOT, "id": None})
    with pytest.raises(mc.MandalaError, match="no usable id"):
        mc.Snapshot.from_api({k: v for k, v in SNAPSHOT.items() if k != "id"})


@pytest.mark.parametrize("flag", ["incremental", "auto", "orphaned", "computer_unreachable"])
def test_a_garbled_snapshot_flag_is_refused(flag: str) -> None:
    with pytest.raises(mc.MandalaError, match=f"{flag} is not a boolean"):
        mc.Snapshot.from_api({**SNAPSHOT, flag: "maybe"})
    assert getattr(mc.Snapshot.from_api({**SNAPSHOT, flag: True}), flag) is True
    assert getattr(mc.Snapshot.from_api({**SNAPSHOT, flag: None}), flag) is False
    assert getattr(mc.Snapshot.from_api(SNAPSHOT), flag) is False


def test_a_build_row_without_an_id_is_refused() -> None:
    with pytest.raises(mc.MandalaError, match="build: a row has no usable id"):
        mc.TemplateBuild.from_api({"ref": "me/x@1", "status": "running"})
    assert mc.TemplateBuild.from_api({"id": "bld-1"}).id == "bld-1"


def test_a_size_without_an_id_or_with_a_garbled_allowed_is_refused() -> None:
    with pytest.raises(mc.MandalaError, match="size: a row has no usable id"):
        mc.Size.from_api({"label": "Small"})
    with pytest.raises(mc.MandalaError, match="allowed is not a boolean"):
        mc.Size.from_api({"id": "small", "allowed": "yes please"})
    assert mc.Size.from_api({"id": "small", "allowed": True}).allowed is True


def test_a_directory_entry_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(mc.MandalaError, match="entry 1 is not an object"):
        mc.GuestDirectory.from_api(
            {"path": "/", "entries": [{"name": "a", "type": "file"}, "b"], "truncated": False}
        )


def test_a_usage_row_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(mc.MandalaError, match="row 0 is not an object"):
        mc.UsageReport.from_api({"usage": {"computers": [7]}})
    with pytest.raises(mc.MandalaError, match="computers is not a list"):
        mc.UsageReport.from_api({"usage": {"computers": "vm-1"}})


WEBHOOK: dict[str, Any] = {"id": "wh-1", "url": "https://example.com/h", "enabled": True}


@pytest.mark.parametrize("computers", ["vm-1", [7], ["vm-1", ""], {"vm-1": True}])
def test_a_webhook_computer_filter_that_is_not_ids_is_refused(computers: object) -> None:
    """Read as empty, a filter means every computer."""
    with pytest.raises(mc.MandalaError, match="computers"):
        mc.Webhook.from_api({**WEBHOOK, "computers": computers})
    assert mc.Webhook.from_api({**WEBHOOK, "computers": ["vm-1"]}).computers == ["vm-1"]
    assert mc.Webhook.from_api(WEBHOOK).computers == []
