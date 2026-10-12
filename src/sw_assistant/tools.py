from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence, cast

from .domain import ModelSnapshot, PlanningError, ToolCall


Validator = Callable[[Any], Any]
INVESTIGATION_TOOLS = frozenset({
    "session.capabilities", "model.references", "sketch.inspect", "feature.inspect",
    "diagnostics.recent", "model.measure_extents",
})
MAX_INVESTIGATION_OUTPUT_BYTES = 60_000
Postcondition = Callable[
    [ToolCall, Mapping[str, Any], ModelSnapshot | None, ModelSnapshot | None],
    tuple[bool, str],
]


def bound_investigation_result(data: dict[str, Any]) -> dict[str, Any]:
    """Leave room for the host's 64 KB envelope, never silently truncate evidence."""
    error = "output_limit"
    try:
        size = len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if size <= MAX_INVESTIGATION_OUTPUT_BYTES:
            return data
    except (TypeError, ValueError, OverflowError, RecursionError):
        error = "invalid_output"
    result: dict[str, Any] = {
        "complete": False, "truncated": True, "output_error": error,
        "max_output_bytes": MAX_INVESTIGATION_OUTPUT_BYTES,
        "issues": ["inquiry output is invalid or exceeds 60 KB; evidence was not returned"],
    }
    for key in ("connected", "document_available"):
        if isinstance(data.get(key), bool):
            result[key] = data[key]
    limit = data.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 100:
        result["retry_hint"] = {"limit": max(1, limit // 2),
                                "note": "Repeat this inquiry with a smaller limit; partial data is not full verification."}
    return result


@dataclass(frozen=True, slots=True)
class ValidationRule:
    """Callable validator carrying the JSON Schema shown to planning models."""

    callback: Validator
    json_schema: Mapping[str, Any]

    def __call__(self, value: Any) -> Any:
        return self.callback(value)


def text(value: Any) -> str:
    result = str(value).strip()
    if not result:
        raise PlanningError("文本参数不能为空。")
    return result


def bounded_text(maximum: int) -> ValidationRule:
    def validate(value: Any) -> str:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise PlanningError(f"文本参数长度必须位于 1 到 {maximum} 个字符之间。")
        return value.strip()
    return ValidationRule(validate, {"type": "string", "minLength": 1, "maxLength": maximum})


def entity_reference(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise PlanningError("entity_ref 必须是 sketch.inspect 返回的 64 位小写实体引用。")
    return value


def number(minimum: float, maximum: float) -> ValidationRule:
    def validate(value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise PlanningError("参数必须是数值。") from exc
        if not math.isfinite(result) or not minimum <= result <= maximum:
            raise PlanningError(f"数值必须位于 {minimum:g} 到 {maximum:g} 之间。")
        return result

    return ValidationRule(
        validate,
        {"type": "number", "minimum": minimum, "maximum": maximum},
    )


def choice(
    *values: str,
    aliases: Mapping[str, str] | None = None,
) -> ValidationRule:
    allowed = {value.casefold(): value for value in values}
    normalized_aliases = {
        str(alias).strip().casefold(): canonical
        for alias, canonical in (aliases or {}).items()
    }
    invalid_aliases = set(normalized_aliases.values()) - set(values)
    if invalid_aliases:
        raise ValueError(f"choice aliases contain invalid targets: {sorted(invalid_aliases)}")

    def validate(value: Any) -> str:
        key = str(value).strip().casefold()
        key = normalized_aliases.get(key, key)
        if key not in allowed:
            received = repr(value)
            if len(received) > 120:
                received = received[:117] + "..."
            raise PlanningError(
                f"收到 {received}；参数必须是以下值之一：{', '.join(values)}。"
            )
        return allowed[key]

    return ValidationRule(validate, {"type": "string", "enum": list(values)})


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).strip().casefold() in {"true", "1", "yes", "是", "真"}:
        return True
    if str(value).strip().casefold() in {"false", "0", "no", "否", "假"}:
        return False
    raise PlanningError("参数必须是布尔值。")


boolean = ValidationRule(_boolean, {"type": "boolean"})


def integer(minimum: int, maximum: int) -> ValidationRule:
    def validate(value: Any) -> int:
        if isinstance(value, bool):
            raise PlanningError("参数必须是整数。")
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise PlanningError("参数必须是整数。") from exc
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise PlanningError("参数必须是整数。") from exc
        if not math.isfinite(numeric) or numeric != result:
            raise PlanningError("参数必须是整数。")
        if not minimum <= result <= maximum:
            raise PlanningError(f"整数必须位于 {minimum} 到 {maximum} 之间。")
        return result

    return ValidationRule(
        validate,
        {"type": "integer", "minimum": minimum, "maximum": maximum},
    )


def _line_profile_is_closed(lines: list[tuple[float, float, float, float]]) -> bool:
    """Return whether every line belongs to a connected closed contour.

    SolidWorks cannot create a normal solid boss/revolve from a dangling line.
    Coordinates are normalized to a micrometre-sized tolerance so values emitted
    independently by a model still join when they only differ by float noise.
    """

    if not lines:
        return True

    tolerance_mm = 1e-6

    def point(x: float, y: float) -> tuple[int, int]:
        return (round(x / tolerance_mm), round(y / tolerance_mm))

    adjacency: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for x1, y1, x2, y2 in lines:
        start = point(x1, y1)
        end = point(x2, y2)
        adjacency.setdefault(start, []).append(end)
        adjacency.setdefault(end, []).append(start)

    if any(len(neighbours) != 2 for neighbours in adjacency.values()):
        return False

    remaining = set(adjacency)
    while remaining:
        seed = next(iter(remaining))
        stack = [seed]
        component: set[tuple[int, int]] = set()
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(adjacency[current])
        if len(component) < 3 or any(len(adjacency[item]) != 2 for item in component):
            return False
        ordered = [seed]
        previous = None
        current = seed
        while True:
            neighbours = adjacency[current]
            following = neighbours[0] if neighbours[0] != previous else neighbours[1]
            if following == seed:
                break
            if following in ordered or len(ordered) > len(component):
                return False
            ordered.append(following)
            previous, current = current, following
        if len(ordered) != len(component):
            return False
        doubled_area = sum(
            ordered[index][0] * ordered[(index + 1) % len(ordered)][1]
            - ordered[(index + 1) % len(ordered)][0] * ordered[index][1]
            for index in range(len(ordered))
        )
        if doubled_area == 0:
            return False
        remaining.difference_update(component)
    return True


@dataclass(frozen=True, slots=True)
class Field:
    validator: Validator
    required: bool = True
    default: Any = None
    description: str = ""

    def schema(self) -> dict[str, Any]:
        result = dict(getattr(self.validator, "json_schema", {}))
        if self.description:
            result["description"] = self.description
        if not self.required:
            result["default"] = self.default
        return result


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    fields: dict[str, Field] = field(default_factory=dict)
    postcondition: Postcondition | None = None
    write_scope: str = "none"

    def validate(self, call: ToolCall) -> ToolCall:
        if call.tool != self.name:
            raise PlanningError(f"工具名称不匹配：{call.tool}")
        unknown = set(call.arguments) - set(self.fields)
        if unknown:
            raise PlanningError(
                f"{self.name} 包含未知参数：{', '.join(sorted(unknown))}",
                details={"tool": self.name, "unknown_fields": sorted(unknown)},
            )
        normalized: dict[str, Any] = {}
        for name, definition in self.fields.items():
            if name not in call.arguments:
                if definition.required:
                    raise PlanningError(
                        f"{self.name} 缺少必填参数 {name}。",
                        details={
                            "tool": self.name,
                            "field": name,
                            "expected_schema": definition.schema(),
                        },
                    )
                normalized[name] = definition.default
                continue
            try:
                normalized[name] = definition.validator(call.arguments[name])
            except PlanningError as exc:
                raise PlanningError(
                    f"{self.name}.{name}: {exc}",
                    details={
                        **exc.details,
                        "tool": self.name,
                        "field": name,
                        "received_value": call.arguments[name],
                        "expected_schema": definition.schema(),
                    },
                ) from exc
        return ToolCall(self.name, normalized, call.description, call.id)

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "write_scope": self.write_scope,
            "arguments": {
                "type": "object",
                "properties": {
                    name: value.schema() for name, value in self.fields.items()
                },
                "required": [
                    name for name, value in self.fields.items() if value.required
                ],
                "additionalProperties": False,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def validate(self, call: ToolCall) -> ToolCall:
        try:
            spec = self._specs[call.tool]
        except KeyError as exc:
            raise PlanningError(f"不支持的工具：{call.tool}") from exc
        return spec.validate(call)

    def validate_plan(self, calls: list[ToolCall]) -> list[ToolCall]:
        validated: list[ToolCall] = []
        for index, call in enumerate(calls, 1):
            try:
                validated.append(self.validate(call))
            except PlanningError as exc:
                raise PlanningError(
                    f"第 {index} 步 ({call.tool}) 参数校验失败：{exc}",
                    details={
                        **exc.details,
                        "step_index": index,
                        "step_id": call.id,
                        "tool": call.tool,
                    },
                ) from exc
        document_mode = "none"
        sketch_active = False
        sketch_entities = 0
        sketch_closed_entities = 0
        sketch_lines: list[tuple[float, float, float, float]] = []
        has_revolve_axis = False
        has_body = False
        document_steps = 0
        for index, call in enumerate(validated, 1):
            tool = call.tool
            if tool == "document.new_part":
                document_steps += 1
                if document_steps > 1:
                    raise PlanningError(f"第 {index} 步不能在同一计划中替换当前文档。")
                document_mode = "part-writable"
                sketch_active = False
                sketch_entities = 0
                sketch_closed_entities = 0
                sketch_lines = []
                has_revolve_axis = False
                has_body = False
            elif tool == "document.open_readonly":
                document_steps += 1
                if document_steps > 1:
                    raise PlanningError(f"第 {index} 步不能在同一计划中替换当前文档。")
                document_mode = "readonly"
                sketch_active = False
                sketch_entities = 0
                sketch_closed_entities = 0
                sketch_lines = []
                has_revolve_axis = False
                has_body = False
            elif tool == "selection.plane":
                if document_mode != "part-writable":
                    raise PlanningError(f"第 {index} 步选择基准面前必须新建可写零件。")
            elif tool == "sketch.begin":
                if document_mode != "part-writable":
                    raise PlanningError(f"第 {index} 步创建草图前必须新建可写零件。")
                if sketch_active:
                    raise PlanningError(f"第 {index} 步不能在活动草图内再次创建草图。")
                sketch_active = True
                sketch_entities = 0
                sketch_closed_entities = 0
                sketch_lines = []
                has_revolve_axis = False
            elif tool == "sketch.begin_face":
                if document_mode != "part-writable" or not has_body:
                    raise PlanningError(
                        f"第 {index} 步在特征端面创建草图前必须先存在可写实体。"
                    )
                if sketch_active:
                    raise PlanningError(f"第 {index} 步不能在活动草图内再次创建草图。")
                sketch_active = True
                sketch_entities = 0
                sketch_closed_entities = 0
                sketch_lines = []
                has_revolve_axis = False
            elif tool in {"sketch.circle", "sketch.rectangle", "sketch.polygon"}:
                if not sketch_active:
                    raise PlanningError(f"第 {index} 步绘制草图实体前必须先创建草图。")
                sketch_entities += 1
                sketch_closed_entities += 1
            elif tool == "sketch.set_circle_radius":
                if document_mode != "part-writable" or not sketch_active or sketch_entities == 0:
                    raise PlanningError(f"第 {index} 步修改圆半径前必须有本任务创建的活动草图实体。")
            elif tool == "sketch.line":
                if not sketch_active:
                    raise PlanningError(f"第 {index} 步绘制草图实体前必须先创建草图。")
                arguments = call.arguments
                sketch_lines.append(
                    (
                        arguments["x1_mm"],
                        arguments["y1_mm"],
                        arguments["x2_mm"],
                        arguments["y2_mm"],
                    )
                )
                sketch_entities += 1
            elif tool == "sketch.centerline":
                if not sketch_active:
                    raise PlanningError(f"第 {index} 步绘制中心线前必须先创建草图。")
                if has_revolve_axis:
                    raise PlanningError(f"第 {index} 步同一旋转草图只能定义一条中心轴。")
                has_revolve_axis = True
            elif tool in {"feature.extrude", "feature.revolve"}:
                if not sketch_active or sketch_entities == 0:
                    raise PlanningError(f"第 {index} 步建模前必须存在包含几何轮廓的活动草图。")
                if tool == "feature.revolve" and not has_revolve_axis:
                    raise PlanningError(f"第 {index} 步旋转前必须在当前草图中创建中心线。")
                if sketch_lines and not _line_profile_is_closed(sketch_lines):
                    raise PlanningError(
                        f"第 {index} 步建模前的直线轮廓没有闭合；"
                        "请补齐缺失边或改用闭合草图工具。"
                    )
                if sketch_closed_entities == 0 and not sketch_lines:
                    raise PlanningError(f"第 {index} 步建模前没有闭合轮廓。")
                if call.arguments["operation"] == "cut" and not has_body:
                    raise PlanningError(f"第 {index} 步切除前必须先存在实体。")
                if call.arguments["operation"] == "boss":
                    has_body = True
                sketch_active = False
                sketch_entities = 0
                sketch_closed_entities = 0
                sketch_lines = []
                has_revolve_axis = False
        return validated

    def postcondition(self, name: str) -> Postcondition | None:
        return self._specs[name].postcondition

    def write_scope(self, name: str) -> str:
        return self._specs[name].write_scope

    def describe(self) -> list[dict[str, Any]]:
        return [self._specs[name].schema() for name in sorted(self._specs)]


def _connected(
    _call: ToolCall,
    _data: Mapping[str, Any],
    _before: ModelSnapshot | None,
    after: ModelSnapshot | None,
) -> tuple[bool, str]:
    return (bool(after and after.connected), "SolidWorks 会话未连接。")


def _new_document(
    _call: ToolCall,
    _data: Mapping[str, Any],
    before: ModelSnapshot | None,
    after: ModelSnapshot | None,
) -> tuple[bool, str]:
    changed = bool(after and after.document_type == "part" and after.document_title)
    if before and after and before.document_title == after.document_title:
        changed = False
    return changed, "未检测到新的零件文档。"


def _selection(
    _call: ToolCall,
    _data: Mapping[str, Any],
    _before: ModelSnapshot | None,
    after: ModelSnapshot | None,
) -> tuple[bool, str]:
    return (bool(after and after.selection_count > 0), "未检测到基准面选择。")


def _sketch(
    _call: ToolCall,
    _data: Mapping[str, Any],
    _before: ModelSnapshot | None,
    after: ModelSnapshot | None,
) -> tuple[bool, str]:
    return (bool(after and after.active_sketch), "SolidWorks 未进入草图编辑状态。")


def _close_number(left: Any, right: Any, *, tolerance: float = 1e-9) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=tolerance)
    except (TypeError, ValueError):
        return False


def _circle_radius_changed(call: ToolCall, data: Mapping[str, Any], before: ModelSnapshot | None,
                           after: ModelSnapshot | None) -> tuple[bool, str]:
    error = "圆半径修改缺少完整、范围一致的几何验证证据。"
    try:
        applied = data["applied"]
        if (not isinstance(applied, Mapping) or applied.get("tool") != call.tool
                or applied.get("entity_ref") != call.arguments["entity_ref"]
                or applied.get("geometry_verified") is not True or applied.get("requires_review") is not False
                or applied.get("unchanged_other_entities") is not True or before is None or after is None):
            return False, error
        if (not before.active_sketch or before.active_sketch != after.active_sketch
                or any(getattr(before, key) != getattr(after, key) for key in (
                    "document_title", "document_path", "document_type", "feature_count", "body_count",
                    "sketch_entity_count", "selection_count"))
                or not math.isclose(before.volume_m3, after.volume_m3, rel_tol=1e-7, abs_tol=1e-12)):
            return False, error
        old, new = applied["geometry_before"], applied["geometry_after"]
        if old.get("geometry_complete") is not True or new.get("geometry_complete") is not True:
            return False, error
        if old.get("sketch_ref") != applied.get("sketch_ref") or old.get("sketch_name") != before.active_sketch:
            return False, error
        old_matches = [item for item in old["segments"] if item["entity_ref"] == call.arguments["entity_ref"]]
        new_matches = [item for item in new["segments"] if item["entity_ref"] == call.arguments["entity_ref"]]
        if len(old_matches) != 1 or len(new_matches) != 1 or old_matches[0].get("is_circle") is not True:
            return False, error
        if (not _close_number(old_matches[0]["radius_mm"], call.arguments["expected_radius_mm"], tolerance=1e-6)
                or not _close_number(new_matches[0]["radius_mm"], call.arguments["radius_mm"], tolerance=1e-6)):
            return False, error
        expected = json.loads(json.dumps(old, allow_nan=False))
        next(item for item in expected["segments"] if item["entity_ref"] == call.arguments["entity_ref"])["radius_mm"] = new_matches[0]["radius_mm"]
        return expected == new, error
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        return False, error


def _close_point(value: Any, expected: tuple[float, float, float]) -> bool:
    return bool(
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(_close_number(value[index], expected[index]) for index in range(3))
    )


def _frame_point(frame: Mapping[str, Any], x_mm: float, y_mm: float) -> tuple[float, float, float]:
    origin = frame.get("origin")
    u = frame.get("u")
    v = frame.get("v")
    if not all(isinstance(value, (list, tuple)) and len(value) == 3 for value in (origin, u, v)):
        raise ValueError("草图坐标证据缺少三维平面基向量。")
    origin_values = tuple(float(value) for value in cast(Sequence[Any], origin))
    u_values = tuple(float(value) for value in cast(Sequence[Any], u))
    v_values = tuple(float(value) for value in cast(Sequence[Any], v))
    x_m = float(x_mm) / 1000.0
    y_m = float(y_mm) / 1000.0
    return (
        origin_values[0] + x_m * u_values[0] + y_m * v_values[0],
        origin_values[1] + x_m * u_values[1] + y_m * v_values[1],
        origin_values[2] + x_m * u_values[2] + y_m * v_values[2],
    )


def _applied_geometry(call: ToolCall, data: Mapping[str, Any]) -> tuple[bool, str]:
    if data.get("manual_verification") and call.tool not in {"feature.extrude", "feature.revolve"}:
        return True, ""
    applied = data.get("applied")
    if not isinstance(applied, Mapping):
        return False, "SolidWorks 没有返回可核对的几何参数证据。"
    if str(applied.get("tool") or "") != call.tool:
        return False, "SolidWorks 返回的几何参数证据与当前工具不匹配。"

    arguments = call.arguments
    try:
        if call.tool == "sketch.circle":
            expected = _frame_point(
                applied.get("frame", {}), arguments["x_mm"], arguments["y_mm"]
            )
            valid = _close_point(applied.get("center_m"), expected) and _close_number(
                applied.get("radius_m"), float(arguments["radius_mm"]) / 1000.0
            )
        elif call.tool == "sketch.rectangle":
            frame = applied.get("frame", {})
            expected_center = _frame_point(
                frame, arguments["center_x_mm"], arguments["center_y_mm"]
            )
            expected_corner = _frame_point(
                frame,
                float(arguments["center_x_mm"]) + float(arguments["width_mm"]) / 2.0,
                float(arguments["center_y_mm"]) + float(arguments["height_mm"]) / 2.0,
            )
            valid = _close_point(applied.get("center_m"), expected_center) and _close_point(
                applied.get("corner_m"), expected_corner
            )
        elif call.tool == "sketch.polygon":
            frame = applied.get("frame", {})
            points = applied.get("points_m")
            rotation = math.radians(float(arguments["rotation_deg"]))
            sides = int(arguments["sides"])
            expected_points = [
                _frame_point(
                    frame,
                    float(arguments["x_mm"])
                    + float(arguments["radius_mm"])
                    * math.cos(rotation + 2.0 * math.pi * index / sides),
                    float(arguments["y_mm"])
                    + float(arguments["radius_mm"])
                    * math.sin(rotation + 2.0 * math.pi * index / sides),
                )
                for index in range(sides)
            ]
            valid = bool(
                isinstance(points, list)
                and len(points) == sides
                and all(_close_point(points[index], expected_points[index]) for index in range(sides))
            )
        elif call.tool in {"sketch.line", "sketch.centerline"}:
            frame = applied.get("frame", {})
            valid = _close_point(
                applied.get("start_m"),
                _frame_point(frame, arguments["x1_mm"], arguments["y1_mm"]),
            ) and _close_point(
                applied.get("end_m"),
                _frame_point(frame, arguments["x2_mm"], arguments["y2_mm"]),
            )
        elif call.tool == "feature.extrude":
            valid = (
                _close_number(applied.get("depth_m"), float(arguments["depth_mm"]) / 1000.0)
                and str(applied.get("operation")) == str(arguments["operation"])
                and str(applied.get("end_condition")) == str(arguments["end_condition"])
                and applied.get("reverse") is arguments["reverse"]
                and (
                    str(arguments["operation"]) == "cut"
                    or applied.get("merge") is arguments["merge"]
                )
            )
        elif call.tool == "feature.revolve":
            valid = (
                _close_number(
                    applied.get("angle_radians"), math.radians(float(arguments["angle_deg"]))
                )
                and str(applied.get("operation")) == str(arguments["operation"])
                and applied.get("reverse") is arguments["reverse"]
                and (
                    str(arguments["operation"]) == "cut"
                    or applied.get("merge") is arguments["merge"]
                )
            )
        else:
            valid = True
    except (KeyError, TypeError, ValueError, OverflowError):
        valid = False
    return valid, "SolidWorks 实际使用的几何参数与计划参数不一致。"


def _sketch_entity(
    call: ToolCall,
    data: Mapping[str, Any],
    before: ModelSnapshot | None,
    after: ModelSnapshot | None,
) -> tuple[bool, str]:
    if not (before and after and after.sketch_entity_count > before.sketch_entity_count):
        return False, "草图实体数量没有增加。"
    return _applied_geometry(call, data)


def _solid_feature_change(
    before_count: Any, before_volume: Any, after_count: Any, after_volume: Any,
    *, operation: str, merge: bool,
) -> tuple[bool, str]:
    """A feature must change solid material beyond numerical mass-property noise.

    Relative tolerance (1e-12 of the larger volume) plus 64 representable float
    steps filters readback noise without imposing a minimum physical part size.
    This is a progress check, not proof of the requested final geometry. Changes
    below the readback's resolution require better evidence, not a success flag.
    Unknown/non-finite measurements never mean zero.
    """
    if (any(type(value) is not int or value < 0 for value in (before_count, after_count))
            or any(type(value) not in (int, float) for value in (before_volume, after_volume))):
        return False, "实体数量或体积测量未知，不能确认特征成功。"
    try:
        before_volume, after_volume = float(before_volume), float(after_volume)
    except (ValueError, OverflowError):
        return False, "实体体积超出可验证数值范围，不能确认特征成功。"
    if any(not math.isfinite(value) or value < 0 for value in (before_volume, after_volume)):
        return False, "实体数量或体积测量未知，不能确认特征成功。"
    if ((before_count == 0) != (before_volume == 0)
            or (after_count == 0) != (after_volume == 0)):
        return False, "实体数量与体积读回不一致，不能确认特征成功。"
    tolerance = max(
        1e-12 * max(before_volume, after_volume),
        64 * max(math.ulp(before_volume), math.ulp(after_volume)),
    )
    delta = after_volume - before_volume
    if operation == "boss":
        if type(merge) is not bool:
            return False, "合并实体选项未知，不能确认特征成功。"
        if merge and after_count > max(before_count, 1):
            return False, "要求合并实体，但操作产生了新的独立实体。"
        if not merge and after_count < before_count:
            return False, "未要求合并实体，但凸台操作丢失了已有实体。"
        if after_count < 1 or delta <= tolerance:
            return False, "凸台没有产生超过数值容差的材料增量；重复或重合操作不算进展。"
    elif operation == "cut":
        # A cut may split one solid into several, or remove a complete body.
        # Body-count direction alone therefore cannot verify material removal.
        if before_count < 1 or delta >= -tolerance:
            return False, "切除没有产生超过数值容差的材料减少。"
    else:
        return False, "未知实体操作，不能确认特征成功。"
    return True, ""


def _feature(
    call: ToolCall,
    data: Mapping[str, Any],
    before: ModelSnapshot | None,
    after: ModelSnapshot | None,
) -> tuple[bool, str]:
    if (before is None or after is None or after.active_sketch != ""
            or type(before.feature_count) is not int or type(after.feature_count) is not int
            or before.feature_count < 0 or after.feature_count <= before.feature_count):
        return False, "未检测到预期特征或实体变化。"
    if any(getattr(before, key) != getattr(after, key) for key in (
        "document_title", "document_path", "document_type",
    )):
        return False, "创建特征期间活动文档改变，不能将不同文档的几何变化作为成功证据。"
    changed, reason = _solid_feature_change(
        before.body_count, before.volume_m3, after.body_count, after.volume_m3,
        operation=call.arguments.get("operation", "boss"), merge=call.arguments.get("merge", True),
    )
    if not changed:
        return False, reason
    return _applied_geometry(call, data)


def _path_value(value: Any) -> str:
    path = str(value).strip()
    if not path:
        raise PlanningError("文件路径不能为空。")
    return path


_path = ValidationRule(_path_value, {"type": "string", "minLength": 1})

_PLANE_ALIASES = {
    "front plane": "front",
    "front_plane": "front",
    "frontplane": "front",
    "前视": "front",
    "前视面": "front",
    "前视基准面": "front",
    "top plane": "top",
    "top_plane": "top",
    "topplane": "top",
    "上视": "top",
    "上视面": "top",
    "上视基准面": "top",
    "right plane": "right",
    "right_plane": "right",
    "rightplane": "right",
    "右视": "right",
    "右视面": "right",
    "右视基准面": "right",
}

_OPERATION_ALIASES = {
    "add": "boss",
    "凸台": "boss",
    "拉伸凸台": "boss",
    "添加材料": "boss",
    "remove": "cut",
    "切除": "cut",
    "拉伸切除": "cut",
    "移除材料": "cut",
}

_END_CONDITION_ALIASES = {
    "given depth": "blind",
    "给定深度": "blind",
    "mid plane": "mid_plane",
    "midplane": "mid_plane",
    "中面": "mid_plane",
}


def build_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec("session.ensure", "连接或启动 SolidWorks", postcondition=_connected))
    registry.register(
        ToolSpec(
            "document.new_part",
            "新建未保存零件",
            postcondition=_new_document,
            write_scope="application",
        )
    )
    registry.register(
        ToolSpec(
            "document.open_readonly",
            "以静默只读方式打开文档",
            {"path": Field(_path)},
            postcondition=lambda _call, _data, _before, after: (
                bool(after and after.document_path),
                "文档没有打开。",
            ),
            write_scope="application",
        )
    )
    registry.register(
        ToolSpec(
            "selection.plane",
            "选择稳定基准面",
            {
                "plane": Field(
                    choice("front", "top", "right", aliases=_PLANE_ALIASES),
                    description="基准面内部标识；必须使用枚举值",
                )
            },
            postcondition=_selection,
            write_scope="document",
        )
    )
    registry.register(
        ToolSpec(
            "sketch.begin",
            "在基准面创建草图；front(x,y)->世界(X,Y)，top(x,y)->世界(X,-Z)，"
            "right(x,y)->世界(Y,Z)",
            {
                "plane": Field(
                    choice("front", "top", "right", aliases=_PLANE_ALIASES),
                    description="基准面内部标识；必须与选中的基准面一致",
                )
            },
            postcondition=_sketch,
            write_scope="document",
        )
    )
    registry.register(
        ToolSpec(
            "sketch.begin_face",
            "在上一建模特征最外侧的平面端面创建草图",
            postcondition=_sketch,
            write_scope="document",
        )
    )
    registry.register(
        ToolSpec(
            "sketch.circle",
            "绘制圆",
            {
                "x_mm": Field(number(-1_000_000, 1_000_000), required=False, default=0.0),
                "y_mm": Field(number(-1_000_000, 1_000_000), required=False, default=0.0),
                "radius_mm": Field(number(0.0001, 1_000_000)),
            },
            postcondition=_sketch_entity,
            write_scope="document",
        )
    )
    registry.register(ToolSpec(
        "sketch.set_circle_radius",
        "仅修改本任务已验证创建的当前草图完整圆半径；先用 sketch.inspect 读取 entity_ref 与旧半径，失败必须停止核对",
        {"entity_ref": Field(ValidationRule(entity_reference, {"type": "string", "pattern": "^[a-f0-9]{64}$"})),
         "expected_radius_mm": Field(number(0.0001, 1_000_000)),
         "radius_mm": Field(number(0.0001, 1_000_000))},
        postcondition=_circle_radius_changed, write_scope="document",
    ))
    registry.register(
        ToolSpec(
            "sketch.rectangle",
            "绘制中心矩形",
            {
                "center_x_mm": Field(number(-1_000_000, 1_000_000), required=False, default=0.0),
                "center_y_mm": Field(number(-1_000_000, 1_000_000), required=False, default=0.0),
                "width_mm": Field(number(0.0001, 1_000_000)),
                "height_mm": Field(number(0.0001, 1_000_000)),
            },
            postcondition=_sketch_entity,
            write_scope="document",
        )
    )
    registry.register(
        ToolSpec(
            "sketch.polygon",
            "绘制闭合正多边形；radius_mm 是外接圆半径",
            {
                "x_mm": Field(number(-1_000_000, 1_000_000), required=False, default=0.0),
                "y_mm": Field(number(-1_000_000, 1_000_000), required=False, default=0.0),
                "radius_mm": Field(number(0.0001, 1_000_000)),
                "sides": Field(integer(3, 128), required=False, default=6),
                "rotation_deg": Field(number(-360.0, 360.0), required=False, default=0.0),
            },
            postcondition=_sketch_entity,
            write_scope="document",
        )
    )
    line_fields = {
        "x1_mm": Field(number(-1_000_000, 1_000_000)),
        "y1_mm": Field(number(-1_000_000, 1_000_000)),
        "x2_mm": Field(number(-1_000_000, 1_000_000)),
        "y2_mm": Field(number(-1_000_000, 1_000_000)),
    }
    registry.register(
        ToolSpec(
            "sketch.line",
            "绘制直线轮廓",
            line_fields,
            postcondition=_sketch_entity,
            write_scope="document",
        )
    )
    registry.register(
        ToolSpec(
            "sketch.centerline",
            "绘制旋转中心线",
            line_fields,
            postcondition=_sketch_entity,
            write_scope="document",
        )
    )
    registry.register(
        ToolSpec(
            "feature.extrude",
            "创建拉伸凸台或切除",
            {
                "depth_mm": Field(number(0.0001, 1_000_000)),
                "operation": Field(
                    choice("boss", "cut", aliases=_OPERATION_ALIASES),
                    required=False,
                    default="boss",
                ),
                "end_condition": Field(
                    choice(
                        "blind", "mid_plane", aliases=_END_CONDITION_ALIASES
                    ),
                    required=False,
                    default="blind",
                ),
                "reverse": Field(boolean, required=False, default=False),
                "merge": Field(boolean, required=False, default=True),
            },
            postcondition=_feature,
            write_scope="document",
        )
    )
    registry.register(
        ToolSpec(
            "feature.revolve",
            "围绕当前草图中心线创建旋转凸台或切除",
            {
                "angle_deg": Field(number(0.0001, 360.0), required=False, default=360.0),
                "operation": Field(
                    choice("boss", "cut", aliases=_OPERATION_ALIASES),
                    required=False,
                    default="boss",
                ),
                "reverse": Field(boolean, required=False, default=False),
                "merge": Field(boolean, required=False, default=True),
            },
            postcondition=_feature,
            write_scope="document",
        )
    )
    registry.register(ToolSpec("model.inspect", "读取当前模型状态"))
    registry.register(ToolSpec(
        "model.observe",
        "只读观察当前文档、基准面、特征错误、实体体积和尺寸；不连接、不打开文件、不改变选择",
        {"include_configuration": Field(boolean, required=False, default=False)},
    ))
    registry.register(ToolSpec(
        "model.measure_extents",
        "只读测量当前零件所有实体（含隐藏实体，最多64个）的模型坐标系X/Y/Z外廓尺寸；"
        "使用六向几何极值，不使用近似包围盒，不是原生命名尺寸；无实体或未知读取不返回零尺寸",
    ))
    registry.register(ToolSpec(
        "session.capabilities",
        "只读查询运行版本、已实现工具及 API 成员可见性；不启动 SolidWorks，成员存在不代表操作必成功",
    ))
    registry.register(ToolSpec(
        "model.references",
        "只读枚举当前文档的基准面和平面面候选，返回持久引用及几何签名；不选择或修改模型",
        {"limit": Field(integer(1, 100), required=False, default=30)},
    ))
    registry.register(ToolSpec(
        "sketch.inspect",
        "只读检查活动草图实体、构造线及轮廓状态；未知结果不会当作有效闭合轮廓",
    ))
    registry.register(ToolSpec(
        "feature.inspect",
        "按精确名称只读查询特征类型、错误码、尺寸与面摘要；limit 分别限制尺寸和面读取数，可减小以缩短结果；不改变模型",
        {"name": Field(bounded_text(250)),
         "limit": Field(integer(1, 100), required=False, default=30)},
    ))
    registry.register(ToolSpec(
        "diagnostics.recent",
        "查询当前任务内已有的最近失败及建议调查工具；不读取外部日志、文件或执行 CAD",
        {"limit": Field(integer(1, 10), required=False, default=3)},
    ))
    return registry
