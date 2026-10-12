import json
import sqlite3

from sw_assistant.domain import Plan, ToolCall
from sw_assistant.infrastructure import HistoryStore


def test_history_records_planning_failures(tmp_path):
    history = HistoryStore(tmp_path / "history.db")
    record_id = history.save_failure("bad request", "model returned invalid JSON")
    row = history.recent()[0]
    assert row["id"] == record_id
    assert row["state"] == "failed"
    assert row["error"] == "model returned invalid JSON"


def test_history_preserves_failed_plan_and_structured_diagnostics(tmp_path):
    path = tmp_path / "history.db"
    history = HistoryStore(path)
    plan = Plan(
        "bad plane",
        [
            ToolCall("document.new_part", {}),
            ToolCall("sketch.begin", {"plane": "左视基准面"}),
        ],
    )
    record_id = history.save_failure(
        "create part",
        "plane invalid",
        plan=plan,
        diagnostics={
            "step_index": 2,
            "tool": "sketch.begin",
            "field": "plane",
            "received_value": "左视基准面",
            "expected_schema": {"type": "string", "enum": ["front", "top", "right"]},
        },
    )
    with sqlite3.connect(path) as db:
        payload = json.loads(
            db.execute(
                "SELECT plan_json FROM plan_history WHERE id=?", (record_id,)
            ).fetchone()[0]
        )
    assert payload["plan"]["steps"][1]["arguments"]["plane"] == "左视基准面"
    assert payload["diagnostics"]["step_index"] == 2
    assert payload["diagnostics"]["expected_schema"]["enum"] == [
        "front",
        "top",
        "right",
    ]


def test_history_plan_is_updated_after_recovery_replacement(tmp_path):
    path = tmp_path / "history.db"
    history = HistoryStore(path)
    plan = Plan("task", [ToolCall("selection.plane", {"plane": "front"})])
    history.save_plan("task", plan)
    plan.steps[0] = ToolCall("selection.plane", {"plane": "top"})
    history.update_plan(plan)
    with sqlite3.connect(path) as db:
        payload = json.loads(
            db.execute("SELECT plan_json FROM plan_history WHERE id=?", (plan.id,)).fetchone()[0]
        )
    assert payload["steps"][0]["arguments"]["plane"] == "top"
