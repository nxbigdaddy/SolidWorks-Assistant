import math

import pytest

from sw_assistant.domain import ModelSnapshot, Plan, PlanningError, ToolCall
from sw_assistant.tools import build_tool_registry


def test_plan_round_trip_and_unknown_field_rejected():
    plan = Plan.from_mapping(
        {"task": "circle", "steps": [{"tool": "session.ensure", "arguments": {}}]}
    )
    assert Plan.from_mapping(plan.to_dict()).task == "circle"
    with pytest.raises(PlanningError, match="未知字段"):
        Plan.from_mapping({"task": "x", "steps": [], "unsafe": True})


def test_non_finite_argument_is_rejected():
    with pytest.raises(PlanningError, match="有限数值"):
        ToolCall.from_mapping(
            {"tool": "sketch.circle", "arguments": {"radius_mm": math.inf}}
        )


def test_registry_normalizes_defaults_and_rejects_unknown_tool():
    registry = build_tool_registry()
    call = registry.validate(ToolCall("sketch.circle", {"radius_mm": 20}))
    assert call.arguments == {"x_mm": 0.0, "y_mm": 0.0, "radius_mm": 20.0}
    assert registry.write_scope("document.open_readonly") == "application"
    with pytest.raises(PlanningError, match="不支持的工具"):
        registry.validate(ToolCall("shell.execute", {}))


def test_registry_schema_exposes_types_enums_ranges_and_object_safety():
    registry = build_tool_registry()
    schemas = {item["name"]: item for item in registry.describe()}

    plane_arguments = schemas["sketch.begin"]["arguments"]
    assert plane_arguments == {
        "type": "object",
        "properties": {
            "plane": {
                "type": "string",
                "enum": ["front", "top", "right"],
                "description": "基准面内部标识；必须与选中的基准面一致",
            }
        },
        "required": ["plane"],
        "additionalProperties": False,
    }

    extrude_properties = schemas["feature.extrude"]["arguments"]["properties"]
    assert extrude_properties["depth_mm"] == {
        "type": "number",
        "minimum": 0.0001,
        "maximum": 1_000_000,
    }
    assert extrude_properties["operation"]["enum"] == ["boss", "cut"]
    assert extrude_properties["reverse"] == {"type": "boolean", "default": False}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Front Plane", "front"),
        ("前视基准面", "front"),
        ("上视", "top"),
        ("RIGHT_PLANE", "right"),
    ],
)
def test_plane_aliases_are_normalized_before_execution(value, expected):
    registry = build_tool_registry()
    call = registry.validate(ToolCall("sketch.begin", {"plane": value}))
    assert call.arguments["plane"] == expected


def test_validation_error_identifies_step_field_received_value_and_schema():
    registry = build_tool_registry()
    with pytest.raises(PlanningError) as captured:
        registry.validate_plan(
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "左视基准面"}),
            ]
        )
    error = captured.value
    assert "第 2 步" in str(error)
    assert "左视基准面" in str(error)
    assert error.details["step_index"] == 2
    assert error.details["tool"] == "sketch.begin"
    assert error.details["field"] == "plane"
    assert error.details["received_value"] == "左视基准面"
    assert error.details["expected_schema"]["enum"] == ["front", "top", "right"]


def test_missing_required_field_keeps_machine_readable_expectation():
    registry = build_tool_registry()
    with pytest.raises(PlanningError) as captured:
        registry.validate_plan([ToolCall("sketch.begin", {})])
    assert captured.value.details["step_index"] == 1
    assert captured.value.details["tool"] == "sketch.begin"
    assert captured.value.details["field"] == "plane"
    assert captured.value.details["expected_schema"]["enum"] == [
        "front",
        "top",
        "right",
    ]


def test_common_feature_aliases_and_chinese_booleans_are_normalized():
    registry = build_tool_registry()
    call = registry.validate(
        ToolCall(
            "feature.extrude",
            {
                "depth_mm": 10,
                "operation": "拉伸切除",
                "end_condition": "中面",
                "reverse": "是",
                "merge": "否",
            },
        )
    )
    assert call.arguments == {
        "depth_mm": 10.0,
        "operation": "cut",
        "end_condition": "mid_plane",
        "reverse": True,
        "merge": False,
    }


def test_extrude_depth_range_is_enforced():
    registry = build_tool_registry()
    with pytest.raises(PlanningError, match="depth_mm"):
        registry.validate(ToolCall("feature.extrude", {"depth_mm": -1}))


def test_plan_sequence_rejects_geometry_before_sketch():
    registry = build_tool_registry()
    with pytest.raises(PlanningError, match="必须先创建草图"):
        registry.validate_plan(
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.circle", {"radius_mm": 2}),
            ]
        )


def test_plan_sequence_rejects_empty_extrude_and_cut_without_body():
    registry = build_tool_registry()
    with pytest.raises(PlanningError, match="包含几何"):
        registry.validate_plan(
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("feature.extrude", {"depth_mm": 10}),
            ]
        )


