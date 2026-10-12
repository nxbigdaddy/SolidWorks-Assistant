"""Local requirement provenance and monotonic acceptance-goal policy.

Clause coverage is a mechanical completeness gate, not proof that a language
model has understood every semantic detail. The original input remains visible.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

from .task_models import GoalCheck, RequirementCheck


def is_control_message(text: str) -> bool:
    """Exact conservative allowlist, never substring or intent classification."""
    normalized = re.sub(r"\s+", " ", str(text)).strip().casefold()
    normalized = normalized.strip(" 。！!？?；;，,.")
    return normalized in {
        "继续", "请继续", "继续执行", "请继续执行", "继续运行", "请继续运行",
        "恢复执行", "请恢复执行", "resume", "continue", "please continue", "please resume",
    }


def migrate_control_requirements(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Locally downgrade only provable legacy control sources; retain all text/goals."""
    messages = {item.get("id"): item for item in task.get("messages", []) if item.get("role") == "user"}
    changes = []
    requirements = task.get("requirements", [])
    for item in requirements:
        if item.get("source_kind") == "control" or item.get("source_message_id") == "initial":
            continue
        message = messages.get(item.get("source_message_id"), {})
        descendants = [child for child in requirements if child.get("parent_id") == item.get("id")]
        if (not item.get("parent_id") and not descendants and is_control_message(message.get("content", ""))
                and is_control_message(item.get("source_text", ""))
                and is_control_message(item.get("description", ""))):
            item["source_kind"] = "control"
            changes.append({"requirement_id": item["id"], "source_message_id": item["source_message_id"],
                            "source_text": item["source_text"], "reason": "exact_control_source",
                            "previous_source_kind": "requirement", "source_kind": "control"})
    return changes


def seed_requirements(text: str, message_id: str, start: int = 1) -> list[dict[str, Any]]:
    clauses = [part.strip() for part in re.split(r"(?<=[。！？!?；;])\s*|\n+", text) if part.strip()]
    clauses = [part for part in clauses if not is_control_message(part)]
    if not clauses:
        return []
    # Avoid truncating input: very long prompts retain their full text in messages.
    if len(clauses) > 32:
        clauses = [text]
    return [RequirementCheck(
        id=f"r{start + index}", description=clause[:4000], source_text=clause[:4000],
        source_message_id=message_id,
    ).model_dump(mode="json") for index, clause in enumerate(clauses)]


def _range(goal: GoalCheck) -> tuple[float, float]:
    value = float(goal.expected)
    return (
        -math.inf if goal.operator == "le" else value - goal.tolerance,
        math.inf if goal.operator == "ge" else value + goal.tolerance,
    )


def observed_target(goal: GoalCheck, observation: dict[str, Any]) -> bool:
    if observation.get("complete") is not True:
        return False
    key = "dimensions" if goal.metric == "dimension_mm" else "features"
    for item in observation.get(key, []):
        if item.get("name") != goal.target:
            continue
        if goal.metric == "dimension_mm":
            value = item.get("value_mm")
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        return isinstance(item.get("type"), str) and bool(item["type"]) and item.get("error_code") is not None
    return False


def dimension_binding_issues(goals: list[dict[str, Any]]) -> list[str]:
    """Independent length goals cannot reuse one parameter or one model axis."""
    targets: dict[tuple[str, str], list[str]] = {}
    for goal in goals:
        if goal.get("metric") in {"dimension_mm", "extent_mm"} and goal.get("binding", "fixed") == "fixed" and goal.get("target"):
            targets.setdefault((goal["metric"], goal["target"]), []).append(goal["id"])
    return [f"独立尺寸目标 {', '.join(ids)} 不能复用同一参数或外廓轴 {metric}:{target}；"
            "相同真实要求应引用同一个 goal_id，不得拿拉伸深度冒充草图边长。"
            for (metric, target), ids in targets.items() if len(ids) > 1]


