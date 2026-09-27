"""``client.workspaces`` (platform OPL-5057): the account's workspaces and their
members, read only, on both clients."""

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
