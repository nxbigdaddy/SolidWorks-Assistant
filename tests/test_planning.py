import pytest

from sw_assistant.domain import ClarificationRequired, Plan, PlanningError, ToolCall
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.planning.planner import ModelClient, OfflinePlanner, PlannerService
from sw_assistant.tools import build_tool_registry


def test_offline_planner_creates_verified_circle_extrude_plan():
    plan = PlannerService(ModelSettings(), build_tool_registry()).plan(
        "新建零件，在前视基准面画半径 20 mm 的圆，拉伸 30 mm"
    )
    assert [step.tool for step in plan.steps] == [
        "session.ensure",
        "document.new_part",
        "selection.plane",
        "sketch.begin",
        "sketch.circle",
        "feature.extrude",
    ]
    assert plan.steps[-1].arguments["depth_mm"] == 30


def test_offline_planner_converts_diameter_and_supports_rectangle():
    circle = OfflinePlanner().plan("上视基准面画直径 50 mm 的圆")
    assert circle.steps[-1].arguments["radius_mm"] == 25
    rectangle = OfflinePlanner().plan("右视基准面画矩形 40 x 20 mm")
    assert rectangle.steps[-1].tool == "sketch.rectangle"


def test_offline_planner_understands_explicit_cylinder_height():
    plan = OfflinePlanner().plan("创建直径 50 mm、高度 30 mm 的圆柱")
    assert plan.steps[-2].arguments["radius_mm"] == 25
    assert plan.steps[-1].tool == "feature.extrude"
    assert plan.steps[-1].arguments["depth_mm"] == 30


def test_offline_planner_never_guesses_missing_depth():
    with pytest.raises(PlanningError, match="明确的拉伸深度"):
        OfflinePlanner().plan("画半径 10 mm 的圆并拉伸")


def test_planning_progress_is_observable():
    events = []
    PlannerService(ModelSettings(), build_tool_registry()).plan(
        "画半径 10 mm 的圆",
        lambda stage, message, details: events.append((stage, message)),
    )
    assert [event[0] for event in events] == ["preparing", "offline", "validating", "completed"]


