"""Geometric proofs and local recovery, using synthetic COM objects only."""
from types import SimpleNamespace

import pytest

from polygon_fakes import Transform, polygon_fixture
from test_circle_repair import Circle, Document

from sw_assistant.solidworks.gateway import PolygonCreationError


@pytest.mark.parametrize(("plane", "transform"), [
    ("front", Transform()),
    ("top", Transform((0, 2, 1), (1, -1, 1))),
    ("right", Transform((2, 1, 0), (-1, 1, 1))),
])
@pytest.mark.parametrize("native", [True, False])
def test_polygon_real_endpoints_match_public_frame_on_each_plane(plane, transform, native):
    gateway, document, _manager = polygon_fixture(plane=plane, transform=transform, native=native)
    result = gateway._polygon(12, 74, 3, 6, 0)
    assert result["points_m"][0] == pytest.approx(gateway._sketch_point(15, 74))
    assert result["points_m"][3] == pytest.approx(gateway._sketch_point(9, 74))
    assert result["geometry_verified"]
    assert len(result["actual_edges_m"]) == len(document.sketch.segments) == 6


def test_polygon_uses_actual_translated_sketch_transform():
    gateway, _document, manager = polygon_fixture(transform=Transform(translation=(-0.03, 0.02, -0.08)))
    gateway._sketch_frame["origin"] = (0.03, -0.02, 0.08)
    result = gateway._polygon(12, 74, 3, 6, 0)
    assert manager.native_calls[0][:3] == pytest.approx((0.012, 0.074, 0))
    assert result["points_m"][0] == pytest.approx((0.045, 0.054, 0.08))


def test_polygon_reads_actual_geometry_not_requested_point_echo():
    gateway, _document, manager = polygon_fixture()
    manager.native_offset = (2e-10, 0, 0)
    result = gateway._polygon(0, 0, 2, 6, 0)
    assert result["points_m"][0][0] == 0.002 + 2e-10
    assert result["points_m"][0][0] != 0.002


def test_polygon_accepts_reversed_edge_direction_and_enumeration_order():
    gateway, _document, manager = polygon_fixture()
    manager.reverse_order = True
    result = gateway._polygon(0, 0, 2, 6, 30)
    assert result["geometry_verified"] and result["boundary_segment_count"] == 6


def test_polygon_rejects_count_correct_but_wrong_geometry_and_cleans_only_new_segments():
    gateway, document, manager = polygon_fixture()
    existing = manager.line((0.1, 0.2, 0), (0.2, 0.3, 0))
    manager.native_offset = (0.01, 0, 0)
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert error.value.evidence["rollback_verified"]
    assert error.value.evidence["document_state_restored"] is False
    assert document.sketch.segments == [existing]
    assert document.delete_calls == 1
    assert len(manager.native_calls) == 1 and not manager.line_calls


def test_polygon_does_not_delete_when_an_existing_entity_was_changed():
    gateway, document, manager = polygon_fixture()
    existing = manager.line((0.1, 0.2, 0), (0.2, 0.3, 0))
    original = manager.CreatePolygon

    def mutate_existing(*args):
        result = original(*args)
        existing.start[0] += 0.01
        return result

    manager.CreatePolygon = mutate_existing
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert error.value.evidence["requires_review"]
    assert not error.value.evidence["rollback_verified"]
    assert document.delete_calls == 0 and len(document.sketch.segments) == 7
    assert not manager.line_calls


def test_polygon_checks_selection_identity_not_only_count_before_delete():
    gateway, document, manager = polygon_fixture(native=False, line_failure=2)
    existing = manager.line((0.1, 0.2, 0), (0.2, 0.3, 0))
    document.SelectionManager.GetSelectedObject6 = lambda *_: existing
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert error.value.evidence["requires_review"]
    assert document.delete_calls == 0 and existing in document.sketch.segments


