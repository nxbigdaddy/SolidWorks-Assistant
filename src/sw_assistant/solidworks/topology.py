from __future__ import annotations

import base64
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


class TopologyResolutionError(RuntimeError):
    """A stored geometric intent no longer identifies one unique entity."""


def _value(obj: Any, name: str, default: Any = None) -> Any:
    try:
        member = getattr(obj, name)
        if hasattr(member, "_oleobj_"):
            return member
        return member() if callable(member) else member
    except Exception:
        return default


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        return list(value)
    return [value]


def _vector(value: Any, length: int = 3) -> tuple[float, ...] | None:
    values = _items(value)
    if len(values) < length:
        return None
    try:
        result = tuple(float(item) for item in values[:length])
    except (TypeError, ValueError):
        return None
    return result if all(math.isfinite(item) for item in result) else None


def _normalize(value: tuple[float, float, float]) -> tuple[float, float, float] | None:
    length = math.sqrt(sum(item * item for item in value))
    if length <= 1e-15:
        return None
    return tuple(item / length for item in value)  # type: ignore[return-value]


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(left[index] * right[index] for index in range(3))


def _distance(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return math.sqrt(sum((left[index] - right[index]) ** 2 for index in range(3)))


def _reference_bytes(value: Any) -> bytes:
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, (tuple, list)):
        output = bytearray()
        for item in value:
            if isinstance(item, int) and 0 <= item <= 255:
                output.append(item)
            elif isinstance(item, (bytes, bytearray)):
                output.extend(item)
        return bytes(output)
    return b""


def document_identity(document: Any) -> tuple[str, str, str]:
    path = str(_value(document, "GetPathName", "") or "")
    title = str(_value(document, "GetTitle", "") or "")
    configuration = ""
    try:
        active = document.ConfigurationManager.ActiveConfiguration
        configuration = str(_value(active, "Name", "") or "")
    except Exception:
        pass
    return path, title, configuration


@dataclass(frozen=True, slots=True)
class PlanarFaceCandidate:
    face: Any = field(compare=False, repr=False)
    center_m: tuple[float, float, float]
    normal: tuple[float, float, float]
    area_m2: float
    box_m: tuple[float, float, float, float, float, float]


@dataclass(frozen=True, slots=True)
class PlanarFaceSignature:
    document_path: str
    document_title: str
    configuration: str
    source_feature_name: str
    center_m: tuple[float, float, float]
    normal: tuple[float, float, float]
    area_m2: float
    box_m: tuple[float, float, float, float, float, float]
    persistent_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SketchLineSignature:
    document_path: str
    document_title: str
    configuration: str
    sketch_feature_name: str
    start_m: tuple[float, float, float]
    end_m: tuple[float, float, float]
    construction: bool
    segment_id: tuple[int, int] | None = None
    segment_type: int | None = None
    sketch_persistent_reference: str = ""
    persistent_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SketchSessionSignature:
    document_path: str
    document_title: str
    configuration: str
    sketch_feature_name: str
    sketch_persistent_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ResolvedFace:
    face: Any = field(compare=False, repr=False)
    strategy: str
    candidate_count: int


@dataclass(frozen=True, slots=True)
class ResolvedSketchLine:
    sketch_feature: Any = field(compare=False, repr=False)
    line: Any = field(compare=False, repr=False)
    strategy: str
    candidate_count: int


def planar_face_candidate(face: Any) -> PlanarFaceCandidate | None:
    surface = _value(face, "GetSurface")
    if surface is None or not bool(_value(surface, "IsPlane", False)):
        return None
    box = _vector(_value(face, "GetBox"), 6)
    normal = _vector(_value(face, "Normal"), 3)
    if box is None or normal is None:
        return None
    normalized = _normalize(normal)  # type: ignore[arg-type]
    if normalized is None:
        return None
    center = tuple((box[index] + box[index + 3]) / 2.0 for index in range(3))
    try:
        area = float(_value(face, "GetArea", 0.0) or 0.0)
    except (TypeError, ValueError):
        area = 0.0
    if not math.isfinite(area) or area < 0.0:
        area = 0.0
    return PlanarFaceCandidate(
        face=face,
        center_m=center,  # type: ignore[arg-type]
        normal=normalized,
        area_m2=area,
        box_m=box,  # type: ignore[arg-type]
    )


def feature_planar_faces(feature: Any) -> list[PlanarFaceCandidate]:
    candidates: list[PlanarFaceCandidate] = []
    for face in _items(_value(feature, "GetFaces")):
        candidate = planar_face_candidate(face)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _feature_by_name(document: Any, name: str) -> Any:
    try:
        feature = document.FeatureByName(name)
        if feature is not None:
            return feature
    except Exception:
        pass
    feature = _value(document, "FirstFeature")
    for _ in range(10000):
        if feature is None:
            break
        if str(_value(feature, "Name", "") or "").casefold() == name.casefold():
            return feature
        feature = _value(feature, "GetNextFeature")
    return None


