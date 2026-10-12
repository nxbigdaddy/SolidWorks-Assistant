"""Synthetic COM only: no SolidWorks process, document, network, or user state."""
from __future__ import annotations

import copy
import contextlib
import queue
from types import SimpleNamespace

import pytest

from sw_assistant.domain import ModelSnapshot, PlanningError, ToolCall
from sw_assistant.solidworks.gateway import CircleEditError, SketchCreationError, SolidWorksGateway
from sw_assistant.tools import build_tool_registry


class UnhashableUnknown:
    __hash__ = None

    def __init__(self, underlying):
        self.underlying = underlying

    def __eq__(self, other):
        return isinstance(other, UnhashableUnknown) and self.underlying is other.underlying


class Entity:
    def __init__(self, reference):
        self.reference = reference.encode()
        self.unknown = object()
        # Like pywin32, every query returns a distinct, unhashable wrapper.
        self._oleobj_ = SimpleNamespace(QueryInterface=lambda _iid: UnhashableUnknown(self.unknown))


class Circle(Entity):
    ConstructionGeometry = False

    def __init__(self, index, center=(0.0, 0.0, 0.0), radius=0.008):
        super().__init__(f"circle-{index}")
        self.index, self.center, self.radius = index, list(center), radius
        self.set_calls = []
        self.is_circle = 1

    def GetType(self):
        return 1

    def GetID(self):
        return [self.index, 1]

    def IsCircle(self):
        return self.is_circle

    def GetRadius(self):
        return self.radius

    def GetCenterPoint2(self):
        return SimpleNamespace(**dict(zip(("X", "Y", "Z"), self.center, strict=True)))

    def GetNormalVector(self):
        return [0, 0, 1]

    def SetRadius(self, radius):
        self.set_calls.append(radius)
        self.radius = radius
        return True


class IdentityTransform:
    ArrayData = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]

    def Inverse(self):
        return self


class Sketch(Entity):
    Is3D = False
    ModelToSketchTransform = IdentityTransform()

    def __init__(self):
        super().__init__("sketch")
        self.segments = []
        self.feature = Entity("sketch-feature")
        self.feature.Name = "Sketch1"
        self.feature.GetTypeName2 = lambda: "ProfileFeature"
        self.feature.GetNextFeature = lambda: None
        self.feature.GetFirstDisplayDimension = lambda: None
        self.feature.GetErrorCode2 = lambda *_args: 0
        self.feature.GetSpecificFeature2 = lambda: self

    @property
    def Name(self):
        return self.feature.Name

    def GetSketchSegments(self):
        return self.segments

    def CheckFeatureUse(self, *_args):
        return 12, 0, len(self.segments)


class Document(Entity):
    def __init__(self):
        super().__init__("doc")
        self.sketch = Sketch()
        self.ConfigurationManager = SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="Default"))
        self.Extension = SimpleNamespace(GetPersistReference3=lambda obj: obj.reference,
                                         CreateMassProperty=lambda: SimpleNamespace(Volume=0.0))
        self.SketchManager = SimpleNamespace(ActiveSketch=self.sketch, CreateCircleByRadius=self.create,
                                             AddToDB=False, DisplayWhenAdded=True)
        self.SelectionManager = SimpleNamespace(GetSelectedObjectCount2=lambda _: 0)

    def create(self, x, y, z, radius):
        circle = Circle(len(self.sketch.segments) + 1, (x, y, z), radius)
        self.sketch.segments.append(circle)
        return circle

    def GetType(self):
        return 1

    def GetTitle(self):
        return "SyntheticPart"

    def GetPathName(self):
        return ""

    def GraphicsRedraw2(self):
        pass

    def FirstFeature(self):
        return self.sketch.feature

    def GetBodies2(self, *_args):
        return []


def math_point(coordinates):
    import pythoncom
    from win32com.client import VARIANT

    assert isinstance(coordinates, VARIANT), "CreatePoint requires an explicitly typed SAFEARRAY"
    assert coordinates.varianttype == pythoncom.VT_ARRAY | pythoncom.VT_R8
    values = list(coordinates.value)
    return SimpleNamespace(ArrayData=values,
                           MultiplyTransform=lambda _: SimpleNamespace(ArrayData=values))


def fixture():
    gateway, document = SolidWorksGateway(), Document()
    utility = SimpleNamespace(CreatePoint=math_point)
    gateway._sw = SimpleNamespace(ActiveDoc=document, GetMathUtility=lambda: utility)
    gateway._bind_sketch_session(document, document.sketch)
    first = gateway._circle(0, 0, 8)
    second = gateway._circle(16, 0, 8)
    assert first["creation_verified"] and second["creation_verified"]
    return gateway, document, first, second


