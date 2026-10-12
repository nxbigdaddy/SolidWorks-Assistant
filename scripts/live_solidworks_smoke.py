from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable

from sw_assistant.domain import ToolCall, ToolError
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.planning import PlannerService
from sw_assistant.solidworks.gateway import SolidWorksGateway, _value
from sw_assistant.tools import ToolRegistry, build_tool_registry


POSITIVE_SCENARIOS = (
    "cylinder",
    "plane-front",
    "plane-top",
    "plane-right",
    "hex-key-corner",
    "shaft-m4",
    "extrude-mid-plane",
    "extrude-reverse",
    "revolve-cylinder",
    "revolve-reverse",
    "revolve-cut",
    "socket-head-m4x30",
)
NEGATIVE_SCENARIOS = (
    "open-profile-rejected",
    "rollback-mismatch",
    "context-drift",
)
ALL_SCENARIOS = POSITIVE_SCENARIOS + NEGATIVE_SCENARIOS


def _extrude_calls(
    plane: str,
    radius_mm: float,
    depth_mm: float,
    *,
    end_condition: str = "blind",
    reverse: bool = False,
) -> list[ToolCall]:
    return [
        ToolCall("session.ensure", {}),
        ToolCall("document.new_part", {}),
        ToolCall("selection.plane", {"plane": plane}),
        ToolCall("sketch.begin", {"plane": plane}),
        ToolCall("sketch.circle", {"radius_mm": radius_mm}),
        ToolCall(
            "feature.extrude",
            {
                "depth_mm": depth_mm,
                "end_condition": end_condition,
                "reverse": reverse,
            },
        ),
    ]


