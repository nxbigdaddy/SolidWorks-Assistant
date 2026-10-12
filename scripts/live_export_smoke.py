from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sw_assistant.library import ExportService
from sw_assistant.solidworks import SolidWorksWorkerClient


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destinations", nargs="+")
    arguments = parser.parse_args()
    source = Path(arguments.source).resolve(strict=True)
    destinations = [Path(item).resolve(strict=False) for item in arguments.destinations]
    if any(item.exists() for item in destinations):
        raise RuntimeError("至少一个导出目标已存在，测试不会覆盖。")
    worker = SolidWorksWorkerClient()
    try:
        exporter = ExportService(worker)
        results = [exporter.export(source, destination) for destination in destinations]
    finally:
        worker.close()
    print(
        json.dumps(
            [
                {
                    "destination": item.destination,
                    "destination_sha256": item.destination_sha256,
                    "source_sha256": item.source_sha256,
                }
                for item in results
            ],
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