def test_polygon_refuses_to_delete_unowned_concurrent_new_geometry():
    gateway, document, manager = polygon_fixture()
    original = manager.CreatePolygon

    def concurrent(*args):
        result = original(*args)
        manager.line((0.1, 0.2, 0), (0.2, 0.3, 0))  # Not returned by this API call.
        return result

    manager.CreatePolygon = concurrent
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert error.value.evidence["requires_review"]
    assert document.delete_calls == 0 and len(document.sketch.segments) == 7


def test_polygon_exception_after_unreported_mutation_does_not_grant_deletion_ownership():
    gateway, document, manager = polygon_fixture()
    original = manager.CreatePolygon

    def uncertain(*args):
        original(*args)
        manager.line((0.1, 0.2, 0), (0.2, 0.3, 0))
        raise RuntimeError("COM call returned no created-entity references")

    manager.CreatePolygon = uncertain
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert error.value.evidence["requires_review"]
    assert document.delete_calls == 0 and not manager.line_calls


def test_polygon_does_not_treat_edit_delete_return_as_restoration_proof():
    gateway, document, _manager = polygon_fixture(native=False, line_failure=2)
    document.EditDelete = lambda: True  # A proxy may report success without deleting.
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert not error.value.evidence["rollback_verified"]
    assert error.value.evidence["requires_review"] and len(document.sketch.segments) == 1


def test_polygon_refuses_geometry_cleanup_after_active_document_switch():
    gateway, document, manager = polygon_fixture()
    original = manager.CreatePolygon

    def switch(*args):
        result = original(*args)
        gateway._sw.ActiveDoc = Document()
        return result

    manager.CreatePolygon = switch
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert error.value.evidence["requires_review"]
    assert document.delete_calls == 0 and not manager.line_calls


@pytest.mark.parametrize("change", ["transform", "unreadable", "flags"])
def test_polygon_preflight_failure_performs_no_write(change):
    gateway, document, manager = polygon_fixture()
    if change == "transform":
        document.sketch.ModelToSketchTransform = Transform(translation=(0, 0, 0.1))
    elif change == "unreadable":
        document.sketch.GetSketchSegments = lambda: object()
    else:
        manager.AddToDB = "unknown"
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert not error.value.evidence["mutation_attempted"]
    assert not manager.native_calls and not manager.line_calls and document.delete_calls == 0


def test_polygon_rejects_unproven_sketch_manager_flag_restoration():
    gateway, document, manager = polygon_fixture(native=False)

    class StuckManager(type(manager)):
        @property
        def AddToDB(self):
            return self.__dict__["AddToDB"]

        @AddToDB.setter
        def AddToDB(self, value):
            if value:
                self.__dict__["AddToDB"] = True

    manager.__class__ = StuckManager
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(0, 0, 2, 6, 0)
    assert error.value.evidence["rollback_verified"]
    assert error.value.evidence["sketch_manager_flags_restored"] is False
    assert error.value.evidence["requires_review"] and document.sketch.segments == []


def test_polygon_validates_native_construction_circle_geometry():
    gateway, document, manager = polygon_fixture()
    original = manager.CreatePolygon

    def with_circle(*args):
        result = original(*args)
        circle = Circle(100, center=args[:3], radius=0.002)
        circle.ConstructionGeometry = True
        document.sketch.segments.append(circle)
        return result

    manager.CreatePolygon = with_circle
    result = gateway._polygon(0, 0, 2, 6, 0)
    assert result["segment_count"] == 7 and result["boundary_segment_count"] == 6


def test_polygon_does_not_accept_silent_math_point_input_corruption():
    gateway, _document, manager = polygon_fixture()
    gateway._sw.GetMathUtility().CreatePoint = lambda _: SimpleNamespace(ArrayData=[0, 0, 0])
    with pytest.raises(PolygonCreationError) as error:
        gateway._polygon(12, 74, 3, 6, 0)
    assert not error.value.evidence["mutation_attempted"] and not manager.native_calls
