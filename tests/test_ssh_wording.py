"""The SSH docstrings say what the platform does (OPL-5640).

Deleting a key closes the sessions opened with it once each computer receives
the new key list; switching SSH on admits only some members' keys; and the
access read asks the computer's host. These docstrings are what ``help()``
shows, so they are pinned here.
"""

from __future__ import annotations

import inspect

from mandala_computer import AsyncComputer, Computer, SshAccess
from mandala_computer._async_resources import AsyncSshKeys
from mandala_computer._resources import SshKeys


def _doc(obj: object) -> str:
    return " ".join((inspect.getdoc(obj) or "").split())


def test_remove_says_sessions_opened_with_the_key_are_closed() -> None:
    for remove in (SshKeys.remove, AsyncSshKeys.remove):
        doc = _doc(remove)
        assert "until it disconnects" not in doc
        assert (
            "a session opened with it is closed once each computer receives the new key list" in doc
        )


def test_set_ssh_access_names_the_keys_that_do_not_log_in() -> None:
    for method in (Computer.set_ssh_access, AsyncComputer.set_ssh_access):
        doc = _doc(method)
        assert "every owner and member" not in doc
        assert (
            "except keys bound to another account and the keys of a member whose seat is suspended"
            in doc
        )
        assert "Viewers' keys never log in." in doc


def test_ssh_access_says_the_read_asks_the_host() -> None:
    for method in (Computer.ssh_access, AsyncComputer.ssh_access):
        doc = _doc(method)
        assert "without waiting on the computer's host" not in doc
        assert "asks the computer's host whether its SSH server is running" in doc
        assert "an unreachable host does not fail the read" in doc


def test_key_count_says_which_keys_are_counted() -> None:
    source = inspect.getsource(SshAccess)
    flat = " ".join(line.strip().lstrip("#:").strip() for line in source.splitlines())
    assert "every key of every owner and member" not in flat
    assert "less keys bound to another account and those of seat-suspended members" in flat
