"""Explicit opt-in, unsaved fixture smoke for the V5 loop; no external model calls."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sw_assistant.agent.task_models import TaskDecision
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.solidworks import SolidWorksWorkerClient
from sw_assistant.tools import build_tool_registry


class FixtureBackend:
    """A scripted oracle isolates CAD integration from model suggestion quality."""

    def __init__(self):
        self.contexts: list[dict] = []

    def decide(self, context: dict) -> TaskDecision:
        self.contexts.append(context)
        round_number = len(self.contexts)
        goals = [
            {"id": "part", "description": "零件文档", "metric": "document_type", "expected": "part"},
            {"id": "body", "description": "一个实体", "metric": "body_count", "expected": 1},
            {"id": "volume", "description": "10 mm 立方体体积", "metric": "volume_mm3",
             "expected": 1000, "tolerance": 0.001},
        ]
        if round_number == 1:
            return TaskDecision.model_validate({
                "action": "act", "reason": "先建立新测试零件与闭合矩形草图。", "phase": "草图阶段",
                "goals": goals,
                "requirements": [{"id": item["id"], "goal_ids": ["volume", "part", "body"]}
                                 for item in context.get("requirements", [])],
                "steps": [
                    {"tool": "session.ensure", "arguments": {}},
                    {"tool": "document.new_part", "arguments": {}},
                    {"tool": "selection.plane", "arguments": {"plane": "front"}},
                    {"tool": "sketch.begin", "arguments": {"plane": "front"}},
                    {"tool": "sketch.rectangle", "arguments": {"width_mm": 10, "height_mm": 10}},
                ],
            })
        if round_number == 2:
            if len(context.get("completed_steps", [])) != 5:
                return TaskDecision(action="review", reason="测试第一阶段未完整执行。")
            return TaskDecision.model_validate({
                "action": "act", "reason": "已收到真实草图证据，再进行拉伸。", "phase": "实体阶段",
                "steps": [{"tool": "feature.extrude", "arguments": {"depth_mm": 10}}],
            })
        if round_number == 3:
            return TaskDecision(action="complete", reason="申请独立验收尺寸与体积。", phase="最终验收")
        return TaskDecision(action="review", reason="固定测试方案未通过，保留现场用于诊断。")

    def close(self):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="create an unsaved 10 mm cube")
    args = parser.parse_args()
    if not args.execute:
        parser.error("Pass --execute to create an isolated unsaved fixture.")
    output = ROOT / "artifacts" / "v5-live-smoke"
    worker = SolidWorksWorkerClient()
    backend = FixtureBackend()

    def progress(name, payload):
        if name in {"task_review", "step_succeeded", "task_completed", "task_feedback"}:
            print(name, payload.get("message", payload.get("call", {}).get("tool", "")), flush=True)

    runner = ContinuousTaskRunner(
        build_tool_registry(), worker, backend, TaskStore(output / "tasks"),
        on_event=progress, max_rounds=8,
    )
    try:
        runner.prepare("新建一个 10 × 10 × 10 mm 立方体，保持未保存。")
        result = runner.run()
        summary = {
            "status": result["status"], "message": result["message"],
            "rounds": result["rounds"], "completed_steps": len(result["completed_steps"]),
            "verification": result["verification"], "external_model_requests": 0,
        }
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 1
    finally:
        worker.close()


if __name__ == "__main__":
    raise SystemExit(main())