def refine_goals(
    existing: list[dict[str, Any]], proposed: list[GoalCheck], observation: dict[str, Any],
    *, bindings: dict[str, Any] | None = None, audit: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    proposed = [item.model_copy(deep=True) for item in proposed] if proposed else [
        GoalCheck.model_validate(item) for item in existing]
    values = {item.id: item for item in proposed}
    bindings = bindings if bindings is not None else {}
    audit = audit if audit is not None else []
    for raw in existing:
        previous = GoalCheck.model_validate(raw)
        current = values.get(previous.id)
        if current is None:
            raise ValueError("已锁定的验收目标不得删除。")
        if current.metric != previous.metric:
            raise ValueError("目标细化不得改变测量指标。")
        binding = bindings.get(previous.id, {})
        if previous.binding == "pending" and current.binding == "fixed":
            if not observed_target(current, observation):
                raise ValueError("尺寸或特征目标必须先通过真实观察确认名称，再完成绑定。")
        elif current.target != previous.target or current.binding != previous.binding:
            if not (previous.binding == current.binding == "fixed" and binding.get("legacy_unverified")
                    and not binding.get("verified") and not binding.get("rebindings", 0)
                    and not observed_target(previous, observation) and observed_target(current, observation)):
                raise ValueError("已确认的目标引用不得改变或重新设为待绑定。")
            if (current.operator, current.expected, current.tolerance) != (
                    previous.operator, previous.expected, previous.tolerance):
                raise ValueError("历史引用修复只能绑定真实名称，不得同时改变数值、运算符或容差。")
            bindings[previous.id] = {**binding, "rebindings": 1, "legacy_unverified": False}
            audit.append({"goal_id": previous.id, "kind": "legacy_reference_rebound",
                          "previous_target": previous.target, "target": current.target,
                          "metric": current.metric, "expected": current.expected,
                          "operator": current.operator, "tolerance": current.tolerance})
        if previous.metric in {"document_type", "feature_present"}:
            if current.expected != previous.expected:
                raise ValueError("目标细化不得改变原验收结果。")
        else:
            old_low, old_high = _range(previous)
            new_low, new_high = _range(current)
            if new_low < old_low or new_high > old_high:
                raise ValueError("目标细化不得放宽验收范围。")
    existing_ids = {item["id"] for item in existing}
    for current in proposed:
        if current.metric not in {"dimension_mm", "feature_present"}:
            continue
        observed = observed_target(current, observation)
        binding = bindings.get(current.id, {})
        if current.id not in existing_ids and not observed:
            current.binding = "pending"
        elif observed:
            current.binding = "fixed"
            if not binding.get("verified"):
                audit.append({"goal_id": current.id, "kind": "reference_observed", "target": current.target,
                              "metric": current.metric})
            bindings[current.id] = {**binding, "verified": True, "target": current.target,
                                    "metric": current.metric, "legacy_unverified": False}
    result = [item.model_dump(mode="json") for item in proposed]
    conflicts = dimension_binding_issues(result)
    if conflicts:
        raise ValueError("；".join(conflicts))
    return result


def merge_requirements(
    existing: list[dict[str, Any]], proposed: list[RequirementCheck],
    goals: list[dict[str, Any]], *, require_complete: bool,
) -> list[dict[str, Any]]:
    values = {item["id"]: copy.deepcopy(item) for item in existing}
    goal_ids = {item["id"] for item in goals}
    for candidate in proposed:
        value = candidate.model_dump(mode="json")
        previous = values.get(candidate.id)
        if previous is not None:
            for key in ("description", "source_message_id", "source_text", "parent_id"):
                if value[key] and value[key] != previous[key]:
                    raise ValueError("原始要求及已记录子目标的来源不可改写。")
                value[key] = previous[key]
            if candidate.source_kind == "control" and previous.get("source_kind", "requirement") != "control":
                raise ValueError("只有宿主可根据确证的纯控制消息分类来源，Agent 不得降级用户要求。")
            value["source_kind"] = previous.get("source_kind", "requirement")
            if not set(previous["goal_ids"]).issubset(value["goal_ids"]):
                raise ValueError("已绑定的要求验收指标不可删除。")
        else:
            parent = values.get(candidate.parent_id)
            if parent is None or not candidate.description.strip():
                raise ValueError("新增子目标必须关联已存在的需求 parent_id，并说明具体内容。")
            if candidate.source_kind == "control" or parent.get("source_kind") == "control":
                raise ValueError("控制来源不能用于新增几何子目标或降级真实要求。")
            if candidate.source_text and candidate.source_text not in parent["source_text"]:
                raise ValueError("子目标引用必须来自对应的原始需求文本。")
            value["source_text"] = candidate.source_text or parent["source_text"]
            value["source_message_id"] = parent["source_message_id"]
        if not set(value["goal_ids"]).issubset(goal_ids):
            raise ValueError("需求 goal_ids 引用了不存在的验收目标。")
        values[candidate.id] = value
    result = list(values.values())
    if len(result) > 96:
        raise ValueError("本任务需求数量超过 96 项上限，请拆分任务。")
    if require_complete:
        issues = contract_issues(result, goals)
        if issues:
            raise ValueError("验收合同尚未完整：" + "；".join(issues))
    return result


def contract_issues(
    requirements: list[dict[str, Any]], goals: list[dict[str, Any]],
    *, verified_geometry_ids: set[str] | None = None,
) -> list[str]:
    if not requirements or not any(item.get("source_kind") != "control" for item in requirements):
        return ["缺少原始用户需求与子目标记录"]
    goals_by_id = {item["id"]: item for item in goals}
    requirements_by_id = {item["id"]: item for item in requirements}
    issues: list[str] = dimension_binding_issues(goals)

    def linked(identifier: str, visited: set[str]) -> set[str]:
        if identifier in visited:
            return set()
        visited.add(identifier)
        item = requirements_by_id[identifier]
        found = set(item.get("goal_ids", []))
        for child in requirements:
            if child.get("parent_id") == identifier:
                found.update(linked(child["id"], visited))
        return found

    for requirement in requirements:
        if requirement.get("source_kind") == "control":
            continue
        refs = linked(requirement["id"], set())
        if not refs or not refs.issubset(goals_by_id):
            issues.append(f"{requirement['id']} 尚未绑定可用 goal_ids")
            continue
        concrete = [goals_by_id[key] for key in refs if goals_by_id[key]["metric"] in {
            "volume_mm3", "dimension_mm", "extent_mm", "feature_present",
        }]
        def concrete_evidence(item: dict[str, Any]) -> bool:
            if verified_geometry_ids is not None and item["id"] not in verified_geometry_ids:
                return False
            if item["metric"] == "feature_present":
                return item["expected"] is True
            numeric = GoalCheck.model_validate(item)
            low, _high = _range(numeric)
            return low > 0

        if not any(concrete_evidence(item) for item in concrete):
            issues.append(f"{requirement['id']} 仅有文档、实体数量或健康指标，缺少具体几何验收")
    return issues