def edit(gateway, reference, *, old=8, new=7):
    call = build_tool_registry().validate(ToolCall("sketch.set_circle_radius", {
        "entity_ref": reference, "expected_radius_mm": old, "radius_mm": new,
    }))
    return gateway.execute(call)[1]["applied"]


def test_readonly_circle_inspection_is_stable_across_reordering():
    gateway, document, first, second = fixture()
    one = gateway._inspect_sketch()
    document.sketch.segments.reverse()
    two = gateway._inspect_sketch()
    assert one == two and one["complete"]
    details = one["sketch"]
    assert details["geometry_complete"]
    assert {item["entity_ref"] for item in details["segments"]} == {first["entity_ref"], second["entity_ref"]}
    assert all(item["editable"] and item["is_circle"] and item["radius_mm"] == 8 for item in details["segments"])
    assert all(not item.set_calls for item in document.sketch.segments)


def test_owner_feature_uses_specific_sketch_identity_not_another_same_named_feature():
    gateway, document, _, _ = fixture()
    impostor = Sketch()
    document.FeatureByName = lambda _name: impostor.feature
    assert gateway._owning_sketch_feature(document, document.sketch) is document.sketch.feature
    document.sketch.feature.GetSpecificFeature2 = lambda: impostor
    with pytest.raises(ValueError, match="owning feature"):
        gateway._owning_sketch_feature(document, document.sketch)


def test_circle_creation_requires_double_safearray_for_nonorigin_math_point():
    gateway, document, _, _ = fixture()
    with pytest.raises(AssertionError, match="explicitly typed"):
        math_point((0.016, 0.0, 0.0))
    created = gateway._circle(-16, 12, 8)
    assert created["creation_verified"] is True
    assert created["center_sketch_m"] == [-0.016, 0.012, 0.0]
    assert len(document.sketch.segments) == 3


def test_circle_creation_rejects_silent_math_point_input_corruption():
    gateway, document, _, _ = fixture()
    utility = gateway._sw.GetMathUtility()

    def corrupted_point(coordinates):
        point = math_point(coordinates)
        # The transformed value alone looks correct; input readback must still
        # prove that COM accepted the original model-space point.
        point.ArrayData = [0.0, 0.0, 0.0]
        return point

    utility.CreatePoint = corrupted_point
    with pytest.raises(SketchCreationError, match="math point input") as error:
        gateway._circle(-16, 12, 8)
    assert error.value.evidence["mutation_attempted"] is False
    assert len(gateway._owned_circles) == 2
    assert len(document.sketch.segments) == 2


def test_math_point_methods_bypass_eager_dispatch_getters_and_preserve_arguments(monkeypatch):
    import pythoncom
    import win32com.client

    monkeypatch.setattr(win32com.client, "Dispatch", lambda value: value)
    calls = []
    argument = object()
    result = SimpleNamespace(ArrayData=[0.016, 0.012, 0.0])

    class EagerDispatch:
        def __init__(self):
            self._oleobj_ = SimpleNamespace(GetIDsOfNames=self.dispid, Invoke=self.invoke)

        def dispid(self, name):
            assert name in {"CreatePoint", "MultiplyTransform"}
            return name

        def invoke(self, name, lcid, flags, want_result, supplied):
            assert lcid == 0 and flags == pythoncom.DISPATCH_METHOD and want_result is True
            assert supplied is argument
            calls.append(name)
            return result

        def __getattr__(self, _name):
            raise AssertionError("Normal property lookup would eagerly create a zero-point and lose arguments")

    value = EagerDispatch()
    for name in ("CreatePoint", "MultiplyTransform"):
        assert SolidWorksGateway._invoke_math_point_method(value, name, argument) is result
    assert calls == ["CreatePoint", "MultiplyTransform"]
    with pytest.raises(ValueError, match="unsupported"):
        SolidWorksGateway._invoke_math_point_method(value, "Unapproved", argument)
    value._oleobj_.Invoke = lambda *_args: None
    with pytest.raises(ValueError, match="no object"):
        SolidWorksGateway._invoke_math_point_method(value, "CreatePoint", argument)


def test_snapshot_resolves_missing_sketch_name_via_owning_feature_identity():
    gateway, document, _, _ = fixture()
    sketch_type = type("UnnamedSketch", (Sketch,), {"Name": property(lambda self: "")})
    document.sketch.__class__ = sketch_type
    assert gateway.snapshot().active_sketch == "Sketch1"
    document.sketch.feature.GetSpecificFeature2 = lambda: Sketch()
    assert gateway.snapshot().active_sketch == "<active-sketch>"


