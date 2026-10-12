from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
import uuid
from collections import Counter
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from .agent import PiSupervisorService, SupervisorService
from .advanced import (
    AssemblyAssistant,
    CapabilityLab,
    CapabilityState,
    CapabilityStatus,
    DemonstrationService,
    DrawingAutomationService,
    DrawingSpec,
    LocalSemanticIndex,
    ManufacturingAnalyzer,
    MateRequest,
    ModelHealthService,
    PdmIntegrationService,
    ProductFamilyService,
    ProductFamilySpec,
    SolidWorksPdmReadAdapter,
    WorkerAssemblyAdapter,
    WorkerDrawingAdapter,
    WorkerPmiAdapter,
    WorkerProductFamilyAdapter,
)
from .automation import PersistentBatchRunner, PersistentJobStore
from .domain import ClarificationRequired, Plan, ToolCall
from .execution import ExecutionEngine
from .execution.command_queue import CommandQueue
from .infrastructure import AppPaths, AuditLog, HistoryStore, SettingsStore
from .library import (
    DerivationService,
    DocumentManagerReader,
    ExportService,
    LibraryAnalyzer,
    LibraryRepository,
    LibraryService,
    PackAndGoService,
)
from .migration import LegacyMigrator
from .planning import PlannerService
from .solidworks import (
    AddinEvent,
    EventBridgeMonitor,
    SolidWorksContextStore,
    SolidWorksWorkerClient,
    capability_snapshot_matches,
    read_capability_snapshot,
)
from .tools import build_tool_registry
from .task_runtime import ContinuousTaskController


