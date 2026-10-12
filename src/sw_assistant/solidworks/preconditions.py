"""Read-only host preconditions, evaluated inside the CAD execution gate.

The caller supplies the previously verified observation, not model-authored
claims. These checks supplement, rather than replace, post-execution evidence.
"""
from __future__ import annotations

import math
from typing import Any, NoReturn

from ..domain import ModelSnapshot, ToolError
from .gateway import SolidWorksGateway, _read_member


SNAPSHOT_FIELDS = (
    "connected", "document_title", "document_path", "document_type", "active_sketch",
    "sketch_entity_count", "feature_count", "body_count", "volume_m3", "selection_count",
)
_HEADER_FIELDS = ("connected", "document_title", "document_path", "document_type")
_COUNT_FIELDS = ("sketch_entity_count", "feature_count", "body_count", "selection_count")


class StateChanged(ToolError):
    """Verified state changed. Worker receipts say whether execution started."""

    def __init__(self, message: str, differences: list[str]) -> None:
        super().__init__(message)
        self.evidence = {"differences": sorted(set(differences))}


def _refuse(fields: list[str], reason: str = "状态与已验证观察不一致") -> NoReturn:
    raise StateChanged(f"执行前检查拒绝操作：{reason}（{', '.join(fields)}）。", fields)


def _text(value: Any, maximum: int = 1024, *, empty: bool = False) -> bool:
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum and "\0" not in value


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _volume(value: Any) -> bool:
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except (TypeError, ValueError, OverflowError):
        return False


def _snapshot(value: Any, prefix: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _refuse([prefix], "缺少有效的模型快照")
    invalid = [f"{prefix}.{key}" for key in SNAPSHOT_FIELDS if key not in value]
    if invalid:
        _refuse(invalid, "模型快照字段缺失")
    if value["connected"] is not True:
        invalid.append(f"{prefix}.connected")
    if not _text(value["document_title"]):
        invalid.append(f"{prefix}.document_title")
    if not _text(value["document_path"], 32768, empty=True):
        invalid.append(f"{prefix}.document_path")
    if value["document_type"] not in ("part", "assembly", "drawing"):
        invalid.append(f"{prefix}.document_type")
    if not _text(value["active_sketch"], empty=True):
        invalid.append(f"{prefix}.active_sketch")
    invalid.extend(f"{prefix}.{key}" for key in _COUNT_FIELDS if not _integer(value[key]))
    volume = value["volume_m3"]
    if not _volume(volume):
        invalid.append(f"{prefix}.volume_m3")
    if invalid:
        _refuse(invalid, "模型快照字段不完整或类型无效")
    return value


def _header(gateway: SolidWorksGateway, expected: dict[str, Any], snapshot: dict[str, Any]) -> None:
    try:
        actual = gateway.probe_document_identity()
    except Exception as exc:
        raise StateChanged("执行前检查无法可靠读取活动文档身份。", ["document_identity"]) from exc
    if not isinstance(actual, dict):
        _refuse(["document_identity"], "原生文档身份读取无效")
    differences = [key for key in _HEADER_FIELDS if actual.get(key) != snapshot[key]
                   or type(actual.get(key)) is not type(snapshot[key])]
    if not _text(actual.get("document_identity")) or actual["document_identity"] != expected["document_identity"]:
        differences.append("document_identity")
    if differences:
        _refuse(differences)


def validate_precondition(gateway: SolidWorksGateway, expected: Any, before: ModelSnapshot | None) -> None:
    """Fail closed when an expected observation cannot be confirmed immediately.

    Must be called under the same serial gate as the following execute. This
    function never activates a document, retries a read, or mutates CAD state.
    """
    if not isinstance(expected, dict) or expected.get("complete") is not True:
        _refuse(["expected.complete"], "缺少完整且已验证的预期观察")
    if "issues" in expected and (not isinstance(expected["issues"], list) or expected["issues"]):
        _refuse(["expected.issues"], "预期观察仍包含读取问题或无效问题列表")
    if not _text(expected.get("document_identity")):
        _refuse(["document_identity"], "缺少有效的预期原生文档身份")
    snapshot = _snapshot(expected.get("snapshot"), "expected.snapshot")
    for key in ("configuration", "configuration_identity"):
        if key in expected and not _text(expected[key], 250 if key == "configuration" else 1024):
            _refuse([key], "预期配置证据无效")
    if "model_update_stamp" in expected and not _integer(expected["model_update_stamp"]):
        _refuse(["model_update_stamp"], "预期模型更新戳无效")
    actual = _snapshot(before.to_dict() if isinstance(before, ModelSnapshot) else None, "before")
    differences = []
    for key in SNAPSHOT_FIELDS:
        same = (math.isclose(snapshot[key], actual[key], rel_tol=1e-7, abs_tol=1e-12)
                if key == "volume_m3" else snapshot[key] == actual[key])
        if not same:
            differences.append(key)
    if differences:
        _refuse(differences)
    _header(gateway, expected, snapshot)
    try:
        document = gateway.require_document()
        if gateway._com_identity(document) != expected["document_identity"]:
            _refuse(["document_identity"])
        if "configuration" in expected or "configuration_identity" in expected:
            configuration = _read_member(_read_member(document, "ConfigurationManager"), "ActiveConfiguration")
            name = _read_member(configuration, "Name")
            identity = gateway._com_identity(configuration)
            if not _text(name, 250) or not _text(identity):
                _refuse(["configuration", "configuration_identity"], "原生配置证据不可读")
            if "configuration" in expected and name != expected["configuration"]:
                differences.append("configuration")
            if "configuration_identity" in expected and identity != expected["configuration_identity"]:
                differences.append("configuration_identity")
        if "model_update_stamp" in expected:
            stamp = _read_member(document, "GetUpdateStamp")
            if not _integer(stamp) or stamp != expected["model_update_stamp"]:
                differences.append("model_update_stamp")
    except StateChanged:
        raise
    except Exception as exc:
        raise StateChanged("执行前检查缺少可靠的原生状态证据。", ["native_state"]) from exc
    if differences:
        _refuse(differences)
    _header(gateway, expected, snapshot)
