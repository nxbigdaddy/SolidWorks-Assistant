"""Bounded historical CAD readbacks for planning, never execution authority."""
from __future__ import annotations

import math
from typing import Any

from .security import scrub_agent_value


def _number(value: Any) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _point(value: Any) -> list[float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(_number(item) for item in value):
        return [float(item) for item in value]
    return None


def _state_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"available": False}
    snapshot = value.get("snapshot", value)
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    summary: dict[str, Any] = {"available": bool(value), "snapshot": {}}
    for field in ("active_sketch", "sketch_entity_count", "body_count", "feature_count", "volume_m3"):
        item = snapshot.get(field)
        if isinstance(item, str):
            summary["snapshot"][field] = scrub_agent_value(item)[:250]
        elif item is None or isinstance(item, bool) or _number(item):
            summary["snapshot"][field] = item
    geometry = value.get("active_sketch_geometry")
    if not isinstance(geometry, dict):
        summary["geometry_available"] = False
        return summary
    segments = geometry.get("segments", [])
    segments = segments if isinstance(segments, list) else []
    # Enumeration may be identity-sorted, not creation-ordered: preserve a small
    # head and tail sample, explicitly never call the truncated sample complete.
    selected = segments if len(segments) <= 8 else segments[:4] + segments[-4:]
    output = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        entry = {key: item[key] for key in ("type", "construction", "is_circle", "radius_mm")
                 if key in item and (isinstance(item[key], bool) or _number(item[key]))}
        for key in ("start_sketch_m", "end_sketch_m", "center_sketch_m", "normal_sketch"):
            point = _point(item.get(key))
            if point is not None:
                entry[key] = point
        output.append(entry)
    summary["sketch_geometry"] = {
        "coordinate_space": "actual_sketch_local", "position_unit": "m", "radius_unit": "mm",
        "source_complete": (value.get("active_sketch_geometry_complete") is True
                            and geometry.get("geometry_complete") is True),
        "segment_count": len(segments), "truncated": len(segments) > len(selected), "segments": output,
    }
    summary["geometry_available"] = True
    return summary


def failure_geometry_summary(entry: Any) -> dict[str, Any]:
    """Retain before/after reads even after a failed candidate has been restored."""
    entry = entry if isinstance(entry, dict) else {}
    result = entry.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    mutation = data.get("mutation") if isinstance(data, dict) else None
    receipt: dict[str, Any] = {}
    if isinstance(mutation, dict) and mutation.get("kind") == "sketch_creation":
        if (isinstance(mutation.get("failure_category"), str)
                and mutation["failure_category"] in {"planning", "adapter_contract", "safety", "unknown"}):
            receipt["failure_category"] = mutation["failure_category"]
        if isinstance(mutation.get("contract_failure_code"), str):
            receipt["contract_failure_code"] = scrub_agent_value(mutation["contract_failure_code"])[:100]
        for key in ("mutation_attempted", "geometry_verified", "rollback_verified", "requires_review",
                    "sketch_manager_flags_restored"):
            if isinstance(mutation.get(key), bool):
                receipt[key] = mutation[key]
        receipt["verification_error"] = scrub_agent_value(str(mutation.get("verification_error", "")))[:700]
        for key in ("requested_points_m", "api_points_m"):
            values = mutation.get(key)
            if isinstance(values, list):
                receipt[key] = [point for value in values[:6] if (point := _point(value)) is not None]
        receipt["coordinate_space"] = {"requested_points_m": "model", "api_points_m": "actual_sketch_local"}
        if isinstance(mutation.get("actual_geometry"), list):
            # Targeted new entities may sit in the middle of an identity-sorted
            # full sketch; do not lose them to the general head/tail sample.
            receipt["new_entities"] = _state_summary({"active_sketch_geometry": {
                "segments": mutation["actual_geometry"], "geometry_complete": False,
            }})["sketch_geometry"]
            receipt["new_entities"]["scope"] = "newly_added_entities_in_failed_creation"
    return {
        "scope": "historical_failed_candidate_not_current_state",
        "before": _state_summary(entry.get("state")),
        "after": _state_summary(entry.get("observation")),
        "creation_verification": receipt,
        "note": "实际草图局部坐标不等于公共平面坐标；须结合实际变换解释。历史失败证据不得作为当前写入前提。",
    }