class ApplicationController(QObject):
    activity = Signal(str, object)
    plan_ready = Signal(object)
    documents_ready = Signal(object)
    selection_references_ready = Signal(object)
    context_ready = Signal(object)

    def _cad_progress(self, event: str, details: dict[str, Any]) -> None:
        phases = {
            "waiting_client_lock": "等待本地执行队列", "waiting_worker": "等待 CAD 工作进程",
            "waiting_gate": "等待 CAD 执行通道", "before_observation": "检查执行前状态",
            "precondition": "核对目标文档与配置", "executing": "执行 CAD 操作",
            "after_observation": "读取操作结果", "begin_candidate": "创建隔离试验副本",
            "saving_checkpoint": "保存并校验检查点", "restoring_candidate": "恢复试验前状态",
            "listing_checkpoints": "核查已保存检查点", "restoring_checkpoint": "打开并验证恢复副本",
            "completed": "CAD 请求已返回，等待任务核验", "not_executed": "请求未提交，未执行",
            "stopped_unknown": "操作已停止，结果待确认", "submitted_unknown": "执行结果待确认",
            "timed_out_unknown": "等待超时，结果待确认",
        }
        payload: dict[str, Any] = {
            key: details[key] for key in ("request_id", "operation", "tool", "phase", "elapsed_seconds")
            if key in details}
        phase = phases.get(str(payload.get("phase")), "等待 CAD 返回")
        payload["message"] = f"{phase} · {int(float(payload.get('elapsed_seconds', 0)))} 秒"
        payload["rpc_event"] = event
        continuous = getattr(self, "continuous", None)
        if continuous is not None:
            continuous._event("cad_progress", payload)
        else:
            self.activity.emit("cad_progress", payload)

    def __init__(self, paths: AppPaths | None = None, parent=None) -> None:
        super().__init__(parent)
        self.paths = paths or AppPaths.default()
        self.paths.ensure()
        self.settings_store = SettingsStore(self.paths.settings)
        self.settings = self.settings_store.load()
        self._credential_marker = self.paths.data / "legacy-credential-reentry-required"
        if (
            self._credential_marker.is_file()
            and self.settings.model.configured
            and not self.settings.model.api_key
        ):
            self.settings.model.credential_unreadable = True
            self.settings_store.warning = (
                "旧版模型服务地址已迁移；出于安全原因 API Key 未迁移，请重新输入并保存。"
            )
        self.registry = build_tool_registry()
        self.worker = SolidWorksWorkerClient(self.paths.data / "candidate-workspace", on_progress=self._cad_progress)
        retention = AuditLog.prune(self.paths.logs, self.settings.log_retention_days)
        session_name = "session-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl"
        self.audit = AuditLog(self.paths.logs / session_name)
        if retention["deleted"] or retention["errors"]:
            self.audit.write(
                "log_retention_applied",
                retention_days=self.settings.log_retention_days,
                deleted=retention["deleted"],
                errors=retention["errors"],
            )
        self.history = HistoryStore(self.paths.history_db)
        self.library_repository = LibraryRepository(self.paths.library_db)
        self.library = LibraryService(self.library_repository, self.worker)
        self.library_analyzer = LibraryAnalyzer(self.library_repository)
        self.document_manager = DocumentManagerReader(self.settings.document_manager_key)
        self.derivation = DerivationService(self.worker)
        self.exporter = ExportService(self.worker)
        self.pack_and_go = PackAndGoService(self.worker)
        self.health = ModelHealthService()
        self.semantic_index = LocalSemanticIndex(self.library_repository)
        self.product_family = ProductFamilyService(WorkerProductFamilyAdapter(self.worker))
        self.assembly_assistant = AssemblyAssistant(
            WorkerAssemblyAdapter(self.worker, self._capability_status)
        )
        self.drawing_automation = DrawingAutomationService(
            WorkerDrawingAdapter(self.worker, self._capability_status)
        )
        self.manufacturing = ManufacturingAnalyzer(
            WorkerPmiAdapter(self.worker, self._capability_status)
        )
        self.pdm = PdmIntegrationService(SolidWorksPdmReadAdapter())
        self.demonstrations = DemonstrationService(self.library_repository)
        self.capability_lab = CapabilityLab(self.library_repository)
        self.jobs = PersistentJobStore(self.library_repository)
        self.jobs.recover_expired()
        self.batch_runner = PersistentBatchRunner(
            self.jobs,
            {
                "health.inspect": self._batch_health,
                "semantic.reindex": self._batch_semantic_reindex,
            },
        )
        self._demonstration_session = ""
        self._demonstration_name = ""
        self._demonstration_pending: dict[int, dict[str, Any]] = {}
        self.library_query = ""
        self.library_offset = 0
        self.library_total = 0
        self.engine = ExecutionEngine(
            self.registry,
            self.worker,
            self.audit,
            on_event=self._on_engine_event,
            preflight=self._execution_preflight,
        )
        self._commands = CommandQueue()
        self._closed = threading.Event()
        self._cancel = threading.Event()
        self._background_lock = threading.Lock()
        self._active_background: tuple[str, str] | None = None
        self._request_lock = threading.Lock()
        self._plan_request_id = 0
        self._automatic_recovery_lock = threading.Lock()
        self._automatic_recovery_active = False
        self._automatic_retry_fingerprints: Counter[str] = Counter()
        self._automatic_repair_total = 0
        self._pi_supervisor_lock = threading.Lock()
        self._pi_supervisor: PiSupervisorService | None = None
        self._pi_plan_id = ""
        self._pi_fallback_plan_id = ""
        self._run_all_requested = False
        self._command_thread = threading.Thread(target=self._command_loop, daemon=True)
        self._command_thread.start()
        self._event_bridge_dir = self.paths.event_bridge
        self.context_store = SolidWorksContextStore()
        self._bridge_session_id = ""
        self._bridge_process_id = 0
        self._bridge_revision = ""
        self._bridge_connected_at = ""
        self._bridge_taskpane_available = False
        self.event_bridge = EventBridgeMonitor(
            self._event_bridge_dir / "events.jsonl",
            self._on_addin_event,
        )
        self.event_bridge.start()
        self.continuous = ContinuousTaskController(self)

    def save_settings(self, settings) -> None:
        self.continuous.stop()
        self._close_pi_supervisor()
        self._pi_fallback_plan_id = ""
        self.settings_store.save(settings)
        self.settings = settings
        self.document_manager = DocumentManagerReader(self.settings.document_manager_key)
        self._credential_marker.unlink(missing_ok=True)
        self.activity.emit("settings_saved", {"model": settings.model.model})

    def migrate_legacy_async(self, archive_root: str | Path) -> None:
        archive = Path(archive_root).resolve()
        marker = self.paths.data / "legacy-migration-v3.json"
        if marker.is_file() or not archive.is_dir():
            return
        self.activity.emit("legacy_migration_started", {"archive": str(archive)})

        def work() -> None:
            try:
                migrator = LegacyMigrator(archive)
                report = migrator.import_library(self.library_repository)
                report.imported_history = migrator.import_history(self.history)
                credentials_required = False
                legacy_settings = migrator.model_settings()
                if legacy_settings and not self.settings.model.configured:
                    migrated = legacy_settings
                    credentials_required = migrated.model.credential_unreadable
                    migrated.model.credential_unreadable = False
                    self.settings_store.save(migrated)
                    self.settings = migrated
                    if credentials_required:
                        self._credential_marker.write_text("1\n", "utf-8")
                        self.settings.model.credential_unreadable = True
                        self.settings_store.warning = (
                            "旧版模型服务地址已迁移；API Key 未迁移，请重新输入并保存。"
                        )
                payload = {
                    "archive": str(archive),
                    "documents": report.imported_documents,
                    "manifests": report.imported_manifests,
                    "tags": report.imported_tags,
                    "history": report.imported_history,
                    "warnings": report.warnings,
                    "credentials_required": credentials_required,
                }
                temporary = marker.with_suffix(".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
                temporary.replace(marker)
                self.audit.write("legacy_migration_completed", **payload)
                self.activity.emit("legacy_migration_completed", payload)
                self.refresh_documents()
            except Exception as exc:
                payload = {"archive": str(archive), "message": str(exc)}
                self.audit.write("legacy_migration_failed", **payload)
                self.activity.emit("legacy_migration_failed", payload)

        threading.Thread(target=work, daemon=True).start()

    def plan_async(self, prompt: str) -> None:
        request_id = self._next_plan_request()
        started = time.monotonic()
        planning_context = self._planning_agent_context()
        parsed = urllib.parse.urlsplit(self.settings.model.api_base)
        base = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        common = {
            "request_id": request_id,
            "mode": "model" if self.settings.model.configured else "offline",
            "model": self.settings.model.model,
            "endpoint": base,
            "prompt_length": len(prompt),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        self.audit.write("planning_started", **common)
        self.activity.emit("planning_started", common)

        def progress(stage: str, message: str, details: dict) -> None:
            if not self._plan_current(request_id):
                return
            payload = {
                "request_id": request_id,
                "stage": stage,
                "message": message,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                **details,
            }
            self.audit.write("planning_progress", **payload)
            self.activity.emit("planning_progress", payload)

        def work() -> None:
            try:
                plan = PlannerService(
                    self.settings.model,
                    self.registry,
                    use_agent=self.settings.agent_enabled,
                    max_model_requests=self.settings.agent_max_model_requests,
                    runtime_context=planning_context,
                ).plan(prompt, progress)
                if not self._plan_current(request_id):
                    return
                self.history.save_plan(prompt, plan)
                payload = {
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "step_count": len(plan.steps),
                    "assumption_count": len(plan.assumptions),
                    "plan_id": plan.id,
                }
                self.audit.write("planning_succeeded", **payload)
                self.activity.emit("planning_succeeded", payload)
                self.plan_ready.emit(plan)
            except ClarificationRequired as exc:
                if not self._plan_current(request_id):
                    return
                payload = {
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "questions": exc.questions,
                    "message": str(exc),
                }
                self.audit.write("planning_clarification", **payload)
                self.activity.emit("planning_clarification", payload)
            except Exception as exc:
                if not self._plan_current(request_id):
                    return
                diagnostics = dict(getattr(exc, "details", {}) or {})
                failed_plan = diagnostics.pop("failed_plan", None)
                failure_id = self.history.save_failure(
                    prompt,
                    str(exc),
                    plan=failed_plan if isinstance(failed_plan, Mapping) else None,
                    diagnostics=diagnostics,
                )
                payload = {
                    "request_id": request_id,
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "failure_id": failure_id,
                }
                if diagnostics:
                    payload["diagnostics"] = diagnostics
                self.audit.write("planning_failed", **payload)
                self.activity.emit("planning_failed", payload)

        threading.Thread(target=work, daemon=True).start()

    def _planning_agent_context(self) -> dict[str, Any]:
        context = self.context_store.latest(require_fresh=True)
        if context is None:
            return {"available": False}
        document = dict(context.document)
        return {
            "available": True,
            "solidworks_revision": context.solidworks_revision,
            "command_in_progress": context.command_in_progress,
            "has_active_document": bool(document),
            "document_type": document.get("document_type", 0),
            "read_only": bool(document.get("read_only")),
            "selection_count": len(context.selections),
            "selection_types": sorted(
                {
                    str(item.type_name or item.selection_type)
                    for item in context.selections
                }
            ),
            "available_capabilities": sorted(
                item.id for item in context.capabilities if item.available
            ),
        }

    def load_plan(self, plan: Plan) -> None:
        self._cancel.clear()
        self._close_pi_supervisor()
        self._pi_fallback_plan_id = ""
        self._run_all_requested = False
        self._automatic_retry_fingerprints.clear()
        self._automatic_repair_total = 0
        with self._automatic_recovery_lock:
            self._automatic_recovery_active = False
        self._commands.put(("load", plan))

    def execute_next(self) -> None:
        self._cancel.clear()
        self._run_all_requested = False
        self._commands.put(("next", None))

    def run_all(self) -> None:
        self._cancel.clear()
        self._run_all_requested = True
        self._commands.put(("all", None))

    def retry_failed(self) -> None:
        self._cancel.clear()
        self._run_all_requested = False
        self._invalidate_plan()
        self._commands.put(("retry", None))

    def replace_failed(self, call: ToolCall) -> None:
        self._cancel.clear()
        self._run_all_requested = False
        self._invalidate_plan()
        self._commands.put(("replace", call))

    def skip_failed(self) -> None:
        self._run_all_requested = False
        self._invalidate_plan()
        self._commands.put(("skip", None))

    def verify_manual_completion(self) -> None:
        self._run_all_requested = False
        self._invalidate_plan()
        self._commands.put(("verify_manual", None))

    def capture_selection_reference(self, alias: str) -> None:
        normalized = str(alias).strip()
        if not normalized:
            self.activity.emit("selection_reference_failed", {"message": "命名目标名称不能为空。"})
            return
        self._commands.put(("capture_selection_reference", normalized))

    def restore_selection_reference(self, reference_id: int) -> None:
        reference = self.library_repository.selection_reference(reference_id)
        if reference is None:
            self.activity.emit(
                "selection_reference_failed", {"message": "命名目标不存在或已经删除。"}
            )
            return
        self._commands.put(("restore_selection_reference", reference))

    def delete_selection_reference(self, reference_id: int) -> None:
        deleted = self.library_repository.delete_selection_reference(reference_id)
        self.activity.emit(
            "selection_reference_deleted",
            {"id": int(reference_id), "deleted": deleted},
        )
        self.refresh_selection_references()

    def refresh_selection_references(self) -> None:
        self.selection_references_ready.emit(self.library_repository.selection_references())

    def repair_failed_async(
        self,
        *,
        automatic: bool = False,
        resume_after: bool = False,
    ) -> None:
        if automatic:
            with self._automatic_recovery_lock:
                if self._automatic_recovery_active:
                    return
                self._automatic_recovery_active = True
        else:
            self._run_all_requested = False
        request_id = self._next_plan_request()
        started = time.monotonic()
        self.activity.emit(
            "repair_started",
            {
                "request_id": request_id,
                "automatic": automatic,
                "agent_backend": self.settings.agent_backend,
            },
        )

        def progress(stage: str, message: str, details: dict) -> None:
            if self._plan_current(request_id):
                self.activity.emit(
                    "repair_progress",
                    {
                        "request_id": request_id,
                        "stage": stage,
                        "message": message,
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                        **details,
                    },
                )

        def release_automatic_recovery() -> None:
            if automatic:
                with self._automatic_recovery_lock:
                    self._automatic_recovery_active = False

        def work() -> None:
            try:
                context = self.engine.recovery_context()
                context["automatic_recovery"] = automatic
                context["resume_after_repair"] = bool(resume_after)
                decision_payload: dict[str, Any] | None = None
                recovery_backend = "legacy"
                if self.settings.agent_enabled:
                    outcome, recovery_backend = self._supervise_recovery(
                        context, progress
                    )
                    decision_payload = outcome.decision.model_dump(mode="json")
                    if outcome.plan is None:
                        payload = {
                            "request_id": request_id,
                            "automatic": automatic,
                            "action": outcome.decision.action.value,
                            "reason": outcome.decision.reason,
                            "confidence": outcome.decision.confidence,
                        }
                        release_automatic_recovery()
                        self.audit.write("agent_review_required", **payload)
                        self.activity.emit("agent_review_required", payload)
                        return
                    plan = outcome.plan
                else:
                    plan = PlannerService(self.settings.model, self.registry).repair(
                        context, progress
                    )
                if not self._plan_current(request_id):
                    release_automatic_recovery()
                    return
                self.audit.write(
                    "repair_plan_succeeded",
                    request_id=request_id,
                    replacement_count=len(plan.steps),
                    duration_seconds=round(time.monotonic() - started, 2),
                    automatic=automatic,
                    agent_enabled=self.settings.agent_enabled,
                    recovery_backend=recovery_backend,
                    agent_decision=decision_payload,
                )
                self._commands.put(
                    (
                        "replace_remaining",
                        (request_id, plan.steps, automatic, bool(resume_after)),
                    )
                )
            except Exception as exc:
                if not self._plan_current(request_id):
                    release_automatic_recovery()
                    return
                payload = {
                    "request_id": request_id,
                    "message": str(exc),
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "automatic": automatic,
                }
                release_automatic_recovery()
                self.audit.write("repair_plan_failed", **payload)
                self.activity.emit("repair_failed", payload)

        threading.Thread(target=work, daemon=True).start()

    def reset(self) -> None:
        self._commands.invalidate()
        self.continuous.stop()
        self._close_pi_supervisor()
        self._pi_fallback_plan_id = ""
        self._run_all_requested = False
        self._invalidate_plan()
        self._cancel.set()
        self.engine.abort()
        self.worker.interrupt()
        self._commands.put(("reset", None))

    def abort_all(self) -> None:
        self._commands.invalidate()
        self.continuous.stop()
        self._close_pi_supervisor()
        self._pi_fallback_plan_id = ""
        self._run_all_requested = False
        self._invalidate_plan()
        self._cancel.set()
        self.engine.abort()
        self.worker.interrupt()
        self.batch_runner.cancel.set()
        for job in self.jobs.list_jobs(limit=1000):
            if job.state not in {"succeeded", "failed", "cancelled"}:
                self.jobs.request_cancel(job.id)
        self.activity.emit("all_cancel_requested", {})

    def _task_preflight(self, call: ToolCall) -> tuple[bool, str]:
        if self._closed.is_set() or self._cancel.is_set():
            return False, "任务已停止。"
        if self.registry.write_scope(call.tool) in {"none", "application"}:
            return True, ""
        context = self.context_store.latest()
        if context is None:
            return True, ""
        if context.is_stale():
            return False, "SolidWorks 事件上下文已过期，请检查当前文档。"
        return context.usable_for_write()

    def scan_library_async(self, root: str, recursive: bool = True) -> None:
        self._start_library_task(
            "scan",
            lambda progress: self.library.scan(
                root, recursive=recursive, cancel=self._cancel, progress=progress
            ),
        )

    def deep_read_async(self, records) -> None:
        self._start_library_task(
            "deep_read",
            lambda progress: self.library.deep_read(
                list(records), cancel=self._cancel, progress=progress
            ),
        )

    def deep_read_all_async(self) -> None:
        self._start_library_task(
            "deep_read",
            lambda progress: self.library.deep_read(
                self.library_repository.readable_documents(),
                cancel=self._cancel,
                progress=progress,
            ),
        )

    def metadata_read_all_async(self) -> None:
        if not self.document_manager.configured:
            self.activity.emit(
                "library_rejected",
                {
                    "kind": "metadata_read",
                    "message": ("请先在设置中填写 SolidWorks Document Manager 许可证密钥。"),
                },
            )
            return
        self._start_library_task(
            "metadata_read",
            lambda progress: self.library.metadata_read(
                self.library_repository.readable_documents(),
                self.document_manager,
                cancel=self._cancel,
                progress=progress,
            ),
        )

    def copy_part_async(self, source: str, destination: str) -> None:
        self._start_derivation_task(
            "byte_copy",
            lambda: self.derivation.copy_part(source, destination, cancel=self._cancel),
        )

    def derive_part_async(
        self, source: str, destination: str, changes_mm: dict[str, float]
    ) -> None:
        self._start_derivation_task(
            "parameter_copy",
            lambda: self.derivation.derive_part(
                source, destination, changes_mm, cancel=self._cancel
            ),
        )

    def pack_and_go_async(
        self,
        source: str,
        destination: str,
        *,
        include_drawings: bool = True,
        include_toolbox: bool = True,
    ) -> None:
        self._start_derivation_task(
            "pack_and_go",
            lambda: self.pack_and_go.package_assembly(
                source,
                destination,
                include_drawings=include_drawings,
                include_toolbox=include_toolbox,
                cancel=self._cancel,
            ),
        )

    def export_document_async(self, source: str, destination: str) -> None:
        self._start_derivation_task(
            "export",
            lambda: self.exporter.export(source, destination, cancel=self._cancel),
        )

    def generate_product_family_async(self, spec: ProductFamilySpec) -> None:
        self._start_derivation_task(
            "product_family",
            lambda: self.product_family.generate(
                spec,
                cancel=self._cancel,
                progress=lambda completed, total, message: self.activity.emit(
                    "advanced_progress",
                    {
                        "kind": "product_family",
                        "completed": completed,
                        "total": total,
                        "message": message,
                    },
                ),
            ),
        )

    def inspect_assembly_async(self, path: str) -> None:
        self._start_analysis_task(
            "assembly_inspect",
            lambda: self.assembly_assistant.inspect(path, cancel=self._cancel).to_dict(),
        )

    def create_assembly_mate_async(self, request: MateRequest) -> None:
        self._start_analysis_task(
            "assembly_mate",
            lambda: dict(self.assembly_assistant.create_mate(request, cancel=self._cancel)),
        )

    def generate_drawing_async(self, spec: DrawingSpec) -> None:
        self._start_derivation_task(
            "drawing",
            lambda: self.drawing_automation.generate(spec, cancel=self._cancel),
        )

    def manufacturing_analysis_async(
        self, document_id: int, *, metrics: Mapping[str, float] | None = None
    ) -> None:
        def analyze() -> dict[str, Any]:
            record = self.library_repository.document(document_id)
            manifest = self.library_repository.manifest(document_id)
            if record is None or record.missing:
                raise ValueError("模型库文档不存在或源文件缺失。")
            if manifest is None:
                raise ValueError("请先读取该文档的完整参数。")
            return self.manufacturing.analyze(
                record.path,
                manifest,
                metrics=metrics,
                cancel=self._cancel,
            ).to_dict()

        self._start_analysis_task("manufacturing", analyze)

    def inspect_pdm_async(self, path: str) -> None:
        self._start_analysis_task(
            "pdm_read",
            lambda: self.pdm.inspect(path, cancel=self._cancel).to_dict(),
        )

    def diagnose_document_async(self, document_id: int) -> None:
        def diagnose() -> dict[str, Any]:
            record = self.library_repository.document(document_id)
            manifest = self.library_repository.manifest(document_id)
            if record is None or record.missing:
                raise ValueError("模型库文档不存在或源文件缺失。")
            if manifest is None:
                raise ValueError("请先读取该文档的完整参数。")
            return self.health.diagnose(manifest).to_dict()

        self._start_analysis_task("health", diagnose)

    def rebuild_semantic_index_async(self) -> None:
        self._start_analysis_task(
            "semantic_reindex",
            lambda: self.semantic_index.rebuild(cancel=self._cancel),
        )

    def semantic_search_async(self, query: str, *, limit: int = 50) -> None:
        self._start_analysis_task(
            "semantic_search",
            lambda: {
                "query": str(query).strip(),
                "hits": [item.to_dict() for item in self.semantic_index.search(query, limit=limit)],
            },
        )

    def create_health_batch(self, document_ids: list[int]) -> str:
        items = [
            {"idempotency_key": f"health:{int(document_id)}", "document_id": int(document_id)}
            for document_id in sorted(set(document_ids))
        ]
        job = self.jobs.create("health.inspect", items, input={"continue_on_error": True})
        self.activity.emit("jobs_changed", self.job_snapshot())
        return job.id

    def run_queued_jobs_async(self) -> None:
        task_id = uuid.uuid4().hex
        if not self._claim_background("persistent_jobs", task_id):
            self.activity.emit("jobs_rejected", {"message": "已有模型库或自动化任务正在运行。"})
            return
        self.batch_runner.cancel.clear()
        self._cancel.clear()
        self.activity.emit("jobs_started", {"task_id": task_id})

        def work() -> None:
            try:
                processed = 0
                while not self._cancel.is_set():
                    job = self.batch_runner.run_once()
                    if job is None:
                        break
                    processed += 1
                    self.activity.emit("jobs_changed", self.job_snapshot())
                self.activity.emit(
                    "jobs_finished",
                    {"task_id": task_id, "processed_jobs": processed},
                )
            except Exception as exc:
                self.activity.emit("jobs_failed", {"task_id": task_id, "message": str(exc)})
            finally:
                self._release_background(task_id)

        threading.Thread(target=work, daemon=True).start()

    def pause_job(self, job_id: str) -> None:
        self.jobs.request_pause(job_id)
        self.activity.emit("jobs_changed", self.job_snapshot())

    def resume_job(self, job_id: str) -> None:
        self.jobs.resume(job_id)
        self.activity.emit("jobs_changed", self.job_snapshot())

    def cancel_job(self, job_id: str) -> None:
        self.jobs.request_cancel(job_id)
        self.activity.emit("jobs_changed", self.job_snapshot())

    def job_snapshot(self) -> list[dict[str, Any]]:
        return [asdict(job) for job in self.jobs.list_jobs(limit=500)]

    def refresh_jobs(self) -> None:
        self.activity.emit("jobs_changed", self.job_snapshot())

    def run_capability_lab_async(self) -> None:
        def run() -> dict[str, Any]:
            snapshot = self._current_capability_snapshot() or {}
            addin = {
                str(item.get("id")): bool(item.get("available"))
                for item in snapshot.get("capabilities", [])
                if isinstance(item, dict)
            }

            def status(capability_id: str, available: bool, reason: str) -> CapabilityStatus:
                return CapabilityStatus(
                    capability_id,
                    CapabilityState.AVAILABLE if available else CapabilityState.UNAVAILABLE,
                    reason,
                    source="desktop-capability-lab",
                    version=str(snapshot.get("solidworks_revision") or ""),
                )

            probes = {
                "taskpane.embedded": lambda: status(
                    "taskpane.embedded",
                    self._bridge_taskpane_available and bool(snapshot),
                    "当前 Add-in 实例已实际创建 Task Pane。",
                ),
                "context.realtime": lambda: status(
                    "context.realtime",
                    self.context_store.latest(require_fresh=True) is not None and bool(snapshot),
                    "当前 Add-in 实例已送达实时上下文，且能力快照与进程匹配。",
                ),
                "health.inspect": lambda: status("health.inspect", True, "本地诊断规则可用"),
                "product_family.generate": lambda: status(
                    "product_family.generate",
                    addin.get("product_family.configuration", False),
                    "当前实例的 Interop 声明包含 SolidWorks 配置 API。",
                ),
                "assembly.inspect": lambda: status(
                    "assembly.inspect", True, "模型库装配体 Manifest 检查可用"
                ),
                "assembly.create_mate": lambda: status(
                    "assembly.create_mate",
                    addin.get("assembly.create_mate", False),
                    "当前实例的 Interop 声明包含 SolidWorks 配合 API。",
                ),
                "drawing.generate": lambda: status(
                    "drawing.generate",
                    addin.get("drawing.generate", False),
                    "当前实例的 Interop 声明包含 SolidWorks 工程图视图 API。",
                ),
                "demonstration.record": lambda: status(
                    "demonstration.record",
                    bool(snapshot) and addin.get("events.commands", False),
                    "当前 Add-in 事件通道已连接，Interop 声明包含命令事件。",
                ),
                "batch.resume": lambda: status("batch.resume", True, "SQLite 租约队列可用"),
                "semantic.local": lambda: status("semantic.local", True, "本地索引可用"),
                "pmi.read": lambda: status(
                    "pmi.read",
                    addin.get("pmi.read", False),
                    "当前实例的 Interop 声明包含 DimXpert API。",
                ),
                "pdm.read": lambda: self.pdm.adapter.capability("pdm.read"),
                "capability.lab": lambda: status(
                    "capability.lab", True, "能力探测、证据入库和版本报告可用"
                ),
            }
            statuses = self.capability_lab.run(
                probes,
                environment="desktop",
                solidworks_revision=str(snapshot.get("solidworks_revision") or ""),
                interop_version=str(snapshot.get("interop_version") or ""),
            )
            ready, missing = self.capability_lab.release_ready(environment="desktop")
            return {
                "release_ready": ready,
                "missing_required": missing,
                "capabilities": [item.to_dict() for item in statuses],
            }

        self._start_analysis_task("capability_lab", run)

    def start_demonstration(self, name: str) -> None:
        if self._demonstration_session:
            raise RuntimeError("已有示教会话正在录制。")
        context = self.context_store.latest(require_fresh=True)
        if context is None or not context.document:
            raise RuntimeError("需要已连接的 SolidWorks 活动文档才能开始示教。")
        document_type = {1: "part", 2: "assembly", 3: "drawing"}.get(
            int(context.document.get("document_type") or 0), ""
        )
        if not document_type:
            raise RuntimeError("当前 SolidWorks 文档类型不支持示教。")
        normalized_name = str(name).strip()
        if not normalized_name:
            raise ValueError("示教配方名称不能为空。")
        self._demonstration_session = self.demonstrations.start(
            document_type,
            document_path=str(context.document.get("path") or ""),
            context={
                "document_type": document_type,
                "selection_types": [item.selection_type for item in context.selections],
                "solidworks_revision": context.solidworks_revision,
            },
        )
        self._demonstration_name = normalized_name
        self._demonstration_pending.clear()
        self.activity.emit(
            "demonstration_started",
            {"session_id": self._demonstration_session, "name": normalized_name},
        )

    def finish_demonstration(self) -> dict[str, Any]:
        session_id = self._demonstration_session
        if not session_id:
            raise RuntimeError("当前没有正在录制的示教会话。")
        self.demonstrations.finish(session_id)
        recipe = self.demonstrations.compile_recipe(session_id, self._demonstration_name)
        self._demonstration_session = ""
        self._demonstration_name = ""
        self._demonstration_pending.clear()
        payload = asdict(recipe)
        self.activity.emit("demonstration_finished", payload)
        return payload

    def approve_demonstration_recipe(self, recipe_id: str) -> dict[str, Any]:
        recipe = self.demonstrations.approve(recipe_id)
        payload = asdict(recipe)
        self.activity.emit("demonstration_approved", payload)
        return payload

    def prepare_demonstration_replay(self, recipe_id: str) -> dict[str, Any]:
        context = self.context_store.latest(require_fresh=True)
        if context is None or not context.document:
            raise RuntimeError("需要已连接的 SolidWorks 活动文档才能引导回放。")
        document_type = {1: "part", 2: "assembly", 3: "drawing"}.get(
            int(context.document.get("document_type") or 0), ""
        )
        payload = self.demonstrations.prepare_replay(
            recipe_id,
            {
                "document_type": document_type,
                "selection_types": [item.selection_type for item in context.selections],
            },
        )
        self.activity.emit("demonstration_replay_ready", payload)
        return payload

    def record_demonstration_replay_result(
        self, recipe_id: str, *, succeeded: bool
    ) -> dict[str, Any]:
        recipe = self.demonstrations.record_replay_result(recipe_id, succeeded=succeeded)
        payload = asdict(recipe)
        payload["last_replay_succeeded"] = succeeded
        self.activity.emit("demonstration_replay_recorded", payload)
        return payload

    def _batch_health(self, _job, item, cancel: threading.Event) -> Mapping[str, Any]:
        if cancel.is_set() or self._cancel.is_set():
            raise InterruptedError("批量健康诊断已取消。")
        document_id = int(item.input.get("document_id") or 0)
        manifest = self.library_repository.manifest(document_id)
        if manifest is None:
            raise ValueError(f"文档 {document_id} 尚无参数 Manifest。")
        return self.health.diagnose(manifest).to_dict()

    def _batch_semantic_reindex(self, _job, _item, cancel: threading.Event) -> Mapping[str, Any]:
        if cancel.is_set() or self._cancel.is_set():
            raise InterruptedError("语义索引重建已取消。")
        return self.semantic_index.rebuild(cancel=cancel)

    def _capability_status(self, capability_id: str) -> CapabilityStatus:
        if capability_id == "assembly.inspect":
            return CapabilityStatus(
                capability_id,
                CapabilityState.AVAILABLE,
                "SolidWorks COM 装配体只读检查已实现。",
                source="python-worker",
            )
        mapping = {
            "assembly.create_mate": "assembly.create_mate",
            "drawing.generate": "drawing.generate",
            "pmi.read": "pmi.read",
        }
        expected = mapping.get(capability_id, capability_id)
        snapshot = self._current_capability_snapshot() or {}
        available = any(
            isinstance(item, Mapping)
            and str(item.get("id") or "") == expected
            and bool(item.get("available"))
            for item in snapshot.get("capabilities", [])
        )
        return CapabilityStatus(
            capability_id,
            CapabilityState.AVAILABLE if available else CapabilityState.UNAVAILABLE,
            (
                f"Add-in 能力矩阵确认 {expected} 可用。"
                if available
                else f"当前 Add-in 实例的能力矩阵尚未确认 {expected}。"
            ),
            source="solidworks-addin",
            version=str(snapshot.get("solidworks_revision") or ""),
            evidence={
                "evidence_level": "declaration",
                "process_id": snapshot.get("process_id"),
            },
        )

    def _current_capability_snapshot(self) -> dict[str, Any] | None:
        context = self.context_store.latest(require_fresh=True)
        if (
            context is None
            or not self._bridge_session_id
            or context.process_id != self._bridge_process_id
        ):
            return None
        snapshot = read_capability_snapshot(self._event_bridge_dir / "capabilities.json")
        revision = context.solidworks_revision or self._bridge_revision
        if not capability_snapshot_matches(
            snapshot,
            process_id=self._bridge_process_id,
            solidworks_revision=revision,
            connected_at_utc=self._bridge_connected_at,
        ):
            return None
        return snapshot

    def compare_documents_async(self, left_id: int, right_id: int) -> None:
        self._start_analysis_task(
            "compare", lambda: self.library_analyzer.compare(left_id, right_id)
        )

    def inspect_dependencies_async(self, document_id: int) -> None:
        self._start_analysis_task(
            "dependencies",
            lambda: {
                "quality": self.library_analyzer.quality_report(document_id),
                "graph": self.library_analyzer.dependency_graph(document_id, cancel=self._cancel),
            },
        )

    def find_similar_async(self, document_id: int) -> None:
        self._start_analysis_task(
            "similar",
            lambda: self.library_analyzer.similar(document_id, cancel=self._cancel),
        )

    def refresh_documents(self, query: str = "", offset: int = 0) -> None:
        self.library_query = str(query).strip()
        self.library_total = self.library_repository.document_count(self.library_query)
        page_size = self.settings.library_page_size
        maximum_offset = max(0, ((self.library_total - 1) // page_size) * page_size)
        self.library_offset = min(max(0, int(offset)), maximum_offset)
        self.documents_ready.emit(
            self.library_repository.documents(self.library_query, page_size, self.library_offset)
        )

    def _start_library_task(self, kind: str, callback) -> None:
        task_id = uuid.uuid4().hex
        if not self._claim_background(kind, task_id):
            self.activity.emit(
                "library_rejected",
                {"kind": kind, "message": "已有模型库或派生任务正在运行。"},
            )
            return
        self._cancel.clear()
        self.activity.emit("library_started", {"kind": kind, "task_id": task_id})

        def progress(value) -> None:
            self.activity.emit("library_progress", value)

        def work() -> None:
            try:
                result = callback(progress)
                self.activity.emit("library_finished", {"kind": kind, "task_id": task_id, **result})
                self.refresh_documents(self.library_query, self.library_offset)
            except Exception as exc:
                self.activity.emit(
                    "library_failed",
                    {"kind": kind, "task_id": task_id, "message": str(exc)},
                )
            finally:
                self._release_background(task_id)

        threading.Thread(target=work, daemon=True).start()

    def _start_analysis_task(self, kind: str, callback) -> None:
        task_id = uuid.uuid4().hex
        if not self._claim_background(kind, task_id):
            self.activity.emit(
                "analysis_rejected",
                {"kind": kind, "message": "已有模型库任务正在运行。"},
            )
            return
        self._cancel.clear()
        self.activity.emit("analysis_started", {"kind": kind, "task_id": task_id})

        def work() -> None:
            try:
                result = callback()
                self.activity.emit(
                    "analysis_succeeded",
                    {"kind": kind, "task_id": task_id, "result": result},
                )
            except InterruptedError as exc:
                self.activity.emit(
                    "analysis_cancelled",
                    {"kind": kind, "task_id": task_id, "message": str(exc)},
                )
            except Exception as exc:
                self.activity.emit(
                    "analysis_failed",
                    {"kind": kind, "task_id": task_id, "message": str(exc)},
                )
            finally:
                self._release_background(task_id)

        threading.Thread(target=work, daemon=True).start()

    def _start_derivation_task(self, strategy: str, callback) -> None:
        task_id = uuid.uuid4().hex
        if not self._claim_background(strategy, task_id):
            self.activity.emit(
                "derivation_rejected",
                {"strategy": strategy, "message": "已有模型库或派生任务正在运行。"},
            )
            return
        self._cancel.clear()
        self.audit.write("derivation_started", task_id=task_id, strategy=strategy)
        self.activity.emit("derivation_started", {"task_id": task_id, "strategy": strategy})

        def work() -> None:
            try:
                result = callback()
                if isinstance(result, Mapping):
                    payload = {"task_id": task_id, "strategy": strategy, **dict(result)}
                else:
                    payload = {
                        "task_id": task_id,
                        "strategy": strategy,
                        "source": result.source,
                        "destination": result.destination,
                        "source_sha256": result.source_sha256,
                        "destination_sha256": result.destination_sha256,
                    }
                    if hasattr(result, "file_count"):
                        payload["file_count"] = result.file_count
                self.audit.write("derivation_succeeded", **payload)
                self.activity.emit("derivation_succeeded", payload)
            except Exception as exc:
                payload = {"task_id": task_id, "strategy": strategy, "message": str(exc)}
                if self._cancel.is_set():
                    self.audit.write("derivation_cancelled", **payload)
                    self.activity.emit("derivation_cancelled", payload)
                else:
                    self.audit.write("derivation_failed", **payload)
                    self.activity.emit("derivation_failed", payload)
            finally:
                self._release_background(task_id)

        threading.Thread(target=work, daemon=True).start()

    def _claim_background(self, kind: str, task_id: str) -> bool:
        with self._background_lock:
            if self._active_background is not None:
                return False
            self._active_background = (kind, task_id)
            return True

    def _release_background(self, task_id: str) -> None:
        with self._background_lock:
            if self._active_background and self._active_background[1] == task_id:
                self._active_background = None

    def _command_loop(self) -> None:
        while not self._closed.is_set():
            epoch, (action, payload) = self._commands.get()
            if action == "shutdown":
                break
            if not self._commands.current(epoch):
                continue
            claimed_id = ""
            scope = ExitStack()
            try:
                scope.enter_context(self.worker.operation())
                if not self._commands.current(epoch):
                    continue
                if action not in {"load", "reset"}:
                    claimed_id = "legacy-" + uuid.uuid4().hex
                    if not self._claim_background("legacy_execution", claimed_id):
                        claimed_id = ""
                        raise RuntimeError("已有持续建模或模型库任务运行中，不能并行操作模型。")
                if action == "load":
                    self.engine.load_plan(payload)
                elif action == "next":
                    self.engine.execute_next()
                elif action == "all":
                    self.engine.run_all()
                elif action == "retry":
                    self.engine.retry_failed()
                elif action == "retry_auto":
                    resume_after = bool(payload)
                    result = self.engine.retry_failed()
                    if (
                        resume_after
                        and result is not None
                        and result.ok
                        and self.engine.state.value == "ready"
                    ):
                        self.engine.run_all()
                elif action == "replace":
                    self.engine.replace_failed(payload)
                    self.engine.execute_next()
                elif action == "skip":
                    self.engine.skip_failed()
                elif action == "verify_manual":
                    self.engine.verify_manual_completion()
                elif action == "capture_selection_reference":
                    self._capture_selection_reference(str(payload))
                elif action == "restore_selection_reference":
                    self._restore_selection_reference(payload)
                elif action == "replace_remaining":
                    request_id, calls, automatic, resume_after = payload
                    try:
                        if not self._plan_current(int(request_id)):
                            continue
                        self.engine.replace_remaining(calls)
                        self._invalidate_plan()
                        if automatic:
                            self.activity.emit(
                                "automatic_repair_applied",
                                {
                                    "replacement_count": len(calls),
                                    "resume_after": bool(resume_after),
                                },
                            )
                        if resume_after and self.engine.state.value == "ready":
                            self.engine.run_all()
                    finally:
                        # A rejected/superseded replacement must never leave
                        # automatic recovery permanently locked out.
                        if automatic:
                            with self._automatic_recovery_lock:
                                self._automatic_recovery_active = False
                elif action == "reset":
                    self.engine.reset()
            except Exception as exc:
                self.audit.write("runtime_error", action=action, message=str(exc))
                self.activity.emit("runtime_error", {"action": action, "message": str(exc)})
            finally:
                scope.close()
                if claimed_id:
                    self._release_background(claimed_id)

    def _capture_selection_reference(self, alias: str) -> None:
        result = self.worker.call(ToolCall("selection.capture_reference", {}), 60)
        if not result.ok:
            raise RuntimeError(result.message or "读取当前选择的持久引用失败。")
        data = dict(result.data)
        reference = self.library_repository.save_selection_reference(
            document_path=str(data.get("document_path") or ""),
            document_title=str(data.get("document_title") or ""),
            alias=alias,
            persistent_ref=str(data.get("persistent_ref") or ""),
            selection_type=int(data.get("selection_type") or 0),
            object_name=str(data.get("object_name") or ""),
        )
        payload = {
            "id": reference.id,
            "alias": reference.alias,
            "document_path": reference.document_path,
            "document_title": reference.document_title,
            "selection_type": reference.selection_type,
            "object_name": reference.object_name,
        }
        self.audit.write("selection_reference_saved", **payload)
        self.activity.emit("selection_reference_saved", payload)
        self.refresh_selection_references()

    def _restore_selection_reference(self, reference) -> None:
        result = self.worker.call(
            ToolCall(
                "selection.restore_reference",
                {
                    "document_path": reference.document_path,
                    "persistent_ref": reference.persistent_ref,
                },
            ),
            60,
        )
        if not result.ok:
            raise RuntimeError(result.message or "恢复命名目标失败。")
        payload = {
            "id": reference.id,
            "alias": reference.alias,
            "document_title": reference.document_title,
            "selection_count": int(result.data.get("selection_count") or 0),
        }
        self.audit.write("selection_reference_restored", **payload)
        self.activity.emit("selection_reference_restored", payload)

    def _next_plan_request(self) -> int:
        with self._request_lock:
            self._plan_request_id += 1
            return self._plan_request_id

    def _execution_preflight(self, call: ToolCall) -> tuple[bool, str]:
        scope = self.registry.write_scope(call.tool)
        if scope == "none":
            return True, ""
        context = self.context_store.latest()
        if context is None:
            # The Python COM worker remains usable without the optional Add-in.
            return True, ""

        expected_title = ""
        if scope == "document" and self.engine.results:
            latest_after = self.engine.results[-1].after
            if latest_after is not None:
                expected_title = latest_after.document_title

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            document_title = str(context.document.get("title") or "")
            title_mismatch = bool(
                expected_title
                and document_title
                and document_title.casefold() != expected_title.casefold()
            )
            if not context.command_in_progress and not title_mismatch:
                break
            time.sleep(0.05)
            refreshed = self.context_store.latest()
            if refreshed is None:
                break
            context = refreshed

        if context.is_stale():
            return False, "SolidWorks 上下文已经过期，请等待事件桥更新后重试。"
        if context.command_in_progress:
            return False, "SolidWorks 正在执行其他命令，请完成或取消该命令后重试。"
        if scope == "application":
            return True, ""

        document_title = str(context.document.get("title") or "")
        if expected_title and document_title.casefold() != expected_title.casefold():
            return False, "SolidWorks 活动文档尚未与刚完成的步骤同步，请确认文档后重试。"
        return context.usable_for_write()

    def _on_engine_event(self, name: str, payload: dict[str, Any]) -> None:
        plan_id = self.engine.plan.id if self.engine.plan else ""
        if plan_id:
            if name == "step_started":
                self.history.update(plan_id, "running")
            elif name == "step_failed":
                result = dict(payload.get("result") or {})
                self.history.update(plan_id, "paused", str(result.get("message") or ""))
            elif name == "plan_completed":
                self.history.update(plan_id, "completed")
            elif name in {"abort_requested", "plan_aborted"}:
                self.history.update(plan_id, "aborted")
            elif name in {
                "retry_ready",
                "failed_step_replaced",
                "remaining_replaced",
                "step_skipped",
                "manual_completion_verified",
            }:
                self.history.update(plan_id, "ready")
            if name in {"failed_step_replaced", "remaining_replaced"} and self.engine.plan:
                self.history.update_plan(self.engine.plan)
        self.activity.emit(name, payload)
        if name == "step_failed":
            self._schedule_automatic_recovery()
        elif name in {"plan_completed", "plan_aborted", "abort_requested"}:
            self._run_all_requested = False
            self._close_pi_supervisor()
            self._pi_fallback_plan_id = ""

    def _schedule_automatic_recovery(self) -> None:
        try:
            assessment = self.engine.recovery_assessment()
        except Exception as exc:
            self.audit.write("recovery_assessment_failed", message=str(exc))
            return
        self.audit.write("recovery_assessed", **assessment)
        self.activity.emit("recovery_assessed", assessment)
        if not self.settings.auto_repair_failures:
            return

        fingerprint = str(assessment.get("fingerprint") or "")
        resume_after = bool(
            self._run_all_requested and self.settings.auto_resume_after_repair
        )
        if assessment.get("safe_retry") and self._automatic_retry_fingerprints[fingerprint] < 1:
            self._automatic_retry_fingerprints[fingerprint] += 1
            payload = {
                "fingerprint": fingerprint,
                "category": assessment.get("category"),
                "resume_after": resume_after,
            }
            self.audit.write("automatic_retry_scheduled", **payload)
            self.activity.emit("automatic_retry_scheduled", payload)
            self._commands.put(("retry_auto", resume_after))
            return

        if not assessment.get("ai_repair_recommended"):
            return
        if not self.settings.model.configured:
            self.activity.emit(
                "automatic_repair_unavailable",
                {"message": "未配置模型服务，无法自动生成替代步骤。"},
            )
            return
        repair_limit = (
            self.settings.pi_max_repair_rounds
            if self.settings.agent_enabled
            and self.settings.agent_backend in {"auto", "pi"}
            else 2
        )
        if self._automatic_repair_total >= repair_limit:
            self.activity.emit(
                "automatic_repair_exhausted",
                {
                    "message": (
                        f"自动修复已达到 {repair_limit} 次上限，"
                        "请人工检查当前模型和参数。"
                    )
                },
            )
            return
        self._automatic_repair_total += 1
        self.repair_failed_async(automatic=True, resume_after=resume_after)

    def _on_addin_event(self, event: AddinEvent) -> None:
        context = self.context_store.update(event)
        if context is not None:
            self.context_ready.emit(context)
            self.activity.emit("solidworks_context", context)
        payload = {
            "name": event.name,
            "timestamp_utc": event.timestamp_utc,
            "process_id": event.process_id,
            "session_id": event.session_id,
            "payload": dict(event.payload),
        }
        self.audit.write("solidworks_addin_event", **payload)
        self.activity.emit("solidworks_event", payload)
        self._record_demonstration_event(event)
        if event.name == "bridge.connected":
            self._bridge_session_id = event.session_id
            self._bridge_process_id = event.process_id
            self._bridge_revision = str(event.payload.get("revision") or "")
            self._bridge_connected_at = event.timestamp_utc
            self._bridge_taskpane_available = bool(event.payload.get("taskpane_available"))
            snapshot = self._current_capability_snapshot()
            if snapshot is not None:
                self.context_store.set_capabilities(snapshot)
                self.activity.emit("solidworks_capabilities", snapshot)
        elif event.name in {"bridge.disconnecting", "solidworks.destroying"} and (
            not self._bridge_session_id or event.session_id == self._bridge_session_id
        ):
            self._bridge_session_id = ""
            self._bridge_process_id = 0
            self._bridge_revision = ""
            self._bridge_connected_at = ""
            self._bridge_taskpane_available = False
            self.context_store.clear()

    def _record_demonstration_event(self, event: AddinEvent) -> None:
        session_id = self._demonstration_session
        if not session_id or event.name not in {"command.started", "command.finished"}:
            return
        command = int(event.payload.get("command") or 0)
        context = event.payload.get("context")
        snapshot = dict(context) if isinstance(context, Mapping) else {}
        if event.name == "command.started":
            self._demonstration_pending[command] = snapshot
            return
        before = self._demonstration_pending.pop(command, {})
        try:
            sequence = self.demonstrations.record_action(
                session_id,
                command_id=command,
                command_name=f"SolidWorks command {command}",
                before=before,
                after=snapshot,
                result={"reason": int(event.payload.get("reason") or 0)},
            )
            self.activity.emit(
                "demonstration_action",
                {"session_id": session_id, "sequence": sequence, "command": command},
            )
        except Exception as exc:
            self.activity.emit(
                "demonstration_failed",
                {"session_id": session_id, "message": str(exc)},
            )

    def _invalidate_plan(self) -> None:
        with self._request_lock:
            self._plan_request_id += 1

    def _supervise_recovery(self, context, progress):
        backend = self.settings.agent_backend
        plan_id = self.engine.plan.id if self.engine.plan else "unbound"
        pi_already_failed = (
            backend == "auto" and self._pi_fallback_plan_id == plan_id
        )
        if backend in {"auto", "pi"} and not pi_already_failed:
            try:
                supervisor = self._get_pi_supervisor()
                return supervisor.supervise(context, progress), "pi"
            except Exception as exc:
                if backend == "pi":
                    raise
                payload = {
                    "message": f"Pi 不可用，已回退内置监督器：{exc}",
                    "error_type": type(exc).__name__,
                }
                self.audit.write("pi_agent_fallback", **payload)
                self.activity.emit("pi_agent_fallback", payload)
                if progress:
                    progress("pi_fallback", payload["message"], payload)
                self._close_pi_supervisor()
                self._pi_fallback_plan_id = plan_id
        outcome = SupervisorService(
            self.settings.model,
            self.registry,
            max_model_requests=self.settings.agent_max_model_requests,
        ).supervise(context, progress)
        return outcome, "pydantic"

    def _get_pi_supervisor(self) -> PiSupervisorService:
        plan_id = self.engine.plan.id if self.engine.plan else "unbound"
        with self._pi_supervisor_lock:
            if self._pi_supervisor is not None and self._pi_plan_id == plan_id:
                return self._pi_supervisor
            if self._pi_supervisor is not None:
                self._pi_supervisor.close()
            token = hashlib.sha256(plan_id.encode("utf-8")).hexdigest()[:20]
            self._pi_supervisor = PiSupervisorService(
                self.settings.model,
                self.registry,
                self.paths.data / "pi-agent" / ("task-" + token),
                session_name="SolidWorks recovery " + token[:8],
                session_id="sw-" + token,
                max_rounds=self.settings.pi_max_repair_rounds,
            )
            self._pi_plan_id = plan_id
            return self._pi_supervisor

    def _close_pi_supervisor(self) -> None:
        with self._pi_supervisor_lock:
            supervisor = self._pi_supervisor
            self._pi_supervisor = None
            self._pi_plan_id = ""
        if supervisor is not None:
            supervisor.close()

    def _plan_current(self, request_id: int) -> bool:
        with self._request_lock:
            return not self._closed.is_set() and request_id == self._plan_request_id

    def shutdown(self) -> None:
        if self._closed.is_set():
            return
        self._invalidate_plan()
        self._commands.invalidate()
        self._closed.set()
        self._cancel.set()
        self.continuous.close()
        self._close_pi_supervisor()
        self.event_bridge.stop()
        self._commands.put(("shutdown", None))
        self._command_thread.join(timeout=5)
        self.worker.close()
        self.library_repository.close_thread_connection()
