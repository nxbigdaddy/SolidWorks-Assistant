"""Deterministic host failure categories and bounded retry windows.

Messages are presentation only: neither recovery authority nor classification is
inferred from their text. Counts survive candidate rollback and task restoration.
"""
from __future__ import annotations

from typing import Any


LIMITS = {"protocol": 6, "planning": 6, "observation": 3, "environment": 3,
          "adapter_contract": 1, "safety": 1, "unknown": 1}
LABELS = {"protocol": "决策格式", "planning": "建模方案", "observation": "模型观察",
          "environment": "运行环境", "adapter_contract": "工具几何契约", "safety": "安全边界", "unknown": "未知执行状态"}
NEXT_ACTIONS = {
    "protocol": "correct_decision_schema", "planning": "revise_plan_from_evidence",
    "observation": "read_only_observation", "environment": "check_environment_read_only",
    "adapter_contract": "host_review_adapter", "safety": "host_review_no_replay", "unknown": "host_review_no_replay",
}
_KINDS = {
    "decision_rejected": "protocol", "policy_rejected": "planning", "repeat_blocked": "planning",
    "verification_failed": "planning", "strategy_no_progress": "planning", "autonomy_retry": "planning",
    "trial_restored": "planning", "observation_incomplete": "observation",
    "verification_unobservable": "observation", "preflight_recoverable": "environment",
    "environment_failed": "environment",
}
_SAFETY_CODES = {"StateChanged", "DocumentChanged", "CandidateStateChanged", "PermissionError",
                 "TaskStopped", "ReviewRequired", "OperationCancelled"}
_ADAPTER_CODES = {"SketchCreationError", "PolygonCreationError", "CircleEditError"}


def classify(kind: str, details: dict[str, Any]) -> str | None:
    result = details.get("result") or details.get("failure") or {}
    result = result if isinstance(result, dict) else {}
    code = result.get("error_code")
    code = code if isinstance(code, str) else None
    if code in _SAFETY_CODES:
        return "safety"
    if details.get("state_uncertain") is True:
        return "unknown"
    if kind in {"step_failed", "trial_restored"} and result:
        data = result.get("data") or {}
        data = data if isinstance(data, dict) else {}
        mutation = data.get("mutation") or data.get("circle_edit") or {}
        mutation = mutation if isinstance(mutation, dict) else {}
        # These enums originate in the local adapter, not in model proposals.
        declared = mutation.get("failure_category")
        declared = declared if isinstance(declared, str) else None
        if declared in {"safety", "unknown"}:
            return declared
        if code in _ADAPTER_CODES:
            return declared if declared in {"planning", "adapter_contract"} else "adapter_contract"
        if kind == "step_failed" and details.get("state_known") is not True:
            return "unknown"
        execution = data.get("execution", {})
        if isinstance(execution, dict) and execution.get("started") is False:
            return "environment"
        call = result.get("call", {})
        if isinstance(call, dict) and call.get("tool") == "session.ensure":
            return "environment"
        return "planning"
    if kind == "step_failed":
        return "unknown"
    return _KINDS.get(kind)


def new_budget() -> dict[str, Any]:
    return {"version": 1, "counts": dict.fromkeys(LIMITS, 0), "totals": dict.fromkeys(LIMITS, 0),
            "last_rounds": {}, "window": 0, "legacy_unclassified": 0, "manual_rearms": dict.fromkeys(LIMITS, 0)}


