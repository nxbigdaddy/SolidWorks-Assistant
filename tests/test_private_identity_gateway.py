"""Private identity probe uses only synthetic COM references; no SolidWorks."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sw_assistant.domain import PlanningError, ToolCall, ToolError
from sw_assistant.solidworks.gateway import SolidWorksGateway
from sw_assistant.tools import build_tool_registry


class Header:
    def __init__(self, title="UnsavedPart", path="", code=1):
        self._unknown = object()
        self._oleobj_ = SimpleNamespace(QueryInterface=lambda _iid: self._unknown)
        self.title, self.path, self.code = title, path, code
        self.reads = []

    def GetTitle(self):
        self.reads.append("title")
        return self.title

    def GetPathName(self):
        self.reads.append("path")
        return self.path

    def GetType(self):
        self.reads.append("type")
        return self.code

    def __getattr__(self, name):
        raise AssertionError(f"Identity probe must not read {name}")


def gateway(document):
    value = SolidWorksGateway()
    value._sw = SimpleNamespace(ActiveDoc=document)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Identity probe must not connect, execute, or inspect geometry")

    value.connect = value.snapshot = value._observe_model = value.execute = forbidden
    return value


def test_private_probe_distinguishes_disconnected_and_known_no_document():
    value = gateway(None)
    expected = {"connected": True, "document_identity": None, "document_title": "",
                "document_path": "", "document_type": "none"}
    assert value.probe_document_identity() == expected
    value._sw = None
    expected["connected"] = False
    assert value.probe_document_identity() == expected


@pytest.mark.parametrize(("code", "kind"), [(1, "part"), (2, "assembly"), (3, "drawing")])
def test_private_probe_reads_only_strict_header_and_reuses_host_identity(code, kind):
    document = Header(path=r"C:\synthetic\example.sldprt", code=code)
    value = gateway(document)
    result = value.probe_document_identity()
    assert result == {"connected": True, "document_identity": value._com_identity(document),
                      "document_title": "UnsavedPart", "document_path": document.path,
                      "document_type": kind}
    assert value.probe_document_identity() == result
    assert document.reads == ["title", "path", "type"] * 2


@pytest.mark.parametrize(("field", "invalid"), [
    ("title", None), ("title", ""), ("title", "a\0b"), ("title", "a" * 1025),
    ("path", None), ("path", False), ("path", "a\0b"), ("path", "a" * 32769),
    ("code", None), ("code", 0), ("code", 4), ("code", True), ("code", 1.0), ("code", "1"),
], ids=["null-title", "empty-title", "nul-title", "oversize-title", "null-path", "bool-path",
        "nul-path", "oversize-path", "null-type", "zero-type", "unknown-type", "bool-type",
        "float-type", "str-type"])
def test_private_probe_never_substitutes_defaults_for_unknown_header(field, invalid):
    document = Header()
    setattr(document, field, invalid)
    with pytest.raises(ToolError):
        gateway(document).probe_document_identity()


@pytest.mark.parametrize("member", ["GetTitle", "GetPathName", "GetType"])
def test_private_probe_fails_closed_on_com_header_exception(member):
    document = Header()

    def unavailable():
        raise RuntimeError("synthetic COM read unavailable")

    setattr(document, member, unavailable)
    with pytest.raises(ToolError, match="synthetic COM read unavailable"):
        gateway(document).probe_document_identity()


def test_private_probe_does_not_treat_failed_active_doc_read_as_no_document():
    class UnreadableApp:
        @property
        def ActiveDoc(self):
            raise RuntimeError("active document unavailable")

    value = gateway(None)
    value._sw = UnreadableApp()
    with pytest.raises(ToolError, match="active document unavailable"):
        value.probe_document_identity()


def test_private_probe_fails_closed_when_identity_is_unreadable():
    document = Header()
    document._oleobj_.QueryInterface = lambda _iid: None
    with pytest.raises(ToolError):
        gateway(document).probe_document_identity()
    assert document.reads == []


def test_private_probe_rejects_active_document_switch_during_header_reads():
    document, replacement = Header(), Header()
    value = gateway(document)

    def switch():
        value._sw.ActiveDoc = replacement
        return document.path

    document.GetPathName = switch
    with pytest.raises(ToolError, match="changed during identity probe"):
        value.probe_document_identity()


def test_private_probe_is_not_a_model_tool_or_execute_method():
    for name in ("identity", "probe_document_identity", "document.identity", "model.identity"):
        with pytest.raises(PlanningError):
            build_tool_registry().validate(ToolCall(name, {}))
        with pytest.raises(ToolError):
            SolidWorksGateway().execute(ToolCall(name, {}))