def test_socket_head_cap_screw_uses_deterministic_iso_plan_without_model(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    called = []
    monkeypatch.setattr(
        ModelClient,
        "create_plan",
        lambda *_args, **_kwargs: called.append(True),
    )
    events = []
    plan = PlannerService(settings, build_tool_registry()).plan(
        "画一个 m4 * 30 的内六角螺栓",
        lambda stage, message, details: events.append(stage),
    )
    assert called == []
    assert plan.task == "ISO 4762 M4×30 内六角圆柱头螺钉"
    assert [step.tool for step in plan.steps] == [
        "session.ensure",
        "document.new_part",
        "selection.plane",
        "sketch.begin",
        "sketch.circle",
        "feature.extrude",
        "sketch.begin_face",
        "sketch.circle",
        "feature.extrude",
        "sketch.begin_face",
        "sketch.polygon",
        "feature.extrude",
    ]
    assert plan.steps[5].arguments["depth_mm"] == 30
    assert plan.steps[7].arguments["radius_mm"] == 3.5
    assert plan.steps[10].arguments["sides"] == 6
    assert plan.steps[-1].arguments["operation"] == "cut"
    assert events == ["preparing", "deterministic", "validating", "completed"]


def test_preflight_requests_missing_dimensions_before_model_call(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    called = []
    monkeypatch.setattr(
        ModelClient,
        "create_plan",
        lambda *_args, **_kwargs: called.append(True),
    )
    with pytest.raises(ClarificationRequired) as captured:
        PlannerService(settings, build_tool_registry()).plan("画一个圆并拉伸")
    assert len(captured.value.questions) == 2
    assert "半径或直径" in captured.value.questions[0]
    assert "拉伸或切除深度" in captured.value.questions[1]
    assert called == []


def test_preflight_clarifies_underspecified_phone_before_model_call(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    called = []
    monkeypatch.setattr(
        ModelClient,
        "create_plan",
        lambda *_args, **_kwargs: called.append(True),
    )
    with pytest.raises(ClarificationRequired) as captured:
        PlannerService(settings, build_tool_registry()).plan(
            "画一个iphone 14 Promax 手机"
        )
    assert any("长×宽×厚" in question for question in captured.value.questions)
    assert any("简化主体" in question for question in captured.value.questions)
    assert called == []


def test_complex_phone_with_dimensions_and_simplified_scope_reaches_model(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    monkeypatch.setattr(
        ModelClient,
        "create_plan",
        lambda *_args, **_kwargs: Plan(
            "simplified phone",
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("sketch.rectangle", {"width_mm": 161, "height_mm": 78}),
                ToolCall("feature.extrude", {"depth_mm": 8}),
            ],
        ),
    )
    plan = PlannerService(settings, build_tool_registry()).plan(
        "创建 161 × 78 × 8 mm 的 iPhone 简化无圆角外形"
    )
    assert plan.steps[-1].arguments["depth_mm"] == 8.0


def test_system_prompt_contains_machine_readable_plane_enum_and_constraints():
    client = ModelClient(
        ModelSettings("https://example.com/v1", "model", "key"),
        build_tool_registry(),
    )
    prompt = client._system_prompt()
    assert '"enum":["front","top","right"]' in prompt
    assert '"additionalProperties":false' in prompt
    assert "枚举值必须原样输出" in prompt


def test_invalid_model_plan_gets_one_targeted_suffix_repair(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    repair_contexts = []
    monkeypatch.setattr(
        ModelClient,
        "create_plan",
        lambda *_args, **_kwargs: Plan(
            "part",
            [
                ToolCall("document.new_part", {}),
                ToolCall("selection.plane", {"plane": "前视基准面"}),
                ToolCall("sketch.begin", {"plane": "左视基准面"}),
                ToolCall("sketch.circle", {"radius_mm": 5}),
                ToolCall("feature.extrude", {"depth_mm": 10}),
            ],
        ),
    )

    def repair(self, context, progress=None):
        repair_contexts.append(context)
        return Plan(
            "repaired suffix",
            [
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("sketch.circle", {"radius_mm": 5}),
                ToolCall("feature.extrude", {"depth_mm": 10}),
            ],
        )

    monkeypatch.setattr(ModelClient, "repair_plan", repair)
    events = []
    plan = PlannerService(settings, build_tool_registry()).plan(
        "创建半径 5 mm、高度 10 mm 的测试零件",
        lambda stage, message, details: events.append((stage, details)),
    )

    assert [step.tool for step in plan.steps] == [
        "document.new_part",
        "selection.plane",
        "sketch.begin",
        "sketch.circle",
        "feature.extrude",
    ]
    assert plan.steps[1].arguments["plane"] == "front"
    assert plan.steps[2].arguments["plane"] == "front"
    assert len(repair_contexts) == 1
    context = repair_contexts[0]
    assert len(context["completed_steps"]) == 2
    assert context["completed_steps"][1]["arguments"]["plane"] == "front"
    assert context["failed_step"]["tool"] == "sketch.begin"
    assert context["error_details"]["received_value"] == "左视基准面"
    assert [stage for stage, _details in events].count("repairing_validation") == 1
    assert events[-1][0] == "completed"


def test_invalid_repaired_plan_stops_after_one_attempt_with_diagnostics(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    monkeypatch.setattr(
        ModelClient,
        "create_plan",
        lambda *_args, **_kwargs: Plan(
            "part",
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "left"}),
            ],
        ),
    )
    repairs = []

    def repair(self, context, progress=None):
        repairs.append(context)
        return Plan("still invalid", [ToolCall("sketch.begin", {"plane": "left"})])

    monkeypatch.setattr(ModelClient, "repair_plan", repair)
    with pytest.raises(PlanningError, match="自动定向修复后仍然无效") as captured:
        PlannerService(settings, build_tool_registry()).plan("创建测试零件")
    assert len(repairs) == 1
    assert captured.value.details["repair_attempts"] == 1
    assert captured.value.details["failed_step_index"] == 2
    assert captured.value.details["failed_plan"]["steps"][1]["arguments"]["plane"] == "left"


def test_sequence_validation_failure_identifies_step_for_targeted_repair(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    monkeypatch.setattr(
        ModelClient,
        "create_plan",
        lambda *_args, **_kwargs: Plan(
            "bad sequence",
            [
                ToolCall("document.new_part", {}),
                ToolCall("sketch.circle", {"radius_mm": 5}),
            ],
        ),
    )
    contexts = []

    def repair(self, context, progress=None):
        contexts.append(context)
        return Plan(
            "fixed sequence",
            [
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("sketch.circle", {"radius_mm": 5}),
            ],
        )

    monkeypatch.setattr(ModelClient, "repair_plan", repair)
    plan = PlannerService(settings, build_tool_registry()).plan("创建测试零件")
    assert [step.tool for step in plan.steps] == [
        "document.new_part",
        "sketch.begin",
        "sketch.circle",
    ]
    assert contexts[0]["error_details"]["step_index"] == 2
    assert contexts[0]["error_details"]["tool"] == "sketch.circle"
    assert contexts[0]["failed_step"]["tool"] == "sketch.circle"


def test_repair_suffix_is_validated_with_completed_prefix(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    context = {
        "completed_steps": [
            ToolCall("session.ensure", {}).to_dict(),
            ToolCall("document.new_part", {}).to_dict(),
            ToolCall("sketch.begin", {"plane": "front"}).to_dict(),
        ]
    }
    monkeypatch.setattr(
        ModelClient,
        "repair_plan",
        lambda self, value, progress=None: Plan(
            "repair",
            [
                ToolCall("sketch.circle", {"radius_mm": 5}),
                ToolCall("feature.extrude", {"depth_mm": 10}),
            ],
        ),
    )
    plan = PlannerService(settings, build_tool_registry()).repair(context)
    assert [step.tool for step in plan.steps] == ["sketch.circle", "feature.extrude"]


def test_repair_suffix_cannot_replace_completed_document(monkeypatch):
    settings = ModelSettings("https://example.com/v1", "model", "key")
    context = {
        "completed_steps": [
            ToolCall("session.ensure", {}).to_dict(),
            ToolCall("document.new_part", {}).to_dict(),
        ]
    }
    monkeypatch.setattr(
        ModelClient,
        "repair_plan",
        lambda self, value, progress=None: Plan(
            "unsafe repair", [ToolCall("document.new_part", {})]
        ),
    )
    with pytest.raises(PlanningError, match="替换当前文档"):
        PlannerService(settings, build_tool_registry()).repair(context)
