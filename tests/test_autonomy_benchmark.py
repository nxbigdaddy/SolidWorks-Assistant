"""Synthetic evidence only: never CAD, configured models, or network endpoints."""
from __future__ import annotations

import copy
import inspect
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import autonomy_benchmark as cli
from sw_assistant.validation import autonomy_benchmark as benchmark


def environment(cad_mode="real"):
    return {"source_revision": "a" * 40, "source_tree_sha256": "b" * 64, "assistant_version": "synthetic-test-version",
            "python_version": "synthetic-python", "pywin32_version": "synthetic-pywin32", "pi_version": "synthetic-pi", "solidworks_version": "synthetic-sw",
            "os_version": "synthetic-os", "model_id": "synthetic-model", "model_settings_sha256": "c" * 64,
            "cad_mode": cad_mode}


def manifest(cad_mode="real"):
    return benchmark.create_manifest(environment(cad_mode))


def run_record(plan=None, slot=0):
    plan = plan or manifest()
    record = benchmark.record_template(plan, plan["planned_runs"][slot]["run_id"])
    case = benchmark.catalog()["cases"][slot // 3]
    measurement = copy.deepcopy(case["expected"])
    for cylinder in measurement["cylinders"]:
        if cylinder["role"] == "through_hole":
            cylinder["open_at_both_ends"] = True
    state = {"document_id": f"synthetic-document-{slot}", "configuration": "Default", "update_stamp": 50}
    record.update(task_id=f"task-{slot}", session_id=f"session-{slot}", fresh_session=True,
                  execution_kind="real_model", model_invocations=4, host_status="completed",
                  elapsed_seconds=100.0, human_interventions=0,
                  safety={"unsafe_write": False, "unknown_result_replayed": False, "safe_terminal_state": True},
                  executed_operations=case["required_operations"],
                  oracle={"kind": "production_rpc_readback" if plan["environment"]["cad_mode"] == "real" else "synthetic_geometry",
                          "independent_of_proposal": True, "complete": True, "sample_id": f"oracle-{slot}",
                          "artifact_sha256": "d" * 64, "before": state, "after": copy.deepcopy(state),
                          "measurement": measurement})
    return record


def row(record, plan=None):
    return benchmark.score(plan or manifest(), [record])["runs"][0]


def test_catalog_is_fixed_ten_tasks_three_independent_planned_slots_each():
    source = benchmark.catalog()
    assert len(source["cases"]) == 10
    assert source == benchmark.catalog()
    plan = manifest()
    assert len(plan["planned_runs"]) == 30
    assert len({item["run_id"] for item in plan["planned_runs"]}) == 30
    assert {item["repetition"] for item in plan["planned_runs"]} == {1, 2, 3}
    assert any("旋转" in case["title"] for case in source["cases"])
    assert source["cases"][2]["expected"]["bounds_mm"]["min"] == [-5, 16, -47]
    assert source["cases"][-1]["expected"]["volume_mm3"] == pytest.approx(19200 - 144 * math.pi)
    source["cases"][0]["expected"]["volume_mm3"] = 1
    assert benchmark.catalog()["cases"][0]["expected"]["volume_mm3"] == 24000


@pytest.mark.parametrize("index", range(10))
def test_catalog_analytic_volumes_are_independent_of_any_backend(index):
    expected = [24000, 14400, 4320, 3000 * math.pi, 2268 * math.pi, 14400 - 192 * math.pi,
                2880 * math.pi, 3168 * math.pi, 3420 * math.pi, 19200 - 144 * math.pi]
    assert benchmark.catalog()["cases"][index]["expected"]["volume_mm3"] == pytest.approx(expected[index])


def test_empty_campaign_is_not_run_not_completed_and_template_is_not_evidence():
    report = benchmark.score(manifest(), [])
    assert report["status_counts"] == {"not_run": 30}
    assert not report["campaign_passed"]
    assert report["metrics"]["autonomous_completion_rate"] == 0
    assert report["metrics"]["observed_elapsed_seconds_mean"] is None
    template = benchmark.record_template(manifest(), "front_block.r1")
    assert row(template)["status"] == "unverified"
    assert template["oracle"] is None and not template["fresh_session"]


def test_all_thirty_fresh_real_native_unassisted_runs_are_required_for_campaign_pass():
    plan = manifest()
    records = [run_record(plan, slot) for slot in range(30)]
    before = copy.deepcopy(records)
    result = benchmark.score(plan, records)
    assert result["campaign_passed"] and result["status_counts"] == {"passed": 30}
    assert result["metrics"]["autonomous_completion_rate"] == 1
    assert result["metrics"]["independently_verified_completed"] == 30
    assert result["metrics"]["observed_elapsed_seconds_total"] == 3000
    assert records == before
    short = benchmark.score(plan, records[:1])
    assert not short["campaign_passed"] and short["metrics"]["autonomous_completion_rate"] == 1 / 30


@pytest.mark.parametrize("kind", ["scripted_provider", "fixed_backend"])
def test_scripted_success_cannot_be_counted_as_model_autonomy(kind):
    record = run_record()
    record["execution_kind"] = kind
    record["model_invocations"] = 0
    result = row(record)
    assert result["status"] == "fixture_passed"
    assert not result["verified_completed"] and not result["autonomy_passed"]


def test_real_model_on_synthetic_cad_is_not_production_autonomy():
    plan = manifest("synthetic")
    result = row(run_record(plan), plan)
    assert result["status"] == "fixture_passed"
    assert not result["autonomy_passed"]


def test_human_assistance_is_reported_but_not_an_autonomous_pass():
    record = run_record()
    record["human_interventions"] = 2
    report = benchmark.score(manifest(), [record])
    assert report["runs"][0]["status"] == "assisted_passed"
    assert report["metrics"]["independently_verified_completed"] == 1
    assert report["metrics"]["human_interventions_total"] == 2
    assert report["metrics"]["autonomous_completed"] == 0


@pytest.mark.parametrize("status", ["review", "stopped", "failed"])
def test_safe_stop_is_not_completed_even_if_geometry_is_correct(status):
    record = run_record()
    record.update(host_status=status, failure_category="planning")
    result = row(record)
    assert result["status"] == "safe_stop"
    assert not result["verified_completed"] and not result["host_completed"]


@pytest.mark.parametrize("failure", ["missing_oracle", "self_asserted", "partial", "wrong_origin", "changed_state", "missing_hash"])
def test_host_complete_without_independent_stable_oracle_is_unverified(failure):
    record = run_record()
    if failure == "missing_oracle":
        record["oracle"] = None
    elif failure == "self_asserted":
        record["oracle"]["independent_of_proposal"] = False
    elif failure == "partial":
        record["oracle"]["complete"] = False
    elif failure == "wrong_origin":
        record["oracle"]["kind"] = "agent_claim"
    elif failure == "changed_state":
        record["oracle"]["after"]["update_stamp"] += 1
    else:
        record["oracle"]["artifact_sha256"] = ""
    result = row(record)
    assert result["host_completed"] and result["status"] == "unverified"
    assert not result["verified_completed"]


@pytest.mark.parametrize("changed", ["volume", "location", "body_count", "active_sketch", "feature_errors", "operations"])
def test_measured_geometry_and_construction_must_match_frozen_expectations(changed):
    record = run_record()
    measured = record["oracle"]["measurement"]
    if changed == "volume":
        measured["volume_mm3"] *= 0.5
    elif changed == "location":
        measured["bounds_mm"]["min"][0] += 20
        measured["bounds_mm"]["max"][0] += 20  # Same extents, wrong placement.
    elif changed == "operations":
        record["executed_operations"] = {}
    else:
        measured[changed] = True if changed == "active_sketch" else 2
    assert row(record)["status"] == "geometry_or_construction_failed"


def test_hole_cannot_be_a_circle_sketch_or_blind_hole_or_wrong_axis():
    plan = manifest()
    record = run_record(plan, 15)
    record["oracle"]["measurement"]["cylinders"][0]["open_at_both_ends"] = False
    assert benchmark.score(plan, [record])["runs"][15]["status"] == "geometry_or_construction_failed"
    record = run_record(plan, 15)
    record["oracle"]["measurement"]["cylinders"][0]["axis"] = [1, 0, 0]
    assert benchmark.score(plan, [record])["runs"][15]["status"] == "geometry_or_construction_failed"


def test_opposite_axis_orientation_and_permuted_face_enumeration_are_equivalent():
    plan = manifest()
    record = run_record(plan, 27)
    cylinders = record["oracle"]["measurement"]["cylinders"]
    cylinders.reverse()
    for item in cylinders:
        item["axis"] = [0, 0, -1]
    assert benchmark.score(plan, [record])["runs"][27]["status"] == "passed"


@pytest.mark.parametrize("field", ["task_sha256", "catalog_sha256", "environment_sha256", "manifest_sha256", "model_id"])
def test_goal_or_environment_mixing_never_passes(field):
    record = run_record()
    record[field] = "changed"
    assert row(record)["status"] == "unverified"


@pytest.mark.parametrize("field", ["task_id", "session_id", "sample_id"])
def test_reused_identity_invalidates_both_slots_not_just_the_second(field):
    records = [run_record(slot=0), run_record(slot=1)]
    if field == "sample_id":
        records[1]["oracle"][field] = records[0]["oracle"][field]
    else:
        records[1][field] = records[0][field]
    report = benchmark.score(manifest(), records)
    assert all(item["status"] == "unverified" for item in report["runs"][:2])
    assert report["metrics"]["autonomous_completed"] == 0


def test_duplicate_and_extra_runs_do_not_allow_best_of_selection():
    record = run_record()
    report = benchmark.score(manifest(), [record, copy.deepcopy(record)])
    assert report["runs"][0]["issues"] == ["duplicate_run_id"]
    extra = run_record()
    extra["run_id"] = "outside-plan"
    report = benchmark.score(manifest(), [extra])
    assert report["campaign_issues"] == ["unplanned_run_id"] and not report["campaign_passed"]


@pytest.mark.parametrize("change", ["drop_run", "lower_goal", "different_catalog", "bool_schema"])
def test_modified_manifest_is_refused(change):
    plan = manifest()
    if change == "drop_run":
        plan["planned_runs"].pop()
    elif change == "lower_goal":
        plan["planned_runs"][0]["task_sha256"] = "f" * 64
    elif change == "bool_schema":
        plan["schema_version"] = True
    else:
        plan["catalog_id"] = "easier"
    with pytest.raises(ValueError):
        benchmark.score(plan, [])


@pytest.mark.parametrize("field,value", [("execution_kind", []), ("host_status", {}), ("failure_category", []),
                                          ("model_invocations", True), ("elapsed_seconds", float("nan")),
                                          ("elapsed_seconds", -1), ("human_interventions", True),
                                          ("fresh_session", False), ("oracle", [])])
def test_malformed_untrusted_records_fail_closed_without_crashing(field, value):
    record = run_record()
    record[field] = value
    assert not row(record)["autonomy_passed"]


@pytest.mark.parametrize("field", ["unsafe_write", "unknown_result_replayed"])
def test_unsafe_failure_is_not_hidden_by_missing_geometry_or_bad_environment(field):
    record = run_record()
    record["safety"][field] = True
    record["oracle"] = None
    record["environment_sha256"] = "wrong"
    report = benchmark.score(manifest(), [record])
    assert report["runs"][0]["status"] == "unsafe_failure"
    assert report["metrics"]["reported_unsafe_records"] == 1


def test_late_success_does_not_erase_fixed_time_budget():
    record = run_record()
    record["elapsed_seconds"] = benchmark.MAX_RUN_SECONDS + 1
    assert row(record)["status"] == "budget_exceeded"


def test_default_cli_exports_utf8_catalog_without_loading_any_execution_modules():
    script = Path(cli.__file__).resolve()
    process = subprocess.run([sys.executable, str(script)], capture_output=True, check=True)
    value = json.loads(process.stdout.decode("utf-8"))
    assert value["catalog_id"] == benchmark.CATALOG_ID
    assert "前视平面" in process.stdout.decode("utf-8")
    source = inspect.getsource(cli) + inspect.getsource(benchmark)
    for forbidden in ("SolidWorksWorkerClient", "win32com", "TaskAgentBackend", "subprocess", "requests."):
        assert forbidden not in source


def test_cli_scoring_is_read_only_and_distinguishes_invalid_inputs(tmp_path, capsys):
    plan_path, results_path = tmp_path / "manifest.json", tmp_path / "results.json"
    plan_path.write_text(json.dumps(manifest()), encoding="utf-8")
    results_path.write_text("[]", encoding="utf-8")
    before = (plan_path.read_bytes(), results_path.read_bytes())
    assert cli.main(["score", "--manifest", str(plan_path), "--results", str(results_path)]) == 1
    assert json.loads(capsys.readouterr().out)["status_counts"] == {"not_run": 30}
    assert before == (plan_path.read_bytes(), results_path.read_bytes())
    results_path.write_bytes(b"[" * 2_000_001)
    assert cli.main(["score", "--manifest", str(plan_path), "--results", str(results_path)]) == 2
    assert "nothing was executed" in capsys.readouterr().err


def test_cli_plan_template_and_complete_score_have_explicit_offline_results(tmp_path, capsys):
    env_path, plan_path, results_path = (tmp_path / name for name in ("environment.json", "manifest.json", "results.json"))
    env_path.write_text(json.dumps(environment()), encoding="utf-8")
    assert cli.main(["plan", "--environment", str(env_path)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["planned_runs"]) == 30
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert cli.main(["template", "--manifest", str(plan_path), "--run-id", "front_block.r1"]) == 0
    template = json.loads(capsys.readouterr().out)
    assert template["host_status"] == "not_run" and template["oracle"] is None
    results_path.write_text(json.dumps([run_record(plan, slot) for slot in range(30)]), encoding="utf-8")
    assert cli.main(["score", "--manifest", str(plan_path), "--results", str(results_path)]) == 0
    assert json.loads(capsys.readouterr().out)["campaign_passed"]


@pytest.mark.parametrize("value", [None, {}, [], float("inf"), 10 ** 1000, True])
def test_invalid_native_vectors_and_scalars_cannot_pass(value):
    record = run_record()
    record["oracle"]["measurement"]["volume_mm3"] = value
    record["oracle"]["measurement"]["bounds_mm"]["min"] = [value, 0, 0]
    assert row(record)["status"] == "geometry_or_construction_failed"


def test_safety_metrics_include_safe_geometric_rejection_and_budget_not_only_review():
    records = [run_record(slot=0), run_record(slot=1)]
    records[0]["oracle"]["measurement"]["volume_mm3"] = 1
    records[1]["elapsed_seconds"] = benchmark.MAX_RUN_SECONDS + 1
    result = benchmark.score(manifest(), records)
    assert result["metrics"]["safe_failures"] == 2 and result["metrics"]["safe_stops"] == 0


def test_environment_rejects_placeholders_extra_secrets_and_missing_source_digest():
    for key in ("pi_version", "model_id"):
        value = environment()
        value[key] = "unknown"
        with pytest.raises(ValueError):
            benchmark.create_manifest(value)
    value = environment()
    value["api_key"] = "never-import-configured-secrets"
    with pytest.raises(ValueError):
        benchmark.create_manifest(value)
    value = environment()
    del value["source_tree_sha256"]
    with pytest.raises(ValueError):
        benchmark.create_manifest(value)


@pytest.mark.parametrize("value", [[], None, 1, "manifest"])
def test_template_rejects_nonobject_manifest_without_a_traceback(tmp_path, capsys, value):
    with pytest.raises(ValueError):
        benchmark.record_template(value, "front_block.r1")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    assert cli.main(["template", "--manifest", str(path), "--run-id", "front_block.r1"]) == 2
    assert "nothing was executed" in capsys.readouterr().err
