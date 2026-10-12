import pytest

from sw_assistant.domain import ToolError
from sw_assistant.solidworks.gateway import PLANE_FRAMES, SolidWorksGateway, _created, _value
from sw_assistant.solidworks.topology import capture_sketch_session_signature


def _bind_test_sketch_session(gateway, document, sketch=None):
    sketch = sketch or _value(document.SketchManager, "ActiveSketch")
    feature = _value(sketch, "GetFeature")
    if feature is None or not str(_value(feature, "Name", "") or ""):
        feature = type("SketchFeature", (), {"Name": "Sketch1"})()
        setattr(type(sketch), "GetFeature", staticmethod(lambda: feature))
    if not hasattr(document, "GetTitle"):
        setattr(type(document), "GetTitle", staticmethod(lambda: "Part1"))
    if not hasattr(document, "GetPathName"):
        setattr(type(document), "GetPathName", staticmethod(lambda: ""))
    gateway._sketch_session = capture_sketch_session_signature(document, sketch)


class _CallableDispatch:
    _oleobj_ = object()

    def __call__(self):
        raise AssertionError("COM Dispatch values must not be invoked as methods")


class _Holder:
    ActiveDoc = _CallableDispatch()

    @staticmethod
    def RevisionNumber():
        return "33.0"


def test_com_dispatch_is_returned_even_when_callable():
    holder = _Holder()
    assert _value(holder, "ActiveDoc") is holder.ActiveDoc
    assert _value(holder, "RevisionNumber") == "33.0"


def test_falsey_com_dispatch_is_still_a_created_entity():
    class FalseyDispatch:
        _oleobj_ = object()

        def __bool__(self):
            return False

    assert _created(FalseyDispatch()) is True


class _Feature:
    Name = ""


class _Sketch:
    @staticmethod
    def GetFeature():
        return _Feature()

    @staticmethod
    def GetSketchSegments():
        return []


class _SketchManager:
    ActiveSketch = _Sketch()


class _SelectionManager:
    @staticmethod
    def GetSelectedObjectCount2(_mark):
        return 0


class _Document:
    SketchManager = _SketchManager()
    SelectionManager = _SelectionManager()

    @staticmethod
    def GetType():
        return 1

    @staticmethod
    def GetTitle():
        return "Part1"

    @staticmethod
    def GetPathName():
        return ""

    @staticmethod
    def FirstFeature():
        return None

    @staticmethod
    def GetBodies2(_body_type, _visible_only):
        return []


def test_active_unnamed_sketch_uses_observable_placeholder():
    snapshot = SolidWorksGateway()._snapshot_document(_Document())
    assert snapshot.active_sketch == "<active-sketch>"


def test_active_sketch_uses_isketch_name_without_get_feature():
    document = _Document()
    sketch = document.SketchManager.ActiveSketch
    sketch.Name = "Sketch-from-ISketch"
    try:
        snapshot = SolidWorksGateway()._snapshot_document(document)
        assert snapshot.active_sketch == "Sketch-from-ISketch"
    finally:
        del sketch.Name


def test_feature_operation_unwraps_instant3d_ice_feature():
    class Instant3DFeature:
        @staticmethod
        def GetTypeName2():
            return "ICE"

        @staticmethod
        def GetTypeName():
            return "Boss"

    assert SolidWorksGateway._feature_operation(Instant3DFeature()) == "boss"


def test_feature_health_rejects_rebuild_error_code(monkeypatch):
    class WarningReference:
        value = False

    class FailedFeature:
        @staticmethod
        def GetErrorCode2(warning):
            warning.value = False
            return 7

    gateway = SolidWorksGateway()
    monkeypatch.setattr(gateway, "_bool_reference", WarningReference)

    with pytest.raises(ToolError, match="重建错误码：7"):
        gateway._feature_health(FailedFeature())


class _PlaneFeature:
    def __init__(self, name, next_feature=None):
        self.Name = name
        self._next = next_feature
        self.select_arguments = None

    @staticmethod
    def GetTypeName2():
        return "RefPlane"

    def GetNextFeature(self):
        return self._next

    def Select2(self, append, mark):
        self.select_arguments = (append, mark)
        return True


class _Extension:
    @staticmethod
    def SelectByID2(*_args):
        return False


class _PlaneDocument:
    Extension = _Extension()

    def __init__(self):
        right = _PlaneFeature("localized-right")
        top = _PlaneFeature("localized-top", right)
        self.front = _PlaneFeature("localized-front", top)
        self.top = top

    @staticmethod
    def GetType():
        return 1

    @staticmethod
    def ClearSelection2(_all):
        return None

    def FirstFeature(self):
        return self.front


