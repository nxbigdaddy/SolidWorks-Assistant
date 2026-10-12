from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from sw_assistant.agent.diagnostics import inspect_recent_failures
from sw_assistant.domain import PlanningError, ToolCall
from sw_assistant.solidworks.gateway import SolidWorksGateway
from sw_assistant.tools import (
    INVESTIGATION_TOOLS,
    MAX_INVESTIGATION_OUTPUT_BYTES,
    bound_investigation_result,
    build_tool_registry,
)


def forbidden(*_args, **_kwargs):
    raise AssertionError("an investigator must not mutate or start anything")


class Face:
    def __init__(self, identifier="face", planar=True):
        self.identifier = identifier
        self.Normal = [0, 0, 1]
        self.planar = planar
        self.Select4 = forbidden
    def GetSurface(self):
        return SimpleNamespace(IsPlane=lambda: self.planar)
    def GetBox(self):
        return [-0.01, -0.01, 0.02, 0.01, 0.01, 0.02]
    def GetArea(self):
        return 0.0004


class Feature:
    def __init__(self, name="Front Plane", kind="RefPlane"):
        self.Name, self.kind = name, kind
        self.next = None
        self.display = None
        self.faces = []
        self.GetDefinition = forbidden
        self.Select2 = forbidden
        self._unknown = object()
        self._oleobj_ = SimpleNamespace(QueryInterface=lambda _: self._unknown)
    def GetTypeName2(self):
        return self.kind
    def GetNextFeature(self):
        return self.next
    def GetErrorCode2(self, *_args):
        return 0
    def GetFirstDisplayDimension(self):
        return self.display
    def GetNextDisplayDimension(self, display):
        return display.next
    def GetFaces(self):
        return self.faces


def display(name, kind, value, following=None):
    dimension = SimpleNamespace(FullName=name + "@TestPart.SLDPRT", SystemValue=value,
                                GetType=lambda: kind)
    return SimpleNamespace(GetDimension2=lambda _: dimension, next=following)


class Document:
    def __init__(self):
        self.feature = Feature()
        self.faces = [Face()]
        self.SketchManager = SimpleNamespace(ActiveSketch=None, InsertSketch=forbidden,
                                             CreateCircleByRadius=forbidden)
        self.FeatureManager = SimpleNamespace(FeatureExtrusion3=forbidden, FeatureRevolve2=forbidden)
        self.Extension = SimpleNamespace(
            GetPersistReference3=lambda obj: str(getattr(obj, "Name", getattr(obj, "identifier", "entity"))).encode(),
            SelectByID2=forbidden,
        )
        self.ClearSelection2 = forbidden
        self.ForceRebuild3 = forbidden
    def GetTitle(self):
        return "TestPart"
    def GetType(self):
        return 1
    def FirstFeature(self):
        if self.SketchManager is not None and self.SketchManager.ActiveSketch is not None:
            return self.SketchManager.ActiveSketch._feature
        return self.feature
    def FeatureByName(self, name):
        return self.feature if self.feature.Name == name else None
    def GetBodies2(self, kind, visible_only):
        assert kind == 0 and visible_only is False
        return [SimpleNamespace(GetFaces=lambda: self.faces)]


def investigate(name, arguments=None, document=None):
    gateway = SolidWorksGateway()
    gateway.connect = forbidden
    gateway._sw = SimpleNamespace(ActiveDoc=document, RevisionNumber=lambda: "33.5.0", NewDocument=forbidden)
    call = build_tool_registry().validate(ToolCall(name, arguments or {}))
    return gateway.execute(call)[1]


def test_capabilities_work_offline_and_never_claim_operation_success():
    gateway = SolidWorksGateway()
    gateway.connect = forbidden
    data = gateway.execute(ToolCall("session.capabilities", {}))[1]
    assert data["complete"] and not data["connected"]
    assert data["solidworks_revision"] is None
    assert not data["operation_success_verified"]
    assert all(item["status"] == "unknown" for item in data["api_members"])
    assert {item["name"] for item in data["implemented_tools"]} >= INVESTIGATION_TOOLS


