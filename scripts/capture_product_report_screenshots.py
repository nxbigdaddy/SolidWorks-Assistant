"""Capture privacy-safe current-version UI screenshots for the product report."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from sw_assistant.domain import Plan, ToolCall
from sw_assistant.infrastructure import AppPaths
from sw_assistant.runtime import ApplicationController
from sw_assistant.ui import APP_STYLE, MainWindow


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "product-report-assets"
DATA = ROOT / ".pytest_tmp" / "product-report-capture"
SAMPLE_ROOT = Path(r"C:\CAD-Demo\智能助手示例库")


def _capture(window: MainWindow, filename: str, app: QApplication) -> None:
    window.show()
    window.raise_()
    app.processEvents()
    pixmap = window.grab()
    destination = OUTPUT / filename
    if pixmap.isNull() or not pixmap.save(str(destination), "PNG"):
        raise RuntimeError(f"无法保存界面截图：{destination}")
    print(f"captured {destination.name}: {pixmap.width()}x{pixmap.height()}")


def _manifest(path: Path) -> dict:
    return {
        "schema_version": 2,
        "path": str(path),
        "title": path.name,
        "document_type": "part",
        "active_configuration": "默认",
        "configurations": [{"name": "默认"}, {"name": "轻量化"}],
        "custom_properties": {"零件编号": "PLATE-120", "设计状态": "已审核"},
        "materials": [{"configuration": "默认", "name": "铝合金 6061"}],
        "features": [
            {"name": "基体-拉伸", "type_name": "BossExtrude", "error_code": 0},
            {"name": "安装孔", "type_name": "CutExtrude", "error_code": 0},
        ],
        "dimensions": [
            {"full_name": "D1@基体草图", "display_value_mm": 120.0, "driving": True},
            {"full_name": "D2@基体草图", "display_value_mm": 80.0, "driving": True},
            {"full_name": "D1@基体-拉伸", "display_value_mm": 12.0, "driving": True},
            {"full_name": "D1@安装孔", "display_value_mm": 8.0, "driving": True},
        ],
        "geometry": {
            "feature_count": 6,
            "body_count": 1,
            "face_count": 18,
            "volume_m3": 0.0000864,
            "mass_kg": 0.233,
            "mass_available": True,
        },
        "references": [],
        "captured_at": "2026-08-13T09:30:00+08:00",
    }


def _prepare_library(controller: ApplicationController) -> None:
    root_id = controller.library_repository.add_root(str(SAMPLE_ROOT))
    samples = (
        ("标准安装板.SLDPRT", "part", 148_224, "ready"),
        ("夹具总成.SLDASM", "assembly", 512_880, "ready"),
        ("标准安装板.SLDDRW", "drawing", 284_160, "ready"),
        ("定位块.SLDPRT", "part", 96_640, "not_read"),
        ("旧版支架.SLDPRT", "part", 131_072, "stale"),
    )
    for index, (name, kind, size, state) in enumerate(samples, 1):
        path = SAMPLE_ROOT / name
        document_id, _ = controller.library_repository.upsert_document(
            root_id=root_id,
            path=str(path),
            document_type=kind,
            size_bytes=size,
            modified_ns=1_723_500_000_000_000_000 + index,
            quick_fingerprint=f"demo-{index:02d}",
        )
        if state in {"ready", "stale"}:
            manifest = _manifest(path)
            if kind == "assembly":
                manifest["document_type"] = "assembly"
                manifest["references"] = [
                    {"name": "标准安装板-1", "path": str(SAMPLE_ROOT / "标准安装板.SLDPRT")},
                    {"name": "定位块-1", "path": str(SAMPLE_ROOT / "定位块.SLDPRT")},
                ]
            elif kind == "drawing":
                manifest["document_type"] = "drawing"
                manifest["drawing"] = {"sheets": 1, "views": ["主视", "俯视", "右视"]}
            encoded = repr(manifest).encode("utf-8")
            controller.library_repository.set_manifest(
                document_id,
                manifest,
                hashlib.sha256(encoded).hexdigest(),
                hashlib.sha256((name + "-content").encode()).hexdigest(),
            )
            if state == "stale":
                with controller.library_repository.connection() as db:
                    db.execute(
                        "UPDATE documents SET parameter_status='stale' WHERE id=?",
                        (document_id,),
                    )


def _seed_jobs(controller: ApplicationController) -> None:
    first = controller.jobs.create(
        "health.inspect",
        [
            {"idempotency_key": "plate", "document_id": 1},
            {"idempotency_key": "assembly", "document_id": 2},
            {"idempotency_key": "drawing", "document_id": 3},
        ],
        input={"source": "product-report"},
        job_id="JOB-20260813-001",
    )
    second = controller.jobs.create(
        "semantic.reindex",
        [
            {"idempotency_key": "part-a", "document_id": 1},
            {"idempotency_key": "part-b", "document_id": 4},
        ],
        input={"source": "product-report"},
        job_id="JOB-20260813-002",
    )
    with controller.library_repository.connection() as db:
        db.execute(
            "UPDATE jobs SET state='succeeded',processed=3,succeeded=3,message='全部模型检查完成' WHERE id=?",
            (first.id,),
        )
        db.execute(
            "UPDATE jobs SET state='paused',processed=1,succeeded=1,message='等待用户继续' WHERE id=?",
            (second.id,),
        )


def main() -> int:
    if DATA.exists():
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    app.setApplicationName("SolidWorks 智能建模助手")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_STYLE)

    paths = AppPaths(
        data=DATA,
        logs=DATA / "logs",
        settings=DATA / "settings.json",
        library_db=DATA / "library.db",
        history_db=DATA / "history.db",
    )
    controller = ApplicationController(paths)
    controller.event_bridge.stop()
    _prepare_library(controller)
    _seed_jobs(controller)

    window = MainWindow(controller)
    window.resize(1600, 900)
    window.context_document.setText("文档：标准安装板.SLDPRT · 零件 · 默认")
    window.context_selection.setText("选择：安装面 · Face1")
    window.context_state.setText("状态：已同步 · 25 项能力")

    plan = Plan(
        "创建直径 40 mm、高 30 mm 的圆柱零件",
        [
            ToolCall("session.ensure", {}, "连接 SolidWorks"),
            ToolCall("document.new_part", {}, "新建零件"),
            ToolCall("sketch.begin", {"plane": "front"}, "在前视基准面开始草图"),
            ToolCall("sketch.circle", {"x_mm": 0, "y_mm": 0, "radius_mm": 20}, "绘制圆"),
            ToolCall(
                "feature.extrude",
                {
                    "depth_mm": 30,
                    "operation": "boss",
                    "end_condition": "blind",
                    "reverse": False,
                    "merge": True,
                },
                "拉伸形成圆柱",
            ),
            ToolCall("model.inspect", {}, "检查最终模型"),
        ],
    )
    window.prompt.setPlainText("新建一个直径 40 mm、高 30 mm 的圆柱零件。")
    window.plan = plan
    window._render_plan_table(["succeeded", "succeeded", "succeeded", "running", "pending", "pending"])
    window.plan_summary.setText("6 步 · 创建直径 40 mm、高 30 mm 的圆柱零件")
    window.progress_card.show()
    window.progress_card.setProperty("state", "success")
    window.progress_title.setText("计划已生成并通过校验")
    window.progress_detail.setText("工具、参数、先后顺序和轮廓闭合性均已检查")
    window.plan_progress.setRange(0, 100)
    window.plan_progress.setValue(100)
    window.elapsed_label.setText("2 秒")
    window.runtime_status.setText("正在执行第 4 步：绘制圆；已完成步骤全部保留")
    window._execution_buttons(True)
    _capture(window, "01-intelligent-modeling.png", app)

    window._navigate(1)
    window.library_root.setText(str(SAMPLE_ROOT))
    controller.refresh_documents()
    app.processEvents()
    window.library_status.setText("已完成 · 成功 5 · 跳过 3 · 失败 0 · 12.6 秒")
    window.library_progress.setRange(0, 5)
    window.library_progress.setValue(5)
    if window.library_table.rowCount():
        window.library_table.selectRow(0)
        window._show_document_detail()
    _capture(window, "02-local-model-library.png", app)

    window._navigate(2)
    controller.refresh_jobs()
    app.processEvents()
    window.automation_detail.setPlainText(
        "版本能力实验摘要\n\n"
        "SolidWorks 2025 SP5.0\n"
        "25 / 25 项 API 能力可用\n\n"
        "持久任务支持暂停、继续、取消、租约回收和程序中断后恢复。\n"
        "人工操作示教必须先审阅配方，再按步骤引导回放。"
    )
    _capture(window, "03-automation-center.png", app)

    window._navigate(0)
    window._on_event(
        "step_failed",
        {
            "index": 3,
            "result": {
                "call": plan.steps[3].to_dict(),
                "message": "未检测到草图实体数量增加。系统已暂停，前三步结果仍然保留。",
            },
        },
    )
    _capture(window, "04-recovery-options.png", app)

    window.close()
    controller.shutdown()
    shutil.rmtree(DATA, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