def test_plane_ordinal_fallback_calls_select2_with_required_arguments(monkeypatch):
    document = _PlaneDocument()
    gateway = SolidWorksGateway()
    gateway._sw = type("Application", (), {"ActiveDoc": document})()
    monkeypatch.setattr(gateway, "_null_dispatch", lambda: None)
    assert gateway._select_plane("top") == "localized-top"
    assert document.top.select_arguments == (False, 0)


class _MassProperty:
    Volume = 0.25
    SurfaceArea = 0.5
    Mass = 7.5
    UseSystemUnits = False


class _MassExtension:
    @staticmethod
    def CreateMassProperty2():
        return _MassProperty()


class _Body:
    @staticmethod
    def GetMassProperties(_density):
        return [0, 0, 0, 0.1, 0.2, 999]


class _MassDocument(_Document):
    Extension = _MassExtension()

    @staticmethod
    def GetBodies2(_body_type, _visible_only):
        return [_Body()]


def test_snapshot_uses_document_mass_property_not_unit_density_body_mass():
    snapshot = SolidWorksGateway()._snapshot_document(_MassDocument())
    assert snapshot.volume_m3 == 0.25
    assert snapshot.surface_area_m2 == 0.5
    assert snapshot.mass_kg == 7.5
    assert snapshot.mass_available is True


def test_readonly_open_uses_exact_open_doc_result_not_unrelated_active_document(tmp_path):
    target = object()
    unrelated = object()

    class Application:
        ActiveDoc = unrelated

        @staticmethod
        def OpenDoc6(*_args):
            return target, 0, 0

    source = tmp_path / "Target.SLDPRT"
    source.write_bytes(b"fixture")
    gateway = SolidWorksGateway()
    gateway._sw = Application()
    document, path = gateway._open_readonly_document(str(source))
    assert document is target
    assert path == str(source.resolve())


def test_extrude_rejects_explicit_rebuild_failure():
    class Sketch:
        @staticmethod
        def CheckFeatureUse(_feature_type, _open_count, _closed_count):
            return 0, 0, 1

    class SketchManager:
        ActiveSketch = Sketch()

        def InsertSketch(self, _update):
            self.ActiveSketch = None

    class FeatureManager:
        @staticmethod
        def FeatureExtrusion3(*_args):
            return object()

    class Document:
        rebuild_calls = 0
        undo_calls = 0

        @staticmethod
        def GetType():
            return 1

        @classmethod
        def ForceRebuild3(cls, _top_only):
            cls.rebuild_calls += 1
            return cls.rebuild_calls > 1

        @classmethod
        def EditUndo2(cls, count):
            cls.undo_calls += count
            return True

    Document.SketchManager = SketchManager()
    Document.FeatureManager = FeatureManager()

    gateway = SolidWorksGateway()
    document = Document()
    gateway._sw = type("Application", (), {"ActiveDoc": document})()
    _bind_test_sketch_session(gateway, document)
    with pytest.raises(ToolError, match="重建失败"):
        gateway._extrude(
            depth_mm=10,
            operation="boss",
            end_condition="blind",
            reverse=False,
            merge=True,
        )
    assert Document.undo_calls == 1
    assert Document.rebuild_calls == 2


def test_extrude_rolls_back_when_created_definition_has_wrong_depth():
    null_dispatch = object()

    class Sketch:
        @staticmethod
        def CheckFeatureUse(_feature_type, _open_count, _closed_count):
            return 0, 0, 1

    class Definition:
        ReverseDirection = False
        Merge = True

        @staticmethod
        def AccessSelections(_document, component):
            assert component is null_dispatch
            return True

        @staticmethod
        def GetEndCondition(_forward):
            return 0

        @staticmethod
        def GetDepth(_forward):
            return 0.02

        @staticmethod
        def ReleaseSelectionAccess():
            return None

    class Feature:
        @staticmethod
        def GetDefinition():
            return Definition()

        @staticmethod
        def GetTypeName2():
            return "Boss"

    class SketchManager:
        ActiveSketch = Sketch()

        def InsertSketch(self, _update):
            self.ActiveSketch = None

    class FeatureManager:
        @staticmethod
        def FeatureExtrusion3(*_args):
            return Feature()

    class Document:
        undo_calls = 0

        @staticmethod
        def GetType():
            return 1

        @staticmethod
        def ForceRebuild3(_top_only):
            return True

        @classmethod
        def EditUndo2(cls, count):
            cls.undo_calls += count
            return True

    Document.SketchManager = SketchManager()
    Document.FeatureManager = FeatureManager()
    gateway = SolidWorksGateway()
    document = Document()
    gateway._sw = type("Application", (), {"ActiveDoc": document})()
    gateway._null_dispatch = lambda: null_dispatch
    _bind_test_sketch_session(gateway, document)

    with pytest.raises(ToolError, match="参数与请求不一致"):
        gateway._extrude(
            depth_mm=10,
            operation="boss",
            end_condition="blind",
            reverse=False,
            merge=True,
        )

    assert Document.undo_calls == 1


