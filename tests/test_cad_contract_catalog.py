"""Pure synthetic numerical oracles; no CAD, files, network or COM calls."""
from __future__ import annotations

import copy
import math

import pytest

from scripts.cad_contract_catalog import CASE_IDS, case, native_to_world_mm, sketch_oracle, solid_oracle
from sw_assistant.domain import ToolCall
from sw_assistant.tools import build_tool_registry


def observations(spec, *, solid):
    state = {"complete": True, "issues": [], "document_identity": "synthetic-doc", "configuration": "Default",
             "configuration_identity": "synthetic-config", "model_update_stamp": 11,
             "features": [{"name": "sketch", "error_code": 0}, {"name": "solid", "error_code": 0}],
             "snapshot": {"connected": True, "document_type": "part", "body_count": int(solid),
                          "volume_m3": spec.volume_mm3 / 1e9 if solid else 0,
                          "feature_count": 2, "active_sketch": "" if solid else "Sketch1", "sketch_entity_count": 0}}
    return [copy.deepcopy(state), copy.deepcopy(state)]


def sketch_evidence(spec):
    rotation = {"front": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                "top": [1, 0, 0, 0, 0, 1, 0, -1, 0],
                "right": [0, 0, 1, 0, 1, 0, -1, 0, 0]}[spec.plane]
    transform = rotation + [-.012, .024, 0, 1, 0, 0, 0]

    def local(point):
        return [sum(point[row] / 1000 * rotation[row * 3 + col] for row in range(3)) + transform[9 + col]
                for col in range(3)]

    segments = []
    for a, b in spec.boundary_edges_mm:
        segments.append({"type": 0, "construction": False, "start_sketch_m": local(a), "end_sketch_m": local(b)})
    for a, b in spec.rectangle_diagonals_mm:
        segments.append({"type": 0, "construction": True, "start_sketch_m": local(a), "end_sketch_m": local(b)})
    if spec.centerline_mm:
        a, b = spec.centerline_mm
        segments.append({"type": 0, "construction": True, "start_sketch_m": local(a), "end_sketch_m": local(b)})
    if spec.circle_center_mm:
        segments.append({"type": 1, "construction": False, "is_circle": True,
                         "center_sketch_m": local(spec.circle_center_mm), "radius_mm": spec.parameters["radius"]})
    result = {"complete": True, "issues": [], "active": True,
              "sketch": {"geometry_complete": True, "transform_complete": True, "coordinate_space": "sketch_local",
                         "model_to_sketch_transform": transform, "document_identity": "synthetic-doc", "configuration": "Default",
                         "segments": segments}}
    observed = observations(spec, solid=False)
    for item in observed:
        item["snapshot"]["sketch_entity_count"] = len(segments)
    return result, observed


def solid_evidence(spec):
    return {"complete": True, "issues": [], "source": "IBody2.GetExtremePoint", "coordinate_system": "model",
            "bodies_count": 1, "document_identity": "synthetic-doc", "configuration": "Default", "configuration_identity": "synthetic-config",
            "min_mm": spec.minimum_mm[:], "max_mm": spec.maximum_mm[:],
            "extents_mm": {axis: high - low for axis, low, high in zip("xyz", spec.minimum_mm, spec.maximum_mm, strict=True)}}


@pytest.mark.parametrize("case_id", CASE_IDS)
@pytest.mark.parametrize("repetition", [1, 2, 3])
def test_all_bounded_catalog_variants_validate_and_have_independent_passable_geometry(case_id, repetition):
    spec = case(case_id, repetition)
    registry = build_tool_registry()
    for call in (*spec.sketch_calls, spec.feature_call):
        registry.validate(ToolCall.from_mapping(call))
    read, observed = sketch_evidence(spec)
    assert sketch_oracle(spec, read, observed)["status"] == "passed"
    assert solid_oracle(spec, solid_evidence(spec), observations(spec, solid=True))["status"] == "passed"
    assert spec.volume_mm3 > 0


def test_variant_parameters_are_not_repeated_identical_geometry():
    values = [case("top.rectangle", repetition) for repetition in (1, 2, 3)]
    assert len({tuple(item.parameters.values()) for item in values}) == 3
    assert len({item.volume_mm3 for item in values}) == 3