def test_capabilities_inspect_method_presence_without_calling_model_operations():
    data = investigate("session.capabilities", document=Document())
    assert data["complete"]
    assert data["solidworks_revision"] == "33.5.0"
    assert {item["status"] for item in data["api_members"]} == {"present"}
    assert not data["operation_success_verified"]


def test_reference_inquiry_returns_plane_and_geometric_face_evidence():
    data = investigate("model.references", document=Document())
    assert data["complete"] and not data["truncated"]
    plane, face = data["references"]
    assert plane["canonical"] == "front" and plane["persist_reference"]
    assert face["kind"] == "planar_face" and face["area_m2"] == 0.0004
    assert face["normal"] == [0, 0, 1]
    assert face["center_m"] == [0, 0, 0.02]
    assert face["exact_reference_available"]


def test_reference_limit_and_feature_cycle_are_reported_as_partial():
    document = Document()
    data = investigate("model.references", {"limit": 1}, document)
    assert not data["complete"] and data["truncated"]
    assert len(data["references"]) == 1
    document.feature.next = document.feature
    data = investigate("model.references", document=document)
    assert not data["complete"]
    assert any("cyclic" in issue for issue in data["issues"])


def test_unknown_reference_or_geometry_cannot_be_reported_as_usable():
    document = Document()
    document.Extension.GetPersistReference3 = lambda _: None
    document.faces[0].GetArea = lambda: float("nan")
    data = investigate("model.references", document=document)
    assert not data["complete"]
    assert len(data["references"]) == 1
    assert data["references"][0]["persist_reference"] is None
    assert not data["references"][0]["exact_reference_available"]


def test_nonplanar_faces_are_known_exclusions_not_failed_reads():
    document = Document()
    document.faces = [Face(planar=False)]
    data = investigate("model.references", document=document)
    assert data["complete"]
    assert [item["kind"] for item in data["references"]] == ["reference_plane"]


def test_feature_inspection_uses_exact_name_and_reads_dimensions_without_editing():
    document = Document()
    feature = document.feature = Feature("Boss1", "Boss")
    feature.display = display("D1@Boss1", 0, 0.02, display("D2@Boss1", 1, 1.5))
    feature.faces = document.faces
    data = investigate("feature.inspect", {"name": "Boss1"}, document)
    assert data["complete"] and data["found"]
    assert data["feature"]["error_code"] == 0
    assert [dim["value_mm"] for dim in data["feature"]["dimensions"]] == [20, None]
    assert data["feature"]["face_count"] == 1
    missing = investigate("feature.inspect", {"name": "boss1"}, document)
    assert missing["complete"] and not missing["found"]


def test_feature_dimension_cycle_and_unknown_error_are_not_success_evidence():
    document = Document()
    feature = document.feature
    feature.display = display("D1@Plane", 0, 0.001)
    feature.display.next = feature.display
    feature.GetErrorCode2 = lambda *_: None
    data = investigate("feature.inspect", {"name": feature.Name}, document)
    assert data["found"] and not data["complete"]
    assert data["feature"]["error_code"] is None
    assert len(data["feature"]["dimensions"]) == 1


@pytest.mark.parametrize("requested_limit, expected", [(None, 30), (3, 3), (100, 100)])
def test_large_feature_respects_default_and_smaller_query_limits(requested_limit, expected):
    document = Document()
    feature = document.feature = Feature("LargeBoss", "Boss")
    for index in reversed(range(101)):
        feature.display = display(f"D{index}@LargeBoss", 0, 0.001, feature.display)
    feature.faces = [Face(str(index)) for index in range(101)]
    arguments = {"name": feature.Name}
    if requested_limit is not None:
        arguments["limit"] = requested_limit
    data = investigate("feature.inspect", arguments, document)
    assert data["limit"] == expected and not data["complete"] and data["truncated"]
    details = data["feature"]
    assert len(details["dimensions"]) == len(details["planar_faces"]) == expected
    assert details["face_count"] == 101 and details["faces_inspected"] == expected
    assert details["dimensions_truncated"] and details["faces_truncated"]
    assert len(json.dumps(data, ensure_ascii=False).encode("utf-8")) <= MAX_INVESTIGATION_OUTPUT_BYTES