def test_sketch_points_follow_front_top_right_and_translated_face_frames():
    gateway = SolidWorksGateway()

    gateway._sketch_frame = dict(PLANE_FRAMES["front"])
    assert gateway._sketch_point(10, 20) == pytest.approx((0.01, 0.02, 0.0))

    gateway._sketch_frame = dict(PLANE_FRAMES["top"])
    assert gateway._sketch_point(10, 20) == pytest.approx((0.01, 0.0, -0.02))

    gateway._sketch_frame = dict(PLANE_FRAMES["right"])
    assert gateway._sketch_point(10, 20) == pytest.approx((0.0, 0.01, 0.02))

    gateway._sketch_frame = {
        "origin": (0.0, 0.03, 0.0),
        "u": (1.0, 0.0, 0.0),
        "v": (0.0, 0.0, -1.0),
        "normal": (0.0, 1.0, 0.0),
    }
    assert gateway._sketch_point(10, 20) == pytest.approx((0.01, 0.03, -0.02))


def test_sketch_write_rejects_active_document_drift_before_calling_api():
    class SketchFeature:
        Name = "Sketch1"

    class Sketch:
        @staticmethod
        def GetFeature():
            return SketchFeature()

        @staticmethod
        def GetSketchSegments():
            return []

    class SketchManager:
        ActiveSketch = Sketch()

        @staticmethod
        def CreateCircleByRadius(*_arguments):
            raise AssertionError("drifted document must not be modified")

    class Document:
        def __init__(self, title):
            self.title = title

        @staticmethod
        def GetType():
            return 1

        @staticmethod
        def GetPathName():
            return ""

        def GetTitle(self):
            return self.title

    Document.SketchManager = SketchManager()

    first = Document("Part1")
    second = Document("Part2")
    application = type("Application", (), {"ActiveDoc": first})()
    gateway = SolidWorksGateway()
    gateway._sw = application
    _bind_test_sketch_session(gateway, first)

    application.ActiveDoc = second
    with pytest.raises(ToolError, match="活动文档或配置已切换"):
        gateway._circle(0, 0, 2)


def test_extrude_profile_check_rejects_open_contour_before_closing_sketch():
    class SketchFeature:
        Name = "Sketch1"

    class Sketch:
        @staticmethod
        def GetFeature():
            return SketchFeature()

        @staticmethod
        def CheckFeatureUse(feature_type, _open_count, _closed_count):
            assert feature_type == 3
            return 6, 0, 0

    class SketchManager:
        ActiveSketch = Sketch()
        close_calls = 0

        @classmethod
        def InsertSketch(cls, _update):
            cls.close_calls += 1
            cls.ActiveSketch = None

    class FeatureManager:
        @staticmethod
        def FeatureExtrusion3(*_arguments):
            raise AssertionError("invalid profile must not reach FeatureExtrusion3")

    class Document:
        @staticmethod
        def GetType():
            return 1

        @staticmethod
        def GetTitle():
            return "Part1"

        @staticmethod
        def GetPathName():
            return ""

    Document.SketchManager = SketchManager()
    Document.FeatureManager = FeatureManager()

    document = Document()
    gateway = SolidWorksGateway()
    gateway._sw = type("Application", (), {"ActiveDoc": document})()
    _bind_test_sketch_session(gateway, document)

    with pytest.raises(ToolError, match="开放轮廓"):
        gateway._extrude(
            depth_mm=10,
            operation="boss",
            end_condition="blind",
            reverse=False,
            merge=True,
        )

    assert SketchManager.close_calls == 0
    assert SketchManager.ActiveSketch is not None


def test_profile_check_records_official_open_and_closed_contour_counts():
    class Sketch:
        @staticmethod
        def CheckFeatureUse(feature_type, _open_count, _closed_count):
            assert feature_type == 13
            return 0, 0, 2

    evidence = SolidWorksGateway._check_sketch_profile(Sketch(), "revolve", "cut")

    assert evidence == {
        "available": True,
        "usage": "cut-revolve",
        "status": 0,
        "open_contours": 0,
        "closed_contours": 2,
    }


