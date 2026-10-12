from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from ..domain import StepResult


_TRANSIENT = re.compile(
    r"(?i)(RPC_E_CALL_REJECTED|被呼叫方拒绝接收呼叫|server busy|暂时不可用|"
    r"正在执行其他命令|上下文已经过期|file.*lock|文件.*占用|timeout|超时)"
)
_CAPABILITY = re.compile(r"(?i)(NotSupported|不支持|未提供可用|能力.+不可用|接口.+不可用)")
_TOPOLOGY = re.compile(
    r"(?i)(拓扑|持久引用|找不到.*(?:面|边|点)|无法.*(?:选择|解析)|草图会话.*失效)"
)
_GEOMETRY = re.compile(
    r"(?i)(PostconditionFailed|重建|实体|草图|轮廓|相交|创建.*失败|参数与请求不一致)"
)
_ENVIRONMENT = re.compile(
    r"(?i)(工作进程.*(?:停止|退出)|无法连接 SolidWorks|COM 会话|安装|许可证)"
)


@dataclass(frozen=True, slots=True)
class RecoveryAssessment:
    category: str
    fingerprint: str
    model_changed: bool
    state_known: bool
    safe_retry: bool
    ai_repair_recommended: bool
    manual_review_required: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "fingerprint": self.fingerprint,
            "model_changed": self.model_changed,
            "state_known": self.state_known,
            "safe_retry": self.safe_retry,
            "ai_repair_recommended": self.ai_repair_recommended,
            "manual_review_required": self.manual_review_required,
            "reason": self.reason,
        }


def _snapshot_signature(result: StepResult) -> dict[str, Any]:
    after = result.after
    if after is None:
        return {}
    return {
        "document_title": after.document_title,
        "document_type": after.document_type,
        "active_sketch": after.active_sketch,
        "sketch_entity_count": after.sketch_entity_count,
        "feature_count": after.feature_count,
        "body_count": after.body_count,
        "volume_m3": after.volume_m3,
        "selection_count": after.selection_count,
    }


def _changed(result: StepResult) -> tuple[bool, bool]:
    before = result.before
    after = result.after
    if before is None or after is None:
        return False, False
    changed = any(
        (
            before.document_title != after.document_title,
            before.document_path != after.document_path,
            before.document_type != after.document_type,
            before.active_sketch != after.active_sketch,
            before.sketch_entity_count != after.sketch_entity_count,
            before.feature_count != after.feature_count,
            before.body_count != after.body_count,
            not math.isclose(
                before.volume_m3,
                after.volume_m3,
                rel_tol=1e-9,
                abs_tol=1e-15,
            ),
        )
    )
    return changed, True


def _fingerprint(result: StepResult, category: str) -> str:
    payload = {
        "tool": result.call.tool,
        "arguments": result.call.arguments,
        "error_code": result.error_code,
        "category": category,
        "state": _snapshot_signature(result),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def assess_failure(result: StepResult) -> RecoveryAssessment:
    text = f"{result.error_code} {result.message}"
    if result.error_code == "ContextUnavailable" or _TRANSIENT.search(text):
        category = "transient"
    elif _CAPABILITY.search(text):
        category = "capability"
    elif _TOPOLOGY.search(text):
        category = "topology"
    elif _GEOMETRY.search(text):
        category = "geometry"
    elif _ENVIRONMENT.search(text):
        category = "environment"
    else:
        category = "unknown"

    model_changed, state_known = _changed(result)
    safe_retry = bool(category == "transient" and state_known and not model_changed)
    ai_repair = bool(
        category in {"geometry", "topology", "capability", "unknown"}
        and state_known
        and not model_changed
    )
    manual_review = bool(model_changed or not state_known)
    if model_changed:
        reason = "失败步骤已经改变模型，必须先回滚或人工核对，禁止原样重试。"
    elif not state_known:
        reason = "失败后无法确认 SolidWorks 状态，必须人工核对当前文档。"
    elif safe_retry:
        reason = "模型未变化且属于暂时性故障，可进行一次受控重试。"
    elif ai_repair:
        reason = "模型未变化，可把结构化故障上下文交给模型生成替代后缀。"
    else:
        reason = "该错误不适合自动重试，需要修复环境或人工处理。"
    return RecoveryAssessment(
        category=category,
        fingerprint=_fingerprint(result, category),
        model_changed=model_changed,
        state_known=state_known,
        safe_retry=safe_retry,
        ai_repair_recommended=ai_repair,
        manual_review_required=manual_review,
        reason=reason,
    )
