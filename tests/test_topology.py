from __future__ import annotations

import pytest

from sw_assistant.solidworks.topology import (
    TopologyResolutionError,
    capture_sketch_line_signature,
    capture_sketch_session_signature,
    capture_terminal_face_signature,
    resolve_planar_face,
    resolve_sketch_line,
    validate_sketch_session,
)


class Surface:
    @staticmethod
    def IsPlane():
        return True


class Face:
    def __init__(self, z: float, normal: tuple[float, float, float], area: float = 0.001):
        self.z = z
        self.Normal = normal
        self.area = area

    @staticmethod
    def GetSurface():
        return Surface()

    def GetBox(self):
        return (-0.01, -0.01, self.z, 0.01, 0.01, self.z)

    def GetArea(self):
        return self.area


class Feature:
    Name = "Boss-Extrude1"

    def __init__(self, faces):
        self.faces = faces

    def GetFaces(self):
        return self.faces


class Document:
    def __init__(self, feature):
        self.feature = feature

    @staticmethod
    def GetPathName():
        return r"C:\models\part.sldprt"

    @staticmethod
    def GetTitle():
        return "part.sldprt"

    def FeatureByName(self, name):
        return self.feature if name == self.feature.Name else None


FRAME = {
    "origin": (0.0, 0.0, 0.0),
    "u": (1.0, 0.0, 0.0),
    "v": (0.0, 1.0, 0.0),
    "normal": (0.0, 0.0, 1.0),
}


def test_terminal_face_is_reacquired_after_rebuild_instead_of_reusing_com_object():
    original_start = Face(0.0, (0.0, 0.0, -1.0))
    original_end = Face(0.03, (0.0, 0.0, 1.0))
    original_feature = Feature([original_start, original_end])
    document = Document(original_feature)

    signature = capture_terminal_face_signature(
        document, original_feature, FRAME, 0.03, reverse=False
    )

    rebuilt_end = Face(0.03, (0.0, 0.0, 1.0))
    document.feature = Feature([Face(0.0, (0.0, 0.0, -1.0)), rebuilt_end])
    resolved = resolve_planar_face(document, signature)

    assert resolved.face is rebuilt_end
    assert resolved.face is not original_end
    assert resolved.strategy == "geometric-signature"


def test_terminal_face_capture_refuses_ambiguous_coplanar_faces():
    feature = Feature(
        [
            Face(0.03, (0.0, 0.0, 1.0), area=0.0005),
            Face(0.03, (0.0, 0.0, 1.0), area=0.0005),
        ]
    )

    with pytest.raises(TopologyResolutionError, match="多个同等匹配"):
        capture_terminal_face_signature(Document(feature), feature, FRAME, 0.03, reverse=False)


def test_persistent_reference_is_only_accepted_when_geometry_still_matches():
    original = Face(0.03, (0.0, 0.0, 1.0))
    feature = Feature([Face(0.0, (0.0, 0.0, -1.0)), original])
    signature = capture_terminal_face_signature(Document(feature), feature, FRAME, 0.03, reverse=False)
    signature = type(signature)(
        **{**signature.to_dict(), "persistent_reference": "AQID"}
    )
    replacement = Face(0.03, (0.0, 0.0, 1.0))
    document = Document(Feature([Face(0.0, (0.0, 0.0, -1.0)), replacement]))
    wrong_face = Face(0.05, (0.0, 0.0, 1.0))

    resolved = resolve_planar_face(document, signature, lambda _payload: wrong_face)

    assert resolved.face is replacement
    assert resolved.strategy == "geometric-signature"


class SketchPoint:
    def __init__(self, x: float, y: float, z: float):
        self.X = x
        self.Y = y
        self.Z = z


class SketchLine:
    ConstructionGeometry = True

    def __init__(
        self,
        start: tuple[float, float, float],
        end: tuple[float, float, float],
    ):
        self.start = SketchPoint(*start)
        self.end = SketchPoint(*end)

    def GetStartPoint2(self):
        return self.start

    def GetEndPoint2(self):
        return self.end


class SketchFeature:
    Name = "Sketch1"

    def __init__(self, lines):
        self.lines = lines

    def GetSpecificFeature2(self):
        lines = self.lines

        class SpecificSketch:
            @staticmethod
            def GetSketchSegments():
                return lines

        return SpecificSketch()


class ActiveSketch:
    def __init__(self, feature):
        self.feature = feature

    def GetFeature(self):
        return self.feature


class SketchDocument:
    def __init__(self, feature):
        self.feature = feature

    @staticmethod
    def GetPathName():
        return r"C:\models\revolve.sldprt"

    @staticmethod
    def GetTitle():
        return "revolve.sldprt"

    def FeatureByName(self, name):
        return self.feature if name == self.feature.Name else None