def test_profile_check_fails_closed_when_com_method_is_unavailable():
    with pytest.raises(ToolError, match="未暴露 ISketch.CheckFeatureUse"):
        SolidWorksGateway._check_sketch_profile(object(), "extrude", "boss")


def test_extrude_refuses_an_active_sketch_without_bound_session():
    class SketchManager:
        ActiveSketch = object()

        @staticmethod
        def InsertSketch(_update):
            raise AssertionError("unbound sketch must not be closed or consumed")

    class FeatureManager:
        @staticmethod
        def FeatureExtrusion3(*_arguments):
            raise AssertionError("unbound sketch must not reach FeatureExtrusion3")

    class Document:
        @staticmethod
        def GetType():
            return 1

    Document.SketchManager = SketchManager()
    Document.FeatureManager = FeatureManager()

    gateway = SolidWorksGateway()
    gateway._sw = type("Application", (), {"ActiveDoc": Document()})()

    with pytest.raises(ToolError, match="不是由本次建模会话"):
        gateway._extrude(
            depth_mm=10,
            operation="boss",
            end_condition="blind",
            reverse=False,
            merge=True,
        )


def test_line_rejects_coincident_endpoints():
    gateway = SolidWorksGateway()
    with pytest.raises(ToolError, match="不能重合"):
        gateway._line(1, 2, 1, 2, centerline=False)


def test_line_uses_exact_geometry_from_first_call_and_restores_flags():
    from test_sketch_creation_coordinates import fixture

    gateway, document, manager = fixture()
    result = gateway._line(0, 0, 2, 0, centerline=False)

    assert len(manager.calls) == 1  # Fixture requires AddToDB during the call.
    assert len(document.sketch.segments) == 1 and result["creation_verified"]
    assert manager.AddToDB is False
    assert manager.DisplayWhenAdded is True


def test_small_origin_circle_uses_exact_geometry_from_first_call():
    from test_sketch_creation_coordinates import fixture

    gateway, document, manager = fixture()
    result = gateway._circle(0, 0, 2)

    assert len(manager.calls) == 1
    assert result["creation_verified"] and len(document.sketch.segments) == 1
    assert manager.AddToDB is False
    assert manager.DisplayWhenAdded is True


def test_polygon_fallback_uses_one_closed_batch_and_restores_sketch_manager_flags():
    from polygon_fakes import polygon_fixture

    gateway, _document, manager = polygon_fixture(native=False)
    evidence = gateway._polygon(0, 0, 2, 6, 30)

    assert evidence["geometry_verified"]
    assert len(manager.line_calls) == 6
    assert manager.line_calls[-1][3:] == pytest.approx(manager.line_calls[0][:3])
    assert manager.AddToDB is False
    assert manager.DisplayWhenAdded is True


def test_polygon_fallback_handles_late_bound_member_lookup_failure():
    from polygon_fakes import polygon_fixture

    gateway, _document, manager = polygon_fixture(native=False)
    evidence = gateway._polygon(0, 0, 2, 6, 30)

    assert evidence["backend"] == "line-fallback"
    assert evidence["segment_count"] == 6
    assert len(manager.line_calls) == 6


def test_polygon_prefers_native_solidworks_operation():
    from polygon_fakes import Transform, polygon_fixture

    # Real right-plane sketch axes differ from the public frame (world Y/Z).
    gateway, _document, manager = polygon_fixture(
        plane="right", transform=Transform((2, 1, 0), (-1, 1, 1))
    )
    evidence = gateway._polygon(0, 90, 2, 6, 0)

    assert evidence["backend"] == "solidworks-native"
    assert len(manager.native_calls) == 1
    assert manager.native_calls[0][:3] == pytest.approx((-0.09, 0.0, 0.0))
    assert manager.native_calls[0][3:6] == pytest.approx((-0.09, 0.002, 0.0))
    assert manager.native_calls[0][6:] == (6, True)
    assert evidence["points_m"][0] == pytest.approx((0.0, 0.002, 0.09))


def test_polygon_retries_native_when_first_result_is_not_committed():
    from polygon_fakes import polygon_fixture

    gateway, _document, manager = polygon_fixture(native_noops=1)
    evidence = gateway._polygon(0, 0, 2, 6, 0)

    assert evidence["backend"] == "solidworks-native"
    assert evidence["commit_attempts"] == 2
    assert evidence["segment_count"] == 6
    assert len(manager.native_calls) == 2