def _ensure_unique_projection(
    ranked: list[tuple[float, PlanarFaceCandidate]],
    *,
    tolerance_m: float,
) -> PlanarFaceCandidate:
    if not ranked:
        raise TopologyResolutionError("特征没有可用于后续草图的平面端面。")
    ranked.sort(key=lambda item: item[0], reverse=True)
    if len(ranked) > 1 and math.isclose(
        ranked[0][0], ranked[1][0], rel_tol=1e-8, abs_tol=tolerance_m
    ):
        raise TopologyResolutionError(
            "存在多个同等匹配的平面端面，拒绝按面序号或偶然枚举顺序选择。"
        )
    return ranked[0][1]


def capture_terminal_face_signature(
    document: Any,
    feature: Any,
    frame: dict[str, tuple[float, ...]],
    depth_m: float,
    *,
    reverse: bool,
) -> PlanarFaceSignature:
    origin = _vector(frame.get("origin"), 3) or (0.0, 0.0, 0.0)
    raw_normal = _vector(frame.get("normal"), 3) or (0.0, 0.0, 1.0)
    sketch_normal = _normalize(raw_normal)  # type: ignore[arg-type]
    if sketch_normal is None:
        raise TopologyResolutionError("草图平面法向量无效。")
    direction = -1.0 if reverse else 1.0
    candidates = feature_planar_faces(feature)
    tolerance = max(abs(depth_m) * 1e-7, 1e-9)
    preferred = [
        (direction * _dot(tuple(c.center_m[index] - origin[index] for index in range(3)), sketch_normal), c)
        for c in candidates
    ]
    forward = [item for item in preferred if item[0] > max(abs(depth_m) * 0.25, tolerance)]
    selected = _ensure_unique_projection(forward or preferred, tolerance_m=tolerance)

    payload = b""
    try:
        payload = _reference_bytes(document.Extension.GetPersistReference3(selected.face))
    except Exception:
        pass
    path, title, configuration = document_identity(document)
    return PlanarFaceSignature(
        document_path=path,
        document_title=title,
        configuration=configuration,
        source_feature_name=str(_value(feature, "Name", "") or ""),
        center_m=selected.center_m,
        normal=selected.normal,
        area_m2=selected.area_m2,
        box_m=selected.box_m,
        persistent_reference=base64.b64encode(payload).decode("ascii") if payload else "",
    )


def _same_document(
    document: Any,
    signature: PlanarFaceSignature | SketchLineSignature | SketchSessionSignature,
) -> bool:
    path, title, configuration = document_identity(document)
    if signature.document_path:
        if not path or signature.document_path.casefold() != path.casefold():
            return False
    elif signature.document_title.casefold() != title.casefold():
        return False
    return not signature.configuration or signature.configuration.casefold() == configuration.casefold()


def _document_context_mismatch(
    document: Any,
    signature: PlanarFaceSignature | SketchLineSignature | SketchSessionSignature,
) -> str:
    """Describe a confirmed document/configuration identity mismatch."""

    path, title, configuration = document_identity(document)
    if signature.document_path:
        if not path:
            return "当前文档路径不可用"
        if signature.document_path.casefold() != path.casefold():
            return "活动文档路径已切换"
    elif signature.document_title.casefold() != title.casefold():
        return "活动文档标题已切换"
    if signature.configuration and signature.configuration.casefold() != configuration.casefold():
        return "活动配置已切换或当前配置不可识别"
    return ""


def capture_sketch_session_signature(
    document: Any, sketch: Any
) -> SketchSessionSignature:
    path, title, configuration = document_identity(document)
    if not path and not title:
        raise TopologyResolutionError("无法识别活动草图所属文档。")
    sketch_name, payload = _sketch_identity(document, sketch)
    if not sketch_name and not payload:
        raise TopologyResolutionError("无法取得活动草图的名称或持久引用。")
    return SketchSessionSignature(
        path,
        title,
        configuration,
        sketch_name,
        base64.b64encode(payload).decode("ascii") if payload else "",
    )


def validate_sketch_session(
    document: Any, sketch: Any, signature: SketchSessionSignature
) -> None:
    mismatch = _document_context_mismatch(document, signature)
    if mismatch:
        raise TopologyResolutionError(
            f"活动文档或配置已切换，草图坐标上下文已经失效（{mismatch}）。"
        )
    sketch_name, current_reference = _sketch_identity(document, sketch)
    if signature.sketch_persistent_reference:
        try:
            expected = base64.b64decode(
                signature.sketch_persistent_reference, validate=True
            )
            if current_reference and current_reference == expected:
                return
            if current_reference:
                raise TopologyResolutionError(
                    "活动草图已切换，不能继续使用原草图坐标。"
                )
        except TopologyResolutionError:
            raise
        except Exception:
            pass
    if (
        not signature.sketch_feature_name
        or not sketch_name
        or sketch_name.casefold() != signature.sketch_feature_name.casefold()
    ):
        raise TopologyResolutionError("活动草图已切换或被重命名，不能继续使用原草图坐标。")