def test_circle_creation_provenance_is_not_granted_to_preexisting_or_unknown_geometry():
    gateway, document, _, _ = fixture()
    manual = document.create(0.02, 0.02, 0, 0.004)
    data = gateway._inspect_sketch()["sketch"]
    entry = next(item for item in data["segments"] if item["entity_id"][0] == manual.index)
    assert not entry["editable"]
    with pytest.raises(CircleEditError) as error:
        edit(gateway, entry["entity_ref"], old=4)
    assert not error.value.evidence["mutation_attempted"]
    document.Extension.GetPersistReference3 = lambda _: None
    with pytest.raises(SketchCreationError, match="persistent reference") as error:
        gateway._circle(0, 16, 8)
    assert not error.value.evidence["mutation_attempted"]
    assert len(document.sketch.segments) == 3


def test_edit_changes_only_the_referenced_circle_and_observation_fingerprint():
    gateway, document, first, _ = fixture()
    before = gateway._observe_model()
    assert before["active_sketch_geometry_complete"]
    assert before["document_identity"] == before["active_sketch_geometry"]["document_identity"]
    assert before["document_identity"] == gateway._inspect_sketch()["sketch"]["document_identity"]
    document.sketch.segments.reverse()
    evidence = edit(gateway, first["entity_ref"])
    assert evidence["geometry_verified"] and evidence["unchanged_other_entities"]
    assert not evidence["requires_review"]
    assert evidence["old_radius_mm"] == 8 and evidence["radius_mm"] == 7
    assert [item.radius for item in document.sketch.segments] == [0.008, 0.007]
    assert before["active_sketch_geometry"] != gateway._observe_model()["active_sketch_geometry"]
    assert all(item["editable"] for item in gateway._inspect_sketch()["sketch"]["segments"])
    assert edit(gateway, first["entity_ref"], old=7, new=6)["geometry_verified"]


def test_eager_radius_setter_attribute_failure_is_already_inside_mutation_boundary():
    gateway, document, first, _ = fixture()

    class EagerSetter(Circle):
        @property
        def SetRadius(self):
            self.radius = 0.001
            raise RuntimeError("late-bound setter attribute changed geometry")

    document.sketch.segments[0].__class__ = EagerSetter
    with pytest.raises(CircleEditError) as error:
        edit(gateway, first["entity_ref"])
    assert error.value.evidence["mutation_attempted"] is True
    assert error.value.evidence["requires_review"] is True
    assert error.value.evidence["geometry_verified"] is False
    assert document.sketch.segments[0].radius == 0.001


def test_unhashable_com_identities_are_retained_bounded_and_never_wrapper_ids():
    gateway = SolidWorksGateway()
    first, alias, separate = Entity("first"), Entity("alias"), Entity("separate")
    alias.unknown = first.unknown
    assert gateway._com_identity(first) == gateway._com_identity(alias)
    assert gateway._com_identity(first) != gateway._com_identity(separate)
    assert len(gateway._com_identities) == 2
    token = gateway._com_identity(first)
    gateway._com_identities.extend((UnhashableUnknown(object()), "unused") for _ in range(4094))
    assert gateway._com_identity(alias) == token
    with pytest.raises(ValueError, match="limit"):
        gateway._com_identity(Entity("overflow"))
    gateway.release()
    assert gateway._com_identities == []


def test_real_pywin32_memory_dictionary_wrappers_share_identity_without_solidworks():
    pythoncom = pytest.importorskip("pythoncom")
    client = pytest.importorskip("win32com.client")
    pythoncom.CoInitialize()
    gateway = SolidWorksGateway()
    try:
        # This neutral Windows in-process class does not open CAD or user files.
        first = client.Dispatch("Scripting.Dictionary")
        alias = client.Dispatch(first._oleobj_.QueryInterface(pythoncom.IID_IDispatch))
        other = client.Dispatch("Scripting.Dictionary")
        left = first._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
        right = alias._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
        assert left is not right and left == right
        assert gateway._com_identity(first) == gateway._com_identity(alias)
        assert gateway._com_identity(first) != gateway._com_identity(other)
        assert len(gateway._com_identities) == 2
    finally:
        gateway.release()
        first = alias = other = left = right = None
        pythoncom.CoUninitialize()


@pytest.mark.parametrize("drift", ["document", "sketch", "entity", "radius", "construction", "duplicate_id", "partial_arc"])
def test_identity_and_geometry_drift_refuses_to_write(drift):
    gateway, document, first, _ = fixture()
    target = document.sketch.segments[0]
    if drift == "document":
        document.unknown = object()
    elif drift == "sketch":
        document.sketch.unknown = object()
    elif drift == "entity":
        target.unknown = object()
    elif drift == "radius":
        target.radius = 0.009
    elif drift == "construction":
        target.ConstructionGeometry = True
    elif drift == "partial_arc":
        target.is_circle = 0
    else:
        document.sketch.segments[1].index = target.index
    with pytest.raises(CircleEditError) as error:
        edit(gateway, first["entity_ref"])
    assert error.value.evidence["requires_review"] and not error.value.evidence["mutation_attempted"]
    assert target.set_calls == []


