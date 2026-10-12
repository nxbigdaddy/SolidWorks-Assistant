"""Native 2D coordinate semantics: local fakes only, never attach to CAD."""
from __future__ import annotations

import math

import pytest

from polygon_fakes import PolygonManager, Transform, polygon_fixture
from test_circle_repair import Circle, Document

from sw_assistant.solidworks.gateway import SketchCreationError
from sw_assistant.solidworks.topology import resolve_sketch_line


class RigidTransform:
    """Model -> sketch with arbitrary rotation and a translated sketch origin."""

    def __init__(self, matrix, translation=(0, 0, 0)):
        self.matrix, self.translation = matrix, translation
        self.ArrayData = [value for row in matrix for value in row] + list(translation) + [1, 0, 0, 0]

    def convert(self, point):
        return [sum(a * b for a, b in zip(row, point, strict=True)) + offset
                for row, offset in zip(self.matrix, self.translation, strict=True)]

    def Inverse(self):
        transposed = [list(row) for row in zip(*self.matrix, strict=True)]
        shift = [-sum(a * b for a, b in zip(row, self.translation, strict=True)) for row in transposed]
        return RigidTransform(transposed, shift)


class Native2DManager(PolygonManager):
    def __init__(self, document):
        super().__init__(document)
        self.calls = []
        self.offset = (0, 0, 0)
        self.reverse = False
        self.noop = False
        self.diagonals = True

    def local(self, point):
        # In a 2D sketch, Z is not an extra in-plane coordinate. This fake
        # exposes the old public->world-only bug instead of echoing its inputs.
        return [point[0] + self.offset[0], point[1] + self.offset[1], self.offset[2]]

    def record(self, name, arguments):
        assert self.AddToDB and not self.DisplayWhenAdded
        self.calls.append((name, arguments))

    def CreateLine(self, *arguments):
        self.record("line", arguments)
        start, end = self.local(arguments[:3]), self.local(arguments[3:])
        if self.noop or start == end:
            return None
        if self.reverse:
            start, end = end, start
        return self.line(start, end)

    def CreateCenterLine(self, *arguments):
        self.record("centerline", arguments)
        entity = self.line(self.local(arguments[:3]), self.local(arguments[3:]))
        entity.ConstructionGeometry = True
        return entity

    def CreateCircleByRadius(self, *arguments):
        self.record("circle", arguments)
        circle = Circle(self.next_id, self.local(arguments[:3]), arguments[3])
        self.next_id += 1
        self.ActiveSketch.segments.append(circle)
        return circle

    def CreateCenterRectangle(self, *arguments):
        self.record("rectangle", arguments)
        cx, cy, _ = self.local(arguments[:3])
        x, y, _ = self.local(arguments[3:])
        if x == cx or y == cy:
            return None
        points = [(2 * cx - x, 2 * cy - y, 0), (x, 2 * cy - y, 0),
                  (x, y, 0), (2 * cx - x, y, 0)]
        result = [self.line(point, points[(index + 1) % 4]) for index, point in enumerate(points)]
        if self.diagonals:
            for index in range(2):
                diagonal = self.line(points[index], points[index + 2])
                diagonal.ConstructionGeometry = True
                result.append(diagonal)
        return result


def fixture(plane="front", *, rotated=False, translated=False):
    transform = {"front": Transform(), "top": Transform((0, 2, 1), (1, -1, 1)),
                 "right": Transform((2, 1, 0), (-1, 1, 1))}[plane]
    gateway, document, _ = polygon_fixture(plane=plane, transform=transform)
    if rotated:
        angle = math.radians(37)
        c, s = math.cos(angle), math.sin(angle)
        transform = RigidTransform([[c, s, 0], [-s, c, 0], [0, 0, 1]])
    if translated:
        origin = [0.03, -0.02, 0.08]
        matrix = transform.matrix if isinstance(transform, RigidTransform) else [
            [transform.signs[row] * float(col == transform.axes[row]) for col in range(3)] for row in range(3)]
        shift = [-sum(a * b for a, b in zip(row, origin, strict=True)) for row in matrix]
        transform = RigidTransform(matrix, shift)
        gateway._sketch_frame["origin"] = origin
    document.sketch.ModelToSketchTransform = transform
    manager = Native2DManager(document)
    document.SketchManager = manager
    return gateway, document, manager