def _sketch_identity(document: Any, sketch: Any) -> tuple[str, bytes]:
    sketch_name = str(_value(sketch, "Name", "") or "")
    legacy_feature = _value(sketch, "GetFeature")
    if not sketch_name:
        sketch_name = str(_value(legacy_feature, "Name", "") or "")
    for candidate in (sketch, legacy_feature):
        if candidate is None:
            continue
        try:
            payload = _reference_bytes(document.Extension.GetPersistReference3(candidate))
        except Exception:
            continue
        if payload:
            return sketch_name, payload
    return sketch_name, b""


def _match_error(candidate: PlanarFaceCandidate, signature: PlanarFaceSignature) -> float | None:
    diagonal = _distance(signature.box_m[:3], signature.box_m[3:])
    position_tolerance = max(5e-7, diagonal * 1e-5)
    position_error = _distance(candidate.center_m, signature.center_m)
    alignment = abs(_dot(candidate.normal, signature.normal))
    area_scale = max(signature.area_m2, 1e-12)
    area_error = abs(candidate.area_m2 - signature.area_m2) / area_scale
    if position_error > position_tolerance or alignment < 0.99999 or area_error > 1e-4:
        return None
    return position_error / position_tolerance + (1.0 - alignment) * 1000.0 + area_error


def resolve_planar_face(
    document: Any,
    signature: PlanarFaceSignature,
    persistent_resolver: Callable[[bytes], Any] | None = None,
) -> ResolvedFace:
    if not _same_document(document, signature):
        raise TopologyResolutionError("活动文档或配置已改变，不能复用上一特征的端面目标。")

    if signature.persistent_reference and persistent_resolver is not None:
        try:
            payload = base64.b64decode(signature.persistent_reference, validate=True)
            resolved = persistent_resolver(payload)
            candidate = planar_face_candidate(resolved) if resolved is not None else None
            if candidate is not None and _match_error(candidate, signature) is not None:
                return ResolvedFace(candidate.face, "persistent-reference", 1)
        except Exception:
            pass

    feature = _feature_by_name(document, signature.source_feature_name)
    if feature is None:
        raise TopologyResolutionError(
            f"源特征 {signature.source_feature_name or '<未命名>'} 已不存在或被重命名。"
        )
    candidates = feature_planar_faces(feature)
    matches: list[tuple[float, PlanarFaceCandidate]] = []
    for candidate in candidates:
        error = _match_error(candidate, signature)
        if error is not None:
            matches.append((error, candidate))
    matches.sort(key=lambda item: item[0])
    if not matches:
        raise TopologyResolutionError(
            "重建后找不到与几何签名一致的端面；拓扑可能已经改变。"
        )
    if len(matches) > 1 and math.isclose(matches[0][0], matches[1][0], abs_tol=1e-6):
        raise TopologyResolutionError(
            "重建后有多个端面同时匹配几何签名，拒绝猜测选择。"
        )
    return ResolvedFace(matches[0][1].face, "geometric-signature", len(candidates))


def _sketch_point_xyz(point: Any) -> tuple[float, float, float] | None:
    if point is None:
        return None
    try:
        result = tuple(float(_value(point, name)) for name in ("X", "Y", "Z"))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(item) for item in result):
        return None
    return result  # type: ignore[return-value]


