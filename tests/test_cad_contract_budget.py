"""TDD contract for matrix-only write-budget admission.

Added as TDD while native acceptance froze src/scripts; no test invokes COM
or a real CAD worker. Insufficient reservation is a budget block, not a
geometry failure and not permission to replay a submitted write.
"""
from __future__ import annotations

import json
import math

import pytest

from scripts import live_cad_contract_matrix as matrix
from scripts.cad_contract_catalog import CASE_IDS, case
from sw_assistant.domain import StepResult
from sw_assistant.solidworks.client import _CANDIDATE_TIMEOUTS, _CHECKPOINT_TIMEOUTS
from test_cad_contract_catalog import observations
from test_live_cad_contract_matrix import environment, report


@pytest.fixture
def budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(matrix.time, "monotonic", lambda: now[0])
    guard = matrix.MatrixBudget(0.0)
    yield guard, now
    guard.timer.cancel()


def remaining(guard, now, seconds):
    now[0] = guard.deadline - seconds


def test_reservations_match_production_rpc_deadlines_not_estimated_mean_duration():
    assert matrix.RPC_TIMEOUT_SECONDS == 120
    assert _CANDIDATE_TIMEOUTS == {"begin": 180, "finish": 240}
    assert _CHECKPOINT_TIMEOUTS["restore"] == 240


@pytest.mark.parametrize("reservation", [0, 150, 210, 270])
@pytest.mark.parametrize("difference", [0, -0.001, -1])
def test_equal_or_insufficient_remaining_budget_is_not_admitted(budget, reservation, difference):
    guard, now = budget
    remaining(guard, now, reservation + difference)
    with pytest.raises(matrix.BudgetBoundary):
        guard.check(minimum_remaining=reservation)


@pytest.mark.parametrize("reservation", [0, 150, 210, 270])
def test_strictly_more_than_full_rpc_plus_margin_is_admitted(budget, reservation):
    guard, now = budget
    remaining(guard, now, reservation + 0.001)
    guard.check(minimum_remaining=reservation)
    assert not guard.expired.is_set()


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_clock_cannot_grant_budget(budget, value):
    guard, now = budget
    now[0] = value
    with pytest.raises(matrix.BudgetBoundary):
        guard.check(minimum_remaining=150)


def test_clock_regression_cannot_silently_extend_available_budget(budget):
    guard, now = budget
    remaining(guard, now, 500)
    guard.check(minimum_remaining=150)
    now[0] -= 1
    with pytest.raises(matrix.BudgetBoundary):
        guard.check(minimum_remaining=150)


@pytest.mark.parametrize("value", [-1, True, math.nan, math.inf, "150"])
def test_invalid_reservation_is_refused(budget, value):
    guard, _ = budget
    with pytest.raises((matrix.BudgetBoundary, ValueError, TypeError)):
        guard.check(minimum_remaining=value)


class NoRPC:
    def call(self, *_args, **_kwargs):
        pytest.fail("insufficient new-part reservation must submit no RPC")

    def call_checked(self, *_args, **_kwargs):
        pytest.fail("insufficient checked-write reservation must submit no RPC")

    def candidate_request(self, *_args, **_kwargs):
        pytest.fail("insufficient candidate reservation must submit no RPC")

    def checkpoint_request(self, *_args, **_kwargs):
        pytest.fail("insufficient restore reservation must submit no RPC")


@pytest.mark.parametrize("available", [150, 149, 0, -1])
def test_new_part_requires_120_second_rpc_plus_30_second_margin_before_submit(budget, available):
    guard, now = budget
    remaining(guard, now, available)
    with pytest.raises(matrix.BudgetBoundary):
        matrix.execute_case(NoRPC(), case("front.rectangle"), guard.check, {"steps": []}, verify_save_reopen=False)


@pytest.mark.parametrize("available", [210, 209, 88.156, 0, -1])
def test_candidate_begin_requires_180_plus_30_before_submit(budget, available):
    guard, now = budget
    remaining(guard, now, available)
    with pytest.raises(matrix.BudgetBoundary):
        matrix.save_reopen(NoRPC(), case("top.circle"), {"document_identity": "source"}, guard.check, {})


