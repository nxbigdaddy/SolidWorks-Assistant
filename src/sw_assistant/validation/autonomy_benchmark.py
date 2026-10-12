"""Frozen synthetic tasks and an offline, fail-closed autonomy scorecard.

This module neither executes tasks nor produces native observations. A separate
authorized collector must supply run records. Hashes detect accidental mixing,
not forged local evidence: provenance declarations are not digital signatures.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter
from typing import Any, TypeGuard


SCHEMA_VERSION = 1
CATALOG_ID = "sw5-supported-solids-10-v1"
REPETITIONS = 3
MAX_RUN_SECONDS = 1800
FAILURE_CATEGORIES = frozenset({
    "protocol", "planning", "observation", "environment", "adapter_contract", "safety", "unknown",
})
EXECUTION_KINDS = frozenset({"real_model", "scripted_provider", "fixed_backend"})
ENVIRONMENT_FIELDS = frozenset({
    "source_revision", "assistant_version", "python_version", "pywin32_version", "pi_version", "solidworks_version",
    "os_version", "model_id", "model_settings_sha256", "source_tree_sha256", "cad_mode",
})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _cylinder(role: str, radius: float, length: float, axis: str,
              center: tuple[float, float, float] = (0, 0, 0)) -> dict[str, Any]:
    return {"role": role, "radius_mm": radius, "length_mm": length,
            "axis": {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}[axis],
            "center_mm": list(center)}


def _case(case_id: str, title: str, prompt: str, volume: float, minimum: list[float],
          maximum: list[float], operations: dict[str, int],
          cylinders: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "case_id": case_id, "title": title, "prompt": prompt,
        "scope": "new_single_configuration_part_no_external_references",
        "maximum_elapsed_seconds": MAX_RUN_SECONDS,
        "required_operations": operations,
        "expected": {"volume_mm3": volume, "bounds_mm": {"min": minimum, "max": maximum},
                     "body_count": 1, "feature_errors": 0, "active_sketch": False,
                     "cylinders": cylinders or []},
        "tolerances": {"position_mm": 0.01, "volume_absolute_mm3": 0.1,
                       "volume_relative": 0.00001, "axis_absolute": 0.000001},
    }
    return {**value, "task_sha256": digest(value)}


def catalog() -> dict[str, Any]:
    """Return independent expectations, never an executable reference solution.

    Bounds and analytic volumes are in MODEL axes, not public sketch axes.
    Cylinder descriptors are coalesced analytic surfaces, not tessellation or
    raw face counts. A through_hole must be independently proved open at BOTH
    end planes; a circle sketch or a proposed cut is not hole evidence.
    """
    pi = math.pi
    cases = [
        _case("front_block", "前视平面长方体", "新建零件。在前视平面以矩形建立居中的40×30×20毫米长方体；模型X/Y/Z分别40/30/20毫米。", 24000,
              [-20, -15, -10], [20, 15, 10], {"sketch.rectangle": 1, "feature.extrude:boss": 1}),
        _case("top_block", "上视平面长方体", "新建零件。在上视平面以矩形建立居中的长方体；模型X/Y/Z尺寸分别40/12/30毫米。", 14400,
              [-20, -6, -15], [20, 6, 15], {"sketch.rectangle": 1, "feature.extrude:boss": 1}),
        _case("right_offset_block", "右视平面偏置直线轮廓", "新建零件。在右视平面用四条普通直线形成矩形闭合轮廓并拉伸；最终模型X/Y/Z尺寸10/18/24毫米，中心为(0,25,-35)毫米。", 4320,
              [-5, 16, -47], [5, 34, -23], {"sketch.line": 4, "feature.extrude:boss": 1}),
        _case("extruded_cylinder", "圆柱", "新建零件，用圆草图拉伸半径10毫米、长30毫米的实心圆柱，轴为模型Z，中心位于原点。", 3000 * pi,
              [-10, -10, -15], [10, 10, 15], {"sketch.circle": 1, "feature.extrude:boss": 1},
              [_cylinder("external", 10, 30, "z")]),
        _case("extruded_ring", "同心圆环", "新建零件，用两个同心圆拉伸圆环，外半径15、内半径6、轴向厚12毫米，轴为模型Z，中心为原点，内孔贯通。", 2268 * pi,
              [-15, -15, -6], [15, 15, 6], {"sketch.circle": 2, "feature.extrude:boss": 1},
              [_cylinder("external", 15, 12, "z"), _cylinder("through_hole", 6, 12, "z")]),
        _case("offset_through_hole", "偏置通孔板", "新建40×30×12毫米居中长方体板，再切出沿模型Z贯通、半径4毫米的圆孔，孔轴经过(8,0,0)毫米。", 14400 - 192 * pi,
              [-20, -15, -6], [20, 15, 6], {"feature.extrude:boss": 1, "feature.extrude:cut": 1},
              [_cylinder("through_hole", 4, 12, "z", (8, 0, 0))]),
        _case("revolved_cylinder", "旋转实心轴", "新建零件，使用普通直线轮廓和明确中心线旋转360度生成实心圆柱；半径12毫米、长度20毫米，轴为模型Y，中心位于原点。", 2880 * pi,
              [-12, -10, -12], [12, 10, 12], {"sketch.line": 4, "sketch.centerline": 1, "feature.revolve:boss": 1},
              [_cylinder("external", 12, 20, "y")]),
        _case("revolved_sleeve", "旋转轴套", "新建零件，使用普通直线轮廓和中心线旋转360度生成轴套；外半径14、内半径8、长度24毫米，轴为模型Y，中心位于原点，孔贯通。", 3168 * pi,
              [-14, -12, -14], [14, 12, 14], {"sketch.line": 4, "sketch.centerline": 1, "feature.revolve:boss": 1},
              [_cylinder("external", 14, 24, "y"), _cylinder("through_hole", 8, 24, "y")]),
        _case("stepped_shaft", "两级阶梯轴", "新建两级同轴实心阶梯轴，使用两次拉伸并合并为一个实体：模型Z从-10到10毫米的一段半径12；Z从10到25毫米的一段半径6；轴经过原点。", 3420 * pi,
              [-12, -12, -10], [12, 12, 25], {"feature.extrude:boss": 2},
              [_cylinder("external", 12, 20, "z"), _cylinder("external", 6, 15, "z", (0, 0, 17.5))]),
        _case("two_hole_plate", "双孔连接板", "新建60×40×8毫米居中长方体板，再切出两个沿模型Z贯通的半径3毫米圆孔；孔轴分别经过(-18,0,0)与(18,0,0)毫米。", 19200 - 144 * pi,
              [-30, -20, -4], [30, 20, 4], {"feature.extrude:boss": 1, "feature.extrude:cut": 1},
              [_cylinder("through_hole", 3, 8, "z", (-18, 0, 0)), _cylinder("through_hole", 3, 8, "z", (18, 0, 0))]),
    ]
    value = {"schema_version": SCHEMA_VERSION, "catalog_id": CATALOG_ID,
             "repetitions": REPETITIONS, "cases": cases}
    return {**value, "catalog_sha256": digest(value)}


def validate_environment(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != ENVIRONMENT_FIELDS:
        raise ValueError("environment must contain exactly the documented fingerprint fields")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 160
           or item.casefold() in {"unset", "unknown", "pending"} for item in value.values()):
        raise ValueError("environment fingerprint is missing, a placeholder, or oversized")
    if not re.fullmatch(r"[0-9a-f]{40}", value["source_revision"]):
        raise ValueError("source_revision must be the full frozen Git commit")
    if any(not _SHA256.fullmatch(value[key]) for key in ("model_settings_sha256", "source_tree_sha256")):
        raise ValueError("model settings and frozen source contents require SHA256 digests")
    if value["cad_mode"] not in {"real", "synthetic"}:
        raise ValueError("cad_mode must be real or synthetic")
    return dict(value)


def create_manifest(environment: Any) -> dict[str, Any]:
    env = validate_environment(environment)
    source = catalog()
    value = {"schema_version": SCHEMA_VERSION, "catalog_id": CATALOG_ID,
             "catalog_sha256": source["catalog_sha256"], "environment": env,
             "environment_sha256": digest(env), "target_execution_kind": "real_model",
             "planned_runs": [{"run_id": f"{case['case_id']}.r{repetition}", "case_id": case["case_id"],
                               "repetition": repetition, "task_sha256": case["task_sha256"]}
                              for case in source["cases"] for repetition in range(1, REPETITIONS + 1)]}
    return {**value, "manifest_sha256": digest(value)}


def _finite(value: Any, *, nonnegative: bool = False) -> TypeGuard[int | float]:
    try:
        return type(value) in {int, float} and math.isfinite(value) and (not nonnegative or value >= 0)
    except OverflowError:
        return False


def _vector(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_finite(item) for item in value)


def _close_vector(first: Any, second: Any, tolerance: float) -> bool:
    return _vector(first) and all(abs(a - b) <= tolerance for a, b in zip(first, second, strict=True))


def geometry_checks(case: dict[str, Any], measurement: Any) -> list[dict[str, Any]]:
    """Compare native readbacks with catalog constants, never submitted targets."""
    expected, tolerance = case["expected"], case["tolerances"]
    measured = measurement if isinstance(measurement, dict) else {}
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool) -> None:
        checks.append({"check": name, "passed": bool(passed)})

    volume = measured.get("volume_mm3")
    add("volume_mm3", _finite(volume) and math.isclose(volume, expected["volume_mm3"],
        rel_tol=tolerance["volume_relative"], abs_tol=tolerance["volume_absolute_mm3"]))
    bounds = measured.get("bounds_mm")
    for side in ("min", "max"):
        add("bounds_" + side, isinstance(bounds, dict) and _close_vector(
            bounds.get(side), expected["bounds_mm"][side], tolerance["position_mm"]))
    for name in ("body_count", "feature_errors"):
        add(name, type(measured.get(name)) is int and measured[name] == expected[name])
    add("closed_sketch", measured.get("active_sketch") is False)
    cylinders = measured.get("cylinders")
    shape_ok = isinstance(cylinders, list) and len(cylinders) == len(expected["cylinders"])
    remaining = list(cylinders) if isinstance(cylinders, list) and shape_ok else []
    for wanted in expected["cylinders"]:
        matches = []
        for index, actual in enumerate(remaining):
            if not isinstance(actual, dict):
                continue
            axis = actual.get("axis")
            match = (actual.get("role") == wanted["role"]
                     and _close_vector(actual.get("center_mm"), wanted["center_mm"], tolerance["position_mm"])
                     and (_close_vector(axis, wanted["axis"], tolerance["axis_absolute"])
                          or _close_vector(axis, [-x for x in wanted["axis"]], tolerance["axis_absolute"])))
            for field in ("radius_mm", "length_mm"):
                match = match and _finite(actual.get(field)) and abs(actual[field] - wanted[field]) <= tolerance["position_mm"]
            if wanted["role"] == "through_hole":
                match = match and actual.get("open_at_both_ends") is True
            if match:
                matches.append(index)
        if len(matches) != 1:
            shape_ok = False
        else:
            remaining.pop(matches[0])
    add("analytic_cylinders_and_through_holes", shape_ok and not remaining)
    return checks


_RUN_FIELDS = frozenset({
    "run_id", "case_id", "repetition", "task_sha256", "catalog_sha256", "manifest_sha256",
    "environment_sha256", "task_id", "session_id", "fresh_session", "execution_kind", "model_id",
    "model_invocations", "host_status", "elapsed_seconds", "human_interventions", "failure_category",
    "safety", "oracle", "executed_operations",
})


def _validate_oracle(oracle: Any, environment: dict[str, Any]) -> TypeGuard[dict[str, Any]]:
    fields = {"kind", "independent_of_proposal", "complete", "sample_id", "artifact_sha256",
              "before", "after", "measurement"}
    if not isinstance(oracle, dict) or set(oracle) != fields:
        return False
    wanted = "production_rpc_readback" if environment["cad_mode"] == "real" else "synthetic_geometry"
    if (oracle.get("kind") != wanted or oracle.get("independent_of_proposal") is not True
            or oracle.get("complete") is not True or not isinstance(oracle.get("sample_id"), str)
            or not _ID.fullmatch(oracle["sample_id"]) or not isinstance(oracle.get("artifact_sha256"), str)
            or not _SHA256.fullmatch(oracle["artifact_sha256"])):
        return False
    before, after = oracle.get("before"), oracle.get("after")
    if not isinstance(before, dict) or set(before) != {"document_id", "configuration", "update_stamp"}:
        return False
    return (before == after and all(isinstance(before[key], str) and 0 < len(before[key]) <= 160
                                    for key in ("document_id", "configuration"))
            and type(before.get("update_stamp")) is int and before["update_stamp"] >= 0)


def _score_run(record: Any, planned: dict[str, Any], manifest: dict[str, Any],
               case: dict[str, Any], duplicate_identifiers: set[tuple[str, str]]) -> dict[str, Any]:
    row: dict[str, Any] = {**planned, "status": "unverified", "issues": [], "checks": [],
                           "host_completed": False, "verified_completed": False, "autonomy_passed": False}
    issues = row["issues"]
    if not isinstance(record, dict) or set(record) != _RUN_FIELDS:
        issues.append("missing_or_unexpected_run_fields")
        return row
    for key in ("run_id", "case_id", "repetition", "task_sha256"):
        if type(record[key]) is not type(planned[key]) or record[key] != planned[key]:
            issues.append("changed_task_or_run:" + key)
    for key in ("catalog_sha256", "environment_sha256", "manifest_sha256"):
        if record[key] != manifest[key]:
            issues.append("mixed_or_changed_contract:" + key)
    for key in ("task_id", "session_id"):
        if not isinstance(record[key], str) or not _ID.fullmatch(record[key]):
            issues.append("invalid_identifier:" + key)
        elif (key, record[key]) in duplicate_identifiers:
            issues.append("reused_identifier:" + key)
    oracle = record.get("oracle")
    if isinstance(oracle, dict) and ("sample_id", str(oracle.get("sample_id"))) in duplicate_identifiers:
        issues.append("reused_oracle_sample")
    if record["fresh_session"] is not True:
        issues.append("session_not_fresh")
    if not isinstance(record["execution_kind"], str) or record["execution_kind"] not in EXECUTION_KINDS:
        issues.append("unknown_execution_kind")
    if record["model_id"] != manifest["environment"]["model_id"]:
        issues.append("mixed_model")
    if (type(record["model_invocations"]) is not int or record["model_invocations"] < 0
            or (record["execution_kind"] == "real_model" and record["model_invocations"] == 0)):
        issues.append("missing_real_model_invocation")
    if not _finite(record["elapsed_seconds"], nonnegative=True):
        issues.append("invalid_elapsed_seconds")
    if type(record["human_interventions"]) is not int or record["human_interventions"] < 0:
        issues.append("invalid_human_interventions")
    if not isinstance(record["host_status"], str) or record["host_status"] not in {"completed", "review", "stopped", "failed"}:
        issues.append("nonterminal_or_unknown_host_status")
    if (record["failure_category"] is not None and
            (not isinstance(record["failure_category"], str) or record["failure_category"] not in FAILURE_CATEGORIES)):
        issues.append("unknown_failure_category")
    if record["host_status"] != "completed" and record["failure_category"] is None:
        issues.append("missing_terminal_failure_classification")
    safety = record["safety"]
    if (not isinstance(safety, dict) or set(safety) != {"unsafe_write", "unknown_result_replayed", "safe_terminal_state"}
            or any(type(value) is not bool for value in safety.values())):
        issues.append("missing_safety_evidence")
    operations = record["executed_operations"]
    if (not isinstance(operations, dict) or len(operations) > 100
            or any(not isinstance(key, str) or len(key) > 100 or type(value) is not int or value < 0
                   for key, value in operations.items())):
        issues.append("invalid_operation_audit")
    if issues:
        if isinstance(safety, dict) and any(safety.get(key) is True for key in ("unsafe_write", "unknown_result_replayed")):
            row["status"] = "unsafe_failure"
        return row
    row.update(execution_kind=record["execution_kind"], host_status=record["host_status"],
               host_completed=record["host_status"] == "completed", elapsed_seconds=record["elapsed_seconds"],
               human_interventions=record["human_interventions"], failure_category=record["failure_category"],
               task_id=record["task_id"], session_id=record["session_id"])
    if safety["unsafe_write"] or safety["unknown_result_replayed"] or not safety["safe_terminal_state"]:
        row.update(status="unsafe_failure", issues=["unsafe_or_unconfirmed_terminal_state"])
    elif record["elapsed_seconds"] > case["maximum_elapsed_seconds"]:
        row.update(status="budget_exceeded", issues=["fixed_run_time_budget_exceeded"])
    elif record["host_status"] != "completed":
        row.update(status="safe_stop", issues=["safe_stop_is_not_task_completion"])
    elif not _validate_oracle(oracle, manifest["environment"]):
        row["issues"].append("missing_or_untrusted_independent_oracle")
    else:
        checks = geometry_checks(case, oracle["measurement"])
        checks.extend({"check": "operation:" + key, "passed": operations.get(key, 0) >= count}
                      for key, count in case["required_operations"].items())
        row["checks"] = checks
        if not all(check["passed"] for check in checks):
            row.update(status="geometry_or_construction_failed", issues=["independent_acceptance_failed"])
        else:
            eligible = record["execution_kind"] == "real_model" and manifest["environment"]["cad_mode"] == "real"
            autonomous = eligible and record["human_interventions"] == 0
            row.update(status="passed" if autonomous else "assisted_passed" if eligible else "fixture_passed",
                       verified_completed=eligible, autonomy_passed=autonomous,
                       oracle_sample_id=oracle["sample_id"], artifact_sha256=oracle["artifact_sha256"])
    return row


def score(manifest: Any, records: Any) -> dict[str, Any]:
    """Score all 30 planned slots; missing/duplicate slots cannot shrink the denominator."""
    if not isinstance(manifest, dict) or digest(manifest) != digest(create_manifest(manifest.get("environment"))):
        raise ValueError("manifest differs from the frozen catalog/environment/30-run contract")
    if not isinstance(records, list) or len(records) > 100:
        raise ValueError("results must be a list containing at most 100 records")
    planned_ids = {item["run_id"] for item in manifest["planned_runs"]}
    grouped: dict[str, list[Any]] = {}
    identifiers: Counter[tuple[str, str]] = Counter()
    campaign_issues = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("run_id"), str):
            campaign_issues.append("record_without_run_id")
            continue
        run_id = record["run_id"]
        if run_id not in planned_ids:
            campaign_issues.append("unplanned_run_id")
        grouped.setdefault(run_id, []).append(record)
        for key in ("task_id", "session_id"):
            if isinstance(record.get(key), str):
                identifiers[(key, record[key])] += 1
        oracle = record.get("oracle")
        if isinstance(oracle, dict) and isinstance(oracle.get("sample_id"), str):
            identifiers[("sample_id", oracle["sample_id"])] += 1
    duplicates = {key for key, count in identifiers.items() if count > 1}
    cases = {case["case_id"]: case for case in catalog()["cases"]}
    rows = []
    for planned in manifest["planned_runs"]:
        found = grouped.get(planned["run_id"], [])
        if len(found) != 1:
            rows.append({**planned, "status": "not_run" if not found else "unverified",
                         "issues": [] if not found else ["duplicate_run_id"], "checks": [],
                         "host_completed": False, "verified_completed": False, "autonomy_passed": False})
        else:
            rows.append(_score_run(found[0], planned, manifest, cases[planned["case_id"]], duplicates))
    count = len(rows)
    real_rows = [row for row in rows if row.get("execution_kind") == "real_model"]
    times = [row["elapsed_seconds"] for row in rows if "elapsed_seconds" in row]
    passed = sum(row["autonomy_passed"] for row in rows)
    return {
        "schema_version": SCHEMA_VERSION, "catalog_id": CATALOG_ID,
        "manifest_sha256": manifest["manifest_sha256"], "environment_sha256": manifest["environment_sha256"],
        "evidence_limits": "Offline declarative evidence audit, not signed provenance or a CAD/model execution. Synthetic/scripted passes are not real autonomy.",
        "campaign_passed": not campaign_issues and passed == count,
        "campaign_issues": campaign_issues, "planned_count": count,
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "metrics": {"real_model_host_completed": sum(row["host_completed"] for row in real_rows),
                    "independently_verified_completed": sum(row["verified_completed"] for row in rows),
                    "autonomous_completed": passed, "autonomous_completion_rate": passed / count,
                    "independently_verified_completion_rate": sum(row["verified_completed"] for row in rows) / count,
                    "observed_elapsed_seconds_total": sum(times),
                    "observed_elapsed_seconds_mean": sum(times) / len(times) if times else None,
                    "timed_run_count": len(times),
                    "human_interventions_total": sum(row.get("human_interventions", 0) for row in rows),
                    "intervention_reported_run_count": sum("human_interventions" in row for row in rows),
                    "safe_stops": sum(row["status"] == "safe_stop" for row in rows),
                    "safe_failures": sum(row["status"] in {"safe_stop", "budget_exceeded", "geometry_or_construction_failed"}
                                         for row in rows),
                    "unsafe_failures": sum(row["status"] == "unsafe_failure" for row in rows),
                    "reported_unsafe_records": sum(isinstance(record, dict) and isinstance(record.get("safety"), dict)
                        and any(record["safety"].get(key) is True for key in ("unsafe_write", "unknown_result_replayed"))
                        for record in records)},
        "runs": rows,
    }


def record_template(manifest: Any, run_id: str) -> dict[str, Any]:
    """A deliberately unverified template, never fabricated measurements/success."""
    if not isinstance(manifest, dict) or digest(manifest) != digest(create_manifest(manifest.get("environment"))):
        raise ValueError("invalid manifest")
    planned = next((item for item in manifest["planned_runs"] if item["run_id"] == run_id), None)
    if planned is None:
        raise ValueError("run_id is not in the manifest")
    return {**copy.deepcopy(planned), **{key: manifest[key] for key in (
        "catalog_sha256", "manifest_sha256", "environment_sha256")},
        "task_id": None, "session_id": None, "fresh_session": False, "execution_kind": "real_model",
        "model_id": manifest["environment"]["model_id"], "model_invocations": 0,
        "host_status": "not_run", "elapsed_seconds": None, "human_interventions": None,
        "failure_category": None, "safety": None, "oracle": None, "executed_operations": {}}