def sketch_line_endpoints(
    line: Any,
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    start = _sketch_point_xyz(_value(line, "GetStartPoint2"))
    end = _sketch_point_xyz(_value(line, "GetEndPoint2"))
    if start is None or end is None or _distance(start, end) <= 1e-12:
        return None
    return start, end


def _sketch_segment_id(line: Any) -> tuple[int, int] | None:
    values = _items(_value(line, "GetID"))
    if len(values) != 2:
        return None
    try:
        return int(values[0]), int(values[1])
    except (TypeError, ValueError):
        return None


def _sketch_segment_type(line: Any) -> int | None:
    value = _value(line, "GetType")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def capture_sketch_line_signature(
    document: Any,
    sketch: Any,
    line: Any,
    *,
    fallback_start_m: tuple[float, float, float] | None = None,
    fallback_end_m: tuple[float, float, float] | None = None,
) -> SketchLineSignature:
    feature_name, sketch_payload = _sketch_identity(document, sketch)
    if not feature_name and not sketch_payload:
        raise TopologyResolutionError("无法取得中心线所属草图的名称或持久引用。")

    endpoints = sketch_line_endpoints(line)
    if endpoints is None and fallback_start_m is not None and fallback_end_m is not None:
        if _distance(fallback_start_m, fallback_end_m) > 1e-12:
            endpoints = fallback_start_m, fallback_end_m
    if endpoints is None:
        raise TopologyResolutionError("无法读取中心线的两个端点。")

    payload = b""
    try:
        payload = _reference_bytes(document.Extension.GetPersistReference3(line))
    except Exception:
        pass
    path, title, configuration = document_identity(document)
    return SketchLineSignature(
        document_path=path,
        document_title=title,
        configuration=configuration,
        sketch_feature_name=feature_name,
        start_m=endpoints[0],
        end_m=endpoints[1],
        construction=bool(_value(line, "ConstructionGeometry", False)),
        segment_id=_sketch_segment_id(line),
        segment_type=_sketch_segment_type(line),
        sketch_persistent_reference=(
            base64.b64encode(sketch_payload).decode("ascii") if sketch_payload else ""
        ),
        persistent_reference=base64.b64encode(payload).decode("ascii") if payload else "",
    )


def _sketch_line_match_error(line: Any, signature: SketchLineSignature) -> float | None:
    if bool(_value(line, "ConstructionGeometry", False)) is not signature.construction:
        return None
    candidate_type = _sketch_segment_type(line)
    if signature.segment_type is not None and candidate_type != signature.segment_type:
        return None
    candidate_id = _sketch_segment_id(line)
    if signature.segment_id is not None and candidate_id == signature.segment_id:
        return 0.0
    endpoints = sketch_line_endpoints(line)
    if endpoints is None:
        return None
    length = _distance(signature.start_m, signature.end_m)
    tolerance = max(1e-7, length * 1e-6)
    direct = max(
        _distance(endpoints[0], signature.start_m),
        _distance(endpoints[1], signature.end_m),
    )
    reverse = max(
        _distance(endpoints[0], signature.end_m),
        _distance(endpoints[1], signature.start_m),
    )
    error = min(direct, reverse)
    return error / tolerance if error <= tolerance else None


def resolve_sketch_line(
    document: Any,
    signature: SketchLineSignature,
    persistent_resolver: Callable[[bytes], Any] | None = None,
) -> ResolvedSketchLine:
    if not _same_document(document, signature):
        raise TopologyResolutionError("活动文档或配置已改变，不能复用旋转中心线目标。")

    sketch_feature = None
    sketch = None
    if signature.sketch_persistent_reference and persistent_resolver is not None:
        try:
            sketch_payload = base64.b64decode(
                signature.sketch_persistent_reference, validate=True
            )
            candidate = persistent_resolver(sketch_payload)
            specific = _value(candidate, "GetSpecificFeature2")
            if specific is not None:
                sketch_feature = candidate
                sketch = specific
            elif hasattr(candidate, "GetSketchSegments"):
                sketch = candidate
        except Exception:
            pass
    if sketch_feature is None and signature.sketch_feature_name:
        sketch_feature = _feature_by_name(document, signature.sketch_feature_name)
    if sketch_feature is None:
        raise TopologyResolutionError(
            f"草图特征 {signature.sketch_feature_name or '<编辑中未命名>'} 已不存在或无法重新解析。"
        )
    if sketch is None:
        sketch = _value(sketch_feature, "GetSpecificFeature2")
    if sketch is None:
        raise TopologyResolutionError("草图特征无法转换为 ISketch，不能重新定位中心线。")
    candidates = _items(_value(sketch, "GetSketchSegments"))

    if signature.persistent_reference and persistent_resolver is not None:
        try:
            payload = base64.b64decode(signature.persistent_reference, validate=True)
            resolved = persistent_resolver(payload)
            if resolved is not None and _sketch_line_match_error(resolved, signature) is not None:
                return ResolvedSketchLine(
                    sketch_feature, resolved, "persistent-reference", len(candidates)
                )
        except Exception:
            pass

    matches: list[tuple[float, Any]] = []
    for candidate in candidates:
        error = _sketch_line_match_error(candidate, signature)
        if error is not None:
            matches.append((error, candidate))
    matches.sort(key=lambda item: item[0])
    if not matches:
        raise TopologyResolutionError("重建后找不到与端点签名一致的旋转中心线。")
    if len(matches) > 1 and math.isclose(matches[0][0], matches[1][0], abs_tol=1e-6):
        raise TopologyResolutionError("重建后有多条中心线匹配端点签名，拒绝猜测选择。")
    return ResolvedSketchLine(
        sketch_feature, matches[0][1], "geometric-signature", len(candidates)
    )