@pytest.mark.parametrize("remaining_after_read", [150, 149, 0, -1])
def test_checked_write_rechecks_reservation_after_observation_consumes_time(budget, remaining_after_read):
    guard, now = budget
    remaining(guard, now, 600)
    previous = observations(case("right.rectangle"), solid=False)[0]
    calls = []

    class Worker(NoRPC):
        def call(self, call, timeout):
            assert call.tool == "model.observe"
            calls.append(call.tool)
            remaining(guard, now, remaining_after_read)
            return StepResult(True, call, "synthetic independent observation", data=previous)

    with pytest.raises(matrix.BudgetBoundary):
        matrix.checked_step(Worker(), case("right.rectangle").sketch_calls[0], previous, guard.check, {"steps": []})
    assert calls == ["model.observe"]


@pytest.mark.parametrize("target", ["finish", "restore"])
@pytest.mark.parametrize("available", [270, 269, 3.109, 0, -1])
def test_finish_and_restore_reserve_240_plus_30_after_prior_work(monkeypatch, budget, target, available):
    guard, now = budget
    remaining(guard, now, 1000)
    events = []

    def accept(*_args, **_kwargs):
        if target == "finish":
            remaining(guard, now, available)
        return {"status": "passed"}, {"document_identity": "candidate"}

    monkeypatch.setattr(matrix, "accept_solid", accept)

    class Worker:
        def candidate_request(self, operation, **payload):
            if operation == "begin":
                events.append(operation)
                return {"ok": True, "status": "begun", "token": "test-token"}
            if target == "finish":
                pytest.fail("finish must not be submitted without its full reservation")
            events.append(operation)
            return {"ok": True, "status": "committed", "checkpoint_id": "a" * 32, "persisted_current_state": True}

        def checkpoint_request(self, operation, **payload):
            if operation == "restore":
                pytest.fail("restore must not be submitted without its full reservation")
            events.append(operation)
            remaining(guard, now, available)
            return {"ok": True, "status": "listed", "issues": [], "checkpoints": [{
                "checkpoint_id": "a" * 32, "integrity": "verified", "native_sha256": "b" * 64, "native_size": 100}]}

    with pytest.raises(matrix.BudgetBoundary):
        matrix.save_reopen(Worker(), case("front.revolve"), {"document_identity": "source"}, guard.check, {})
    assert events == (["begin"] if target == "finish" else ["begin", "finish", "list"])


def test_budget_block_preserves_completed_case_and_marks_remaining_not_run(monkeypatch, tmp_path):
    _gateway, _workers, _executions = environment(monkeypatch, tmp_path)
    original = matrix.execute_case

    def execute(worker, spec, check_budget, record, **kwargs):
        if spec.case_id != CASE_IDS[0]:
            raise matrix.BudgetBoundary("budget_boundary: no full RPC window remains")
        return original(worker, spec, check_budget, record, **kwargs)

    monkeypatch.setattr(matrix, "execute_case", execute)
    assert matrix.main(["--execute", "--case", "all"]) == 2
    actual = report(tmp_path)
    assert actual["status"] == "blocked" and actual["reason_code"] == "budget_boundary"
    assert not actual["passed"] and not actual["budget_exhausted"]
    assert actual["counts"] == {"passed": 1, "blocked": 1, "failed": 0, "not_run": len(CASE_IDS) - 2}
    assert actual["cases"][0]["status"] == "passed"
    assert actual["cases"][1]["reason_code"] == "budget_boundary"
    assert actual["cases"][1]["sketch_geometry"]["status"] == "not_run"
    assert all(item["reason"] == "budget_boundary" for item in actual["cases"][2:])
    json.dumps(actual, allow_nan=False)


def test_budget_block_during_save_is_not_reported_as_solid_geometry_failure(monkeypatch, tmp_path):
    environment(monkeypatch, tmp_path)

    def execute(worker, spec, check_budget, record, **kwargs):
        record["sketch_geometry"] = {"status": "passed"}
        record["solid_geometry"] = {"status": "passed"}
        record["save_reopen"] = {"status": "failed", "finish_receipt": {"status": "committed"}}
        raise matrix.BudgetBoundary("budget_boundary: cannot reserve checkpoint.restore")

    monkeypatch.setattr(matrix, "execute_case", execute)
    assert matrix.main(["--execute", "--verify-save-reopen"]) == 2
    actual = report(tmp_path)
    current = next(item for item in actual["cases"] if item["selected"])
    assert actual["status"] == current["status"] == "blocked"
    assert current["sketch_geometry"]["status"] == current["solid_geometry"]["status"] == "passed"
    assert current["save_reopen"]["status"] == "blocked"
    assert current["save_reopen"]["reason_code"] == "budget_boundary"
    assert actual["counts"]["failed"] == 0
