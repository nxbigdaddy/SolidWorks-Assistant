from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from ..domain import utc_now
from ..library.repository import LibraryRepository
from .models import CapabilityState, CapabilityStatus


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    id: str
    group: str
    description: str
    required_for_release: bool = False


DEFAULT_CAPABILITIES = (
    CapabilityDefinition("taskpane.embedded", "addin", "SolidWorks 内嵌 Task Pane", True),
    CapabilityDefinition("context.realtime", "addin", "实时文档和选择上下文", True),
    CapabilityDefinition("health.inspect", "quality", "模型健康诊断", True),
    CapabilityDefinition("product_family.generate", "configuration", "产品族批量生成"),
    CapabilityDefinition("assembly.inspect", "assembly", "装配体结构检查"),
    CapabilityDefinition("assembly.create_mate", "assembly", "创建配合"),
    CapabilityDefinition("drawing.generate", "drawing", "工程图自动生成"),
    CapabilityDefinition("demonstration.record", "learning", "人工操作记录"),
    CapabilityDefinition("batch.resume", "automation", "持久批任务恢复", True),
    CapabilityDefinition("semantic.local", "library", "本地语义搜索", True),
    CapabilityDefinition("pmi.read", "manufacturing", "PMI/DimXpert 读取"),
    CapabilityDefinition("pdm.read", "enterprise", "PDM 只读集成"),
    CapabilityDefinition("capability.lab", "compatibility", "多版本能力实验室", True),
)


Probe = Callable[[], CapabilityStatus]


class CapabilityLab:
    def __init__(
        self,
        repository: LibraryRepository,
        definitions: Iterable[CapabilityDefinition] = DEFAULT_CAPABILITIES,
    ) -> None:
        self.repository = repository
        self.definitions = {item.id: item for item in definitions}

    def run(
        self,
        probes: dict[str, Probe],
        *,
        environment: str,
        solidworks_revision: str = "",
        interop_version: str = "",
    ) -> list[CapabilityStatus]:
        statuses: list[CapabilityStatus] = []
        for capability_id, definition in self.definitions.items():
            probe = probes.get(capability_id)
            if probe is None:
                status = CapabilityStatus(
                    capability_id,
                    CapabilityState.UNKNOWN,
                    "本次实验没有提供探测器。",
                    source="capability-lab",
                    version=solidworks_revision,
                )
            else:
                try:
                    status = probe()
                except Exception as exc:
                    status = CapabilityStatus(
                        capability_id,
                        CapabilityState.UNAVAILABLE,
                        f"探测失败：{type(exc).__name__}: {exc}",
                        source="capability-lab",
                        version=solidworks_revision,
                    )
            if status.id != capability_id:
                raise ValueError(
                    f"能力探测器 {definition.id} 返回了错误 ID：{status.id}"
                )
            self._record(
                status,
                environment=environment,
                solidworks_revision=solidworks_revision,
                interop_version=interop_version,
            )
            statuses.append(status)
        return statuses

    def latest_matrix(self, *, environment: str = "") -> list[dict]:
        sql = (
            "SELECT r.* FROM capability_runs r JOIN (SELECT capability_id,MAX(created_at) latest "
            "FROM capability_runs"
        )
        params: list[object] = []
        if environment:
            sql += " WHERE environment=?"
            params.append(environment)
        sql += " GROUP BY capability_id) x ON x.capability_id=r.capability_id AND x.latest=r.created_at"
        with self.repository.connection() as db:
            rows = db.execute(sql, params).fetchall()
        result: list[dict] = []
        for row in rows:
            definition = self.definitions.get(str(row["capability_id"]))
            result.append(
                {
                    "capability_id": str(row["capability_id"]),
                    "group": definition.group if definition else "unknown",
                    "required_for_release": bool(
                        definition.required_for_release if definition else False
                    ),
                    "state": str(row["state"]),
                    "environment": str(row["environment"]),
                    "solidworks_revision": str(row["solidworks_revision"]),
                    "interop_version": str(row["interop_version"]),
                    "evidence": json.loads(str(row["evidence_json"])),
                    "created_at": str(row["created_at"]),
                }
            )
        result.sort(key=lambda item: (item["group"], item["capability_id"]))
        return result

    def release_ready(self, *, environment: str = "") -> tuple[bool, list[str]]:
        matrix = {item["capability_id"]: item for item in self.latest_matrix(environment=environment)}
        missing = [
            item.id
            for item in self.definitions.values()
            if item.required_for_release
            and matrix.get(item.id, {}).get("state") != CapabilityState.AVAILABLE.value
        ]
        return not missing, sorted(missing)

    def write_report(self, path: str | Path, *, environment: str = "") -> None:
        matrix = self.latest_matrix(environment=environment)
        ready, missing = self.release_ready(environment=environment)
        Path(path).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "generated_at": utc_now(),
                    "environment": environment,
                    "release_ready": ready,
                    "missing_required": missing,
                    "capabilities": matrix,
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )

    def _record(
        self,
        status: CapabilityStatus,
        *,
        environment: str,
        solidworks_revision: str,
        interop_version: str,
    ) -> None:
        with self.repository.connection() as db:
            db.execute(
                "INSERT INTO capability_runs(id,environment,solidworks_revision,interop_version,"
                "capability_id,state,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    environment,
                    solidworks_revision,
                    interop_version,
                    status.id,
                    status.state.value,
                    json.dumps(status.to_dict(), ensure_ascii=False, sort_keys=True),
                    utc_now(),
                ),
            )