def test_polygon_falls_back_when_native_results_never_commit():
    from polygon_fakes import polygon_fixture

    gateway, _document, manager = polygon_fixture(native_noops=2)
    evidence = gateway._polygon(0, 0, 2, 6, 0)

    assert evidence["backend"] == "line-fallback"
    assert evidence["segment_count"] == 6
    assert len(manager.native_calls) == 2
    assert len(manager.line_calls) == 6


def test_polygon_fallback_rolls_back_partial_lines():
    from polygon_fakes import polygon_fixture

    gateway, document, manager = polygon_fixture(native=False, line_failure=2)
    with pytest.raises(ToolError, match="已自动回滚") as error:
        gateway._polygon(0, 0, 2, 6, 0)

    assert document.sketch.segments == []
    assert manager.AddToDB is False
    assert manager.DisplayWhenAdded is True
    assert error.value.evidence["rollback_verified"]
    assert error.value.evidence["rollback_scope"] == "new_sketch_segments"
    assert error.value.evidence["document_state_restored"] is False


def test_last_extrude_terminal_face_can_start_the_next_sketch(monkeypatch):
    class Surface:
        @staticmethod
        def IsPlane():
            return True

    class Face:
        def __init__(self, y, normal):
            self.y = y
            self.Normal = normal
            self.selected = False

        @staticmethod
        def GetSurface():
            return Surface()

        def GetBox(self):
            return (-0.002, self.y, -0.002, 0.002, self.y, 0.002)

        def Select4(self, _append, _data):
            self.selected = True
            return True

    start = Face(0.0, (0.0, -1.0, 0.0))
    end = Face(0.03, (0.0, 1.0, 0.0))

    class Feature:
        Name = "Boss-Extrude1"

        @staticmethod
        def GetFaces():
            return [start, end]

    feature = Feature()

    class NewSketchFeature:
        Name = "Sketch2"

    class NewSketch:
        @staticmethod
        def GetFeature():
            return NewSketchFeature()

    class SketchManager:
        ActiveSketch = None

        def InsertSketch(self, _update):
            self.ActiveSketch = NewSketch()

    class Document:
        class SelectionManagerType:
            @staticmethod
            def GetSelectedObjectCount2(_mark):
                return 1

        SelectionManager = SelectionManagerType()

        @staticmethod
        def GetType():
            return 1

        @staticmethod
        def ClearSelection2(_all):
            return None

        @staticmethod
        def ForceRebuild3(_top_only):
            return True

        @staticmethod
        def GetTitle():
            return "Part1"

        @staticmethod
        def GetPathName():
            return ""

        @staticmethod
        def FeatureByName(name):
            return feature if name == feature.Name else None

    Document.SketchManager = SketchManager()

    gateway = SolidWorksGateway()
    gateway._sw = type("Application", (), {"ActiveDoc": Document()})()
    gateway._sketch_frame = {
        "origin": (0.0, 0.0, 0.0),
        "origin_xy": (0.0, 0.0),
        "u": (1.0, 0.0, 0.0),
        "v": (0.0, 0.0, -1.0),
        "normal": (0.0, 1.0, 0.0),
    }
    gateway._remember_terminal_face(feature, 0.03)
    monkeypatch.setattr(gateway, "_null_dispatch", lambda: None)
    gateway._begin_sketch_on_last_face()

    assert end.selected is True
    assert gateway._sketch_frame["origin"] == pytest.approx((0.0, 0.03, 0.0))
    assert Document.SketchManager.ActiveSketch is not None


