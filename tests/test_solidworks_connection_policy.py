"""Host launch policy survives spawn; all COM modules below are local fakes."""
from __future__ import annotations

import queue
import sys
from types import SimpleNamespace

import pytest

from sw_assistant.domain import ToolError
from sw_assistant.solidworks.client import SolidWorksWorkerClient
from sw_assistant.solidworks.gateway import SolidWorksGateway
from sw_assistant.solidworks import worker as worker_module


class ComFailure(Exception):
    def __init__(self, code):
        self.hresult = code


def fake_com(monkeypatch, error=None):
    actions = []
    application = SimpleNamespace(Visible=False)

    def get_active(_name):
        actions.append("attach")
        if error:
            raise error
        return application

    def dispatch(_name):
        actions.append("launch")
        return application

    client = SimpleNamespace(GetActiveObject=get_active, Dispatch=dispatch)
    monkeypatch.setitem(sys.modules, "win32com", SimpleNamespace(client=client))
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(CoInitialize=lambda: None, CoUninitialize=lambda: None))
    return actions, application


@pytest.mark.parametrize("allow", [True, False])
def test_existing_application_connects_without_launch(monkeypatch, allow):
    actions, app = fake_com(monkeypatch)
    gateway = SolidWorksGateway()
    gateway._allow_start = allow
    assert gateway.connect() is app and actions == ["attach"]
    gateway.release(close_owned=False)


def test_only_confirmed_absent_application_with_host_permission_can_launch(monkeypatch):
    actions, app = fake_com(monkeypatch, ComFailure(-2147221021))
    gateway = SolidWorksGateway()
    assert gateway.connect() is app and actions == ["attach", "launch"]
    gateway.release(close_owned=False)


@pytest.mark.parametrize("error", [ComFailure(-2147221021), ComFailure(-2147024891), RuntimeError("unavailable")])
@pytest.mark.parametrize("allow", [True, False])
def test_no_launch_on_unknown_connection_or_attach_only_absence(monkeypatch, error, allow):
    if allow and getattr(error, "hresult", None) == -2147221021:
        return
    actions, _ = fake_com(monkeypatch, error)
    gateway = SolidWorksGateway()
    gateway._allow_start = allow
    with pytest.raises(ToolError):
        gateway.connect()
    assert actions == ["attach"] and gateway._sw is None


def test_attach_only_flag_is_host_config_not_tool_payload_and_survives_spawn():
    client = SolidWorksWorkerClient(allow_start_solidworks=False)
    created = []

    def process(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(start=lambda: None, is_alive=lambda: True)

    client._context = SimpleNamespace(Queue=queue.Queue, Process=process)
    client.start()
    assert created[0]["args"][-1] is False
    assert created[0]["target"] is worker_module.worker_main
    assert len(created[0]["args"]) == 4


def test_worker_sets_launch_mode_before_any_request(monkeypatch):
    modes = []
    gateway = SimpleNamespace(release=lambda **_: modes.append(gateway._allow_start))
    monkeypatch.setattr(worker_module, "SolidWorksGateway", lambda **_: gateway)
    requests = queue.Queue()
    requests.put({"kind": "shutdown"})
    worker_module.worker_main(requests, queue.Queue(), allow_start_solidworks=False)
    assert modes == [False]


@pytest.mark.parametrize("value", [None, "false", 0, 1])
def test_launch_policy_rejects_nonboolean_config(value):
    with pytest.raises(ValueError):
        SolidWorksWorkerClient(allow_start_solidworks=value)
    with pytest.raises(ValueError):
        worker_module.worker_main(queue.Queue(), queue.Queue(), allow_start_solidworks=value)
