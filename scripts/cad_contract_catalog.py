"""Fixed synthetic contracts and independent numerical oracles; no CAD imports.

Public frame meanings are the product contract, not imported from the gateway.
Native SketchPoint coordinates are decoded independently from the observed
ISketch.ModelToSketchTransform, using SOLIDWORKS' row-vector transform layout.
https://help.solidworks.com/2021/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IMathTransform~ArrayData.html
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

PLANES = ("front", "top", "right")
KINDS = ("line_loop", "rectangle", "circle", "revolve")
CASE_IDS = tuple(f"{plane}.{kind}" for plane in PLANES for kind in KINDS)
MAX_REPEATS = 3
MM_TOLERANCE = 0.0001
VOLUME_TOLERANCE_MM3 = 0.001


def world_mm(plane: str, x: float, y: float, normal: float = 0) -> list[float]:
    if plane == "front":
        return [x, y, normal]
    if plane == "top":
        return [x, normal, -y]
    if plane == "right":
        return [normal, x, y]
    raise ValueError("unknown fixed plane")


def number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("missing or nonfinite numeric evidence")
    return float(value)


def vector(value: Any, size: int) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError("missing coordinate vector")
    return [number(item) for item in value]


def native_to_world_mm(point: Any, transform: Any) -> list[float]:
    """Invert q = p R + t without calling gateway helpers or COM math objects."""
    values = vector(transform, 16)
    native = vector(point, 3)
    rows = [values[index:index + 3] for index in (0, 3, 6)]
    if (not math.isclose(values[12], 1.0, abs_tol=1e-9, rel_tol=1e-9)
            or any(not math.isclose(sum(a * b for a, b in zip(row, other, strict=True)),
                                    float(i == j), abs_tol=1e-9, rel_tol=1e-9)
                   for i, row in enumerate(rows) for j, other in enumerate(rows))):
        raise ValueError("native transform is not a rigid metre frame")
    if abs(native[2]) > MM_TOLERANCE / 1000:
        raise ValueError("point is not in the active 2D sketch plane")
    shifted = [native[index] - values[9 + index] for index in range(3)]
    return [sum(a * b for a, b in zip(row, shifted, strict=True)) * 1000 for row in rows]


def close(first: Any, second: Any, *, tolerance: float = MM_TOLERANCE) -> bool:
    try:
        return abs(number(first) - number(second)) <= tolerance
    except ValueError:
        return False


def points_close(first: Any, second: Any) -> bool:
    return all(close(a, b) for a, b in zip(vector(first, 3), vector(second, 3), strict=True))


@dataclass(frozen=True)
class ContractCase:
    case_id: str
    repetition: int
    plane: str
    kind: str
    parameters: dict[str, float]
    sketch_calls: tuple[dict[str, Any], ...]
    feature_call: dict[str, Any]
    boundary_edges_mm: tuple[tuple[list[float], list[float]], ...]
    centerline_mm: tuple[list[float], list[float]] | None
    circle_center_mm: list[float] | None
    rectangle_diagonals_mm: tuple[tuple[list[float], list[float]], ...]
    minimum_mm: list[float]
    maximum_mm: list[float]
    volume_mm3: float

    def public(self) -> dict[str, Any]:
        return {"case_id": self.case_id, "repetition": self.repetition, "plane": self.plane,
                "kind": self.kind, "parameters": self.parameters, "expected_volume_mm3": self.volume_mm3,
                "expected_min_mm": self.minimum_mm, "expected_max_mm": self.maximum_mm,
                "expected_body_count": 1, "synthetic": True}


def case(case_id: str, repetition: int = 1) -> ContractCase:
    if case_id not in CASE_IDS or type(repetition) is not int or not 1 <= repetition <= MAX_REPEATS:
        raise ValueError("unknown case or out-of-bounds repetition")
    plane, kind = case_id.split(".")
    variant = repetition - 1
    p = {"x": 14.0 + 2 * variant, "y": 17.0 - variant,
         "width": 10.0 + variant, "height": 8.0 + variant,
         "depth": 6.0 + variant, "radius": 4.0 + variant / 2,
         "axis_x": 2.0 + variant}
    corners = [world_mm(plane, p["x"] + sx * p["width"] / 2, p["y"] + sy * p["height"] / 2)
               for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    boundary = tuple((point, corners[(index + 1) % 4]) for index, point in enumerate(corners))
    diagonals = ((corners[0], corners[2]), (corners[1], corners[3])) if kind in {"rectangle", "revolve"} else ()
    axis = None
    circle_center = None
    calls: list[dict[str, Any]] = [{"tool": "sketch.begin", "arguments": {"plane": plane}}]
    if kind == "circle":
        circle_center = world_mm(plane, p["x"], p["y"])
        boundary = ()
        calls.append({"tool": "sketch.circle", "arguments": {"x_mm": p["x"], "y_mm": p["y"], "radius_mm": p["radius"]}})
        extent_u = extent_v = p["radius"]
        volume = math.pi * p["radius"] ** 2 * p["depth"]
    else:
        extent_u, extent_v = p["width"] / 2, p["height"] / 2
        volume = p["width"] * p["height"] * p["depth"]
        if kind == "line_loop":
            xy = [(p["x"] + sx * extent_u, p["y"] + sy * extent_v)
                  for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
            for index, start in enumerate(xy):
                end = xy[(index + 1) % 4]
                calls.append({"tool": "sketch.line", "arguments": {
                    "x1_mm": start[0], "y1_mm": start[1], "x2_mm": end[0], "y2_mm": end[1]}})
        else:
            calls.append({"tool": "sketch.rectangle", "arguments": {
                "center_x_mm": p["x"], "center_y_mm": p["y"], "width_mm": p["width"], "height_mm": p["height"]}})
    feature = {"tool": "feature.extrude", "arguments": {"depth_mm": p["depth"], "operation": "boss", "merge": True,
                                                         "reverse": False, "end_condition": "blind"}}
    bounds = [world_mm(plane, p["x"] + su * extent_u, p["y"] + sv * extent_v, depth)
              for su in (-1, 1) for sv in (-1, 1) for depth in (0.0, p["depth"])]
    if kind == "revolve":
        axis = (world_mm(plane, p["axis_x"], p["y"] - p["height"]),
                world_mm(plane, p["axis_x"], p["y"] + p["height"]))
        calls.append({"tool": "sketch.centerline", "arguments": {
            "x1_mm": p["axis_x"], "y1_mm": p["y"] - p["height"],
            "x2_mm": p["axis_x"], "y2_mm": p["y"] + p["height"]}})
        feature = {"tool": "feature.revolve", "arguments": {"angle_deg": 360.0, "operation": "boss", "merge": True, "reverse": False}}
        outer = p["x"] - p["axis_x"] + p["width"] / 2
        inner = p["x"] - p["axis_x"] - p["width"] / 2
        volume = math.pi * (outer ** 2 - inner ** 2) * p["height"]
        bounds = [world_mm(plane, p["axis_x"] + su * outer, p["y"] + sv * p["height"] / 2, sn * outer)
                  for su in (-1, 1) for sv in (-1, 1) for sn in (-1, 1)]
    return ContractCase(case_id, repetition, plane, kind, p, tuple(calls), feature, boundary, axis,
                        circle_center, diagonals, [min(point[i] for point in bounds) for i in range(3)],
                        [max(point[i] for point in bounds) for i in range(3)], volume)


def stable_observations(observations: list[dict[str, Any]]) -> bool:
    if len(observations) < 2:
        return False
    required = ("document_identity", "configuration", "configuration_identity")
    first = observations[0]
    for item in observations:
        if (item.get("complete") is not True or item.get("issues") != []
                or type(item.get("model_update_stamp")) is not int or item["model_update_stamp"] < 0
                or any(not isinstance(item.get(key), str) or not item[key] or item[key] != first.get(key) for key in required)
                or item["model_update_stamp"] != first.get("model_update_stamp")):
            return False
    return True


def _edge_matches(actual: tuple[Any, Any], expected: tuple[Any, Any]) -> bool:
    return ((points_close(actual[0], expected[0]) and points_close(actual[1], expected[1]))
            or (points_close(actual[0], expected[1]) and points_close(actual[1], expected[0])))


def sketch_oracle(spec: ContractCase, read: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    checks: dict[str, bool] = {"stable_independent_observations": stable_observations(observations)}
    result: dict[str, Any] = {"status": "failed", "checks": checks, "source": "sketch.inspect actual segments/transform", "actual_world_geometry": []}
    try:
        sketch = read.get("sketch", {})
        checks["complete_sketch_geometry"] = (read.get("complete") is True and read.get("issues") == []
            and read.get("active") is True and sketch.get("geometry_complete") is True
            and sketch.get("transform_complete") is True and sketch.get("coordinate_space") == "sketch_local")
        checks["same_document_configuration"] = (bool(observations) and sketch.get("document_identity") == observations[0].get("document_identity")
                                                  and sketch.get("configuration") == observations[0].get("configuration"))
        if not all(checks.values()):
            return result
        transform = sketch["model_to_sketch_transform"]
        segments = sketch["segments"]
        if not isinstance(segments, list) or len(segments) > 16:
            raise ValueError("invalid bounded sketch segments")
        remaining = list(spec.boundary_edges_mm)
        diagonals = list(spec.rectangle_diagonals_mm)
        axis = spec.centerline_mm
        circle_needed = spec.circle_center_mm is not None
        construction_count = 0
        for item in segments:
            if type(item.get("construction")) is not bool or type(item.get("type")) is not int:
                raise ValueError("entity type/construction state is not explicit")
            if item.get("type") == 1 and not item["construction"] and circle_needed:
                center = native_to_world_mm(item.get("center_sketch_m"), transform)
                if (item.get("is_circle") is not True or not points_close(center, spec.circle_center_mm)
                        or not close(item.get("radius_mm"), spec.parameters["radius"])):
                    raise ValueError("actual circle center/radius differs")
                circle_needed = False
                result["actual_world_geometry"].append({"circle_center_mm": center, "radius_mm": item["radius_mm"]})
                continue
            if item.get("type") != 0:
                raise ValueError("unexpected sketch entity type")
            endpoints = (native_to_world_mm(item.get("start_sketch_m"), transform),
                         native_to_world_mm(item.get("end_sketch_m"), transform))
            result["actual_world_geometry"].append({"start_mm": endpoints[0], "end_mm": endpoints[1], "construction": item["construction"]})
            if item["construction"]:
                construction_count += 1
                if axis is not None and _edge_matches(endpoints, axis):
                    axis = None
                    continue
                expected = diagonals
            else:
                expected = remaining
            matches = [i for i, candidate in enumerate(expected) if _edge_matches(endpoints, candidate)]
            if len(matches) != 1:
                raise ValueError("actual segment endpoints do not match a unique requested edge")
            expected.pop(matches[0])
        checks["all_requested_geometry_present"] = not remaining and axis is None and not circle_needed
        expected_axis = int(spec.centerline_mm is not None)
        checks["construction_geometry_exact"] = construction_count in {expected_axis, expected_axis + len(spec.rectangle_diagonals_mm)}
        checks["observation_entity_counts_match"] = all(type(item.get("snapshot", {}).get("sketch_entity_count")) is int
                                                          and item["snapshot"]["sketch_entity_count"] == len(segments) for item in observations)
        checks["zero_solid_before_feature"] = all(type(item.get("snapshot", {}).get("body_count")) is int
                                                   and item["snapshot"]["body_count"] == 0
                                                   and close(item.get("snapshot", {}).get("volume_m3"), 0, tolerance=1e-12)
                                                   and bool(item.get("snapshot", {}).get("active_sketch")) for item in observations)
        result["status"] = "passed" if all(checks.values()) else "failed"
    except (KeyError, TypeError, ValueError) as exc:
        result["reason"] = str(exc)
    return result


def solid_oracle(spec: ContractCase, measurement: dict[str, Any], observations: list[dict[str, Any]]) -> dict[str, Any]:
    checks = {"stable_independent_observations": stable_observations(observations)}
    result: dict[str, Any] = {"status": "failed", "checks": checks, "expected": spec.public()}
    try:
        checks["native_extremes_complete"] = (measurement.get("complete") is True and measurement.get("issues") == []
                                               and measurement.get("source") == "IBody2.GetExtremePoint"
                                               and measurement.get("coordinate_system") == "model"
                                               and type(measurement.get("bodies_count")) is int and measurement["bodies_count"] == 1)
        checks["measurement_same_document_configuration"] = bool(observations) and all(
            measurement.get(key) == observations[0].get(key) for key in ("document_identity", "configuration", "configuration_identity"))
        checks["position_minimum"] = points_close(measurement.get("min_mm"), spec.minimum_mm)
        checks["position_maximum"] = points_close(measurement.get("max_mm"), spec.maximum_mm)
        checks["three_dimensions"] = all(close(measurement.get("extents_mm", {}).get(axis), high - low)
                                          for axis, low, high in zip(("x", "y", "z"), spec.minimum_mm, spec.maximum_mm, strict=True))
        for i, observation in enumerate(observations):
            snapshot = observation.get("snapshot", {})
            features = observation.get("features")
            checks[f"observation_{i}_solid"] = (type(snapshot.get("body_count")) is int and snapshot["body_count"] == 1 and snapshot.get("active_sketch") == ""
                                                and snapshot.get("document_type") == "part" and snapshot.get("connected") is True)
            checks[f"observation_{i}_volume"] = close(number(snapshot.get("volume_m3")) * 1e9, spec.volume_mm3,
                                                       tolerance=VOLUME_TOLERANCE_MM3)
            checks[f"observation_{i}_feature_health"] = (isinstance(features, list) and bool(features)
                and type(snapshot.get("feature_count")) is int and snapshot["feature_count"] == len(features)
                and all(type(item.get("error_code")) is int and item["error_code"] == 0 for item in features))
        result["actual"] = {"min_mm": measurement.get("min_mm"), "max_mm": measurement.get("max_mm"),
                            "extents_mm": measurement.get("extents_mm"), "volume_mm3": number(observations[-1]["snapshot"]["volume_m3"]) * 1e9}
        result["status"] = "passed" if all(checks.values()) else "failed"
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        result["reason"] = str(exc)
    return result
