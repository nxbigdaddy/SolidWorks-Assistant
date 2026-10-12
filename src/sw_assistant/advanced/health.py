from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .models import DiagnosticIssue, HealthReport, Severity


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    require_material_for_parts: bool = True
    required_properties: tuple[str, ...] = ()
    warn_feature_count: int = 500


class ModelHealthService:
    def __init__(self, policy: HealthPolicy | None = None) -> None:
        self.policy = policy or HealthPolicy()

    def diagnose(self, manifest: Mapping[str, Any]) -> HealthReport:
        issues: list[DiagnosticIssue] = []
        document_type = str(manifest.get("document_type") or "unknown").casefold()
        document_path = str(manifest.get("path") or "")
        features = self._mappings(manifest.get("features"))
        references = self._mappings(manifest.get("references"))

        for feature in features:
            error_code = self._integer(feature.get("error_code"))
            name = str(feature.get("name") or "未命名特征")
            if error_code:
                issues.append(
                    DiagnosticIssue(
                        "feature.rebuild_error",
                        Severity.ERROR,
                        f"特征 {name} 返回重建错误码 {error_code}。",
                        scope="feature",
                        object_name=name,
                        evidence={"error_code": error_code, "type": feature.get("type")},
                        recommendation="在特征树中检查父引用、草图和尺寸后重新生成。",
                    )
                )
            sketch_status = str(feature.get("sketch_status") or "").casefold()
            if sketch_status in {"under_defined", "underdefined", "-1"}:
                issues.append(
                    DiagnosticIssue(
                        "sketch.under_defined",
                        Severity.WARNING,
                        f"草图 {name} 未完全定义。",
                        scope="sketch",
                        object_name=name,
                        recommendation="补充尺寸或几何关系，并确认没有意外自由度。",
                    )
                )

        for reference in references:
            path = str(reference.get("path") or reference.get("name") or "未知引用")
            missing = reference.get("missing") is True or reference.get("resolved") is False
            if missing:
                issues.append(
                    DiagnosticIssue(
                        "reference.missing",
                        Severity.CRITICAL,
                        f"引用无法解析：{path}",
                        scope="reference",
                        object_name=path,
                        evidence={"reference": dict(reference)},
                        recommendation="定位正确文件或通过 Pack and Go 修复引用后再继续。",
                    )
                )
            elif bool(reference.get("suppressed")):
                issues.append(
                    DiagnosticIssue(
                        "reference.suppressed",
                        Severity.INFO,
                        f"引用处于压缩状态：{path}",
                        scope="reference",
                        object_name=path,
                        recommendation="确认该压缩状态属于目标配置。",
                    )
                )

        if document_type == "part" and self.policy.require_material_for_parts:
            material = manifest.get("material")
            if not self._has_material(material):
                issues.append(
                    DiagnosticIssue(
                        "part.material_missing",
                        Severity.WARNING,
                        "零件没有可识别的材料。",
                        recommendation="指定材料后重新计算质量属性。",
                    )
                )

        properties = self._property_names(manifest.get("custom_properties"))
        for required in self.policy.required_properties:
            if required.casefold() not in properties:
                issues.append(
                    DiagnosticIssue(
                        "property.required_missing",
                        Severity.WARNING,
                        f"缺少必填自定义属性：{required}",
                        scope="property",
                        object_name=required,
                        recommendation="按企业模板补全属性。",
                    )
                )

        if len(features) > self.policy.warn_feature_count:
            issues.append(
                DiagnosticIssue(
                    "model.feature_count_high",
                    Severity.INFO,
                    f"特征数量为 {len(features)}，可能影响重建性能。",
                    evidence={"feature_count": len(features)},
                    recommendation="检查重复特征、阵列和可简化结构。",
                )
            )

        geometry = manifest.get("geometry")
        if document_type == "part" and isinstance(geometry, Mapping):
            if self._integer(geometry.get("body_count")) <= 0:
                issues.append(
                    DiagnosticIssue(
                        "part.no_body",
                        Severity.ERROR,
                        "零件中没有可识别实体。",
                        recommendation="检查所有实体特征是否失败或被压缩。",
                    )
                )

        if document_type == "drawing":
            drawing = manifest.get("drawing")
            views = drawing.get("views") if isinstance(drawing, Mapping) else None
            if not views:
                issues.append(
                    DiagnosticIssue(
                        "drawing.no_views",
                        Severity.ERROR,
                        "工程图没有可识别视图。",
                        recommendation="插入至少一个模型视图并检查引用模型。",
                    )
                )

        score = max(0, 100 - sum(self._penalty(item.severity) for item in issues))
        if any(item.severity is Severity.CRITICAL for item in issues):
            status = "critical"
        elif any(item.severity is Severity.ERROR for item in issues):
            status = "error"
        elif any(item.severity is Severity.WARNING for item in issues):
            status = "warning"
        else:
            status = "healthy"
        return HealthReport(document_path, status, score, tuple(issues))

    @staticmethod
    def _mappings(value: Any) -> list[Mapping[str, Any]]:
        if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
            return []
        return [item for item in value if isinstance(item, Mapping)]

    @staticmethod
    def _integer(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _has_material(value: Any) -> bool:
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, Mapping):
            return any(bool(str(item).strip()) for item in value.values())
        return False

    @staticmethod
    def _property_names(value: Any) -> set[str]:
        if isinstance(value, Mapping):
            return {str(name).casefold() for name in value}
        names: set[str] = set()
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            for item in value:
                if isinstance(item, Mapping) and item.get("name"):
                    names.add(str(item["name"]).casefold())
        return names

    @staticmethod
    def _penalty(severity: Severity) -> int:
        return {
            Severity.INFO: 2,
            Severity.WARNING: 8,
            Severity.ERROR: 20,
            Severity.CRITICAL: 30,
        }[severity]
