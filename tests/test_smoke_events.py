"""Exercise the live smoke driver's decisions with an entirely local fake VM."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

import mandala_computer as mc


@pytest.fixture
def smoke(monkeypatch):
    monkeypatch.setenv("MANDALA_API_KEY", "com_test")
    spec = importlib.util.spec_from_file_location(
        "smoke_events_under_test", Path(__file__).parents[1] / "scripts" / "smoke_events.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStream:
    def __init__(self, vm, options):
        self.vm = vm
        self.options = options
        self.event_types = ["window.opened"]
        self.windows = []
        self.cursor = "earlier-cursor"
        self.watching = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.vm.connected = False

    def __iter__(self):
        # Like the SDK, subscription begins only when the iterator advances.
        self.vm.connected = True
        callback = self.options.get("on_connect")
        hello = SimpleNamespace(watching=[SimpleNamespace(path="/tmp/sdk-smoke", armed=False)])
        if callback:
            callback(hello)
        timeout = self.options.get("timeout")
        if timeout == 120:
            # Replaying the old cursor would select an unrelated historic window.
            if self.options.get("since"):
                yield SimpleNamespace(
                    type="window.opened", source="guest", window=self.vm.old_window
                )
            # Reconnect after the action but before its event is delivered.
            if callback:
                callback(hello)
            if self.vm.opens:
                yield SimpleNamespace(type="window.opened", source="guest", window=self.vm.window)
        elif timeout in (90, 45):
            yield SimpleNamespace(type="process.exited", pid=42, exit_code=7, lost=False)
        elif timeout == 180:
            yield SimpleNamespace(
                type="file.changed", armed=True, watch="/tmp/sdk-smoke", path=None, kind=None
            )
            yield SimpleNamespace(
                type="file.changed",
                path="/tmp/sdk-smoke/a.txt",
                kind="created",
                is_dir=False,
                watch="/tmp/sdk-smoke",
            )
        else:
            yield SimpleNamespace(type="computer.ready")


class FakeVM:
    id = "vm-cleanup-recovery"
    status = "running"

    def __init__(self, delete_error=None, work_error=None):
        self.delete_error = delete_error
        self.work_error = work_error
        self.deleted = False
        self.suspended = False
        self.connected = False
        self.opens = []
        self.window = SimpleNamespace(id=1, x=10, y=20, wm_class="browser", visible=True)
        self.old_window = SimpleNamespace(id=99, x=0, y=0, wm_class="historic", visible=True)

    def wait_until_running(self, **kwargs):
        if self.work_error:
            raise self.work_error

    def wait_for(self, event, **kwargs):
        if self.suspended:
            raise mc.MandalaError("computer suspended")
        if event == "file.changed":
            raise mc.MandalaError("nominated none")
        return SimpleNamespace(type="computer.ready", source="guest", synthesized=True)

    def events(self, **kwargs):
        return FakeStream(self, kwargs)

    def start_exec(self, command):
        return SimpleNamespace(pid=42)

    def exec(self, command):
        pass

    def open(self, url):
        self.opens.append((url, self.connected))

    def windows(self):
        return [self.window]

    def suspend(self):
        self.suspended = True

    def delete(self):
        self.deleted = True
        if self.delete_error:
            raise self.delete_error


def install_vm(smoke, monkeypatch, vm):
    client = SimpleNamespace(
        base_url="https://api.test", computers=SimpleNamespace(create=lambda **kwargs: vm)
    )
    monkeypatch.setattr(smoke.mc, "Client", lambda key: client)


def test_window_action_follows_subscription_and_is_not_repeated(smoke, monkeypatch):
    vm = FakeVM()
    install_vm(smoke, monkeypatch, vm)

    assert smoke.main() == 0
    assert vm.opens == [("https://example.com", True)]
    assert vm.deleted


@pytest.mark.parametrize("prior_failures", [0, 2])
def test_cleanup_failure_counts_and_identifies_computer(smoke, monkeypatch, capsys, prior_failures):
    vm = FakeVM(delete_error=mc.MandalaError("delete refused"))
    install_vm(smoke, monkeypatch, vm)
    smoke.FAILURES = prior_failures

    assert smoke.main() == 1
    assert smoke.FAILURES == prior_failures + 1
    output = capsys.readouterr().out
    assert "all checks passed" not in output
    assert any(vm.id in line and "delete refused" in line for line in output.splitlines())


def test_cleanup_failure_preserves_original_exception(smoke, monkeypatch, capsys):
    original = RuntimeError("readiness failed")
    vm = FakeVM(delete_error=mc.MandalaError("delete refused"), work_error=original)
    install_vm(smoke, monkeypatch, vm)

    with pytest.raises(RuntimeError) as caught:
        smoke.main()
    assert caught.value is original
    assert vm.deleted
    assert smoke.FAILURES == 1
    assert any(
        vm.id in line and "delete refused" in line for line in capsys.readouterr().out.splitlines()
    )