def execute(gateway, kind):
    if kind == "circle":
        return gateway._circle(60, -250, 15)
    if kind == "rectangle":
        return gateway._rectangle(0, -250, 120, 30)
    return gateway._line(60, -265, 60, -235, centerline=kind == "centerline")


@pytest.mark.parametrize("kind", ["line", "centerline", "rectangle", "circle"])
@pytest.mark.parametrize("plane,rotated,translated", [
    ("front", False, False), ("top", False, False), ("right", False, False),
    ("top", False, True), ("front", True, False), ("front", True, True),
])
def test_each_primitive_preserves_public_world_contract_and_proves_actual_native_geometry(kind, plane, rotated, translated):
    gateway, document, manager = fixture(plane, rotated=rotated, translated=translated)
    result = execute(gateway, kind)
    assert result["geometry_verified"] and result["creation_verified"]
    assert result["sketch_manager_flags_restored"] and not result["requires_review"]
    assert manager.AddToDB is False and manager.DisplayWhenAdded is True
    assert result["actual_geometry"]
    if kind in {"line", "centerline"}:
        assert result["start_m"] == pytest.approx(gateway._sketch_point(60, -265))
        assert result["end_m"] == pytest.approx(gateway._sketch_point(60, -235))
        if kind == "centerline":
            actual = document.sketch.segments[0]
            assert gateway._last_centerline.start_m == pytest.approx(actual.start)
            assert gateway._last_centerline.end_m == pytest.approx(actual.end)
            assert gateway._last_centerline.construction is True
    elif kind == "circle":
        assert result["center_m"] == pytest.approx(gateway._sketch_point(60, -250))
        assert result["radius_m"] == pytest.approx(0.015)
        assert result["entity_ref"] in gateway._owned_circles
        assert result["center_sketch_m"] == pytest.approx(document.sketch.segments[0].center)
    else:
        assert result["center_m"] == pytest.approx(gateway._sketch_point(0, -250))
        assert result["corner_m"] == pytest.approx(gateway._sketch_point(60, -235))
        assert result["boundary_segment_count"] == 4
        assert result["backend"] == ("transformed-four-edges" if rotated else "native-center-rectangle")


def test_top_handle_horizontal_and_vertical_no_longer_project_to_origin_or_zero_length():
    gateway, document, _ = fixture("top")
    first = gateway._line(-60, -265, 60, -265, centerline=False)
    second = gateway._line(60, -265, 60, -235, centerline=False)
    assert document.sketch.segments[0].start == pytest.approx([-.06, -.265, 0])
    assert document.sketch.segments[0].end == pytest.approx([.06, -.265, 0])
    assert document.sketch.segments[1].end == pytest.approx([.06, -.235, 0])
    assert first["start_m"] == pytest.approx([-.06, 0, .265])
    assert second["end_m"] == pytest.approx([.06, 0, .235])


@pytest.mark.parametrize("kind", ["line", "centerline", "rectangle", "circle"])
def test_success_values_are_actual_readback_not_input_echo(kind):
    gateway, _, manager = fixture("top")
    manager.offset = (2e-10, 0, 0)
    result = execute(gateway, kind)
    key = "center_m" if kind in {"rectangle", "circle"} else "start_m"
    expected_x = 0.0 if kind == "rectangle" else .06
    assert result[key][0] == pytest.approx(expected_x + 2e-10, abs=1e-15)
    assert result[key][0] != expected_x


