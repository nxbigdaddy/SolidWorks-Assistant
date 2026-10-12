from __future__ import annotations

from types import SimpleNamespace

import pytest

from sw_assistant.domain import ToolCall
from sw_assistant.solidworks.gateway import SolidWorksGateway
from sw_assistant.tools import build_tool_registry


class Dimension:
    FullName = "D1@Sketch1@Part1.SLDPRT"
    SystemValue = 0.025
    def GetType(self):
        return 0


class Display:
    def __init__(self, dimension=None):
        self.dimension = dimension or Dimension()
        self.next = None
    def GetDimension2(self, _index):
        return self.dimension
    def GetNext5(self):
        return self.next


class Feature:
    Name = "Front Plane"
    def __init__(self):
        self.next = None
        self.display = None
    def GetTypeName2(self):
        return "RefPlane"
    def GetErrorCode2(self, *args):
        return 0
    def GetFirstDisplayDimension(self):
        return self.display
    def GetNextDisplayDimension(self, previous):
        return previous.next
    def GetNextFeature(self):
        return self.next


class Document:
    def __init__(self):
        self._unknown = object()
        self._oleobj_ = SimpleNamespace(QueryInterface=lambda _iid: self._unknown)
        self.feature = Feature()
        self.SketchManager = SimpleNamespace(ActiveSketch=None)
        self.SelectionManager = SimpleNamespace(
            GetSelectedObjectCount2=lambda _: 2,
            GetSelectedObjectType3=lambda *_: 4,
            GetSelectedObject6=lambda index, _: SimpleNamespace(Name=f"plane{index}"),
        )
        self.mutations = []
        self.body = SimpleNamespace(GetMassProperties=lambda _: [0, 0, 0, 0.00001, 0.002])
    def GetTitle(self):
        return "Part1"
    def GetPathName(self):
        return ""
    def GetType(self):
        return 1
    def GetBodies2(self, kind, visible_only):
        assert kind == 0 and visible_only is False
        return [self.body]
    def FirstFeature(self):
        return self.feature
    def ClearSelection2(self, *args):
        self.mutations.append("selection")
    def ForceRebuild3(self, *args):
        self.mutations.append("rebuild")


def observe(document):
    gateway = SolidWorksGateway()
    gateway._sw = SimpleNamespace(ActiveDoc=document)
    _, data = gateway.execute(ToolCall("model.observe", {}))
    return data


def test_disconnected_and_no_document_observations_are_known_empty():
    gateway = SolidWorksGateway()
    _, data = gateway.execute(ToolCall("model.observe", {}))
    assert data["complete"] and not data["snapshot"]["connected"]
    data = observe(None)
    assert data["complete"] and data["snapshot"]["connected"]
    assert data["snapshot"]["document_title"] == ""


def test_observation_reads_physical_values_planes_dimensions_without_mutations():
    document = Document()
    document.feature.display = Display()
    data = observe(document)
    assert data["complete"] and data["issues"] == []
    assert data["document_identity"]
    assert data["snapshot"]["body_count"] == 1
    assert data["snapshot"]["volume_mm3"] == pytest.approx(10_000)
    assert data["snapshot"]["selection_count"] == 2
    assert data["dimensions"][0]["name"] == "D1@Sketch1"
    assert data["dimensions"][0]["value_mm"] == 25
    assert data["planes"] == [{"name": "Front Plane"}]
    assert document.mutations == []
    assert data["selection"][0]["name"] == "plane1"


def test_unknown_feature_error_code_is_not_reported_as_healthy():
    document = Document()
    def failed(*_args):
        raise RuntimeError("COM failed")
    document.feature.GetErrorCode2 = failed
    data = observe(document)
    assert not data["complete"]
    assert data["features"][0]["error_code"] is None


def test_known_feature_error_stays_visible_in_complete_observation():
    document = Document()
    document.feature.GetErrorCode2 = lambda *args: 17
    data = observe(document)
    assert data["complete"]
    assert data["features"][0]["error_code"] == 17


def test_body_measurement_failure_stays_unknown():
    document = Document()
    document.body.GetMassProperties = lambda _: None
    data = observe(document)
    assert not data["complete"]
    assert data["snapshot"]["body_count"] is None
    assert data["snapshot"]["volume_m3"] is None


@pytest.mark.parametrize("kind", [1, 2, -1])
def test_non_length_dimensions_are_never_converted_to_millimeters(kind):
    document = Document()
    dimension = Dimension()
    dimension.GetType = lambda: kind
    document.feature.display = Display(dimension)
    data = observe(document)
    assert data["dimensions"][0]["value_mm"] is None
    assert data["complete"] is (kind != -1)


def test_feature_and_dimension_cycles_cannot_hang_or_look_complete():
    document = Document()
    document.feature.next = document.feature
    data = observe(document)
    assert not data["complete"] and len(data["features"]) == 1
    document.feature.next = None
    display = Display()
    display.next = display
    document.feature.display = display
    data = observe(document)
    assert not data["complete"] and len(data["dimensions"]) == 1


def test_active_document_com_failure_is_not_a_known_empty_document():
    class FailedApplication:
        @property
        def ActiveDoc(self):
            raise RuntimeError("RPC disconnected")
    gateway = SolidWorksGateway()
    gateway._sw = FailedApplication()
    _, data = gateway.execute(ToolCall("model.observe", {}))
    assert not data["complete"] and data["issues"]


