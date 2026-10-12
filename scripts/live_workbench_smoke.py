"""Opt-in: real Pi + local scripted model + real unsaved SolidWorks fixture."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sw_assistant.agent.task_backend import TaskAgentBackend
from sw_assistant.agent.task_runner import ContinuousTaskRunner
from sw_assistant.agent.task_store import TaskStore
from sw_assistant.infrastructure import ModelSettings
from sw_assistant.solidworks import SolidWorksWorkerClient
from sw_assistant.tools import build_tool_registry


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("Pass --execute to create one new unsaved fixture; no existing CAD file is saved.")
    output = ROOT / "artifacts" / "v51-live-workbench"
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            index = len(requests)
            if index in {1, 4}:
                tool = "session.capabilities" if index == 1 else "model.references"
                delta = {"role": "assistant", "tool_calls": [{
                    "index": 0, "id": f"investigate_{index}", "type": "function", "function": {
                        "name": "sw_investigate", "arguments": json.dumps({
                            "tool": tool, "arguments_json": "{}" if index == 1 else '{"limit":5}',
                        }),
                    },
                }]}
                reason = "tool_calls"
            else:
                if index == 2:
                    decision = {"action": "act", "reason": "已查询能力，先建立新零件。",
                                "phase": "准备文档", "goals": [
                                    {"id": "volume", "description": "20 × 10 × 10 mm 棱柱体积",
                                     "metric": "volume_mm3", "expected": 2000, "tolerance": 0.001}],
                                "requirements": [{"id": "r1", "goal_ids": ["volume"]}],
                                "steps": [{"tool": "session.ensure"}, {"tool": "document.new_part"}]}
                elif index == 3:
                    # Deliberately invalid proposal: it must be rejected locally
                    # before any CAD write, then returned to the same Pi session.
                    decision = {"action": "act", "reason": "候选基准面方案", "steps": [
                        {"tool": "sketch.begin", "arguments": {"plane": "missing_plane"}}]}
                elif index == 5:
                    decision = {"action": "act", "reason": "已查询实际候选基准面，改用稳定 front 方案。",
                                "phase": "修正并建模", "steps": [
                                    {"tool": "sketch.begin", "arguments": {"plane": "front"}},
                                    {"tool": "sketch.rectangle", "arguments": {"width_mm": 20, "height_mm": 10}},
                                    {"tool": "feature.extrude", "arguments": {"depth_mm": 10}}]}
                elif index == 6:
                    decision = {"action": "complete", "reason": "根据实际返回证据申请独立最终验收。"}
                else:
                    decision = {"action": "review", "review_kind": "user_decision",
                                "review_critical": True, "reason": "隔离测试偏离预期流程，保留现场并停止。"}
                delta = {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)}
                reason = "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for part, finish in ((delta, None), ({}, reason)):
                chunk = {"id": f"chatcmpl-{index}", "object": "chat.completion.chunk", "created": 1,
                         "model": "local-workbench-fixture", "choices": [{
                             "index": 0, "delta": part, "finish_reason": finish}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *_args):
            pass

    def progress(name, payload):
        if name in {"step_succeeded", "task_review", "task_feedback", "investigation_started",
                    "investigation_completed", "task_completed"}:
            print(name, payload.get("message", payload.get("tool", payload.get("call", {}).get("tool", ""))),
                  flush=True)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registry, worker = build_tool_registry(), SolidWorksWorkerClient()
    backend = TaskAgentBackend(
        ModelSettings(f"http://127.0.0.1:{server.server_port}/v1", "local-workbench-fixture",
                      "synthetic-local-only", timeout_seconds=180),
        registry, output / "agent", session_id="live-workbench-" + str(time.time_ns()), backend="pi",
    )
    runner = ContinuousTaskRunner(registry, worker, backend, TaskStore(output / "tasks"),
                                  on_event=progress, max_rounds=10)
    backend.set_investigator(runner.investigate)
    started = time.monotonic()
    try:
        with worker.operation():
            runner.prepare("新建一个 20 × 10 × 10 mm 的未保存矩形棱柱。")
            result = runner.run()
        summary = {"status": result["status"], "message": result["message"],
                   "duration_seconds": round(time.monotonic() - started, 2), "rounds": result["rounds"],
                   "completed_steps": len(result["completed_steps"]), "verification": result["verification"],
                   "diagnostic_tools": [item["tool"] for item in result.get("diagnostic_history", [])],
                   "diagnostic_success": [item.get("ok") for item in result.get("diagnostic_history", [])],
                   "local_model_requests": len(requests), "external_model_requests": 0,
                   "decision_rejections": sum(item.get("kind") in {"decision_rejected", "policy_rejected"}
                                               for item in result.get("feedback", []))}
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        return 0 if result["status"] == "completed" and len(requests) == 6 else 1
    finally:
        backend.close()
        worker.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


if __name__ == "__main__":
    raise SystemExit(main())