def test_exact_feature_limit_does_not_falsely_report_truncation():
    document = Document()
    feature = document.feature
    feature.display = display("D1@Plane", 0, 0.001, display("D2@Plane", 0, 0.002))
    feature.faces = [Face("first"), Face("second")]
    data = investigate("feature.inspect", {"name": feature.Name, "limit": 2}, document)
    assert data["complete"] and not data["truncated"]
    assert not data["feature"]["dimensions_truncated"] and not data["feature"]["faces_truncated"]
    assert len(data["feature"]["dimensions"]) == len(data["feature"]["planar_faces"]) == 2


def test_large_feature_byte_limit_is_explicit_and_can_be_requeried_smaller():
    document = Document()
    feature = document.feature
    feature.faces = [Face(str(index)) for index in range(100)]
    document.Extension.GetPersistReference3 = lambda _: b"r" * 1024
    data = investigate("feature.inspect", {"name": feature.Name, "limit": 100}, document)
    assert not data["complete"] and data["truncated"] and data["output_error"] == "output_limit"
    assert data["retry_hint"]["limit"] < 100
    assert len(json.dumps(data).encode()) <= MAX_INVESTIGATION_OUTPUT_BYTES
    smaller = investigate("feature.inspect", {"name": feature.Name, "limit": 10}, document)
    assert "output_error" not in smaller and not smaller["complete"] and smaller["truncated"]
    assert len(smaller["feature"]["planar_faces"]) == 10
    assert smaller["feature"]["face_count"] == 100


def sketch(*, check_status=0, closed=1):
    segment = SimpleNamespace(
        GetType=lambda: 0, ConstructionGeometry=False,
        GetStartPoint2=lambda: SimpleNamespace(X=0, Y=0, Z=0),
        GetEndPoint2=lambda: SimpleNamespace(X=0.01, Y=0, Z=0),
    )
    feature = Feature("Sketch1", "ProfileFeature")
    unknown = object()
    value = SimpleNamespace(Name="Sketch1", _feature=feature,
                            ModelToSketchTransform=SimpleNamespace(ArrayData=[1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]),
                            _oleobj_=SimpleNamespace(QueryInterface=lambda _: unknown),
                            GetSketchSegments=lambda: [segment],
                            CheckFeatureUse=lambda *_: (check_status, 0, closed))
    feature.GetSpecificFeature2 = lambda: value
    return value


def test_sketch_inquiry_reports_geometry_and_known_contour_failures():
    document = Document()
    document.SketchManager.ActiveSketch = sketch(check_status=6, closed=0)
    data = investigate("sketch.inspect", document=document)
    assert data["complete"] and data["active"]
    assert data["sketch"]["entity_count"] == 1
    assert data["sketch"]["segments"][0]["end_sketch_m"] == [0.01, 0, 0]
    assert data["sketch"]["transform_complete"]
    assert data["sketch"]["coordinate_space"] == "sketch_local"
    assert len(data["sketch"]["model_to_sketch_transform"]) == 16
    assert all(item["status"] == 6 and not item["usable"] for item in data["sketch"]["profile_checks"])


def test_sketch_unknown_status_and_missing_manager_remain_unknown():
    document = Document()
    document.SketchManager.ActiveSketch = sketch()
    document.SketchManager.ActiveSketch.CheckFeatureUse = lambda *_: None
    data = investigate("sketch.inspect", document=document)
    assert not data["complete"]
    assert data["sketch"]["profile_checks"][0]["usable"] is None
    document.SketchManager = None
    data = investigate("sketch.inspect", document=document)
    assert not data["complete"] and data["active"] is None


@pytest.mark.parametrize("transform", [None, [1] * 15, [float("nan")] * 16])
def test_sketch_transform_is_actual_readonly_data_and_unreadable_is_incomplete(transform):
    document = Document()
    document.SketchManager.ActiveSketch = sketch()
    document.SketchManager.ActiveSketch.ModelToSketchTransform = SimpleNamespace(ArrayData=transform)
    data = investigate("sketch.inspect", document=document)
    assert not data["complete"]
    assert data["sketch"]["transform_complete"] is False
    assert data["sketch"]["model_to_sketch_transform"] is None