def test_document_identity_failure_cannot_produce_complete_write_evidence():
    document = Document()

    def fail(_iid):
        raise RuntimeError("synthetic IUnknown failure")

    document._oleobj_.QueryInterface = fail
    data = observe(document)
    assert not data["complete"] and data["document_identity"] is None
    assert data["snapshot"]["connected"] and data["snapshot"]["document_title"] == "Part1"
    assert any(issue.startswith("DocumentIdentity:") for issue in data["issues"])
    assert document.mutations == []


@pytest.mark.parametrize("registered", [False, True])
def test_full_identity_registry_rejects_unknown_document_but_reuses_known_token(registered):
    gateway, document = SolidWorksGateway(), Document()
    gateway._sw = SimpleNamespace(ActiveDoc=document)
    token = gateway._com_identity(document) if registered else None
    gateway._com_identities.extend((object(), f"synthetic-{index}") for index in
                                   range(4096 - len(gateway._com_identities)))
    _, data = gateway.execute(ToolCall("model.observe", {}))
    assert len(gateway._com_identities) == 4096
    assert data["complete"] is registered
    assert data["document_identity"] == token
    assert any(issue.startswith("DocumentIdentity:") for issue in data["issues"]) is not registered
    assert document.mutations == []


def test_observation_is_registered_without_write_permission():
    registry = build_tool_registry()
    call = registry.validate(ToolCall("model.observe", {}))
    assert call.tool == "model.observe"
    assert registry.write_scope("model.observe") == "none"


def test_unidentifiable_selected_face_makes_observation_incomplete():
    document = Document()
    document.SelectionManager.GetSelectedObject6 = lambda *_: object()
    data = observe(document)
    assert not data["complete"]
    assert any("Selection[" in issue for issue in data["issues"])


def test_selected_face_persistent_reference_is_read_without_selecting():
    document = Document()
    document.SelectionManager.GetSelectedObject6 = lambda *_: object()
    document.Extension = SimpleNamespace(GetPersistReference3=lambda _: b"stable-face")
    data = observe(document)
    assert data["complete"]
    assert data["selection"][0]["persist_reference"]
    assert document.mutations == []


def test_feature_dimensions_use_feature_enumerator_not_drawing_view_next():
    document = Document()
    first = Display()
    second_dimension = Dimension()
    second_dimension.FullName = "D2@Sketch1@Part1.SLDPRT"
    second_dimension.SystemValue = 0.05
    first.next = Display(second_dimension)
    first.GetNext5 = lambda: (_ for _ in ()).throw(AssertionError("view-only API"))
    document.feature.display = first
    data = observe(document)
    assert data["complete"]
    assert [item["value_mm"] for item in data["dimensions"]] == [25, 50]


def test_configuration_observation_is_opt_in_and_strictly_bound():
    document = Document()
    gateway = SolidWorksGateway()
    gateway._sw = SimpleNamespace(ActiveDoc=document)
    assert gateway._observe_model()["complete"]
    unknown = object()
    config = SimpleNamespace(Name="Default", _oleobj_=SimpleNamespace(QueryInterface=lambda _: unknown))
    document.ConfigurationManager = SimpleNamespace(ActiveConfiguration=config)
    call = build_tool_registry().validate(ToolCall("model.observe", {"include_configuration": True}))
    _, data = gateway.execute(call)
    assert data["complete"] and data["configuration"] == "Default"
    assert data["configuration_identity"] == gateway._com_identity(config)
    assert "configuration" not in gateway._observe_model()
    assert build_tool_registry().validate(ToolCall("model.observe", {})).arguments == {"include_configuration": False}


@pytest.mark.parametrize("mode", ["disconnected", "no_document", "missing_config", "unknown_config_identity",
                                  "name_changed", "identity_changed", "document_changed"])
def test_configuration_observation_fails_closed_on_unknown_or_mid_read_drift(mode):
    document = Document()
    gateway = SolidWorksGateway()
    gateway._sw = SimpleNamespace(ActiveDoc=document)
    unknown = object()
    config = SimpleNamespace(Name="Default", _oleobj_=SimpleNamespace(QueryInterface=lambda _: unknown))
    document.ConfigurationManager = SimpleNamespace(ActiveConfiguration=config)
    if mode == "disconnected":
        gateway._sw = None
    elif mode == "no_document":
        gateway._sw.ActiveDoc = None
    elif mode == "missing_config":
        document.ConfigurationManager.ActiveConfiguration = None
    elif mode == "unknown_config_identity":
        config._oleobj_.QueryInterface = lambda _: None
    else:
        original = document.GetBodies2

        def changed(*args):
            if mode == "name_changed":
                config.Name = "Other"
            elif mode == "identity_changed":
                config._oleobj_.QueryInterface = lambda _: object()
            else:
                gateway._sw.ActiveDoc = Document()
            return original(*args)

        document.GetBodies2 = changed
    data = gateway._observe_model(include_configuration=True)
    assert data["complete"] is False and data["issues"]
    assert data["configuration"] is None and data["configuration_identity"] is None
    if mode == "no_document":
        assert data["snapshot"]["connected"] is True
