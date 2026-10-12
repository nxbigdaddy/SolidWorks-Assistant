"""Bounded local retrieval over the current task record, with no file or CAD access."""
from __future__ import annotations

import math
from itertools import islice
from typing import Any

from ..tools import INVESTIGATION_TOOLS, ToolRegistry, bound_investigation_result
from .security import scrub_agent_value
from .failure_evidence import failure_geometry_summary


def _arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in islice(value.items(), 16):
        if isinstance(item, str):
            item = item[:500]
        elif isinstance(item, float) and not math.isfinite(item):
            item = "<non-finite>"
        elif not isinstance(item, (int, float, bool, type(None))):
            item = "<structured-value>"
        result[str(key)[:100]] = item
    return result


def inspect_recent_failures(
    task: dict[str, Any], registry: ToolRegistry, *, limit: int = 3,
) -> dict[str, Any]:
    """Return evidence and suggested questions, never a diagnosis asserted as fact."""
    if not isinstance(task, dict) or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        return {"complete": False, "issues": ["invalid task context or limit"], "failures": []}
    failures = task.get("failure_attempts", [])
    feedback = task.get("feedback", [])
    failures = failures if isinstance(failures, list) else []
    feedback = feedback if isinstance(feedback, list) else []
    recent_feedback = [item for item in feedback[-30:] if isinstance(item, dict) and
                       any(word in str(item.get("kind", "")) for word in ("fail", "reject", "error", "blocked"))]
    # Keep the last actual CAD failure AND the latest policy/contract correction.
    # Feedback is not merely a fallback after the first geometry failure exists.
    candidates = [{**item, "source": "cad_failure"} for item in failures if isinstance(item, dict)]
    candidates.extend({**item, "source": "task_feedback"} for item in recent_feedback)
    candidates.sort(key=lambda item: str(item.get("time", "")))
    source = candidates[-limit:]
    latest_policy = next((item for item in reversed(recent_feedback)
                          if item.get("kind") in {"policy_rejected", "decision_rejected", "verification_failed", "repeat_blocked"}), None)
    if latest_policy and not any(item.get("message") == latest_policy.get("message") for item in source):
        source = (source[-(limit - 1):] if limit > 1 else []) + [{**latest_policy, "source": "task_feedback"}]
    if limit > 1 and failures and not any(item.get("source") == "cad_failure" for item in source):
        latest_failure = next((item for item in reversed(failures) if isinstance(item, dict)), None)
        if latest_failure:
            source = [{**latest_failure, "source": "cad_failure"}] + source[-(limit - 1):]
    observations = task.get("latest_observation", {})
    observations = observations if isinstance(observations, dict) else {}
    snapshot = observations.get("snapshot", {})
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    output = []
    for entry in source:
        if not isinstance(entry, dict):
            continue
        result = entry.get("result", {})
        result = result if isinstance(result, dict) else {}
        call = entry.get("call", result.get("call", {}))
        call = call if isinstance(call, dict) else {}
        message = str(result.get("message", entry.get("message", "")))[:1200]
        code = str(result.get("error_code", ""))[:120]
        tool = str(call.get("tool", ""))[:100]
        text = (tool + " " + code + " " + message).casefold()
        category = "unknown"
        suggested = ["session.capabilities", "model.observe"]
        guidance = "先确认模型现场和工具能力，再决定是否调整方案；本条建议不代表已确认根因。"
        if entry.get("kind") in {"policy_rejected", "decision_rejected"}:
            category = "policy_or_contract"
            suggested = ["model.observe", "feature.inspect", "diagnostics.recent"]
            guidance = "先修正最新候选或验收合同错误；保持原用户要求、测量指标和数值范围，不要重复执行旧的几何失败。"
        elif any(term in text for term in ("plane", "selection", "face", "reference", "基准面", "选择", "引用", "端面")):
            category = "reference_or_selection"
            suggested += ["model.references", "sketch.inspect"]
            guidance = "查询实际基准面和平面面候选，核对当前文档、草图及引用，不依赖固定面序号。"
        elif any(term in text for term in ("sketch", "contour", "profile", "草图", "轮廓", "闭合", "中心线")):
            category = "sketch_profile"
            suggested += ["sketch.inspect", "model.references"]
            guidance = "核对草图实体与构造线、轮廓检查结果；未知闭合状态不能作为建模前提。"
        elif any(term in text for term in ("timeout", "rpc", "disconnect", "超时", "断开")):
            category = "connection_or_timeout"
            guidance = "先读取会话连接和模型状态，查明上一次调用是否已生效，避免盲目重复写入。"
        elif any(term in text for term in ("feature", "rebuild", "特征", "重建", "verification", "验收")):
            category = "feature_or_verification"
            suggested += ["feature.inspect", "sketch.inspect"]
            guidance = "按已观察到的精确名称读取特征及参数，再与锁定验收目标比较；不能降低目标来通过验收。"
        output.append({"tool": tool, "arguments": _arguments(call.get("arguments")),
                       "source": entry.get("source", ""), "kind": entry.get("kind", "cad_failure"),
                       "time": str(entry.get("time", "")),
                       "message": message, "error_code": code,
                       "geometry_evidence": failure_geometry_summary(entry),
                       "category_hypothesis": category, "guidance": guidance,
                       "suggested_tools": list(dict.fromkeys(suggested))})
    tools = [{"name": item["name"], "description": str(item["description"])[:300]}
             for item in registry.describe() if item["name"] in INVESTIGATION_TOOLS]
    return bound_investigation_result(scrub_agent_value({
        "complete": True, "issues": [], "scope": "current_task_memory_only",
        "failures": output, "failure_count_available": len(failures), "feedback_count_available": len(recent_feedback),
        "truncated": len(candidates) > limit, "limit": limit,
        "state": _arguments({key: snapshot.get(key) for key in (
            "connected", "document_type", "active_sketch", "body_count", "feature_count",
        )}),
        "available_investigations": tools,
        "note": "建议依据当前任务摘要生成；没有读取任何日志文件，也没有执行或授权重试。",
    }))