def _positive_scenario(
    scenario: str, registry: ToolRegistry
) -> tuple[list[ToolCall], float | None]:
    if scenario == "socket-head-m4x30":
        calls = PlannerService(ModelSettings(), registry).plan(
            "画一个 M4×30 的内六角螺栓"
        ).steps
        expected_mm3 = (
            math.pi * 2.0**2 * 30.0
            + math.pi * 3.5**2 * 4.0
            - math.sqrt(3.0) / 2.0 * 3.0**2 * 2.0
        )
        return calls, expected_mm3 / 1_000_000_000.0
    if scenario == "hex-key-corner":
        radius = 3.464
        return (
            [
                ToolCall("session.ensure", {}),
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall(
                    "sketch.polygon",
                    {
                        "x_mm": 0.0,
                        "y_mm": 0.0,
                        "radius_mm": radius,
                        "sides": 6,
                        "rotation_deg": 0.0,
                    },
                ),
                ToolCall("feature.extrude", {"depth_mm": 90.0, "merge": True}),
                ToolCall("sketch.begin", {"plane": "right"}),
                ToolCall(
                    "sketch.polygon",
                    {
                        "x_mm": 0.0,
                        "y_mm": 90.0,
                        "radius_mm": radius,
                        "sides": 6,
                        "rotation_deg": 0.0,
                    },
                ),
                ToolCall("feature.extrude", {"depth_mm": 30.0, "merge": True}),
            ],
            None,
        )
    if scenario == "shaft-m4":
        return _extrude_calls("front", 2.0, 30.0), math.pi * 2.0**2 * 30.0 / 1e9
    if scenario == "extrude-mid-plane":
        return (
            _extrude_calls("front", 3.0, 8.0, end_condition="mid_plane"),
            math.pi * 3.0**2 * 8.0 / 1e9,
        )
    if scenario == "extrude-reverse":
        return (
            _extrude_calls("front", 3.0, 4.0, reverse=True),
            math.pi * 3.0**2 * 4.0 / 1e9,
        )
    if scenario in {"revolve-cylinder", "revolve-reverse"}:
        angle_deg = 180.0 if scenario == "revolve-reverse" else 360.0
        calls = [
            ToolCall("session.ensure", {}),
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "front"}),
            ToolCall(
                "sketch.rectangle",
                {
                    "center_x_mm": 2.5,
                    "center_y_mm": 0.0,
                    "width_mm": 5.0,
                    "height_mm": 10.0,
                },
            ),
            ToolCall(
                "sketch.centerline",
                {"x1_mm": 0.0, "y1_mm": -7.0, "x2_mm": 0.0, "y2_mm": 7.0},
            ),
            ToolCall(
                "feature.revolve",
                {"angle_deg": angle_deg, "reverse": scenario == "revolve-reverse"},
            ),
        ]
        return calls, math.pi * 5.0**2 * 10.0 * angle_deg / 360.0 / 1e9
    if scenario == "revolve-cut":
        return (
            [
                ToolCall("session.ensure", {}),
                ToolCall("document.new_part", {}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall("sketch.circle", {"radius_mm": 10.0}),
                ToolCall("feature.extrude", {"depth_mm": 10.0}),
                ToolCall("sketch.begin", {"plane": "front"}),
                ToolCall(
                    "sketch.rectangle",
                    {
                        "center_x_mm": 5.0,
                        "center_y_mm": 0.0,
                        "width_mm": 4.0,
                        "height_mm": 4.0,
                    },
                ),
                ToolCall(
                    "sketch.centerline",
                    {"x1_mm": 0.0, "y1_mm": -8.0, "x2_mm": 0.0, "y2_mm": 8.0},
                ),
                ToolCall("feature.revolve", {"angle_deg": 360.0, "operation": "cut"}),
            ],
            None,
        )
    plane = {
        "cylinder": "front",
        "plane-front": "front",
        "plane-top": "top",
        "plane-right": "right",
    }[scenario]
    radius_mm = 5.0 if scenario == "cylinder" else 3.0
    depth_mm = 10.0 if scenario == "cylinder" else 4.0
    return (
        _extrude_calls(plane, radius_mm, depth_mm),
        math.pi * radius_mm**2 * depth_mm / 1e9,
    )


def _execute_steps(
    gateway: SolidWorksGateway,
    registry: ToolRegistry,
    calls: list[ToolCall],
    created_documents: list[Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    validated = registry.validate_plan(calls)
    for index, call in enumerate(validated, 1):
        try:
            before = gateway.snapshot()
            message, data = gateway.execute(call)
            after = gateway.snapshot()
            postcondition = registry.postcondition(call.tool)
            if postcondition is not None:
                ok, failure = postcondition(call, data, before, after)
                if not ok:
                    raise RuntimeError(failure)
            if call.tool == "document.new_part":
                created_documents.append(gateway.document)
            records.append(
                {
                    "tool": call.tool,
                    "message": message,
                    "data": data,
                    "before": before.to_dict(),
                    "after": after.to_dict(),
                }
            )
        except Exception as exc:
            raise RuntimeError(
                f"第 {index}/{len(validated)} 步 {call.tool} 失败：{exc}"
            ) from exc
    return records


def _assert_positive_result(
    scenario: str, records: list[dict[str, Any]], expected_volume: float | None
) -> None:
    final = records[-1]["after"]
    actual_volume = float(final.get("volume_m3") or 0.0)
    if expected_volume is not None and not math.isclose(
        actual_volume, expected_volume, rel_tol=0.005, abs_tol=1e-12
    ):
        raise RuntimeError(
            "最终实体体积与场景参数不一致："
            f"expected={expected_volume:.12g}, actual={actual_volume:.12g}"
        )
    if scenario == "hex-key-corner":
        before_volume = float(records[-1]["before"].get("volume_m3") or 0.0)
        polygon_backends = [
            record["data"].get("applied", {}).get("backend")
            for record in records
            if record["tool"] == "sketch.polygon"
        ]
        if actual_volume <= before_volume:
            raise RuntimeError("右视基准面的短六角段没有增加实体体积。")
        if polygon_backends != ["solidworks-native", "solidworks-native"]:
            raise RuntimeError(
                "六角扳手拐角场景没有全部使用 SolidWorks 原生 CreatePolygon："
                f"{polygon_backends}"
            )
    elif expected_volume is None:
        before_volume = float(records[-1]["before"].get("volume_m3") or 0.0)
        if not 0.0 < actual_volume < before_volume:
            raise RuntimeError(
                "旋转切除没有使单实体体积减少："
                f"before={before_volume:.12g}, actual={actual_volume:.12g}"
            )
    if final["document_path"] or final["body_count"] != 1 or final["active_sketch"]:
        raise RuntimeError(f"{scenario} 最终文档状态不符合隔离实体模型约定。")
    for record in records:
        if record["tool"] not in {"feature.extrude", "feature.revolve"}:
            continue
        applied = record["data"].get("applied", {})
        health = applied.get("feature_health", {})
        if health != {"available": True, "error_code": 0, "warning": False}:
            raise RuntimeError(f"{record['tool']} 未取得无错误的 GetErrorCode2 证据。")
        profile = applied.get("profile_check", {})
        if not profile.get("available") or profile.get("status") != 0:
            raise RuntimeError(f"{record['tool']} 未取得有效的 CheckFeatureUse 证据。")


def _open_profile_rejected(
    gateway: SolidWorksGateway,
    registry: ToolRegistry,
    created_documents: list[Any],
) -> list[dict[str, Any]]:
    records = _execute_steps(
        gateway,
        registry,
        [
            ToolCall("session.ensure", {}),
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "front"}),
            ToolCall(
                "sketch.line",
                {"x1_mm": 0.0, "y1_mm": 0.0, "x2_mm": 5.0, "y2_mm": 0.0},
            ),
        ],
        created_documents,
    )
    before = gateway.snapshot()
    call = registry.validate(ToolCall("feature.extrude", {"depth_mm": 10.0}))
    try:
        gateway.execute(call)
    except ToolError as exc:
        error = str(exc)
    else:
        raise RuntimeError("开放轮廓未被运行时轮廓检查拒绝。")
    after = gateway.snapshot()
    comparable = ("active_sketch", "sketch_entity_count", "feature_count", "body_count")
    if any(getattr(before, name) != getattr(after, name) for name in comparable):
        raise RuntimeError("开放轮廓失败改变了草图或模型拓扑。")
    if not after.active_sketch or after.body_count != 0 or after.volume_m3 != 0.0:
        raise RuntimeError("开放轮廓失败后没有保留可修复的活动草图。")
    records.append(
        {
            "tool": "assert.open-profile-rejected",
            "message": "开放轮廓在特征写入前被拒绝，活动草图保持不变。",
            "data": {"expected_error": error},
            "before": before.to_dict(),
            "after": after.to_dict(),
        }
    )
    return records


def _rollback_mismatch(
    gateway: SolidWorksGateway,
    registry: ToolRegistry,
    created_documents: list[Any],
) -> list[dict[str, Any]]:
    records = _execute_steps(
        gateway,
        registry,
        [
            ToolCall("session.ensure", {}),
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "front"}),
            ToolCall("sketch.circle", {"radius_mm": 5.0}),
        ],
        created_documents,
    )
    before = gateway.snapshot()
    original: Callable[[Any, Any], dict[str, Any]] = gateway._extrude_feature_evidence

    def mismatched(feature: Any, document: Any) -> dict[str, Any]:
        evidence = dict(original(feature, document))
        evidence["depth_m"] = float(evidence["depth_m"]) * 2.0
        return evidence

    gateway._extrude_feature_evidence = mismatched  # type: ignore[method-assign]
    call = registry.validate(ToolCall("feature.extrude", {"depth_mm": 10.0}))
    try:
        gateway.execute(call)
    except ToolError as exc:
        error = str(exc)
    else:
        raise RuntimeError("故意制造的特征定义不一致未触发失败。")
    finally:
        gateway._extrude_feature_evidence = original  # type: ignore[assignment]
    after = gateway.snapshot()
    if (
        after.feature_count != before.feature_count
        or after.body_count != before.body_count
        or not math.isclose(after.volume_m3, before.volume_m3, abs_tol=1e-15)
    ):
        raise RuntimeError("特征定义不一致后 EditUndo2 未恢复原模型拓扑。")
    records.append(
        {
            "tool": "assert.definition-mismatch-rollback",
            "message": "特征定义读回不符后已真实撤销，模型拓扑恢复。",
            "data": {"expected_error": error},
            "before": before.to_dict(),
            "after": after.to_dict(),
        }
    )
    return records