def test_centerline_and_revolve_use_official_selection_marks_and_radians():
    from types import SimpleNamespace
    from test_circle_repair import Entity, IdentityTransform, math_point

    class Selectable(Entity):
        def __init__(self, start=None, end=None):
            super().__init__(f"selectable-{id(self)}")
            self.selections = []
            self.start = start
            self.end = end
            self.ConstructionGeometry = start is not None

        def Select2(self, append, mark):
            self.selections.append((append, mark))
            return True

        def GetStartPoint2(self):
            return self.start

        def GetEndPoint2(self):
            return self.end

        def GetID(self):
            return (17, 4) if self.ConstructionGeometry else None

        def GetType(self):
            return 0 if self.ConstructionGeometry else None

    sketch_feature = Selectable()
    sketch_feature.Name = "Sketch1"

    class Point:
        def __init__(self, x, y, z):
            self.X = x
            self.Y = y
            self.Z = z

    original_axis = Selectable(Point(0.0, -0.01, 0.0), Point(0.0, 0.01, 0.0))
    rebuilt_axis = Selectable(Point(0.0, -0.01, 0.0), Point(0.0, 0.01, 0.0))

    class ClosedSketch:
        @staticmethod
        def GetSketchSegments():
            return [rebuilt_axis]

    sketch_feature.GetTypeName2 = lambda: "ProfileFeature"

    class Sketch(Entity):
        Name = "Sketch1"
        Is3D = False
        ModelToSketchTransform = IdentityTransform()

        def __init__(self):
            super().__init__("active-sketch")
            self.segments = []

        def GetSketchSegments(self):
            return self.segments

        @staticmethod
        def CheckFeatureUse(_feature_type, _open_count, _closed_count):
            return 0, 0, 1

    class SketchManager:
        ActiveSketch = Sketch()
        AddToDB = False
        DisplayWhenAdded = True

        def CreateCenterLine(self, *_args):
            assert self.AddToDB is True
            self.ActiveSketch.segments.append(original_axis)
            return original_axis

        def InsertSketch(self, _update):
            self.ActiveSketch = None

    class Definition:
        ReverseDirection = False
        Merge = True

        @staticmethod
        def AccessSelections(_document, _component):
            return True

        @staticmethod
        def GetRevolutionAngle(_forward):
            return 3.141592653589793

        @staticmethod
        def ReleaseSelectionAccess():
            return None

    class Feature:
        @staticmethod
        def GetDefinition():
            return Definition()

        @staticmethod
        def GetTypeName2():
            return "Revolution"

    class FeatureManager:
        arguments = None

        def FeatureRevolve2(self, *arguments):
            self.arguments = arguments
            return Feature()

    class Document(Entity):
        def __init__(self):
            super().__init__("revolve-document")
            self.Extension = SimpleNamespace(GetPersistReference3=lambda entity: entity.reference)
            self.ConfigurationManager = SimpleNamespace(ActiveConfiguration=SimpleNamespace(Name="Default"))

        class SelectionManager:
            @staticmethod
            def GetSelectedObjectCount2(mark):
                return {-1: 2, 0: 1, 4: 1}.get(mark, 0)

        @staticmethod
        def GetType():
            return 1

        @staticmethod
        def ClearSelection2(_all):
            return None

        @staticmethod
        def ForceRebuild3(_top_only):
            return True

        @staticmethod
        def GetPathName():
            return r"C:\models\revolve.sldprt"

        @staticmethod
        def GetTitle():
            return "revolve.sldprt"

        @staticmethod
        def FeatureByName(name):
            return sketch_feature if name == "Sketch1" else None

        @staticmethod
        def GraphicsRedraw2():
            return None

    Document.SketchManager = SketchManager()
    Document.FeatureManager = FeatureManager()
    document = Document()
    sketch_feature.GetSpecificFeature2 = lambda: document.SketchManager.ActiveSketch or ClosedSketch()
    gateway = SolidWorksGateway()
    gateway._sw = SimpleNamespace(ActiveDoc=document, GetMathUtility=lambda: SimpleNamespace(CreatePoint=math_point))
    _bind_test_sketch_session(gateway, document)

    gateway._line(0, -10, 0, 10, centerline=True)
    evidence = gateway._revolve(
        angle_deg=180,
        operation="boss",
        reverse=False,
        merge=True,
    )

    assert sketch_feature.selections == [(False, 0)]
    assert original_axis.selections == []
    assert rebuilt_axis.selections == [(True, 4)]
    assert Document.FeatureManager.arguments[8] == pytest.approx(3.141592653589793)
    assert Document.FeatureManager.arguments[3] is False
    assert evidence["axis_resolution"]["selection_counts"] == {
        "all": 2,
        "profile_mark_0": 1,
        "axis_mark_4": 1,
    }


def test_capture_and_restore_persistent_selection_reference(tmp_path, monkeypatch):
    source = tmp_path / "Part1.SLDPRT"
    source.write_bytes(b"part")

    class SelectionManager:
        count = 1

        def GetSelectedObjectCount2(self, _mark):
            return self.count

        @staticmethod
        def GetSelectedObjectType3(_index, _mark):
            return 2

        def GetSelectedObject6(self, _index, _mark):
            return selected

    manager = SelectionManager()

    class Selected:
        Name = "Face1"

        @staticmethod
        def Select4(_append, _select_data):
            manager.count = 1
            return True

    selected = Selected()

    class Extension:
        @staticmethod
        def GetPersistReference3(_value):
            return [1, 2, 255]

        @staticmethod
        def GetObjectByPersistReference3(_value):
            return selected

    class Document:
        @staticmethod
        def GetPathName():
            return str(source)

        @staticmethod
        def GetTitle():
            return "Part1.SLDPRT"

        @staticmethod
        def ClearSelection2(_all):
            manager.count = 0

    Document.SelectionManager = manager
    Document.Extension = Extension()

    gateway = SolidWorksGateway()
    gateway._sw = type("Application", (), {"ActiveDoc": Document()})()
    monkeypatch.setattr(gateway, "_null_dispatch", lambda: None)
    message, captured = gateway._capture_selection_reference()
    assert "持久引用" in message
    assert captured["persistent_ref"] == "AQL/"
    assert captured["selection_type"] == 2
    assert captured["object_name"] == "Face1"

    restored_message, restored = gateway._restore_selection_reference(
        str(source), captured["persistent_ref"]
    )
    assert "恢复" in restored_message
    assert restored["selection_count"] == 1


