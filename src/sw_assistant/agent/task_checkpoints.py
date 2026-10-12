"""Explicit host checkpoint recovery; never a model-callable restore tool."""
from __future__ import annotations

import copy
import re
import threading
from typing import Any

from ..domain import ToolCall, utc_now
from ..solidworks.checkpoints import _clean, _equivalent, _state
from .task_contract import observed_target
from .task_models import GoalCheck

_CHECKPOINT_ID = re.compile(r"[0-9a-f]{32}\Z")
_PREFIX_FIELDS = ("completed_steps", "evidence", "created_features")


class CheckpointRunnerMixin:
    _busy_owner: int | None

    def _checkpoint_alive(self: Any, epoch: int) -> bool:
        with self._lock:
            return bool(self._task) and not self._cancelled and epoch == self._epoch

    def _record_checkpoint(self: Any, result: dict[str, Any], observation: dict[str, Any],
                            epoch: int) -> bool:
        """Called only after committed candidate state was independently checked."""
        if not callable(getattr(self.worker, "checkpoint_request", None)):
            return True  # Legacy/fake workers do not advertise durable recovery.
        checkpoint_id = result.get("checkpoint_id")
        if (result.get("persisted_current_state") is not True or not isinstance(checkpoint_id, str)
                or not _CHECKPOINT_ID.fullmatch(checkpoint_id)):
            self._review(epoch, "候选已采用但缺少可验证的持久检查点编号，已暂停；不能把初始副本文件当作最终结果。")
            return False
        record = {
            "task_id": self._task["id"], "checkpoint_id": checkpoint_id,
            "parent_id": result.get("checkpoint_parent_id"), "created_at": utc_now(),
            "observation": copy.deepcopy(observation),
            "prefix": {key: copy.deepcopy(self._task.get(key, [])) for key in _PREFIX_FIELDS},
        }
        records = [item for item in self._task.get("checkpoint_records", [])
                   if item.get("checkpoint_id") != checkpoint_id]
        records.append(record)
        return bool(self._change(epoch, checkpoint_records=records, active_checkpoint_id=checkpoint_id))

    def refresh_checkpoints(self: Any) -> dict[str, Any]:
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("任务仍在运行。")
        try:
            epoch = self._epoch
            if not self._checkpoint_alive(epoch):
                return self.snapshot()
            request = getattr(self.worker, "checkpoint_request", None)
            if not callable(request):
                raise ValueError("当前工作进程不支持持久检查点。")
            response = request("list", task_id=self._task["id"])
            if not self._checkpoint_alive(epoch):
                return self.snapshot()
            if response.get("ok") is not True or response.get("status") != "listed":
                raise ValueError("检查点列表读取失败：" + str(response.get("reason", "返回状态未知")))
            records = {item["checkpoint_id"]: item for item in self._task.get("checkpoint_records", [])
                       if isinstance(item, dict) and item.get("task_id") == self._task["id"]
                       and isinstance(item.get("checkpoint_id"), str)}
            catalog = []
            for item in response.get("checkpoints", []):
                if not isinstance(item, dict) or item.get("integrity") != "verified":
                    continue
                key = item.get("checkpoint_id")
                if not isinstance(key, str) or not _CHECKPOINT_ID.fullmatch(key):
                    continue
                host = records.get(key)
                catalog.append({"checkpoint_id": key, "parent_id": item.get("parent_id"),
                                "created_at": str(item.get("created_at") or ""),
                                "integrity": "verified", "host_prefix_available": host is not None,
                                "completed_count": len(host.get("prefix", {}).get("completed_steps", [])) if host else 0})
            self._change(epoch, checkpoint_catalog=catalog, checkpoint_issues=response.get("issues", []))
            self._event("checkpoints_listed", epoch, message=f"找到 {len(catalog)} 个磁盘校验通过的检查点；恢复不会自动继续执行。")
            return self.snapshot()
        finally:
            self._busy.release()

    def restore_checkpoint(self: Any, checkpoint_id: str) -> dict[str, Any]:
        """User action: restore geometry prefix, retain current goals and budgets."""
        from .task_runner import _identity

        if not isinstance(checkpoint_id, str) or not _CHECKPOINT_ID.fullmatch(checkpoint_id):
            raise ValueError("检查点编号无效。")
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("任务仍在运行。")
        epoch = self._epoch
        try:
            self._busy_owner = threading.get_ident()
            if not self._checkpoint_alive(epoch):
                return self.snapshot()
            matches = [item for item in self._task.get("checkpoint_records", [])
                       if isinstance(item, dict) and item.get("checkpoint_id") == checkpoint_id
                       and item.get("task_id") == self._task["id"]]
            if len(matches) != 1:
                raise ValueError("没有唯一匹配本任务的宿主成功前缀，不能仅凭磁盘文件或旧试验编号恢复。")
            record = copy.deepcopy(matches[0])
            prefix, expected = record.get("prefix"), record.get("observation")
            if (not isinstance(prefix, dict) or not isinstance(expected, dict)
                    or set(prefix) != set(_PREFIX_FIELDS)
                    or any(not isinstance(prefix.get(key), list) for key in _PREFIX_FIELDS)
                    or not any(item.get("tool") == "document.new_part"
                               for item in prefix["completed_steps"] if isinstance(item, dict))):
                raise ValueError("检查点成功前缀不完整。")
            _clean(expected)
            if not callable(getattr(self.worker, "checkpoint_request", None)):
                raise ValueError("当前工作进程不支持持久检查点。")
            for call in (ToolCall("session.ensure", {}), ToolCall("document.new_part", {})):
                allowed, reason = self.preflight(call)
                if not self._checkpoint_alive(epoch):
                    return self.snapshot()
                if not allowed:
                    raise ValueError(reason or "当前权限不允许恢复独立模型副本。")
            self._change(epoch, status="running", phase="checkpoint_restore", pending_decision=None,
                         pending_steps=[], message="正在恢复独立检查点副本；完成后等待你明确继续。")
            self._event("checkpoint_restore_started", epoch, message="正在连接并验证检查点；未知活动文档不会被接管。")
            if not self._checkpoint_alive(epoch):
                return self.snapshot()
            call = ToolCall("session.ensure", {}, "为显式检查点恢复连接 SolidWorks")
            connected = self.worker.call(call, 120)
            if not self._checkpoint_alive(epoch):
                return self.snapshot()
            verifier = self.registry.postcondition(call.tool)
            if (not connected.ok or verifier is None
                    or not verifier(call, connected.data, connected.before, connected.after)[0]):
                raise ValueError("SolidWorks 连接未通过检查；没有尝试恢复副本。")
            response = self.worker.checkpoint_request("restore", task_id=self._task["id"], checkpoint_id=checkpoint_id)
            if not self._checkpoint_alive(epoch):
                return self.snapshot()
            if (response.get("ok") is not True or response.get("status") != "restored"
                    or response.get("checkpoint_id") != checkpoint_id
                    or response.get("checkpoint_preserved") is not True):
                raise ValueError("检查点恢复未确认，已保留现场：" + str(response.get("reason", "响应无效")))
            observed = response.get("observation")
            _clean(observed)
            if not _equivalent(_state(expected, identity=False), _state(observed, identity=False)):
                raise ValueError("恢复副本与宿主保存的成功前缀不一致。")
            read = self.worker.call(ToolCall("model.observe", {"include_configuration": True}), 120)
            if not self._checkpoint_alive(epoch):
                return self.snapshot()
            fresh = read.data
            if not read.ok:
                raise ValueError("恢复后的独立观察失败，不能采用旧任务证据。")
            _clean(fresh)
            if not _equivalent(_state(observed), _state(fresh)):
                raise ValueError("恢复完成后模型又发生变化，已停止。")
            # Keep all user requirements/goals, action/decision budgets, failures
            # and messages. Only verified geometry success history rolls back.
            history = copy.deepcopy(self._task.get("checkpoint_restore_history", []))
            history.append({"checkpoint_id": checkpoint_id, "time": utc_now(),
                            "discarded_completed_count": len(self._task.get("completed_steps", [])),
                            "active_trial_abandoned": bool(self._task.get("active_trial"))})
            trial_history = copy.deepcopy(self._task.get("trial_history", []))
            if self._task.get("active_trial"):
                old_trial = copy.deepcopy(self._task["active_trial"])
                old_trial.pop("token", None)
                old_trial.update(status="abandoned", finished_at=utc_now(),
                                 reason="Explicit verified checkpoint restore; old trial was not adopted.")
                trial_history.append(old_trial)
            verification_history = copy.deepcopy(self._task.get("verification_history", []))
            if self._task.get("verification"):
                verification_history.append(self._task["verification"])
            bindings = copy.deepcopy(self._task.get("goal_bindings", {}))
            for raw in self._task.get("goals", []):
                goal = GoalCheck.model_validate(raw)
                if goal.metric in {"dimension_mm", "feature_present"}:
                    binding = bindings.setdefault(goal.id, {})
                    binding.update(verified=observed_target(goal, fresh),
                                   checkpoint_missing=not observed_target(goal, fresh))
            audit = copy.deepcopy(self._task.get("contract_audit", []))
            audit.append({"kind": "checkpoint_geometry_rebound", "time": utc_now(),
                          "checkpoint_id": checkpoint_id, "goals_unchanged": True})
            self._change(epoch, **prefix, expected_observation=fresh, latest_observation=fresh,
                         bound_document=list(_identity(fresh)), active_trial=None, trial_history=trial_history,
                         active_checkpoint_id=checkpoint_id, checkpoint_restore_history=history,
                         verification={}, verification_history=verification_history, created_circles=[],
                         pending_decision=None, pending_steps=[], dirty_failure=None, resume_pending=False,
                         gateway_context_reset=True, restored=False, connection_refreshed=False,
                         goal_bindings=bindings, contract_audit=audit,
                         status="ready", phase="ready", review_kind="", questions=[],
                         message="检查点副本及成功前缀已独立验证恢复；当前需求全部保留。尚未继续执行，请点击继续执行。")
            self._event("checkpoint_restored", epoch, message="检查点恢复完成，当前目标仍需重新验收；未自动执行下一阶段。")
            return self.snapshot()
        except Exception as exc:
            if self._checkpoint_alive(epoch):
                self._review(epoch, "检查点恢复未完成：" + str(exc))
            return self.snapshot()
        finally:
            self._busy_owner = None
            self._busy.release()
