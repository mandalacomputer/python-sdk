"""``client.workspaces`` (platform OPL-5057): the account's workspaces and their
members, and their create, rename and delete (OPL-5473), on both clients."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

import mandala_computer as mc

BASE = "https://api.test/api/v1"

WORKSPACE: dict[str, Any] = {
    "id": "wsp-0123456789ab",
    "name": "acme",
    "created_at": "2026-09-01T00:00:00.000Z",
}
MEMBER: dict[str, Any] = {
    "user_id": "usr-0123456789abcdef",
    "email": "dana@example.com",
    "name": None,
    "role": "member",
    "accepted_at": "2026-09-02T00:00:00.000Z",
    "suspended": False,
}
SCOPED = "An API key confined to a workspace cannot list members: the list is the whole account's."


def client() -> mc.Client:
    return mc.Client("com_test", base_url=BASE)


def mock_platform() -> None:
    respx.get(f"{BASE}/workspaces").mock(return_value=httpx.Response(200, json=[WORKSPACE]))
    respx.get(f"{BASE}/workspaces/{WORKSPACE['id']}").mock(
        return_value=httpx.Response(200, json=WORKSPACE)
    )
    respx.get(f"{BASE}/workspaces/{WORKSPACE['id']}/members").mock(
        return_value=httpx.Response(200, json=[MEMBER, {**MEMBER, "suspended": True}])
    )


def check(listed: list[mc.Workspace], got: mc.Workspace, members: list[mc.WorkspaceMember]) -> None:
    expected = mc.Workspace(
        id="wsp-0123456789ab", name="acme", created_at="2026-09-01T00:00:00.000Z"
    )
    assert listed == [expected]
    assert got == expected and got.raw == WORKSPACE
    assert members[0] == mc.WorkspaceMember(
        user_id="usr-0123456789abcdef",
        email="dana@example.com",
        name=None,
        role="member",
        accepted_at="2026-09-02T00:00:00.000Z",
        suspended=False,
    )
    assert members[1].suspended is True


@respx.mock
def test_list_get_and_members() -> None:
    mock_platform()
    c = client()
    check(
        c.workspaces.list(),
        c.workspaces.get(WORKSPACE["id"]),
        c.workspaces.members(WORKSPACE["id"]),
    )


@respx.mock
async def test_async_list_get_and_members() -> None:
    mock_platform()
    async with mc.AsyncClient("com_test", base_url=BASE) as c:
        check(
            await c.workspaces.list(),
            await c.workspaces.get(WORKSPACE["id"]),
            await c.workspaces.members(WORKSPACE["id"]),
        )


@respx.mock
def test_an_id_is_one_path_segment() -> None:
    route = respx.get(f"{BASE}/workspaces/a%2F..%2Fb").mock(
        return_value=httpx.Response(200, json=WORKSPACE)
    )
    client().workspaces.get("a/../b")
    assert route.called


@respx.mock
def test_an_id_it_cannot_see_is_not_found_and_members_refuse_a_scoped_key() -> None:
    respx.get(f"{BASE}/workspaces/wsp-other").mock(
        return_value=httpx.Response(404, json={"error": "Not found."})
    )
    respx.get(f"{BASE}/workspaces/{WORKSPACE['id']}/members").mock(
        return_value=httpx.Response(403, json={"error": SCOPED})
    )
    c = client()
    with pytest.raises(mc.NotFoundError):
        c.workspaces.get("wsp-other")
    with pytest.raises(mc.PermissionDeniedError, match="cannot list members"):
        c.workspaces.members(WORKSPACE["id"])


@respx.mock
async def test_async_members_refuse_a_scoped_key() -> None:
    respx.get(f"{BASE}/workspaces/{WORKSPACE['id']}/members").mock(
        return_value=httpx.Response(403, json={"error": SCOPED})
    )
    async with mc.AsyncClient("com_test", base_url=BASE) as c:
        with pytest.raises(mc.PermissionDeniedError):
            await c.workspaces.members(WORKSPACE["id"])


@pytest.mark.parametrize(
    ("route", "body", "where"),
    [
        ("workspaces", [{**WORKSPACE, "id": ""}], "workspace 0"),
        ("workspaces/wsp-1/members", [{**MEMBER, "suspended": "no"}], "workspace member 0"),
        ("workspaces/wsp-1/members", [{**MEMBER, "user_id": None}], "workspace member 0"),
        ("workspaces/wsp-1/members", [{**MEMBER, "name": 3}], "workspace member 0"),
    ],
)
@respx.mock
def test_a_row_it_cannot_read_is_refused(route: str, body: object, where: str) -> None:
    # Not reported as a member who can sign in, or a workspace with no id to
    # ask about again.
    respx.get(f"{BASE}/{route}").mock(return_value=httpx.Response(200, json=body))
    c = client()
    with pytest.raises(mc.MandalaError, match=where):
        if route == "workspaces":
            c.workspaces.list()
        else:
            c.workspaces.members("wsp-1")


# --- the writes (platform OPL-5473) -------------------------------------------

DELETED: dict[str, Any] = {"ok": True, "revoked_keys": 2}


def mock_writes() -> tuple[respx.Route, respx.Route, respx.Route]:
    made = respx.post(f"{BASE}/workspaces").mock(
        return_value=httpx.Response(201, json={**WORKSPACE, "name": "acme-2"})
    )
    renamed = respx.patch(f"{BASE}/workspaces/{WORKSPACE['id']}").mock(
        return_value=httpx.Response(200, json={**WORKSPACE, "name": "acme-3"})
    )
    gone = respx.delete(f"{BASE}/workspaces/{WORKSPACE['id']}").mock(
        return_value=httpx.Response(200, json=DELETED)
    )
    return made, renamed, gone


@respx.mock
def test_create_rename_delete_send_name_only_and_decode() -> None:
    import json

    made, renamed, gone = mock_writes()
    with client() as c:
        assert c.workspaces.create("acme-2").name == "acme-2"
        assert c.workspaces.rename(WORKSPACE["id"], "acme-3").name == "acme-3"
        result = c.workspaces.delete(WORKSPACE["id"])
    assert result == mc.WorkspaceDeleted(revoked_keys=2) and result.raw == DELETED
    assert json.loads(made.calls.last.request.content) == {"name": "acme-2"}
    assert json.loads(renamed.calls.last.request.content) == {"name": "acme-3"}
    assert gone.calls.last.request.content == b""


@respx.mock
async def test_the_async_client_creates_renames_and_deletes() -> None:
    import json

    made, renamed, gone = mock_writes()
    async with mc.AsyncClient("com_test", base_url=BASE) as c:
        assert (await c.workspaces.create("acme-2")).name == "acme-2"
        assert (await c.workspaces.rename(WORKSPACE["id"], "acme-3")).name == "acme-3"
        assert (await c.workspaces.delete(WORKSPACE["id"])).revoked_keys == 2
    assert json.loads(made.calls.last.request.content) == {"name": "acme-2"}
    assert json.loads(renamed.calls.last.request.content) == {"name": "acme-3"}
    assert gone.called


@respx.mock
@pytest.mark.parametrize("name", ["", "   ", None, 7])
def test_a_missing_or_empty_name_is_refused_before_any_request(name: Any) -> None:
    made, renamed, _ = mock_writes()
    with client() as c:
        with pytest.raises((ValueError, TypeError)):
            c.workspaces.create(name)
        with pytest.raises((ValueError, TypeError)):
            c.workspaces.rename(WORKSPACE["id"], name)
    assert not made.called and not renamed.called


@respx.mock
@pytest.mark.parametrize(
    "answer", [{"ok": True}, {"ok": True, "revoked_keys": -1}, {"revoked_keys": 0}, {}]
)
def test_a_delete_answer_that_cannot_count_its_keys_is_refused(answer: dict[str, Any]) -> None:
    respx.delete(f"{BASE}/workspaces/{WORKSPACE['id']}").mock(
        return_value=httpx.Response(200, json=answer)
    )
    with client() as c, pytest.raises(mc.MandalaError):
        c.workspaces.delete(WORKSPACE["id"])


@respx.mock
def test_a_scoped_key_is_permission_denied_and_a_foreign_id_not_found() -> None:
    sentence = "Workspaces cannot be created, renamed or deleted with a workspace-scoped API key."
    respx.post(f"{BASE}/workspaces").mock(
        return_value=httpx.Response(403, json={"error": sentence})
    )
    respx.patch(f"{BASE}/workspaces/wsp-ffffffffffff").mock(
        return_value=httpx.Response(404, json={"error": "workspace not found"})
    )
    with client() as c:
        with pytest.raises(mc.PermissionDeniedError):
            c.workspaces.create("x")
        with pytest.raises(mc.NotFoundError):
            c.workspaces.rename("wsp-ffffffffffff", "x")


# --- computers in a workspace (platform OPL-5543) ---------------------------
#
# An account-wide key names the workspace to create in with ``workspace_id``,
# and narrows the listing to one workspace, or to ``"unassigned"``.

COMPUTER: dict[str, Any] = {"id": "vm-1", "name": "dev", "status": "stopped", "os": "linux"}


def mock_computers() -> tuple[respx.Route, respx.Route]:
    made = respx.post(f"{BASE}/computers").mock(return_value=httpx.Response(201, json=COMPUTER))
    listed = respx.get(f"{BASE}/computers").mock(return_value=httpx.Response(200, json=[COMPUTER]))
    return made, listed


@respx.mock
def test_create_and_list_send_the_workspace() -> None:
    import json

    made, listed = mock_computers()
    with client() as c:
        c.computers.create(template="base", workspace_id=WORKSPACE["id"])
        assert json.loads(made.calls.last.request.content)["workspace_id"] == WORKSPACE["id"]
        c.computers.list(workspace_id=WORKSPACE["id"])
        assert dict(listed.calls.last.request.url.params) == {"workspace_id": WORKSPACE["id"]}
        c.computers.list(workspace_id="unassigned", state="live")
        assert dict(listed.calls.last.request.url.params) == {
            "workspace_id": "unassigned",
            "state": "live",
        }
        c.computers.create(template="base")
        assert "workspace_id" not in json.loads(made.calls.last.request.content)
        c.computers.list()
        assert dict(listed.calls.last.request.url.params) == {}


@respx.mock
def test_launch_sends_the_workspace_on_its_create() -> None:
    import json

    made, _ = mock_computers()
    with client() as c, pytest.raises(Exception):  # noqa: B017 - only the create is asserted
        c.computers.launch(template="base", workspace_id=WORKSPACE["id"], timeout=0.01)
    assert json.loads(made.calls.last.request.content)["workspace_id"] == WORKSPACE["id"]


@respx.mock
async def test_the_async_client_sends_the_workspace() -> None:
    import json

    made, listed = mock_computers()
    async with mc.AsyncClient("com_test", base_url=BASE) as c:
        await c.computers.create(template="base", workspace_id=WORKSPACE["id"])
        assert json.loads(made.calls.last.request.content)["workspace_id"] == WORKSPACE["id"]
        await c.computers.list(workspace_id="unassigned")
        assert dict(listed.calls.last.request.url.params) == {"workspace_id": "unassigned"}


@respx.mock
@pytest.mark.parametrize("bad", ["", " wsp-0123456789ab", 7])
def test_a_workspace_id_the_platform_would_refuse_is_refused_before_any_request(
    bad: Any,
) -> None:
    made, listed = mock_computers()
    # ValueError, not TypeError: a client without the parameter raises the
    # latter for the keyword itself, which must not pass for this check.
    with client() as c:
        with pytest.raises(ValueError, match="workspace_id must be"):
            c.computers.create(template="base", workspace_id=bad)
        with pytest.raises(ValueError, match="workspace_id must be"):
            c.computers.list(workspace_id=bad)
    assert not made.called and not listed.called
