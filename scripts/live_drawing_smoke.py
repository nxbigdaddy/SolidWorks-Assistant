from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sw_assistant.advanced import (
    CapabilityState,
    CapabilityStatus,
    DrawingAutomationService,
    DrawingSpec,
    DrawingViewSpec,
    WorkerDrawingAdapter,
)
from sw_assistant.solidworks import SolidWorksWorkerClient


def _available(capability_id: str) -> CapabilityStatus:
    return CapabilityStatus(
        capability_id,
        CapabilityState.AVAILABLE,
        "live drawing smoke",
        source="live-smoke",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model")
    parser.add_argument("template")
    parser.add_argument("destination")
    parser.add_argument("--bom", action="store_true")
    parser.add_argument("--section", action="store_true")
    parser.add_argument("--three-view", action="store_true")
    arguments = parser.parse_args()
    destination = Path(arguments.destination).resolve(strict=False)
    if destination.exists():
        raise RuntimeError("目标工程图已存在，测试不会覆盖。")
    views = (
        (
            DrawingViewSpec("主视图", "front", x=0.08, y=0.18),
            DrawingViewSpec("俯视图", "top", x=0.08, y=0.08),
            DrawingViewSpec("右视图", "right", x=0.20, y=0.18),
        )
        if arguments.three_view
        else (
            DrawingViewSpec("主视图", "front", x=0.08, y=0.18),
            DrawingViewSpec("等轴测", "isometric", x=0.20, y=0.18),
        )
    )
    if arguments.section:
        views += (
            DrawingViewSpec(
                "剖视图A", "section", x=0.20, y=0.08, projected_from="主视图"
            ),
        )
    worker = SolidWorksWorkerClient()
    try:
        result = DrawingAutomationService(WorkerDrawingAdapter(worker, _available)).generate(
            DrawingSpec(
                str(Path(arguments.model).resolve(strict=True)),
                str(destination),
                str(Path(arguments.template).resolve(strict=True)),
                views,
                include_bom=arguments.bom,
                include_balloons=arguments.bom,
                import_model_dimensions=True,
            )
        )
    finally:
        worker.close()
    print(json.dumps(dict(result), ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