@pytest.mark.parametrize("failure", ["false", "wrong_radius", "other_circle", "center", "exception", "read_error"])
def test_failed_or_partial_edit_never_claims_success_or_automatically_rolls_back(failure):
    gateway, document, first, _ = fixture()
    target, other = document.sketch.segments
    setter = target.SetRadius

    def fail(radius):
        setter(radius)
        if failure == "false":
            return False
        if failure == "wrong_radius":
            target.radius = 0.006
        elif failure == "other_circle":
            other.radius = 0.006
        elif failure == "center":
            target.center[0] += 0.001
        elif failure == "exception":
            raise RuntimeError("synthetic error after write")
        elif failure == "read_error":
            target.GetRadius = lambda: float("nan")
        return True

    target.SetRadius = fail
    with pytest.raises(CircleEditError) as error:
        edit(gateway, first["entity_ref"])
    assert error.value.evidence["mutation_attempted"] and error.value.evidence["requires_review"]
    assert error.value.evidence["geometry_verified"] is False
    assert target.set_calls == [0.007]


def test_wrong_expected_radius_and_oversize_sketch_do_not_write():
    gateway, document, first, _ = fixture()
    with pytest.raises(CircleEditError) as error:
        edit(gateway, first["entity_ref"], old=9)
    assert error.value.evidence["mutation_attempted"] is False
    for index in range(3, 130):
        document.sketch.segments.append(Circle(index))
    inspection = gateway._inspect_sketch()
    assert not inspection["complete"] and not inspection["sketch"]["geometry_complete"]
    with pytest.raises(CircleEditError) as error:
        edit(gateway, first["entity_ref"])
    assert error.value.evidence["mutation_attempted"] is False
    assert not any(item.set_calls for item in document.sketch.segments)


def test_worker_propagates_radius_edit_uncertainty_without_real_com(monkeypatch):
    from sw_assistant.solidworks import worker as module

    evidence = {"requires_review": True, "mutation_attempted": True, "geometry_verified": False,
                "geometry_before": {"radius_mm": 8}, "geometry_after": None}

    class FakeGateway:
        def __init__(self, **_kwargs):
            pass

        def snapshot(self):
            return ModelSnapshot()

        def execute(self, _call):
            raise CircleEditError("uncertain fixture edit", evidence)

        def release(self, **_kwargs):
            pass

    monkeypatch.setattr(module, "SolidWorksGateway", FakeGateway)
    monkeypatch.setattr(module, "solidworks_serial_gate", lambda _: contextlib.nullcontext())
    requests, responses = queue.Queue(), queue.Queue()
    requests.put({"id": "synthetic", "call": {"tool": "sketch.set_circle_radius", "arguments": {}}})
    requests.put({"kind": "shutdown"})
    module.worker_main(requests, responses)
    result = responses.get_nowait()["result"]
    assert not result["ok"] and result["error_code"] == "CircleEditError"
    assert result["data"]["circle_edit"] == evidence


def test_new_circle_tool_registry_and_independent_postcondition_enforce_scope():
    registry = build_tool_registry()
    assert registry.write_scope("sketch.set_circle_radius") == "document"
    for arguments in ({"entity_ref": "index:1", "expected_radius_mm": 8, "radius_mm": 7},
                      {"entity_ref": "a" * 64, "expected_radius_mm": 8, "radius_mm": float("nan")},
                      {"entity_ref": "a" * 64, "expected_radius_mm": 8, "radius_mm": 7, "code": "forbidden"}):
        with pytest.raises(PlanningError):
            registry.validate(ToolCall("sketch.set_circle_radius", arguments))
    gateway, _, first, _ = fixture()
    evidence = edit(gateway, first["entity_ref"])
    call = ToolCall("sketch.set_circle_radius", {"entity_ref": first["entity_ref"], "expected_radius_mm": 8, "radius_mm": 7})
    check = registry.postcondition(call.tool)
    snapshot = ModelSnapshot(connected=True, document_title="SyntheticPart", document_type="part",
                             active_sketch="Sketch1", sketch_entity_count=2)
    assert check(call, {"applied": evidence}, snapshot, snapshot)[0]
    noisy = copy.deepcopy(snapshot)
    noisy.volume_m3 += 1e-14
    assert check(call, {"applied": evidence}, snapshot, noisy)[0]
    noisy.volume_m3 += 1e-8
    assert not check(call, {"applied": evidence}, snapshot, noisy)[0]
    changed = copy.deepcopy(evidence)
    changed["geometry_after"]["segments"][0]["center_sketch_m"][0] += 1
    assert not check(call, {"applied": changed}, snapshot, snapshot)[0]
    assert not check(call, {"manual_verification": True}, snapshot, snapshot)[0]
    with pytest.raises(PlanningError):
        registry.validate_plan([call])