def _new_unsaved_part(gateway: SolidWorksGateway) -> Any:
    app = gateway.connect()
    template = gateway._part_template(app)
    document = app.NewDocument(template, 0, 0.0, 0.0) if template else app.NewPart()
    if document is None or document is False:
        document = _value(app, "ActiveDoc")
    if document is None or int(_value(document, "GetType", 0) or 0) != 1:
        raise RuntimeError("上下文漂移测试无法创建第二个未保存零件。")
    return document


def _context_drift(
    gateway: SolidWorksGateway,
    registry: ToolRegistry,
    created_documents: list[Any],
) -> list[dict[str, Any]]:
    records = _execute_steps(
        gateway,
        registry,
        [
            ToolCall("session.ensure", {}),
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "front"}),
            ToolCall("sketch.circle", {"radius_mm": 2.0}),
        ],
        created_documents,
    )
    second = _new_unsaved_part(gateway)
    created_documents.append(second)
    gateway._select_plane("front")
    second.SketchManager.InsertSketch(True)
    if _value(second.SketchManager, "ActiveSketch") is None:
        raise RuntimeError("上下文漂移测试无法进入第二个零件的草图。")
    before = gateway.snapshot()
    call = registry.validate(ToolCall("sketch.circle", {"radius_mm": 1.0}))
    try:
        gateway.execute(call)
    except ToolError as exc:
        error = str(exc)
    else:
        raise RuntimeError("活动文档切换后仍允许向错误草图写入。")
    after = gateway.snapshot()
    comparable = ("active_sketch", "sketch_entity_count", "feature_count", "body_count")
    if any(getattr(before, name) != getattr(after, name) for name in comparable):
        raise RuntimeError("上下文漂移保护触发后仍改变了第二个零件。")
    if gateway._sketch_session is not None:
        raise RuntimeError("上下文漂移后未清除失效的草图会话。")
    records.append(
        {
            "tool": "assert.context-drift-rejected",
            "message": "活动文档切换在写入前被拒绝，第二个零件保持不变。",
            "data": {"expected_error": error},
            "before": before.to_dict(),
            "after": after.to_dict(),
        }
    )
    return records