@pytest.mark.parametrize("kind", ["line", "centerline", "rectangle", "circle"])
def test_wrong_actual_geometry_fails_closed_without_replay_or_blind_cleanup(kind):
    gateway, document, manager = fixture("top")
    manager.offset = (.01, 0, 0)
    with pytest.raises(SketchCreationError) as error:
        execute(gateway, kind)
    evidence = error.value.evidence
    assert evidence["mutation_attempted"] and evidence["requires_review"]
    assert not evidence["geometry_verified"] and not evidence["rollback_verified"]
    assert evidence["actual_geometry"] and evidence["api_points_m"]
    assert evidence["coordinate_space"]["actual_geometry"] == "sketch"
    assert len(manager.calls) == 1 and document.delete_calls == 0
    assert not gateway._owned_circles and gateway._last_centerline is None


@pytest.mark.parametrize("change", ["transform", "off_plane", "unreadable", "scale"])
def test_invalid_preflight_never_writes(change):
    gateway, document, manager = fixture("top")
    if change == "transform":
        document.sketch.ModelToSketchTransform = object()
    elif change == "off_plane":
        gateway._sketch_frame["origin"] = [0, .03, 0]
    elif change == "unreadable":
        document.sketch.GetSketchSegments = lambda: object()
    else:
        document.sketch.ModelToSketchTransform.ArrayData[12] = 2
    with pytest.raises(SketchCreationError) as error:
        execute(gateway, "line")
    assert not error.value.evidence["mutation_attempted"]
    assert not error.value.evidence["requires_review"] and not manager.calls


@pytest.mark.parametrize("change", ["existing", "unreadable", "document", "transform", "return_none", "extra", "construction"])
def test_postflight_detects_unknown_or_changed_geometry_and_never_replays(change):
    gateway, document, manager = fixture("top")
    existing = manager.line([.1, .1, 0], [.2, .1, 0])
    original = manager.CreateLine

    def alter(*args):
        result = original(*args)
        if change == "existing":
            existing.start[0] += .01
        elif change == "unreadable":
            result.GetStartPoint2 = lambda: None
        elif change == "document":
            gateway._sw.ActiveDoc = Document()
        elif change == "transform":
            document.sketch.ModelToSketchTransform.ArrayData[9] += .001
        elif change == "return_none":
            return None
        elif change == "extra":
            manager.line([.2, .2, 0], [.3, .3, 0])
        else:
            result.ConstructionGeometry = True
        return result

    manager.CreateLine = alter
    with pytest.raises(SketchCreationError) as error:
        execute(gateway, "line")
    assert error.value.evidence["requires_review"]
    assert len(manager.calls) == 1 and document.delete_calls == 0


def test_centerline_signature_resolves_in_native_coordinates_after_closing_sketch():
    gateway, document, _ = fixture("right")
    execute(gateway, "centerline")
    signature = gateway._last_centerline
    document.SketchManager.ActiveSketch = None
    document.FeatureByName = lambda _name: document.sketch.feature
    resolved = resolve_sketch_line(document, signature)
    assert resolved is not None
    assert signature.start_m == pytest.approx([.265, .06, 0])
    assert signature.end_m == pytest.approx([.235, .06, 0])


def test_reversed_native_endpoints_keep_requested_public_orientation():
    gateway, _, manager = fixture("top")
    manager.reverse = True
    result = execute(gateway, "line")
    assert result["start_m"] == pytest.approx(gateway._sketch_point(60, -265))
    assert result["end_m"] == pytest.approx(gateway._sketch_point(60, -235))


def test_noop_has_one_attempt_and_does_not_enter_a_retry_loop():
    gateway, _, manager = fixture()
    manager.noop = True
    with pytest.raises(SketchCreationError) as error:
        execute(gateway, "line")
    assert len(manager.calls) == 1 and error.value.evidence["requires_review"]
    assert error.value.evidence["actual_geometry"] == []