def test_revolve_requires_profile_and_centerline_then_creates_body_state():
    registry = build_tool_registry()
    prefix = [
        ToolCall("document.new_part", {}),
        ToolCall("sketch.begin", {"plane": "front"}),
        ToolCall("sketch.line", {"x1_mm": 0, "y1_mm": 0, "x2_mm": 5, "y2_mm": 0}),
        ToolCall("sketch.line", {"x1_mm": 5, "y1_mm": 0, "x2_mm": 5, "y2_mm": 10}),
        ToolCall("sketch.line", {"x1_mm": 5, "y1_mm": 10, "x2_mm": 0, "y2_mm": 10}),
        ToolCall("sketch.line", {"x1_mm": 0, "y1_mm": 10, "x2_mm": 0, "y2_mm": 0}),
    ]
    with pytest.raises(PlanningError, match="中心线"):
        registry.validate_plan(prefix + [ToolCall("feature.revolve", {})])

    plan = registry.validate_plan(
        prefix
        + [
            ToolCall(
                "sketch.centerline",
                {"x1_mm": 0, "y1_mm": -10, "x2_mm": 0, "y2_mm": 10},
            ),
            ToolCall("feature.revolve", {"angle_deg": 180}),
        ]
    )
    assert plan[-1].arguments == {
        "angle_deg": 180.0,
        "operation": "boss",
        "reverse": False,
        "merge": True,
    }


def test_revolve_cut_requires_existing_body():
    registry = build_tool_registry()
    with pytest.raises(PlanningError, match="切除前"):
        registry.validate_plan(
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("sketch.circle", {"radius_mm": 5}),
                ToolCall(
                    "sketch.centerline",
                    {"x1_mm": 0, "y1_mm": -10, "x2_mm": 0, "y2_mm": 10},
                ),
                ToolCall("feature.revolve", {"operation": "cut"}),
            ]
        )
    with pytest.raises(PlanningError, match="切除前"):
        registry.validate_plan(
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("sketch.circle", {"radius_mm": 2}),
                ToolCall("feature.extrude", {"depth_mm": 10, "operation": "cut"}),
            ]
        )


def test_open_line_profile_is_rejected_before_solidworks_execution():
    registry = build_tool_registry()
    with pytest.raises(PlanningError, match="没有闭合"):
        registry.validate_plan(
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall(
                    "sketch.line",
                    {"x1_mm": 0, "y1_mm": 0, "x2_mm": 5, "y2_mm": 0},
                ),
                ToolCall("feature.extrude", {"depth_mm": 10}),
            ]
        )


def test_polygon_and_feature_face_form_a_valid_followup_cut():
    registry = build_tool_registry()
    plan = registry.validate_plan(
        [
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "top"}),
            ToolCall("sketch.circle", {"radius_mm": 2}),
            ToolCall("feature.extrude", {"depth_mm": 30}),
            ToolCall("sketch.begin_face", {}),
            ToolCall("sketch.polygon", {"radius_mm": 2, "sides": 6}),
            ToolCall("feature.extrude", {"depth_mm": 2, "operation": "cut"}),
        ]
    )
    assert plan[-2].arguments == {
        "x_mm": 0.0,
        "y_mm": 0.0,
        "radius_mm": 2.0,
        "sides": 6,
        "rotation_deg": 0.0,
    }


def test_sketch_postcondition_rejects_wrong_plane_coordinates():
    registry = build_tool_registry()
    call = registry.validate(
        ToolCall("sketch.circle", {"x_mm": 10, "y_mm": 20, "radius_mm": 5})
    )
    postcondition = registry.postcondition(call.tool)
    assert postcondition is not None
    before = ModelSnapshot(active_sketch="Sketch1", sketch_entity_count=0)
    after = ModelSnapshot(active_sketch="Sketch1", sketch_entity_count=1)
    wrong = {
        "applied": {
            "tool": "sketch.circle",
            "frame": {
                "origin": [0.0, 0.0, 0.0],
                "u": [1.0, 0.0, 0.0],
                "v": [0.0, 0.0, -1.0],
            },
            "center_m": [0.01, 0.02, 0.0],
            "radius_m": 0.005,
        }
    }
    assert postcondition(call, wrong, before, after)[0] is False

    correct = {
        "applied": {
            **wrong["applied"],
            "center_m": [0.01, 0.0, -0.02],
        }
    }
    assert postcondition(call, correct, before, after)[0] is True


def test_feature_postcondition_requires_matching_applied_depth():
    registry = build_tool_registry()
    call = registry.validate(ToolCall("feature.extrude", {"depth_mm": 30}))
    postcondition = registry.postcondition(call.tool)
    assert postcondition is not None
    before = ModelSnapshot(feature_count=3, body_count=0, volume_m3=0.0)
    after = ModelSnapshot(feature_count=4, body_count=1, volume_m3=0.001)
    evidence = {
        "applied": {
            "tool": "feature.extrude",
            "depth_m": 0.003,
            "operation": "boss",
            "end_condition": "blind",
            "reverse": False,
            "merge": True,
        }
    }
    assert postcondition(call, evidence, before, after)[0] is False
    evidence["applied"]["depth_m"] = 0.03
    assert postcondition(call, evidence, before, after)[0] is True
