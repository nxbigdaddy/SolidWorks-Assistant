from __future__ import annotations

import math
import threading
from collections import Counter, deque
from pathlib import Path
from typing import Any

from .repository import DocumentRecord, LibraryRepository


def _mapping_diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_keys = set(left)
    right_keys = set(right)
    return {
        "added": {key: right[key] for key in sorted(right_keys - left_keys)},
        "removed": {key: left[key] for key in sorted(left_keys - right_keys)},
        "changed": {
            key: {"before": left[key], "after": right[key]}
            for key in sorted(left_keys & right_keys)
            if left[key] != right[key]
        },
    }


def _dimension_values(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        str(item.get("name")): item.get("display_value_mm", item.get("system_value"))
        for item in manifest.get("dimensions", [])
        if isinstance(item, dict) and item.get("name")
    }


def _property_values(manifest: dict[str, Any]) -> dict[str, Any]:
    output = {
        f"document:{key}": value
        for key, value in dict(manifest.get("custom_properties") or {}).items()
    }
    for configuration, properties in dict(
        manifest.get("configuration_properties") or {}
    ).items():
        for key, value in dict(properties or {}).items():
            output[f"configuration:{configuration}:{key}"] = value
    return output


def compare_manifests(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_features = [
        {"name": item.get("name"), "type": item.get("type")}
        for item in left.get("features", [])
        if isinstance(item, dict)
    ]
    right_features = [
        {"name": item.get("name"), "type": item.get("type")}
        for item in right.get("features", [])
        if isinstance(item, dict)
    ]
    scalar_keys = (
        "document_type",
        "configurations",
        "material",
    )
    scalar_changes = {
        key: {"before": left.get(key), "after": right.get(key)}
        for key in scalar_keys
        if left.get(key) != right.get(key)
    }
    left_geometry = dict(left.get("geometry") or {})
    right_geometry = dict(right.get("geometry") or {})
    for key in ("mass_kg", "mass_available", "volume_m3", "surface_area_m2", "body_count"):
        if left_geometry.get(key) != right_geometry.get(key):
            scalar_changes[f"geometry.{key}"] = {
                "before": left_geometry.get(key),
                "after": right_geometry.get(key),
            }
    dimensions = _mapping_diff(_dimension_values(left), _dimension_values(right))
    properties = _mapping_diff(_property_values(left), _property_values(right))
    changed_count = (
        sum(len(section) for section in dimensions.values())
        + sum(len(section) for section in properties.values())
        + len(scalar_changes)
        + int(left_features != right_features)
    )
    return {
        "equal": changed_count == 0,
        "change_groups": changed_count,
        "dimensions": dimensions,
        "properties": properties,
        "features": {
            "changed": left_features != right_features,
            "before": left_features,
            "after": right_features,
        },
        "summary": scalar_changes,
    }


class LibraryAnalyzer:
    def __init__(self, repository: LibraryRepository) -> None:
        self.repository = repository

    def compare(self, left_id: int, right_id: int) -> dict[str, Any]:
        left_record = self._record(left_id)
        right_record = self._record(right_id)
        left = self._manifest(left_record)
        right = self._manifest(right_record)
        return {
            "left": self._record_summary(left_record),
            "right": self._record_summary(right_record),
            "differences": compare_manifests(left, right),
        }

    def quality_report(self, document_id: int) -> dict[str, Any]:
        record = self._record(document_id)
        manifest = self._manifest(record)
        issues: list[dict[str, str]] = []
        for feature in manifest.get("features", []):
            if isinstance(feature, dict) and int(feature.get("error_code") or 0) != 0:
                issues.append(
                    {
                        "severity": "error",
                        "code": "feature_error",
                        "message": f"特征 {feature.get('name') or '?'} 返回错误码 {feature.get('error_code')}。",
                    }
                )
        references = self._references(record, manifest)
        seen: Counter[str] = Counter()
        for reference in references:
            path = reference["path"]
            if path:
                seen[path.casefold()] += 1
            if not path:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "empty_reference",
                        "message": f"引用 {reference['name'] or '?'} 没有可解析路径。",
                    }
                )
            elif not Path(path).is_file():
                issues.append(
                    {
                        "severity": "error",
                        "code": "missing_reference",
                        "message": f"引用文件不存在：{path}",
                    }
                )
            if reference.get("suppressed"):
                issues.append(
                    {
                        "severity": "info",
                        "code": "suppressed_reference",
                        "message": f"引用处于压缩状态：{reference['name'] or path}",
                    }
                )
        for path, count in seen.items():
            if count > 1:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "duplicate_reference",
                        "message": f"同一引用路径出现 {count} 次：{path}",
                    }
                )
        if record.document_type in {"part", "assembly"} and not bool(
            dict(manifest.get("geometry") or {}).get("mass_available")
        ):
            issues.append(
                {
                    "severity": "info",
                    "code": "mass_unavailable",
                    "message": "质量属性不可用；可能尚未分配材料或完整重建。",
                }
            )
        severity = (
            "error"
            if any(item["severity"] == "error" for item in issues)
            else "warning"
            if any(item["severity"] == "warning" for item in issues)
            else "ok"
        )
        return {
            "document": self._record_summary(record),
            "status": severity,
            "issue_count": len(issues),
            "issues": issues,
        }

    def dependency_graph(
        self,
        document_id: int,
        max_nodes: int = 5000,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        root = self._record(document_id)
        queue: deque[DocumentRecord] = deque([root])
        visited: set[int] = set()
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        while queue and len(visited) < max_nodes:
            if cancel and cancel.is_set():
                raise InterruptedError("任务已停止。")
            record = queue.popleft()
            if record.id in visited:
                continue
            visited.add(record.id)
            manifest = self.repository.manifest(record.id) or {}
            nodes.append(self._record_summary(record))
            for reference in self._references(record, manifest):
                target = self.repository.document_by_path(reference["path"])
                edges.append(
                    {
                        "source_id": record.id,
                        "target_id": target.id if target else None,
                        "path": reference["path"],
                        "name": reference["name"],
                        "kind": reference["kind"],
                        "resolved": target is not None,
                        "file_exists": bool(reference["path"] and Path(reference["path"]).is_file()),
                    }
                )
                if target and target.id not in visited:
                    queue.append(target)
        return {
            "root_id": root.id,
            "truncated": bool(queue),
            "nodes": nodes,
            "edges": edges,
        }

    def similar(
        self,
        document_id: int,
        limit: int = 10,
        cancel: threading.Event | None = None,
    ) -> list[dict[str, Any]]:
        source_record = self._record(document_id)
        source = self._manifest(source_record)
        scored: list[dict[str, Any]] = []
        for candidate in self.repository.analyzable_documents():
            if cancel and cancel.is_set():
                raise InterruptedError("任务已停止。")
            if candidate.id == source_record.id or candidate.document_type != source_record.document_type:
                continue
            manifest = self.repository.manifest(candidate.id)
            if not manifest:
                continue
            score, reasons = self._similarity(source, manifest)
            scored.append(
                {
                    **self._record_summary(candidate),
                    "score": round(score, 4),
                    "reasons": reasons,
                }
            )
        scored.sort(key=lambda item: (-float(item["score"]), str(item["path"]).casefold()))
        return scored[: max(1, min(int(limit), 100))]

    @staticmethod
    def _similarity(left: dict[str, Any], right: dict[str, Any]) -> tuple[float, list[str]]:
        left_features = Counter(
            str(item.get("type") or "")
            for item in left.get("features", [])
            if isinstance(item, dict) and item.get("type")
        )
        right_features = Counter(
            str(item.get("type") or "")
            for item in right.get("features", [])
            if isinstance(item, dict) and item.get("type")
        )
        feature_score = LibraryAnalyzer._cosine(left_features, right_features)
        left_dimensions = Counter(_dimension_values(left).keys())
        right_dimensions = Counter(_dimension_values(right).keys())
        dimension_score = LibraryAnalyzer._cosine(left_dimensions, right_dimensions)
        left_properties = set(_property_values(left))
        right_properties = set(_property_values(right))
        union = left_properties | right_properties
        property_score = len(left_properties & right_properties) / len(union) if union else 0.0
        material_score = float(
            bool(left.get("material")) and left.get("material") == right.get("material")
        )
        score = (
            feature_score * 0.5
            + dimension_score * 0.25
            + property_score * 0.15
            + material_score * 0.1
        )
        reasons = [
            f"特征结构 {feature_score:.0%}",
            f"参数名称 {dimension_score:.0%}",
            f"属性字段 {property_score:.0%}",
        ]
        if material_score:
            reasons.append("材料一致")
        return score, reasons

    @staticmethod
    def _cosine(left: Counter[str], right: Counter[str]) -> float:
        if not left and not right:
            return 0.0
        if not left or not right:
            return 0.0
        dot = sum(left[key] * right[key] for key in left.keys() & right.keys())
        left_norm = math.sqrt(sum(value * value for value in left.values()))
        right_norm = math.sqrt(sum(value * value for value in right.values()))
        return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0

    def _record(self, document_id: int) -> DocumentRecord:
        record = self.repository.document(document_id)
        if record is None:
            raise ValueError(f"模型库记录不存在：{document_id}")
        return record

    def _manifest(self, record: DocumentRecord) -> dict[str, Any]:
        manifest = self.repository.manifest(record.id)
        if not manifest:
            raise ValueError(f"{record.name} 尚未读取参数，无法分析。")
        return manifest

    @staticmethod
    def _record_summary(record: DocumentRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "name": record.name,
            "path": record.path,
            "document_type": record.document_type,
            "parameter_status": record.parameter_status,
            "missing": record.missing,
        }

    @staticmethod
    def _references(record: DocumentRecord, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        references: list[dict[str, Any]] = []
        for item in manifest.get("references", []):
            if not isinstance(item, dict):
                continue
            references.append(
                {
                    "name": str(item.get("name") or ""),
                    "path": LibraryAnalyzer._resolve_reference(record.path, item.get("path")),
                    "kind": "component",
                    "suppressed": bool(item.get("suppressed")),
                }
            )
        for item in dict(manifest.get("drawing") or {}).get("views", []):
            if not isinstance(item, dict):
                continue
            raw_path = item.get("referenced_document")
            if not raw_path:
                continue
            references.append(
                {
                    "name": str(item.get("name") or ""),
                    "path": LibraryAnalyzer._resolve_reference(record.path, raw_path),
                    "kind": "drawing_view",
                    "suppressed": False,
                }
            )
        return references

    @staticmethod
    def _resolve_reference(source: str, reference: Any) -> str:
        raw = str(reference or "").strip()
        if not raw:
            return ""
        path = Path(raw)
        if not path.is_absolute():
            path = Path(source).parent / path
        return str(path.resolve(strict=False))