def test_persistent_reference_accepts_dynamic_com_memoryview():
    assert SolidWorksGateway._reference_bytes(memoryview(b"\x00\x7f\xff")) == b"\x00\x7f\xff"


def test_mate_creation_uses_returned_feature_as_success_evidence(tmp_path, monkeypatch):
    source = tmp_path / "Assembly1.SLDASM"
    source.write_bytes(b"assembly")

    class SelectionManager:
        @staticmethod
        def GetSelectedObjectCount2(mark):
            return 2 if mark == 1 else 0

    class Extension:
        pass

    class Document:
        @staticmethod
        def GetSaveFlag():
            return False

        @staticmethod
        def ClearSelection2(_all):
            return None

        @staticmethod
        def ForceRebuild3(_top_only):
            return True

    Document.Extension = Extension()
    Document.SelectionManager = SelectionManager()

    class BaseMateData:
        TypeName = -1
        # Some late-bound SolidWorks wrappers expose MateAlignment (2) when
        # callers try to read the base-interface ErrorStatus property.
        ErrorStatus = 2

    class TypedMateData:
        EntitiesToMate = ()
        MateAlignment = -1

    base_data = BaseMateData()
    typed_data = TypedMateData()

    class Feature:
        Name = "Parallel1"

    class Assembly:
        @staticmethod
        def CreateMateData(_mate_type):
            return object()

        @staticmethod
        def CreateMate(_data):
            return Feature()

    class Application:
        @staticmethod
        def GetOpenDocumentByName(_path):
            return Document()

    gateway = SolidWorksGateway()
    gateway._sw = Application()
    monkeypatch.setattr(gateway, "connect", lambda: gateway._sw)
    entities = iter((object(), object()))
    monkeypatch.setattr(
        gateway, "_object_by_persistent_reference", lambda _extension, _payload: next(entities)
    )
    monkeypatch.setattr(gateway, "_cast_mate_entity", lambda value: value)

    def cast(value, interface):
        if interface == "AssemblyDoc":
            return Assembly()
        if interface == "IMateFeatureData":
            return base_data
        return typed_data

    monkeypatch.setattr(gateway, "_cast_document", cast)
    monkeypatch.setattr(gateway, "_select_mate_entity", lambda *_args: True)
    monkeypatch.setattr(gateway, "_mate_entities_array", lambda first, second: (first, second))

    result = gateway._create_assembly_mate(
        {
            "assembly_path": str(source),
            "first_reference": "AQ==",
            "second_reference": "Ag==",
            "mate_type": "parallel",
            "alignment": "closest",
        }
    )

    assert result["created"] is True
    assert result["solidworks_status"] == 1
    assert result["feature_name"] == "Parallel1"
    assert typed_data.EntitiesToMate and typed_data.MateAlignment == 2


def test_restore_persistent_reference_rejects_another_active_document(tmp_path):
    active = tmp_path / "Active.SLDPRT"
    expected = tmp_path / "Expected.SLDPRT"
    active.write_bytes(b"active")
    expected.write_bytes(b"expected")

    class Document:
        @staticmethod
        def GetPathName():
            return str(active)

    gateway = SolidWorksGateway()
    gateway._sw = type("Application", (), {"ActiveDoc": Document()})()
    with pytest.raises(ToolError, match="不属于当前活动文档"):
        gateway._restore_selection_reference(str(expected), "AQID")


def test_document_qualified_dimension_name_is_normalized_for_safe_copies():
    assert (
        SolidWorksGateway._local_parameter_name("D1@Boss-Extrude1@OriginalDocument.Part")
        == "D1@Boss-Extrude1"
    )
    assert SolidWorksGateway._local_parameter_name("D1@Sketch1") == "D1@Sketch1"