def test_centerline_is_reacquired_after_sketch_rebuild():
    original = SketchLine((0.0, -0.01, 0.0), (0.0, 0.01, 0.0))
    feature = SketchFeature([original])
    document = SketchDocument(feature)
    signature = capture_sketch_line_signature(
        document, ActiveSketch(feature), original
    )

    rebuilt = SketchLine((0.0, -0.01, 0.0), (0.0, 0.01, 0.0))
    document.feature = SketchFeature([rebuilt])
    resolved = resolve_sketch_line(document, signature)

    assert resolved.line is rebuilt
    assert resolved.line is not original
    assert resolved.strategy == "geometric-signature"


def test_centerline_resolution_refuses_ambiguous_duplicate_geometry():
    original = SketchLine((0.0, -0.01, 0.0), (0.0, 0.01, 0.0))
    feature = SketchFeature([original])
    document = SketchDocument(feature)
    signature = capture_sketch_line_signature(
        document, ActiveSketch(feature), original
    )
    document.feature = SketchFeature(
        [
            SketchLine((0.0, -0.01, 0.0), (0.0, 0.01, 0.0)),
            SketchLine((0.0, 0.01, 0.0), (0.0, -0.01, 0.0)),
        ]
    )

    with pytest.raises(TopologyResolutionError, match="多条中心线"):
        resolve_sketch_line(document, signature)


def test_centerline_can_be_reacquired_through_generic_sketch_segment_id():
    class GenericSegment:
        ConstructionGeometry = True

        @staticmethod
        def GetID():
            return (17, 4)

        @staticmethod
        def GetType():
            return 0

    original = GenericSegment()
    feature = SketchFeature([original])
    document = SketchDocument(feature)
    signature = capture_sketch_line_signature(
        document,
        ActiveSketch(feature),
        original,
        fallback_start_m=(0.0, -0.01, 0.0),
        fallback_end_m=(0.0, 0.01, 0.0),
    )

    rebuilt = GenericSegment()
    document.feature = SketchFeature([rebuilt])
    resolved = resolve_sketch_line(document, signature)

    assert resolved.line is rebuilt
    assert resolved.strategy == "geometric-signature"


def test_sketch_session_rejects_document_or_active_sketch_drift():
    feature = SketchFeature([])
    first_document = SketchDocument(feature)
    signature = capture_sketch_session_signature(
        first_document, ActiveSketch(feature)
    )

    switched_document = SketchDocument(feature)
    switched_document.GetTitle = lambda: "another-part.sldprt"
    switched_document.GetPathName = lambda: r"C:\models\another-part.sldprt"
    with pytest.raises(TopologyResolutionError, match="活动文档或配置已切换"):
        validate_sketch_session(switched_document, ActiveSketch(feature), signature)

    renamed_feature = SketchFeature([])
    renamed_feature.Name = "Sketch2"
    with pytest.raises(TopologyResolutionError, match="活动草图已切换"):
        validate_sketch_session(
            first_document, ActiveSketch(renamed_feature), signature
        )


def test_unnamed_active_sketch_session_uses_feature_persistent_reference():
    class UnnamedFeature(SketchFeature):
        Name = ""

        def __init__(self, reference):
            super().__init__([])
            self.reference = reference

    class Extension:
        @staticmethod
        def GetPersistReference3(value):
            return value.reference

    feature = UnnamedFeature(b"sketch-one")
    document = SketchDocument(feature)
    document.Extension = Extension()
    signature = capture_sketch_session_signature(document, ActiveSketch(feature))

    assert signature.sketch_feature_name == ""
    assert signature.sketch_persistent_reference
    validate_sketch_session(document, ActiveSketch(feature), signature)

    switched = UnnamedFeature(b"sketch-two")
    with pytest.raises(TopologyResolutionError, match="活动草图已切换"):
        validate_sketch_session(document, ActiveSketch(switched), signature)


def test_unnamed_centerline_reacquires_owning_sketch_by_persistent_reference():
    class GenericSegment:
        ConstructionGeometry = True

        def __init__(self, reference):
            self.reference = reference

        @staticmethod
        def GetID():
            return (31, 7)

        @staticmethod
        def GetType():
            return 0

    class UnnamedFeature(SketchFeature):
        Name = ""

        def __init__(self, lines, reference):
            super().__init__(lines)
            self.reference = reference

    class Extension:
        @staticmethod
        def GetPersistReference3(value):
            return value.reference

    original_line = GenericSegment(b"line")
    original_feature = UnnamedFeature([original_line], b"sketch")
    document = SketchDocument(original_feature)
    document.Extension = Extension()
    signature = capture_sketch_line_signature(
        document,
        ActiveSketch(original_feature),
        original_line,
        fallback_start_m=(0.0, -0.01, 0.0),
        fallback_end_m=(0.0, 0.01, 0.0),
    )

    rebuilt_line = GenericSegment(b"line")
    rebuilt_feature = UnnamedFeature([rebuilt_line], b"sketch")

    def resolve(payload):
        return rebuilt_feature if payload == b"sketch" else rebuilt_line

    resolved = resolve_sketch_line(document, signature, resolve)

    assert resolved.sketch_feature is rebuilt_feature
    assert resolved.line is rebuilt_line
    assert resolved.strategy == "persistent-reference"