def ensure_budget(task: dict[str, Any]) -> dict[str, Any]:
    current = task.get("failure_budget")
    if isinstance(current, dict) and current.get("version") == 1:
        for field in ("counts", "totals"):
            values = current.setdefault(field, {})
            if not isinstance(values, dict):
                values = current[field] = dict(LIMITS)  # Corrupt persisted counters must fail closed.
            for category in LIMITS:
                value = values.get(category, 0)
                values[category] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else LIMITS[category]
        if not isinstance(current.setdefault("last_rounds", {}), dict):
            current["last_rounds"] = {}
            current["counts"]["unknown"] = max(1, current["counts"]["unknown"])
        if not isinstance(current.setdefault("manual_rearms", dict.fromkeys(LIMITS, 0)), dict):
            current["manual_rearms"] = dict.fromkeys(LIMITS, 1)
        return current
    budget = new_budget()
    task["failure_budget"] = budget
    seen: set[tuple[int, str]] = set()
    for item in task.get("feedback", []):
        if not isinstance(item, dict):
            continue
        migrated_category = classify(str(item.get("kind", "")), item)
        round_number = item.get("round")
        if migrated_category is None or not isinstance(round_number, int):
            continue
        key = round_number, migrated_category
        if key in seen:
            continue
        seen.add(key)
        budget["counts"][migrated_category] += 1
        budget["totals"][migrated_category] += 1
        budget["last_rounds"][migrated_category] = max(round_number, budget["last_rounds"].get(migrated_category, 0))
    legacy = task.get("no_progress", 0)
    legacy = legacy if isinstance(legacy, int) and not isinstance(legacy, bool) and legacy > 0 else 0
    # A mixed legacy count cannot honestly be renamed "geometry failures".
    budget["legacy_unclassified"] = max(0, legacy - len({number for number, _ in seen}))
    if budget["legacy_unclassified"]:
        budget["counts"]["unknown"] = max(1, budget["counts"]["unknown"])
        budget["totals"]["unknown"] = max(1, budget["totals"]["unknown"])
    task["no_progress"] = budget["counts"]["planning"]
    if budget["counts"]["unknown"]:
        task["last_failure"] = {"failure_category": "unknown", "next_action": "host_review_no_replay",
                                "requires_review": True, "retry_allowed": False, "legacy_record": True}
    return budget


def record_failure(task: dict[str, Any], category: str, *, force_review: bool = False) -> dict[str, Any]:
    budget = ensure_budget(task)
    round_number = task.get("rounds", 0)
    if budget["last_rounds"].get(category) != round_number:
        budget["counts"][category] += 1
        budget["totals"][category] += 1
        budget["last_rounds"][category] = round_number
    count, limit = budget["counts"][category], LIMITS[category]
    paused = force_review or count >= limit or category in {"safety", "unknown", "adapter_contract"}
    assessment = {"failure_category": category, "attempts": count, "limit": limit,
                  "remaining": max(0, limit - count), "retry_allowed": not paused,
                  "next_action": (NEXT_ACTIONS[category] if not paused or category in {"safety", "unknown", "adapter_contract"}
                                  else "host_review_budget" if count >= limit else "host_review"), "requires_review": paused}
    task["last_failure"] = assessment
    task["no_progress"] = budget["counts"]["planning"]
    return assessment


def reset_verified_window(task: dict[str, Any]) -> None:
    budget = ensure_budget(task)
    budget["counts"] = dict.fromkeys(LIMITS, 0)
    # Keep same-round deduplication across an accepted prefix. Lifetime totals
    # remain monotonic even when committed geometry starts a new retry window.
    budget["window"] = budget.get("window", 0) + 1
    task["no_progress"] = 0
    task["last_failure"] = {}


def remaining(task: dict[str, Any]) -> dict[str, Any]:
    budget = ensure_budget(task)
    return {category: {"used": budget["counts"][category], "limit": limit,
                       "remaining": max(0, limit - budget["counts"][category]),
                       "total": budget["totals"][category],
                       "manual_rearm_remaining": int(category not in {"safety", "unknown", "adapter_contract"}
                                                     and not any(budget["manual_rearms"].values()))}
            for category, limit in LIMITS.items()}


def exhausted(task: dict[str, Any]) -> str | None:
    budget = ensure_budget(task)
    return next((category for category, limit in LIMITS.items()
                 if category not in {"safety", "unknown"} and budget["counts"][category] >= limit), None)


def manual_rearm_candidates(task: dict[str, Any]) -> tuple[str, ...]:
    """Called only by explicit host resume, never by an Agent or note parser."""
    budget = ensure_budget(task)
    categories = tuple(category for category, limit in LIMITS.items()
                       if category not in {"safety", "unknown"} and budget["counts"][category] >= limit)
    if "adapter_contract" in categories:
        raise ValueError("工具几何契约故障已隔离；人工备注不能重新授权该工具，请先修复软件并新建任务。")
    if categories and any(budget["manual_rearms"].values()):
        raise ValueError("本任务的人工核对后额外窗口已用完；请保留证据并拆分为新任务。")
    return categories


def grant_manual_window(task: dict[str, Any], categories: tuple[str, ...]) -> None:
    """The caller must first independently reconcile the observed CAD boundary."""
    if manual_rearm_candidates(task) != categories:
        raise ValueError("人工核对期间预算状态已改变，未重新授权。")
    budget = ensure_budget(task)
    for category in categories:
        budget["manual_rearms"][category] = 1
        budget["counts"][category] = 0
    task["no_progress"] = budget["counts"]["planning"]
    task["last_failure"] = {}