def test_localized_standard_views_are_resolved_for_drawing_generation():
    resolved = SolidWorksGateway._drawing_orientation_names(["*前视", "*上视", "*右视", "*等轴测"])
    assert resolved == {
        "front": "*前视",
        "top": "*上视",
        "right": "*右视",
        "isometric": "*等轴测",
    }


def test_drawing_bom_uses_view_api_and_parts_only_configuration():
    class View:
        arguments = None

        @classmethod
        def InsertBomTable6(cls, *arguments):
            cls.arguments = arguments
            return object()

    result = SolidWorksGateway._insert_view_bom(
        View(),
        configuration="规格-B",
        template_path=r"C:\templates\bom-standard.sldbomtbt",
    )

    assert result is not None
    assert View.arguments == (
        False,
        0.02,
        0.28,
        1,  # top-left anchor
        1,  # parts-only BOM
        "规格-B",
        r"C:\templates\bom-standard.sldbomtbt",
        False,
        0,
        False,
        False,
        False,
    )


def test_drawing_bom_falls_back_to_legacy_view_api():
    class View:
        arguments = None

        @classmethod
        def InsertBomTable4(cls, *arguments):
            cls.arguments = arguments
            return object()

    SolidWorksGateway._insert_view_bom(
        View(), configuration="Default", template_path="bom.sldbomtbt"
    )
    assert View.arguments == (
        False,
        0.02,
        0.28,
        1,
        1,
        "Default",
        "bom.sldbomtbt",
        False,
        0,
        False,
    )


def test_activate_document_falls_back_to_byref_error_argument(monkeypatch):
    sentinel = object()

    class Application:
        calls = []

        @classmethod
        def ActivateDoc3(cls, *arguments):
            cls.calls.append(arguments)
            if arguments[-1] == 0:
                raise TypeError("dynamic COM requires a by-reference output")
            return sentinel

    gateway = SolidWorksGateway()
    error_reference = object()
    monkeypatch.setattr(gateway, "_int_reference", lambda: error_reference)

    assert gateway._activate_document(Application(), "Drawing1") is sentinel
    assert Application.calls == [
        ("Drawing1", False, 0, 0),
        ("Drawing1", False, 0, error_reference),
    ]


def test_release_gracefully_exits_only_an_owned_idle_application():
    class Application:
        ActiveDoc = None
        exits = 0

        @classmethod
        def ExitApp(cls):
            cls.exits += 1

    owned = SolidWorksGateway()
    owned._sw = Application()
    owned._owns_application = True
    owned.release(close_owned=True)
    assert Application.exits == 1

    attached = SolidWorksGateway()
    attached._sw = Application()
    attached._owns_application = False
    attached.release(close_owned=True)
    assert Application.exits == 1


def test_release_preserves_owned_application_with_an_active_document():
    class Application:
        ActiveDoc = object()

        @staticmethod
        def ExitApp():
            raise AssertionError("an application with an active document must remain open")

    gateway = SolidWorksGateway()
    gateway._sw = Application()
    gateway._owns_application = True
    gateway.release(close_owned=True)


def test_section_view_uses_model_bounds_and_selected_cut_line(monkeypatch):
    created_view = object()

    class Model:
        @staticmethod
        def GetType():
            return 1

        @staticmethod
        def GetPartBox(_include_hidden):
            return [-0.01, -0.02, -0.03, 0.03, 0.02, 0.01]

    class Segment:
        selected = None

        @classmethod
        def Select4(cls, append, data):
            cls.selected = (append, data)
            return True

    class SketchManager:
        line = None

        @classmethod
        def CreateLine(cls, *arguments):
            cls.line = arguments
            return Segment()

    class Document:
        @staticmethod
        def ClearSelection2(_all):
            return None

    Document.SketchManager = SketchManager()

    class Parent:
        @staticmethod
        def GetName2():
            return "主视图"

    class Drawing:
        section_arguments = None

        @staticmethod
        def ActivateView(name):
            return name == "主视图"

        @classmethod
        def CreateSectionViewAt5(cls, *arguments):
            cls.section_arguments = arguments
            return created_view

    gateway = SolidWorksGateway()
    monkeypatch.setattr(gateway, "_null_dispatch", lambda: None)
    result = gateway._create_section_view(
        Document(), Drawing(), Parent(), Model(), x=0.2, y=0.1, label="A"
    )

    assert result is created_view
    assert SketchManager.line == pytest.approx((0.01, -0.03, 0, 0.01, 0.03, 0))
    assert Segment.selected == (False, None)
    assert Drawing.section_arguments == (0.2, 0.1, 0.0, "A", 8, None, 0.0)
