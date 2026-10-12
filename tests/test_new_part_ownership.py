from types import SimpleNamespace

import pytest

from sw_assistant.domain import ToolError
from sw_assistant.solidworks.gateway import SolidWorksGateway


@pytest.mark.parametrize("returned", [None, "existing_active", "existing_inactive"])
def test_failed_create_never_adopts_user_document(monkeypatch, returned):
    active, inactive = object(), object()
    app = SimpleNamespace(ActiveDoc=active, GetDocuments=lambda: [active, inactive],
                          NewPart=lambda: {None: None, "existing_active": active,
                                           "existing_inactive": inactive}[returned])
    gateway = SolidWorksGateway()
    monkeypatch.setattr(gateway, "connect", lambda: app)
    monkeypatch.setattr(gateway, "_part_template", lambda app: "")
    monkeypatch.setattr(gateway, "_com_identity", lambda value: str(id(value)))
    registered = []
    monkeypatch.setattr(gateway, "register_candidate_document", registered.append)
    with pytest.raises(ToolError, match="未创建"):
        gateway._new_part()
    assert registered == []


def test_only_native_new_document_return_is_registered(monkeypatch):
    previous = object()
    created = SimpleNamespace(GetType=lambda: 1, GetTitle=lambda: "SyntheticNewPart")
    app = SimpleNamespace(ActiveDoc=previous, GetDocuments=lambda: [previous], NewPart=lambda: created)
    gateway = SolidWorksGateway()
    monkeypatch.setattr(gateway, "connect", lambda: app)
    monkeypatch.setattr(gateway, "_part_template", lambda app: "")
    monkeypatch.setattr(gateway, "_com_identity", lambda value: str(id(value)))
    registered = []
    monkeypatch.setattr(gateway, "register_candidate_document", registered.append)
    assert gateway._new_part()[1]["title"] == "SyntheticNewPart"
    assert registered == [created]