def _close_unsaved_documents(gateway: SolidWorksGateway, documents: list[Any]) -> None:
    if gateway._sw is None:
        return
    closed: set[str] = set()
    for document in reversed(documents):
        title = str(_value(document, "GetTitle", ""))
        path = str(_value(document, "GetPathName", ""))
        if title and not path and title not in closed:
            gateway._sw.CloseDoc(title)
            closed.add(title)


def run(*, keep_open: bool = False, scenario: str = "cylinder") -> list[dict[str, Any]]:
    gateway = SolidWorksGateway(visible=True)
    registry = build_tool_registry()
    created_documents: list[Any] = []
    try:
        if scenario in POSITIVE_SCENARIOS:
            calls, expected_volume = _positive_scenario(scenario, registry)
            records = _execute_steps(gateway, registry, calls, created_documents)
            _assert_positive_result(scenario, records, expected_volume)
            return records
        if scenario == "open-profile-rejected":
            return _open_profile_rejected(gateway, registry, created_documents)
        if scenario == "rollback-mismatch":
            return _rollback_mismatch(gateway, registry, created_documents)
        if scenario == "context-drift":
            return _context_drift(gateway, registry, created_documents)
        raise ValueError(f"未知场景：{scenario}")
    finally:
        if not keep_open:
            _close_unsaved_documents(gateway, created_documents)
        gateway.release(close_owned=not keep_open)


def _summary(scenario: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    feature_evidence: list[dict[str, Any]] = []
    sketch_evidence: list[dict[str, Any]] = []
    for record in records:
        if record["tool"] == "sketch.polygon":
            applied = record["data"].get("applied", {})
            sketch_evidence.append(
                {
                    key: applied[key]
                    for key in ("tool", "backend", "segment_count", "commit_attempts")
                    if key in applied
                }
            )
        if record["tool"] not in {"feature.extrude", "feature.revolve"}:
            continue
        applied = record["data"].get("applied", {})
        feature_evidence.append(
            {
                key: applied[key]
                for key in (
                    "tool",
                    "operation",
                    "depth_m",
                    "angle_radians",
                    "end_condition",
                    "reverse",
                    "merge",
                    "profile_check",
                    "feature_health",
                    "axis_resolution",
                )
                if key in applied
            }
        )
    summary = {
        "ok": True,
        "scenario": scenario,
        "steps": len(records),
        "final": records[-1]["after"] if records else {},
        "assertion": records[-1]["message"] if records else "",
    }
    if feature_evidence:
        summary["feature_evidence"] = feature_evidence
    if sketch_evidence:
        summary["sketch_evidence"] = sketch_evidence
    if records and "expected_error" in records[-1]["data"]:
        summary["expected_error"] = records[-1]["data"]["expected_error"]
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run non-saving SolidWorks modeling smoke tests")
    parser.add_argument(
        "--keep-open",
        action="store_true",
        help="leave the newly created unsaved test part open for inspection",
    )
    parser.add_argument(
        "--scenario",
        choices=ALL_SCENARIOS + ("all",),
        default="cylinder",
        help="modeling scenario to execute",
    )
    parser.add_argument("--result-json", help="optional path for the complete JSON summary")
    arguments = parser.parse_args(argv)
    try:
        if arguments.scenario == "all":
            if arguments.keep_open:
                raise ValueError("all 场景不能与 --keep-open 同时使用。")
            summaries: list[dict[str, Any]] = []
            for index, name in enumerate(ALL_SCENARIOS, 1):
                print(
                    json.dumps(
                        {"progress": f"{index}/{len(ALL_SCENARIOS)}", "scenario": name}
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                summaries.append(_summary(name, run(scenario=name)))
            payload = {"ok": True, "scenarios": summaries}
        else:
            payload = _summary(
                arguments.scenario,
                run(keep_open=arguments.keep_open, scenario=arguments.scenario),
            )
    except Exception as exc:
        payload = {"ok": False, "scenario": arguments.scenario, "error": str(exc)}
    text = json.dumps(payload, ensure_ascii=True)
    if arguments.result_json:
        output = Path(arguments.result_json).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(text)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