@pytest.mark.parametrize("case_id,repeat", [("custom", 1), ("front.line_loop", 0), ("front.circle", 4), ("right.rectangle", True)])
def test_unbounded_or_unknown_cases_are_refused(case_id, repeat):
    with pytest.raises(ValueError):
        case(case_id, repeat)


def test_math_array_row_vector_layout_with_nonsymmetric_rotation_and_translation():
    angle = math.radians(31)
    c, s = math.cos(angle), math.sin(angle)
    # q=(x,y,0)R+t, not R(x,y,0). A transpose mistake must fail this test.
    transform = [c, s, 0, -s, c, 0, 0, 0, 1, .07, -.04, 0, 1, 0, 0, 0]
    x, y = .031, -.052
    actual = [x * c - y * s + .07, x * s + y * c - .04, 0]
    assert native_to_world_mm(actual, transform) == pytest.approx([31, -52, 0])


def test_old_top_coordinate_projection_is_rejected_even_with_success_applied_echo():
    spec = case("top.line_loop")
    read, observed = sketch_evidence(spec)
    for segment, (a, b) in zip(read["sketch"]["segments"], spec.boundary_edges_mm, strict=True):
        segment["start_sketch_m"] = [a[0] / 1000, a[1] / 1000, 0]
        segment["end_sketch_m"] = [b[0] / 1000, b[1] / 1000, 0]
    read["applied"] = {"creation_verified": True, "geometry_verified": True}
    assert sketch_oracle(spec, read, observed)["status"] == "failed"


@pytest.mark.parametrize("change", ["point", "radius", "transform", "missing_transform", "construction", "extra", "stamp", "config", "incomplete"])
def test_sketch_oracle_rejects_inconsistent_or_missing_actual_evidence(change):
    spec = case("right.circle")
    read, observed = sketch_evidence(spec)
    if change == "point":
        read["sketch"]["segments"][0]["center_sketch_m"][0] += .01
    elif change == "radius":
        read["sketch"]["segments"][0]["radius_mm"] += 1
    elif change == "transform":
        read["sketch"]["model_to_sketch_transform"][12] = 2
    elif change == "missing_transform":
        del read["sketch"]["model_to_sketch_transform"]
    elif change == "construction":
        read["sketch"]["segments"][0]["construction"] = True
    elif change == "extra":
        read["sketch"]["segments"] *= 2
    elif change == "stamp":
        observed[1]["model_update_stamp"] += 1
    elif change == "config":
        observed[1]["configuration"] = "Other"
    else:
        read["complete"] = False
    assert sketch_oracle(spec, read, observed)["status"] != "passed"


@pytest.mark.parametrize("change", ["min", "max", "dimension", "volume", "health", "missing_health", "stamp", "config", "source", "body", "nan"])
def test_solid_oracle_rejects_equal_size_wrong_position_or_other_corruption(change):
    spec = case("top.revolve")
    measured, observed = solid_evidence(spec), observations(spec, solid=True)
    if change in {"min", "max"}:
        measured[f"{change}_mm"][0] += 3
    elif change == "dimension":
        measured["extents_mm"]["x"] += 1
    elif change == "volume":
        observed[0]["snapshot"]["volume_m3"] += 1e-8
    elif change == "health":
        observed[1]["features"][0]["error_code"] = 1
    elif change == "missing_health":
        observed[0]["features"] = []
    elif change == "stamp":
        observed[1]["model_update_stamp"] += 1
    elif change == "config":
        measured["configuration_identity"] = "changed"
    elif change == "source":
        measured["source"] = "applied"
    elif change == "body":
        observed[0]["snapshot"]["body_count"] = 2
    else:
        measured["max_mm"][0] = math.nan
    assert solid_oracle(spec, measured, observed)["status"] == "failed"


@pytest.mark.parametrize("mutation", ["diagonal", "extra", "partial"])
def test_rectangle_oracle_rejects_corrupt_or_partial_construction_geometry(mutation):
    spec = case("top.rectangle")
    read, observed = sketch_evidence(spec)
    segments = read["sketch"]["segments"]
    if mutation == "diagonal":
        segments[-1]["end_sketch_m"][0] += .003
    elif mutation == "extra":
        segments.append(copy.deepcopy(segments[-1]))
    else:
        segments.pop()
    assert sketch_oracle(spec, read, observed)["status"] == "failed"