def test_inquiry_result_has_a_hard_byte_limit():
    document = Document()
    document.faces = [Face(str(index)) for index in range(100)]
    document.Extension.GetPersistReference3 = lambda _: b"r" * 1024
    data = investigate("model.references", {"limit": 100}, document)
    assert not data["complete"] and data["truncated"]
    assert len(json.dumps(data).encode()) <= MAX_INVESTIGATION_OUTPUT_BYTES
    assert data["output_error"] == "output_limit" and data["retry_hint"]["limit"] < 100


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_inquiry_result_rejects_nonfinite_values(value):
    data = bound_investigation_result({"complete": True, "measurement": value})
    assert not data["complete"] and data["output_error"] == "invalid_output"
    json.dumps(data, allow_nan=False)


def test_investigation_registry_rejects_unbounded_or_extra_arguments():
    registry = build_tool_registry()
    for name in INVESTIGATION_TOOLS:
        assert registry.write_scope(name) == "none"
    with pytest.raises(PlanningError):
        registry.validate(ToolCall("feature.inspect", {"name": "x" * 251}))
    assert registry.validate(ToolCall("feature.inspect", {"name": "Boss1"})).arguments["limit"] == 30
    for limit in (0, 101, True):
        with pytest.raises(PlanningError):
            registry.validate(ToolCall("feature.inspect", {"name": "Boss1", "limit": limit}))
    with pytest.raises(PlanningError):
        registry.validate(ToolCall("model.references", {"limit": 10000}))
    with pytest.raises(PlanningError):
        registry.validate(ToolCall("sketch.inspect", {"code": "forbidden"}))
    calls = [ToolCall("session.capabilities", {}), ToolCall("model.references", {}),
             ToolCall("diagnostics.recent", {}), ToolCall("session.ensure", {}),
             ToolCall("document.new_part", {})]
    assert len(registry.validate_plan(calls)) == len(calls)


def test_recent_diagnostics_only_retrieve_bounded_task_memory_and_redact_secrets():
    task = {"failure_attempts": [{"call": {"tool": "selection.plane", "arguments": {
        "plane": "front", "api_key": "private-key", "path": "E:\\private\\part.SLDPRT"}},
        "result": {"message": "Cannot find plane; api_key=also-private", "error_code": "ToolError"}}
        for _ in range(20)]}
    data = inspect_recent_failures(task, build_tool_registry(), limit=2)
    assert data["complete"] and len(data["failures"]) == 2
    assert data["truncated"]
    assert data["scope"] == "current_task_memory_only"
    assert all(item["category_hypothesis"] == "reference_or_selection" for item in data["failures"])
    encoded = json.dumps(data)
    assert "private-key" not in encoded and "also-private" not in encoded and "E:" not in encoded
    assert "model.references" in data["failures"][0]["suggested_tools"]


def test_recent_diagnostics_empty_and_invalid_contexts_are_explicit():
    registry = build_tool_registry()
    assert inspect_recent_failures({}, registry)["failures"] == []
    assert not inspect_recent_failures({}, registry, limit=999)["complete"]
    data = inspect_recent_failures({"feedback": [{"kind": "verification_failed", "message": "验收未通过"}]}, registry)
    assert data["failures"][0]["category_hypothesis"] == "feature_or_verification"


def test_recent_diagnostics_multibyte_byte_limit_and_smaller_retry():
    task = {"failure_attempts": [{
        "call": {"tool": "feature.extrude", "arguments": {f"a{index}": "尺寸" * 300 for index in range(16)}},
        "result": {"message": "特征失败" * 300},
    } for _ in range(10)]}
    registry = build_tool_registry()
    data = inspect_recent_failures(task, registry, limit=10)
    assert not data["complete"] and data["output_error"] == "output_limit"
    assert len(json.dumps(data, ensure_ascii=False).encode("utf-8")) <= MAX_INVESTIGATION_OUTPUT_BYTES
    smaller = inspect_recent_failures(task, registry, limit=1)
    assert smaller["complete"] and smaller["truncated"] and len(smaller["failures"]) == 1
    assert len(json.dumps(smaller, ensure_ascii=False).encode("utf-8")) <= MAX_INVESTIGATION_OUTPUT_BYTES
